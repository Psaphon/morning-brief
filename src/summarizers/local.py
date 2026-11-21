"""Local LLM summarizer — Ollama (default model in config.py).

Sends unsummarized articles to an Ollama instance, gets back schema-validated
JSON facts per article, and stores them in the database.

Ollama API docs: POST /api/generate with model, prompt, stream=false.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from ..config import OllamaConfig
from ..processors.interest import Interests, filter_by_feed_minimum, interest_score
from ..validators.grounding import ValidatorRun
from .schema import EXTRACTION_SCHEMA, ExtractionError, parse_extraction

if TYPE_CHECKING:
    from ..db import Database

logger = logging.getLogger(__name__)

# Default cap on input text for callers of truncate_text. Extraction sizes its own
# cap from the configured context window (see extraction_max_chars).
MAX_INPUT_CHARS = 12_000  # ~3000 tokens at ~4 chars/token

# Tokens reserved for the extraction prompt template, title and metadata.
PROMPT_RESERVE_TOKENS = 600
MIN_INPUT_CHARS = 1_000

# How many articles to summarize per batch before pausing
BATCH_SIZE = 5
BATCH_PAUSE_SECONDS = 1.0

# Ollama API timeout — summarizing a long article can take a while
REQUEST_TIMEOUT = 120.0

# Longer timeout for briefing — one call with larger context
BRIEFING_TIMEOUT = 300.0

# JSON schema passed as Ollama's ``format``: the model's output is constrained to
# it, so a quote inside a segment's text is always escaped. Without it, gemma4
# wrote "atmospheric river," unescaped mid-string and the 2026-11-19 briefing
# fell back to plain text with no segments.
BRIEFING_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "text": {"type": "string"},
                    "source_article_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["topic", "text", "source_article_ids"],
            },
        },
    },
    "required": ["segments"],
}

# Retry config
MAX_RETRIES = 2
RETRY_DELAY = 3.0

# Marks a per-article extraction request (the test stub keys on it).
EXTRACTION_MARKER = "extract the facts from the following news article"

EXTRACTION_PROMPT = """You are a careful news analyst. Read the article below and \
extract the facts from the following news article as JSON.

Rules:
- Facts only: who did what, the figures, and when. No evaluation, opinion, or impact language.
- Copy names, tokens and companies exactly as written in the article.
- Copy figures exactly as written, with their units.
- entities and numbers: at most 10 each, the ones what_happened depends on first. No repeats.
- what_happened: at most 2 sentences.
- event_date: the date of the event as YYYY-MM-DD, or null if the article gives none. \
Resolve relative dates ("yesterday", "next Friday") against the published date.
- status: "happened", "scheduled" (still to come) or "ongoing".
- is_roundup: true ONLY if the article is a list of separate, unrelated news items under one \
headline, such as "Morning briefing", "5 things to know today" or "Theme park news: A, B and C". \
One story told with background, several people, places, figures or reactions is NOT a roundup: \
use false. When unsure, use false.

Article title: {title}
Source: {source}
Category: {category}
Published: {published}

