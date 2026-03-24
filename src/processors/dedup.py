"""Article deduplication for Morning Brief.

Three-layer dedup:
1. URL hash — exact URL match
2. Title fuzzy match — catches reformatted URLs for same article
3. Content hash — catches syndicated articles with different URLs
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Normalize a title for fuzzy comparison."""
    title = title.lower().strip()
    # Remove common prefixes like "Breaking:", "Updated:", etc.
    title = re.sub(r"^(breaking|updated|exclusive|opinion|analysis):\s*", "", title)
    # Remove non-alphanumeric except spaces
    title = re.sub(r"[^a-z0-9\s]", "", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicate articles from a list.

    Yields unique articles, skipping duplicates detected by URL hash,
    normalized title, or content hash.
    """
    seen_url_hashes: set[str] = set()
    seen_titles: set[str] = set()
    seen_content_hashes: set[str] = set()
    unique: list[dict] = []
    dupes = 0

    for article in articles:
        # Layer 1: URL hash
        url_hash = article.get("url_hash", "")
        if url_hash in seen_url_hashes:
            dupes += 1
            continue
        seen_url_hashes.add(url_hash)

        # Layer 2: Normalized title
        norm_title = _normalize_title(article.get("title", ""))
        if norm_title and norm_title in seen_titles:
            dupes += 1
            continue
        if norm_title:
            seen_titles.add(norm_title)

        # Layer 3: Content hash
        content_hash = article.get("content_hash")
        if content_hash and content_hash in seen_content_hashes:
            dupes += 1
            continue
        if content_hash:
            seen_content_hashes.add(content_hash)

        unique.append(article)

    if dupes:
        logger.info("Dedup removed %d duplicates, %d unique remain", dupes, len(unique))

    return unique
