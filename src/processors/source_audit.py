"""Per-feed source audit: how much real article text does each feed deliver?

Read-only: fetches feeds and article pages live and never touches the
database or the signals output.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import httpx
import trafilatura

from ..http import make_client

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 20.0
MAX_CONCURRENT = 5
MAX_ITEMS_PER_FEED = 50

DEFAULT_MIN_WORDS = 150
DEFAULT_MARKERS_PATH = Path("config/paywall_markers.txt")

# Verdict thresholds (shares of items)
FAILING_ERROR_SHARE = 0.5
PAYWALLED_SHARE = 0.3
TRUNCATED_SHARE = 0.5


@dataclass
class FeedAudit:
    source: str
    url: str
    items: int
    median_words: float
    short_share: float
    paywall_share: float
    fetch_errors: int
    verdict: str


def load_paywall_markers(path: Path = DEFAULT_MARKERS_PATH) -> list[str]:
    """Load lowercase marker phrases, skipping blanks and # comments."""
    if not path.exists():
        logger.warning("Paywall marker file not found: %s", path)
        return []
    markers = []
    for line in path.read_text().splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            markers.append(line)
    return markers


def classify(
    items: int,
    errors: int,
    feed_failed: bool,
    median_words: float,
    short_share: float,
    paywall_share: float,
    min_words: int,
    kind: str = "article",
) -> str:
    """Return ok / ok-post / truncated / paywalled / failing.

    ``post`` feeds are short by design: unless failing they are ``ok-post``.
    """
    if feed_failed or items == 0 or errors / items >= FAILING_ERROR_SHARE:
        return "failing"
    if kind == "post":
        return "ok-post"
    if paywall_share >= PAYWALLED_SHARE:
        return "paywalled"
    if short_share >= TRUNCATED_SHARE or median_words < min_words:
        return "truncated"
    return "ok"


def _recent(entry: feedparser.FeedParserDict, cutoff: datetime) -> bool:
    """Items with no parseable date are kept."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return True
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc) >= cutoff


def _entry_text(entry: feedparser.FeedParserDict) -> str:
    parts = [entry.get("summary") or ""]
    for content in entry.get("content") or []:
        parts.append(content.get("value") or "")
    return " ".join(parts)


async def _audit_item(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    entry: feedparser.FeedParserDict,
    fetch_page: bool = True,
) -> tuple[int, str, bool]:
    """Return (word_count, text, fetch_error) for one feed item.

    ``fetch_page=False`` (post feeds) measures the feed's own text; no request is made.
    """
    link = entry.get("link", "")
    text = ""
    error = False
    async with sem:
        if fetch_page:
            try:
                response = await client.get(link, timeout=FETCH_TIMEOUT)
                response.raise_for_status()
                text = trafilatura.extract(response.text) or ""
            except Exception as e:  # one bad article must not abort the audit
                logger.debug("Audit fetch failed for %s: %s", link, e)
                error = True
    if not text:
        # Same fallback the pipeline uses: whatever the feed itself carries.
        text = trafilatura.extract(_entry_text(entry)) or _entry_text(entry)
    return len(text.split()), text, error


async def audit_feed(
    client: httpx.AsyncClient,
    feed: dict[str, str],
    days: int,
    min_words: int,
    markers: list[str],
    sem: asyncio.Semaphore,
    now: datetime | None = None,
) -> FeedAudit:
    """Audit a single feed. Never raises."""
    source, url = feed["source"], feed["url"]
    kind = feed.get("kind", "article")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    def result(items: int, errors: int, failed: bool, words: list[int], paywalled: int):
        median = float(statistics.median(words)) if words else 0.0
        short = sum(1 for w in words if w < min_words) / items if items else 0.0
        pay = paywalled / items if items else 0.0
        verdict = classify(items, errors, failed, median, short, pay, min_words, kind)
        return FeedAudit(
            source, url, items, median, round(short, 3), round(pay, 3), errors, verdict
        )

    try:
        response = await client.get(url, timeout=FETCH_TIMEOUT)
        response.raise_for_status()
        parsed = feedparser.parse(response.text)
    except Exception as e:
        logger.warning("Audit: feed fetch failed for %s: %s", source, e)
        return result(0, 1, True, [], 0)

    if parsed.bozo and not parsed.entries:
        logger.warning("Audit: malformed feed from %s", source)
        return result(0, 1, True, [], 0)

    entries = [e for e in parsed.entries if e.get("link") and _recent(e, cutoff)]
    entries = entries[:MAX_ITEMS_PER_FEED]
    outcomes = await asyncio.gather(*(_audit_item(client, sem, e, kind != "post") for e in entries))

    words = [w for w, _, _ in outcomes]
    errors = sum(1 for _, _, err in outcomes if err)
    paywalled = sum(1 for _, t, _ in outcomes if any(m in t.lower() for m in markers))
    return result(len(entries), errors, False, words, paywalled)


async def audit_feeds(
    feeds: list[dict[str, str]],
    days: int,
    min_words: int = DEFAULT_MIN_WORDS,
    markers: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[FeedAudit]:
    """Audit all feeds. Pass ``client`` to inject a transport (tests)."""
    markers = markers if markers is not None else load_paywall_markers()
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    owns_client = client is None
    if client is None:
        client = make_client()
    try:
        return list(
            await asyncio.gather(
                *(audit_feed(client, f, days, min_words, markers, sem) for f in feeds)
            )
        )
    finally:
        if owns_client:
            await client.aclose()


def write_report(audits: list[FeedAudit], out_dir: Path, days: int, min_words: int) -> Path:
    """Write data/source-audit-<date>.json and return its path."""
    import json

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"source-audit-{today}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": today,
        "days": days,
        "min_words": min_words,
        "feeds": [asdict(a) for a in audits],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path