Article text:
{text}
"""


@dataclass
class SummaryResult:
    """Result of extracting facts from a single article.

    ``summary`` is the trimmed ``what_happened``. ``extraction`` is the validated
    extraction dict on success. ``extraction_failed`` is set only when the model
    replied twice with an invalid extraction (not for timeouts or HTTP errors,
    which are worth retrying on the next run).
    """

    article_id: int
    summary: str
    model: str
    elapsed_seconds: float
    success: bool
    error: str | None = None
    extraction: dict | None = None
    extraction_failed: bool = False


@dataclass
class BatchMetrics:
    """Aggregate timing and throughput stats for a summarization batch."""

    total_articles: int
    succeeded: int
    failed: int
    total_seconds: float
    articles_per_minute: float


def select_balanced_articles(
    articles: list[dict],
    per_category: int = 8,
    total: int = 50,
    interests: Interests | None = None,
) -> list[dict]:
    """Select articles for summarization using category-balanced round-robin.

    Picks up to `per_category` articles per category (highest-scored first),
    then caps the overall result at `total`. Input should already be sorted by
    score descending (as returned by ``Database.get_unsummarized_articles``).

    Args:
        articles: Candidate articles, pre-sorted by score descending.
        per_category: Maximum articles to take from any single category.
        total: Hard cap on the total number of articles returned.
        interests: Operator interests. When non-empty, articles from feeds with
            ``min_interest`` that score below it are dropped, and each category is
            ordered by interest score, then by the existing order. When None or
            empty, selection is unchanged.

    Returns:
        Balanced list of articles, sorted by score descending, length <= total.
    """
    if interests:
        articles = filter_by_feed_minimum(articles, interests)
        interest_of = {id(a): interest_score(a, interests) for a in articles}
        articles = sorted(articles, key=lambda a: interest_of[id(a)], reverse=True)

    by_category: dict[str, list[dict]] = {}
    for article in articles:
        cat = article.get("category", "")
        by_category.setdefault(cat, []).append(article)

    selected: list[dict] = []
    for cat_articles in by_category.values():
        selected.extend(cat_articles[:per_category])

    if interests:
        # Interest first, so the total cap never cuts an interest match for a
        # higher-scored article the operator didn't ask for.
        selected.sort(key=lambda a: (interest_of[id(a)], a.get("score") or 0), reverse=True)
    else:
        selected.sort(key=lambda a: a.get("score") or 0, reverse=True)
    return selected[:total]


def truncate_text(text: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    """Truncate article text to fit within model context.

    Tries to break at a sentence boundary near the limit.
    """
    if len(text) <= max_chars:
        return text

    # Find the last sentence boundary before the limit
    truncated = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        last = truncated.rfind(sep)
        if last > max_chars * 0.8:  # Don't cut too aggressively
            return truncated[: last + 1]

    # No good boundary found, just cut at the limit
    return truncated + "..."


async def check_ollama_available(client: httpx.AsyncClient, config: OllamaConfig) -> bool:
    """Check if Ollama is running and the model is available."""
    try:
        resp = await client.get(f"{config.host}/api/tags", timeout=10.0)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        model_names = [m.get("name", "") for m in models]

        # Ollama model names can include ":latest" suffix
        base_model = config.model.split(":")[0]
        available = any(base_model in name for name in model_names)

        if not available:
            logger.warning(
                "Model '%s' not found in Ollama. Available: %s",
                config.model,
                ", ".join(model_names) or "(none)",
            )
            logger.warning("Pull it with: ollama pull %s", config.model)
        return available
    except httpx.HTTPError as e:
        logger.warning("Ollama not reachable at %s: %s", config.host, e)
        return False


def extraction_max_chars(config: OllamaConfig) -> int:
    """Article-text cap so prompt + output fit the context window."""
    room = config.num_ctx - config.extract_num_predict - PROMPT_RESERVE_TOKENS
    return max(MIN_INPUT_CHARS, room * config.chars_per_token)


def _published_date(article: dict) -> str:
    """The article's published date (else fetched date) as YYYY-MM-DD, or 'unknown'."""
    stamp = article.get("published_at") or article.get("fetched_at") or ""
    return stamp[:10] or "unknown"


