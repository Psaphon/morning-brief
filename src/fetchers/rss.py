"""RSS feed fetcher for Morning Brief.

Parses the FEEDS.md registry, fetches all feeds concurrently via httpx,
and returns raw article dicts ready for processing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import feedparser
import httpx

logger = logging.getLogger(__name__)

# Timeout for individual feed fetches
FETCH_TIMEOUT = 30.0
MAX_CONCURRENT = 10


@dataclass
class RawArticle:
    """An article as fetched from an RSS feed, before processing."""

    url: str
    title: str
    source: str
    category: str
    author: str | None = None
    published: str | None = None
    summary: str | None = None  # RSS-provided summary, not LLM


def parse_feeds_md(feeds_path: Path) -> list[dict[str, str]]:
    """Parse FEEDS.md to extract feed URLs and metadata.

    Looks for markdown table rows with URL patterns.
    Returns list of {source, url, category} dicts.
    """
    if not feeds_path.exists():
        logger.warning("Feeds file not found: %s", feeds_path)
        return []

    text = feeds_path.read_text()
    feeds: list[dict[str, str]] = []

    current_category = "uncategorized"

    # Match ### headings for category
    # Match table rows with backtick-wrapped URLs
    for line in text.splitlines():
        heading_match = re.match(r"^###\s+(.+)", line)
        if heading_match:
            current_category = heading_match.group(1).strip()
            continue

        # Match: | Source Name | `https://...` | status | notes |
        row_match = re.match(
            r"\|\s*([^|]+?)\s*\|\s*`(https?://[^`]+)`\s*\|\s*(\w+)\s*\|",
            line,
        )
        if row_match:
            source = row_match.group(1).strip()
            url = row_match.group(2).strip()
            status = row_match.group(3).strip()

            # Skip broken/deprecated feeds
            if status in ("broken", "deprecated"):
                continue

            feeds.append(
                {
                    "source": source,
                    "url": url,
                    "category": current_category,
                }
            )

    logger.info("Parsed %d feeds from %s", len(feeds), feeds_path)
    return feeds


async def fetch_feed(
    client: httpx.AsyncClient,
    feed_info: dict[str, str],
) -> list[RawArticle]:
    """Fetch and parse a single RSS feed. Returns articles or empty list on error."""
    url = feed_info["url"]
    source = feed_info["source"]
    category = feed_info["category"]

    try:
        response = await client.get(url, timeout=FETCH_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch %s (%s): %s", source, url, e)
        return []

    parsed = feedparser.parse(response.text)

    if parsed.bozo and not parsed.entries:
        logger.warning("Malformed feed from %s: %s", source, parsed.bozo_exception)
        return []

    articles: list[RawArticle] = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        title = entry.get("title", "")
        if not link or not title:
            continue

        articles.append(
            RawArticle(
                url=link,
                title=title.strip(),
                source=source,
                category=category,
                author=entry.get("author"),
                published=entry.get("published"),
                summary=entry.get("summary"),
            )
        )

    logger.info("Fetched %d articles from %s", len(articles), source)
    return articles


async def fetch_all_feeds(feeds_path: Path) -> list[RawArticle]:
    """Fetch all RSS feeds concurrently and return combined articles."""
    feed_list = parse_feeds_md(feeds_path)
    if not feed_list:
        return []

    all_articles: list[RawArticle] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "MorningBrief/0.1 (news aggregator; contact via GitHub)"},
    ) as client:
        # Fetch in batches to respect rate limits
        for i in range(0, len(feed_list), MAX_CONCURRENT):
            batch = feed_list[i : i + MAX_CONCURRENT]
            tasks = [fetch_feed(client, feed) for feed in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    all_articles.extend(result)
                elif isinstance(result, Exception):
                    logger.warning("Feed batch error: %s", result)

    logger.info("Total: %d articles from %d feeds", len(all_articles), len(feed_list))
    return all_articles
