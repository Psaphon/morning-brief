"""Local LLM summarizer — Qwen via Ollama.

Sends unsummarized articles to an Ollama instance, gets back concise
2-3 sentence summaries, and stores them in the database.

Ollama API docs: POST /api/generate with model, prompt, stream=false.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from ..config import OllamaConfig

logger = logging.getLogger(__name__)

# Approximate token limit for input text. Qwen 2.5 7B handles ~8K context
# but we keep it conservative to leave room for the prompt template and output.
MAX_INPUT_CHARS = 12_000  # ~3000 tokens at ~4 chars/token

# How many articles to summarize per batch before pausing
BATCH_SIZE = 5
BATCH_PAUSE_SECONDS = 1.0

# Ollama API timeout — summarizing a long article can take a while
REQUEST_TIMEOUT = 120.0

# Retry config
MAX_RETRIES = 2
RETRY_DELAY = 3.0

SUMMARY_PROMPT = """You are a news analyst writing a morning intelligence briefing.

Summarize the following article in 2-3 concise, factual sentences. Focus on:
- What happened (the key facts)
- Why it matters (significance or impact)
- Who is involved (key actors or organizations)

Do NOT include opinions, speculation, or editorializing. Write in third person.
If the article text is too short or unclear to summarize meaningfully, respond with
exactly: "Insufficient content for summary."

Article title: {title}
Source: {source}
Category: {category}

Article text:
{text}

Summary:"""


@dataclass
class SummaryResult:
    """Result of summarizing a single article."""

    article_id: int
    summary: str
    model: str
    elapsed_seconds: float
    success: bool
    error: str | None = None


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


async def summarize_single(
    client: httpx.AsyncClient,
    config: OllamaConfig,
    article: dict,
) -> SummaryResult:
    """Summarize a single article via Ollama."""
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

    truncated = truncate_text(full_text)
    prompt = SUMMARY_PROMPT.format(
        title=title,
        source=source,
        category=category,
        text=truncated,
    )

    start = time.monotonic()

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.post(
                f"{config.host}/api/generate",
                json={
                    "model": config.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "top_p": 0.9,
                        "num_predict": 256,
                    },
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            summary = data.get("response", "").strip()
            elapsed = time.monotonic() - start

            if not summary:
                return SummaryResult(
                    article_id=article_id,
                    summary="",
                    model=config.model,
                    elapsed_seconds=elapsed,
                    success=False,
                    error="Empty response from Ollama",
                )

            return SummaryResult(
                article_id=article_id,
                summary=summary,
                model=config.model,
                elapsed_seconds=elapsed,
                success=True,
            )

        except httpx.TimeoutException:
            elapsed = time.monotonic() - start
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Timeout summarizing article %d (attempt %d/%d), retrying...",
                    article_id,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )
                await _async_sleep(RETRY_DELAY)
                continue
            return SummaryResult(
                article_id=article_id,
                summary="",
                model=config.model,
                elapsed_seconds=elapsed,
                success=False,
                error="Timeout after retries",
            )

        except httpx.HTTPError as e:
            elapsed = time.monotonic() - start
            if attempt < MAX_RETRIES:
                logger.warning(
                    "HTTP error summarizing article %d: %s (attempt %d/%d)",
                    article_id,
                    e,
                    attempt + 1,
                    MAX_RETRIES + 1,
                )
                await _async_sleep(RETRY_DELAY)
                continue
            return SummaryResult(
                article_id=article_id,
                summary="",
                model=config.model,
                elapsed_seconds=elapsed,
                success=False,
                error=str(e),
            )

    # Should not reach here, but just in case
    return SummaryResult(
        article_id=article_id,
        summary="",
        model=config.model,
        elapsed_seconds=time.monotonic() - start,
        success=False,
        error="Exhausted retries",
    )


async def _async_sleep(seconds: float) -> None:
    """Async sleep wrapper for testability."""
    import asyncio

    await asyncio.sleep(seconds)


async def summarize_articles(
    config: OllamaConfig,
    articles: list[dict],
) -> list[SummaryResult]:
    """Summarize a batch of articles via Ollama.

    Processes articles in small batches with pauses to avoid
    overwhelming the local model.
    """
    if not articles:
        logger.info("No articles to summarize")
        return []

    async with httpx.AsyncClient() as client:
        # Pre-flight check
        available = await check_ollama_available(client, config)
        if not available:
            logger.warning("Skipping summarization — Ollama not available")
            return []

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

        logger.info(
            "Summarization complete: %d succeeded, %d failed out of %d",
            succeeded,
            failed,
            total,
        )
        return results
