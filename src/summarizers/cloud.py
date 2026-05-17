"""Cloud LLM synthesizer — Claude API for cross-topic narrative.

Optional upgrade: when ANTHROPIC_API_KEY is set, uses Claude instead of
Ollama for the daily briefing. One API call per pipeline run, rate-limited
by design. Falls back to Ollama if the key is not set or the call fails.

Uses the Claude Messages API directly via httpx (no SDK dependency).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from .local import _build_briefing_prompt, _parse_briefing_response

if TYPE_CHECKING:
    from ..db import Database

logger = logging.getLogger(__name__)

# Claude model to use for synthesis
CLAUDE_MODEL = "claude-sonnet-4-6"

# API endpoint
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

# Keep input small to limit cost — ~4000 tokens of summaries
MAX_PROMPT_CHARS = 16_000  # ~4000 tokens at ~4 chars/token

# Timeout for the single synthesis call
REQUEST_TIMEOUT = 120.0


async def generate_daily_briefing_claude(
    db: Database,
    anthropic_api_key: str,
) -> str | None:
    """Generate a daily briefing using the Claude API.

    Checks for an existing briefing first (skips if already generated today).
    Makes a single API call with all today's article summaries as context.
    Stores the structured segment map alongside the briefing text.

    Returns the rendered briefing text, or None if the API key is not set,
    the API call fails, or no summaries are available.

    Args:
        db: Database instance (must already be connected).
        anthropic_api_key: Anthropic API key. If empty, returns None immediately.
    """
    if not anthropic_api_key:
        logger.debug("ANTHROPIC_API_KEY not set — skipping Claude synthesis")
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing = db.get_briefing(today)
    if existing:
        logger.info("Daily briefing already exists for %s, reusing", today)
        return existing["content"]

    articles = db.get_todays_articles()
    summaries_by_category: dict[str, list[tuple[int, str]]] = {}
    for article in articles:
        if not article.get("summary"):
            continue
        cat = article.get("category", "General")
        summaries_by_category.setdefault(cat, []).append((article["id"], article["summary"]))

    if not summaries_by_category:
        logger.info("No summaries available for Claude briefing")
        return None

    prompt = _build_briefing_prompt(summaries_by_category)
    if not prompt:
        logger.warning("Claude briefing prompt is empty")
        return None

    # Truncate prompt to keep costs low
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS]
        logger.debug("Truncated briefing prompt to %d chars", MAX_PROMPT_CHARS)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                CLAUDE_API_URL,
                headers={
                    "x-api-key": anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 2048,
                    "messages": [
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

        content_blocks = data.get("content", [])
        raw = ""
        for block in content_blocks:
            if block.get("type") == "text":
                raw += block.get("text", "")
        raw = raw.strip()

        if not raw:
            logger.warning("Empty response from Claude API")
            return None

        rendered, segment_map_json = _parse_briefing_response(raw)
        db.save_briefing(today, rendered, CLAUDE_MODEL, segment_map_json)
        logger.info("Claude daily briefing generated (%d chars)", len(rendered))
        return rendered

    except httpx.HTTPStatusError as e:
        logger.warning(
            "Claude API request failed (HTTP %d): %s",
            e.response.status_code,
            e.response.text[:200],
        )
        return None
    except httpx.HTTPError as e:
        logger.warning("Claude API request failed: %s", e)
        return None