async def summarize_single(
    client: httpx.AsyncClient,
    config: OllamaConfig,
    article: dict,
) -> SummaryResult:
    """Extract validated JSON facts from a single article via Ollama.

    A reply that is not a valid extraction is retried once; a second invalid reply
    returns a result with ``extraction_failed`` set. The raw article text is never
    used as a fallback summary.
    """
    article_id = article["id"]
    title = article.get("title", "Untitled")
    source = article.get("source", "Unknown")
    category = article.get("category", "General")
    full_text = article.get("full_text", "")

    if not full_text or len(full_text.strip()) < 100:
        return SummaryResult(
            article_id=article_id,
            summary="Insufficient content for summary.",
            model=config.model,
            elapsed_seconds=0.0,
            success=False,
            error="Article text too short",
        )

    prompt = EXTRACTION_PROMPT.format(
        title=title,
        source=source,
        category=category,
        published=_published_date(article),
        text=truncate_text(full_text, extraction_max_chars(config)),
    )

    start = time.monotonic()
    transport_attempts = 0
    invalid_replies = 0

    while True:
        try:
            resp = await client.post(
                f"{config.host}/api/generate",
                json={
                    "model": config.model,
                    "prompt": prompt,
                    "stream": False,
                    # Thinking models (gemma4) reason before answering unless told not to:
                    # slower, and it eats num_predict. Older models ignore the flag.
                    "think": False,
                    "format": EXTRACTION_SCHEMA,
                    "options": {
                        "num_ctx": config.num_ctx,
                        "temperature": config.extract_temperature,
                        "seed": config.extract_seed,
                        "num_predict": config.extract_num_predict,
                    },
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()
            try:
                if not raw:
                    raise ExtractionError("Empty response from Ollama")
                extraction = parse_extraction(raw, config.extract_max_words)
            except ExtractionError as e:
                invalid_replies += 1
                if invalid_replies < 2:
                    logger.info(
                        "Invalid extraction for article %d (%s), retrying once", article_id, e
                    )
                    continue
                logger.warning("Extraction failed for article %d: %s", article_id, e)
                return SummaryResult(
                    article_id=article_id,
                    summary="",
                    model=config.model,
                    elapsed_seconds=time.monotonic() - start,
                    success=False,
                    error=str(e),
                    extraction_failed=True,
                )

            return SummaryResult(
                article_id=article_id,
                summary=extraction["what_happened"],
                model=config.model,
                elapsed_seconds=time.monotonic() - start,
                success=True,
                extraction=extraction,
            )

        except httpx.TimeoutException:
            error = "Timeout after retries"
            detail = "timeout"
        except httpx.HTTPError as e:
            error = detail = str(e)

        if transport_attempts < MAX_RETRIES:
            logger.warning(
                "Error summarizing article %d: %s (attempt %d/%d), retrying...",
                article_id,
                detail,
                transport_attempts + 1,
                MAX_RETRIES + 1,
            )
            transport_attempts += 1
            await _async_sleep(RETRY_DELAY)
            continue
        return SummaryResult(
            article_id=article_id,
            summary="",
            model=config.model,
            elapsed_seconds=time.monotonic() - start,
            success=False,
            error=error,
        )


async def _async_sleep(seconds: float) -> None:
    """Async sleep wrapper for testability."""
    import asyncio

    await asyncio.sleep(seconds)


async def summarize_articles(
    config: OllamaConfig,
    articles: list[dict],
) -> tuple[list[SummaryResult], BatchMetrics]:
    """Summarize a batch of articles via Ollama.

    Processes articles in small batches with pauses to avoid
    overwhelming the local model.

    Returns a tuple of (results, metrics) where metrics contains
    aggregate timing and throughput stats for the batch.
    """
    if not articles:
        logger.info("No articles to summarize")
        empty_metrics = BatchMetrics(
            total_articles=0,
            succeeded=0,
            failed=0,
            total_seconds=0.0,
            articles_per_minute=0.0,
        )
        return [], empty_metrics

    batch_start = time.monotonic()

    async with httpx.AsyncClient() as client:
        # Pre-flight check
        available = await check_ollama_available(client, config)
        if not available:
            logger.warning("Skipping summarization — Ollama not available")
            empty_metrics = BatchMetrics(
                total_articles=len(articles),
                succeeded=0,
                failed=0,
                total_seconds=0.0,
                articles_per_minute=0.0,
            )
            return [], empty_metrics

        results: list[SummaryResult] = []
        total = len(articles)
        succeeded = 0
        failed = 0

        for i in range(0, total, BATCH_SIZE):
            batch = articles[i : i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

            logger.info(
                "Summarizing batch %d/%d (%d articles)...",
                batch_num,
                total_batches,
                len(batch),
            )

            for article in batch:
                result = await summarize_single(client, config, article)
                results.append(result)

                if result.success:
                    succeeded += 1
                    logger.debug(
                        "Summarized [%s] %s (%.1fs)",
                        article["source"],
                        article["title"][:60],
                        result.elapsed_seconds,
                    )
                else:
                    failed += 1
                    logger.warning(
                        "Failed to summarize [%s] %s: %s",
                        article["source"],
                        article["title"][:60],
                        result.error,
                    )

            # Pause between batches to let the model breathe
            if i + BATCH_SIZE < total:
                await _async_sleep(BATCH_PAUSE_SECONDS)

        total_seconds = time.monotonic() - batch_start
        articles_per_minute = (succeeded / total_seconds * 60) if total_seconds > 0 else 0.0

        metrics = BatchMetrics(
            total_articles=total,
            succeeded=succeeded,
            failed=failed,
            total_seconds=total_seconds,
            articles_per_minute=articles_per_minute,
        )

        logger.info(
            "Summarization complete: %d succeeded, %d failed out of %d "
            "(%.1fs total, %.1f articles/min)",
            succeeded,
            failed,
            total,
            total_seconds,
            articles_per_minute,
        )
        return results, metrics


def _build_briefing_prompt(summaries_by_category: dict[str, list[tuple[int, str]]]) -> str:
    """Build the Ollama prompt for generating a structured daily briefing.

    Takes article summaries (with IDs) grouped by category and returns a prompt
    that instructs the model to output a JSON segment map.

    Args:
        summaries_by_category: Dict mapping category name to a list of
            (article_id, summary) tuples. Article IDs are embedded in the prompt
            so the model can reference them in source_article_ids.

    Returns:
        Prompt string, or empty string if no summaries provided.
    """
    if not summaries_by_category:
        return ""

    parts: list[str] = []

    parts.append("Here are today's article summaries, organized by topic:\n")
    for category, id_summary_pairs in summaries_by_category.items():
        if not id_summary_pairs:
            continue
        parts.append(f"## {category}")
        for article_id, summary in id_summary_pairs:
            parts.append(f"  [{article_id}] {summary}")
        parts.append("")

    parts.append(
        "Using the summaries above, write a structured daily intelligence briefing.\n\n"
        "Output ONLY a JSON object in this exact format (no other text before or after):\n"
        '{"segments": [\n'
        '  {"topic": "Thematic section title", "text": "2-3 paragraph narrative for this theme.", '
        '"source_article_ids": [1, 2, 3]},\n'
        "  ...\n"
        "]}\n\n"
        "Requirements:\n"
        "- 4-6 thematic segments total, grouping related stories across categories.\n"
        "- The first segment should cover the most significant developments (the lead).\n"
        "- The last segment should look ahead: what to watch for tomorrow.\n"
        "- Each segment text should be 2-3 paragraphs, professional analyst tone.\n"
        "- source_article_ids lists the bracketed article IDs that informed that segment.\n"
        "- Total text across all segments: 500-1000 words.\n"
        "- Do not editorialize or speculate. Stick to what the summaries say.\n"
        "- Output ONLY the JSON object. Do not include markdown fences or any other text."
    )

    return "\n".join(parts)


def _briefing_articles(articles: list[dict]) -> list[dict]:
    """Articles that feed the briefing: skips those without a summary, roundups (several
    unrelated stories in one summary) and failed or invalidated extractions.
    Signals read the raw articles and are unaffected.
    """
    return [
        a
        for a in articles
        if a.get("summary") and not a.get("is_roundup") and not a.get("extraction_failed")
    ]


def _briefing_summaries(articles: list[dict]) -> dict[str, list[tuple[int, str]]]:
    """Group article summaries by category for the briefing prompt."""
    by_category: dict[str, list[tuple[int, str]]] = {}
    for article in _briefing_articles(articles):
        cat = article.get("category", "General")
        by_category.setdefault(cat, []).append((article["id"], article["summary"]))
    return by_category


def validate_briefing(
    rendered: str,
    segment_map_json: str | None,
    articles: list[dict],
    validator: ValidatorRun | None,
) -> tuple[str, str | None]:
    """Ground-check a parsed briefing; return ``(rendered, segment_map_json)`` to save.

    Each segment's text is checked against the titles and summaries of the articles it
    cites; a segment citing no known article is checked against the whole briefing
    input. A segment with every sentence dropped is removed, and the rendered text and
    segment map are rebuilt together so they stay consistent. ``rendered == ""`` means
    nothing survived. With no validator, or mode ``off``, nothing changes.
    """
    if validator is None or validator.mode == "off":
        return rendered, segment_map_json

    sources = {
        a["id"]: f"{a.get('title', '')}\n{a['summary']}" for a in _briefing_articles(articles)
    }
    everything = "\n".join(sources.values())

    if segment_map_json is None:  # plain-text fallback: no segments to cite
        return validator.apply(rendered, everything, "briefing"), None

    kept: list[dict] = []
    for seg in json.loads(segment_map_json):
        cited = [i for i in seg["source_article_ids"] if isinstance(i, int) and i in sources]
        source = "\n".join(sources[i] for i in cited) if cited else everything
        text = validator.apply(seg["text"], source, f"briefing segment {seg['topic']!r}")
        if text:
            kept.append({**seg, "text": text})
    if not kept:
        return "", None
    return "\n\n".join(seg["text"] for seg in kept), json.dumps(kept)


def _parse_briefing_response(raw: str) -> tuple[str, str | None]:
    """Parse Ollama's response as a structured JSON briefing.

    Attempts to extract a JSON object with a ``segments`` list. Each segment
    must have ``topic``, ``text``, and ``source_article_ids`` keys.

    Returns:
        A tuple of (rendered_text, segment_map_json) where:
        - rendered_text is the concatenated segment texts (or raw if parsing fails).
        - segment_map_json is the JSON-encoded segments list, or None on failure.
    """
    # Strip markdown code fences if the model wrapped the JSON
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Drop opening fence (```json or ```) and closing fence
        inner = [line for line in lines[1:] if line.strip() != "```"]
        cleaned = "\n".join(inner).strip()

    try:
        data = json.loads(cleaned)
        segments = data.get("segments")
        if not isinstance(segments, list) or not segments:
            raise ValueError("Missing or empty segments list")

        for seg in segments:
            if not isinstance(seg.get("topic"), str):
                raise ValueError("Segment missing 'topic'")
            if not isinstance(seg.get("text"), str):
                raise ValueError("Segment missing 'text'")
            if not isinstance(seg.get("source_article_ids"), list):
                raise ValueError("Segment missing 'source_article_ids'")

        rendered = "\n\n".join(seg["text"] for seg in segments)
        segment_map_json = json.dumps(segments)
        logger.info(
            "Parsed structured briefing: %d segments, %d chars",
            len(segments),
            len(rendered),
        )
        return rendered, segment_map_json

    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.warning("Briefing JSON parse failed (%s) — storing plain text, no segments", e)
        return raw, None


async def generate_daily_briefing(
    db: Database,
    config: OllamaConfig,
    validator: ValidatorRun | None = None,
) -> str | None:
    """Generate a combined daily briefing from today's article summaries.

    Checks for an existing briefing first (skip if already generated today).
    Returns the rendered briefing text, or None if Ollama is unavailable or no
    summaries exist.

    The briefing prompt instructs Ollama to output structured JSON
    (``{"segments": [{"topic": ..., "text": ..., "source_article_ids": [...]}]}``).
    If parsing succeeds the segment map is stored alongside the briefing text.
    If parsing fails the raw response is stored as plain text with no segments
    (graceful fallback).

    With a *validator*, each segment is ground-checked before saving (see
    ``validate_briefing``); if nothing survives no briefing is saved.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing = db.get_briefing(today)
    if existing:
        logger.info("Daily briefing already exists for %s, reusing", today)
        return existing["content"]

    articles = db.get_todays_articles()
    summaries_by_category = _briefing_summaries(articles)

    if not summaries_by_category:
        logger.info("No summaries available for daily briefing")
        return None

    prompt = _build_briefing_prompt(summaries_by_category)
    if not prompt:
        logger.warning("Briefing prompt is empty")
        return None

    async with httpx.AsyncClient() as client:
        available = await check_ollama_available(client, config)
        if not available:
            logger.warning("Skipping daily briefing — Ollama not available")
            return None

        try:
            resp = await client.post(
                f"{config.host}/api/generate",
                json={
                    "model": config.model,
                    "prompt": prompt,
                    "stream": False,
                    # Thinking models (gemma4) reason before answering unless told not to:
                    # slower, and it eats num_predict. Older models ignore the flag.
                    "think": False,
                    "format": BRIEFING_SCHEMA,
                    "options": {
                        "temperature": 0.5,
                        "top_p": 0.9,
                        "num_predict": 1500,
                    },
                },
                timeout=BRIEFING_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "").strip()

            if not raw:
                logger.warning("Empty briefing response from Ollama")
                return None

            rendered, segment_map_json = _parse_briefing_response(raw)
            rendered, segment_map_json = validate_briefing(
                rendered, segment_map_json, articles, validator
            )
            if not rendered:
                logger.warning("Every briefing sentence failed validation — no briefing saved")
                return None
            db.save_briefing(today, rendered, config.model, segment_map_json)
            logger.info("Daily briefing generated (%d chars)", len(rendered))
            return rendered

        except httpx.HTTPError as e:
            logger.warning("Failed to generate daily briefing: %s", e)
            return None
