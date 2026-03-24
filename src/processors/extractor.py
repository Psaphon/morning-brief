"""Article full-text extraction using trafilatura."""

from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from ..fetchers.rss import RawArticle

logger = logging.getLogger(__name__)

EXTRACT_TIMEOUT = 15.0
MAX_CONCURRENT = 5


async def extract_single(
    client: httpx.AsyncClient,
    article: RawArticle,
) -> dict | None:
    """Fetch and extract full text for a single article.

    Returns a dict ready for db.insert_article, or None on failure.
    """
    try:
        response = await client.get(article.url, timeout=EXTRACT_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.debug("Failed to fetch article %s: %s", article.url, e)
        # Fall back to RSS summary if available
        full_text = article.summary
        if not full_text:
            return None
        return _article_to_dict(article, full_text)

    extracted = trafilatura.extract(response.text)
    if not extracted:
        # Fall back to RSS summary
        extracted = article.summary

    if not extracted:
        logger.debug("No text extracted from %s", article.url)
        return None

    return _article_to_dict(article, extracted)


def _article_to_dict(article: RawArticle, full_text: str) -> dict:
    """Convert a RawArticle + extracted text to a dict for db insertion."""
    import hashlib

    url_hash = hashlib.sha256(article.url.encode()).hexdigest()[:16]
    content_hash = hashlib.sha256(full_text.encode()).hexdigest()[:16] if full_text else None

    return {
        "url": article.url,
        "url_hash": url_hash,
        "title": article.title,
        "source": article.source,
        "category": article.category,
        "author": article.author,
        "published_at": article.published,
        "full_text": full_text,
        "content_hash": content_hash,
    }


async def extract_articles(raw_articles: list[RawArticle]) -> list[dict]:
    """Extract full text for all articles concurrently."""
    if not raw_articles:
        return []

    results: list[dict] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "MorningBrief/0.1"},
    ) as client:
        for i in range(0, len(raw_articles), MAX_CONCURRENT):
            batch = raw_articles[i : i + MAX_CONCURRENT]
            tasks = [extract_single(client, a) for a in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, dict):
                    results.append(result)
                elif isinstance(result, Exception):
                    logger.debug("Extraction error: %s", result)

    logger.info("Extracted %d/%d articles", len(results), len(raw_articles))
    return results
