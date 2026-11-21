"""Group short posts (Bluesky, Mastodon) from one author into a single source entry.

A thread or a burst of posts is one voice, so it must not count as many sources.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..fetchers.rss import RawArticle, html_to_text

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 100


def post_text(article: RawArticle) -> str:
    """The post's text: its content or summary with HTML stripped (title as a last resort)."""
    return html_to_text(article.content or article.summary) or article.title.strip()


def _parse_time(value: str | None) -> datetime | None:
    """Parse an RSS (RFC 822) or Atom (ISO 8601) timestamp; naive times are taken as UTC."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _merge(posts: list[tuple[datetime, RawArticle]]) -> RawArticle:
    first = posts[0][1]
    text = "\n\n".join(post_text(a) for _, a in posts)
    title = (text.splitlines() or [first.title])[0][:TITLE_MAX_CHARS]
    return RawArticle(
        url=first.url,
        title=title,
        source=first.source,
        category=first.category,
        author=first.author,
        published=first.published,
        summary=text,
        content=text,
        kind="post",
    )


def group_posts(raw_articles: list[RawArticle], hours: float) -> list[RawArticle]:
    """Merge ``post`` articles of one feed and author, each within *hours* of the previous.

    The author falls back to the feed name. Merged text joins the posts in time order;
    url, author and ``published`` come from the earliest post and the title is its first
    line cut to 100 characters. A post with no parseable time, and every non-post
    article, passes through unchanged. Pure: the input list is not modified.
    """
    others: list[RawArticle] = []
    groups: dict[tuple[str, str], list[tuple[datetime, RawArticle]]] = {}
    for article in raw_articles:
        when = _parse_time(article.published) if article.kind == "post" else None
        if when is None:
            others.append(article)
            continue
        key = (article.source, article.author or article.source)
        groups.setdefault(key, []).append((when, article))

    merged: list[RawArticle] = []
    for posts in groups.values():
        ordered = sorted(posts, key=lambda p: p[0])
        current: list[tuple[datetime, RawArticle]] = []
        for when, article in ordered:
            if current and (when - current[-1][0]).total_seconds() > hours * 3600:
                merged.append(_merge(current) if len(current) > 1 else current[0][1])
                current = []
            current.append((when, article))
        if current:
            merged.append(_merge(current) if len(current) > 1 else current[0][1])

    logger.info(
        "Grouped posts: %d articles in, %d out", len(raw_articles), len(others) + len(merged)
    )
    return others + merged
