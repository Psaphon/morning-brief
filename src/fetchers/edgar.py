"""SEC EDGAR 8-K fetcher for the filings lane (docs/SIGNAL-SCHEMA.md section 12).

Polls ``data.sec.gov/submissions/CIK{cik10}.json`` for each universe stock, keeps new 8-Ks
whose items are in ``FILINGS_ITEMS``, and stores them (primary document plus EX-99.*
exhibits) in the ``filings`` table. It does not extract numbers or compute direction.

Acceptance time zone (verified by the PM against live EDGAR on 2026-11-22): the API's
``acceptanceDateTime`` (e.g. ``2026-10-30T10:32:50.000Z``) is **true UTC**. EDGAR's own
index pages show US Eastern time: ``2026-10-30 06:32:50`` (EDT, UTC-4) is API
``10:32:50Z``, and ``2026-01-30 06:31:24`` (EST, UTC-5) is API ``11:31:24Z``. Those two
pairs are pinned in tests/test_edgar_fetcher.py. Never treat the value as Eastern, and
never substitute fetch time for it (contract section 12.5).

Unlike the other fetchers, EDGAR is never contacted without ``CONTACT_EMAIL``. Neither the
address nor the User-Agent is ever logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import tomllib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from ..config import Config
from ..db import Database
from ..http import make_client
from ..timeutil import UTC_Z_FORMAT
from .rss import html_to_text

logger = logging.getLogger(__name__)

SEC_DATA_BASE = "https://data.sec.gov"
SEC_WWW_BASE = "https://www.sec.gov"
TICKER_MAP_PATH = Path("config/ticker_map.toml")

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
CIK_RE = re.compile(r"^\d{10}$")
EXHIBIT_TYPE_RE = re.compile(r"^EX-99(\..+)?$", re.IGNORECASE)
RETRYABLE_STATUS = frozenset({403, 429}) | frozenset(range(500, 600))
MAX_RETRY_AFTER_S = 60.0
REQUEST_TIMEOUT_S = 30.0


class EdgarRequestError(Exception):
    """A request failed for good (non-retryable status, or retries exhausted)."""


class EdgarBlocked(Exception):
    """403s persisted past the retries: the SEC may be blocking us. Stop the whole run."""


class InvalidAcceptanceTime(ValueError):
    """The acceptance time is unusable (no zone, unparseable or in the future)."""


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def parse_acceptance_time(value: object, now: datetime) -> datetime:
    """Parse ``acceptanceDateTime`` as UTC; raise InvalidAcceptanceTime if it is unusable.

    Rejects a missing or zone-less value (never guessing a zone) and one later than *now*.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidAcceptanceTime("missing")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise InvalidAcceptanceTime("unparseable") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidAcceptanceTime("no zone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now:
        raise InvalidAcceptanceTime("in the future")
    return parsed


# ---------------------------------------------------------------------------
# Rate limiting and the HTTP client
# ---------------------------------------------------------------------------


class RateLimiter:
    """At most *max_requests* acquisitions in any rolling *window_s* window."""

    def __init__(
        self,
        max_requests: int,
        *,
        window_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_requests = max(1, max_requests)
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self.window_s:
                    self._sent.popleft()
                if len(self._sent) < self.max_requests:
                    self._sent.append(now)
                    return
                await self._sleep(self._sent[0] + self.window_s - now)


class EdgarClient:
    """Rate-limited, retrying GET client for SEC hosts, built on ``make_client``."""

    def __init__(
        self,
        contact_email: str,
        *,
        max_rps: int = 5,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep
        self._jitter = jitter
        self._limiter = RateLimiter(max_rps, clock=clock, sleep=sleep)
        self._http = make_client(
            contact_email=contact_email, contact_url="", timeout=REQUEST_TIMEOUT_S
        )

    async def __aenter__(self) -> EdgarClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _backoff_s(self, attempt: int, retry_after: str | None) -> float:
        delay = self.backoff_base_s * (2**attempt) * (1 + self._jitter())
        if retry_after and retry_after.strip().isdigit():
            delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_S))
        return delay

    async def get(self, url: str) -> bytes:
        """GET *url* and return the body.

        429, 403 and 5xx (and transport errors) back off exponentially with jitter, up to
        ``max_retries`` retries. Persistent 403 raises EdgarBlocked; anything else that
        still fails raises EdgarRequestError.
        """
        status: int | None = None
        for attempt in range(self.max_retries + 1):
            await self._limiter.acquire()
            retry_after = None
            try:
                response = await self._http.get(url)
            except httpx.TransportError as exc:
                status = None
                reason = type(exc).__name__
            else:
                status = response.status_code
                if status < 400:
                    return response.content
                if status not in RETRYABLE_STATUS:
                    raise EdgarRequestError(f"HTTP {status} for {url}")
                reason = f"HTTP {status}"
                retry_after = response.headers.get("Retry-After")
            if attempt == self.max_retries:
                break
            delay = self._backoff_s(attempt, retry_after)
            logger.warning(
                "EDGAR %s for %s; retry %d/%d in %.1fs",
                reason,
                url,
                attempt + 1,
                self.max_retries,
                delay,
            )
            await self._sleep(delay)
        if status == 403:
            raise EdgarBlocked(url)
        raise EdgarRequestError(f"giving up on {url} after {self.max_retries + 1} attempts")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _IndexTableParser(HTMLParser):
    """Collects every table row as a list of (cell text, first link href) pairs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str | None]]] = []
        self._row: list[tuple[str, str | None]] | None = None
        self._cell_text: list[str] | None = None
        self._cell_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell_text, self._cell_href = [], None
        elif tag == "a" and self._cell_text is not None and self._cell_href is None:
            self._cell_href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._cell_text is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell_text is not None:
            self._row.append((" ".join("".join(self._cell_text).split()), self._cell_href))
            self._cell_text = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _document_path(href: str) -> str:
    """The archive path an index-page link points at (unwraps the ``/ix?doc=`` viewer)."""
    parsed = urlparse(href)
    if parsed.path.rstrip("/") == "/ix" and "doc" in parse_qs(parsed.query):
        return unquote(parse_qs(parsed.query)["doc"][0])
    return parsed.path


def parse_exhibit_links(index_html: str, folder_path: str) -> list[tuple[str, str]]:
    """(type, archive path) for each ``EX-99.*`` row of the filing index document table.

    Columns are Seq, Description, Document, Type, Size, located by header text when there
    is one. Links outside *folder_path* (the filing's own archive folder) are ignored.
    """
    parser = _IndexTableParser()
    parser.feed(index_html)
    type_col, doc_col = 3, 2
    found: list[tuple[str, str]] = []
    for row in parser.rows:
        labels = [text.lower() for text, _ in row]
        if "type" in labels and "document" in labels:
            type_col, doc_col = labels.index("type"), labels.index("document")
            continue
        if len(row) <= max(type_col, doc_col):
            continue
        doc_type, href = row[type_col][0], row[doc_col][1]
        if not EXHIBIT_TYPE_RE.match(doc_type) or not href:
            continue
        path = _document_path(href)
        if not path.startswith(folder_path) or path == folder_path:
            logger.warning("Ignoring %s link outside the filing folder", doc_type)
            continue
        found.append((doc_type.upper(), path))
    return found


def load_stock_ciks(path: Path, *, include_former: bool = False) -> list[tuple[str, str]]:
    """(ticker, cik10) for each stock in the ticker map.

    ``former_ciks`` (predecessor registrants) are ignored by default, as the daily fetch needs.
    With ``include_former`` each stock is followed by one pair per usable former CIK, for the
    backfill.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    stocks: list[tuple[str, str]] = []
    for entry in raw.get("tickers", []):
        if entry.get("asset_class") != "stock":
            continue
        cik = str(entry.get("cik", ""))
        if not CIK_RE.match(cik):
            logger.warning("Ticker %s has no usable CIK; skipping it", entry.get("symbol"))
            continue
        stocks.append((entry["symbol"], cik))
        if not include_former:
            continue
        for former in entry.get("former_ciks", []) or []:
            former = str(former)
            if not CIK_RE.match(former):
                logger.warning("Ticker %s: ignoring malformed former CIK", entry.get("symbol"))
            elif (entry["symbol"], former) not in stocks:
                stocks.append((entry["symbol"], former))
    return stocks


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------


@dataclass
class FetchSummary:
    ciks_polled: int = 0
    new: int = 0
    skipped: int = 0  # candidates already stored
    rejected: int = 0  # unusable acceptance time
    failed: int = 0  # requests that gave up; retried on the next run
    blocked: bool = False
    no_contact: bool = False

    def line(self) -> str:
        text = (
            f"Filings fetch: {self.ciks_polled} CIKs polled, {self.new} new, "
            f"{self.skipped} skipped, {self.rejected} rejected"
        )
        if self.failed:
            text += f", {self.failed} failed"
        if self.blocked:
            text += " (stopped: SEC returned 403)"
        if self.no_contact:
            text += " (CONTACT_EMAIL is not set)"
        return text


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _candidates(recent: dict) -> list[dict]:
    """8-K rows of ``filings.recent`` (parallel arrays). ``8-K/A`` is deliberately excluded."""
    keys = ("accessionNumber", "form", "acceptanceDateTime", "items", "primaryDocument")
    columns = [recent.get(k) for k in keys]
    if not all(isinstance(c, list) for c in columns):
        return []
    return [dict(zip(keys, values)) for values in zip(*columns) if values[1] == "8-K"]


def submission_columns(payload: dict) -> dict:
    """The parallel arrays of a submissions document, whichever shape it has.

    The main file keeps them under ``filings.recent``; an older paged file
    (``CIK..-submissions-001.json``) has them at the top level.
    """
    recent = payload.get("filings", {}).get("recent") if isinstance(payload, dict) else None
    return recent if isinstance(recent, dict) else payload


async def store_filing(
    client: EdgarClient,
    db: Database,
    ticker: str,
    cik: str,
    accession: str,
    accepted: datetime,
    items: list[str],
    primary_name: str,
    now: Callable[[], datetime],
    www_base: str,
) -> bool | None:
    """Fetch an 8-K's primary document and EX-99.* exhibits and store them.

    The same code serves the daily fetch and the backfill. Returns True when a new row was
    stored, False when the accession already existed, and None when a request gave up (nothing
    is stored, so a later run retries). ``cik`` is the filer that owns the archive folder.
    """
    folder = f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
    primary_url = f"{www_base}{folder}{quote(primary_name, safe='')}"
    try:
        primary_html = await client.get(primary_url)
        index_html = (await client.get(f"{www_base}{folder}{accession}-index.htm")).decode(
            "utf-8", errors="replace"
        )
        exhibits = []
        for doc_type, path in parse_exhibit_links(index_html, folder):
            body = await client.get(f"{www_base}{path}")
            exhibits.append(
                {
                    "type": doc_type,
                    "document": path.rsplit("/", 1)[-1],
                    "url": f"{www_base}{path}",
                    "html": body.decode("utf-8", errors="replace"),
                }
            )
    except EdgarRequestError as exc:
        logger.warning("%s %s: not stored, will retry next run (%s)", ticker, accession, exc)
        return None

    return db.insert_filing(
        accession=accession,
        cik=cik,
        ticker=ticker,
        form="8-K",
        items=items,
        accepted_at=accepted.strftime(UTC_Z_FORMAT),
        fetched_at=now().strftime(UTC_Z_FORMAT),
        primary_doc_url=primary_url,
        primary_html=primary_html,
        exhibits=exhibits,
        primary_text=html_to_text(primary_html.decode("utf-8", errors="replace")),
        exhibit_text="\n\n".join(f"[{e['type']}]\n{html_to_text(e['html'])}" for e in exhibits),
    )


async def _process_cik(
    client: EdgarClient,
    db: Database,
    config: Config,
    ticker: str,
    cik: str,
    summary: FetchSummary,
    now: Callable[[], datetime],
    data_base: str,
    www_base: str,
    cutoff: datetime,
) -> None:
    submissions = json.loads(await client.get(f"{data_base}/submissions/CIK{cik}.json"))
    recent = submissions.get("filings", {}).get("recent", {})
    keep_items = set(config.filings_items)

    for row in _candidates(recent):
        accession = str(row["accessionNumber"])
        if not ACCESSION_RE.match(accession):
            logger.warning("%s: ignoring malformed accession number", ticker)
            continue
        try:
            accepted = parse_acceptance_time(row["acceptanceDateTime"], now())
        except InvalidAcceptanceTime as exc:
            summary.rejected += 1
            logger.warning("%s %s: rejected acceptance time (%s)", ticker, accession, exc)
            continue
        if accepted < cutoff:
            continue
        items = [i.strip() for i in str(row["items"] or "").split(",")]
        items = [i for i in items if i in keep_items]
        if not items:
            continue
        if db.has_filing(accession):
            summary.skipped += 1
            continue
        primary_name = str(row["primaryDocument"] or "")
        if not primary_name:
            logger.warning("%s %s: no primary document listed", ticker, accession)
            continue

        stored = await store_filing(
            client, db, ticker, cik, accession, accepted, items, primary_name, now, www_base
        )
        if stored is None:
            summary.failed += 1
        elif stored:
            summary.new += 1
            logger.info("%s: stored 8-K %s (items %s)", ticker, accession, ",".join(items))
        else:
            summary.skipped += 1


async def fetch_filings(
    config: Config,
    db: Database,
    *,
    ticker_map_path: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
    data_base: str | None = None,
    www_base: str | None = None,
    **client_options: object,
) -> FetchSummary:
    """Fetch new 8-Ks for every universe stock into the ``filings`` table.

    Never raises for EDGAR problems: a CIK that fails is logged and skipped, and persistent
    403s end the run with one ERROR. With no ``CONTACT_EMAIL`` it logs one WARNING and sends
    nothing. ``client_options`` (clock, sleep, jitter, backoff_base_s) are for tests.
    """
    summary = FetchSummary()
    if not config.contact_email:
        summary.no_contact = True
        logger.warning("CONTACT_EMAIL is not set; skipping the EDGAR fetch (SEC requires contact)")
        return summary

    stocks = load_stock_ciks(ticker_map_path or TICKER_MAP_PATH)
    cutoff = now() - timedelta(days=config.filings_lookback_days)
    data_base = (data_base or SEC_DATA_BASE).rstrip("/")
    www_base = (www_base or SEC_WWW_BASE).rstrip("/")

    async with EdgarClient(
        config.contact_email,
        max_rps=config.sec_max_rps,
        max_retries=config.sec_max_retries,
        **client_options,  # type: ignore[arg-type]
    ) as client:
        for ticker, cik in stocks:
            summary.ciks_polled += 1
            try:
                await _process_cik(
                    client, db, config, ticker, cik, summary, now, data_base, www_base, cutoff
                )
            except EdgarBlocked:
                summary.blocked = True
                logger.error("EDGAR kept returning 403; stopping the filings fetch for this run")
                break
            except (EdgarRequestError, ValueError) as exc:
                summary.failed += 1
                logger.warning("%s: submissions fetch failed (%s)", ticker, exc)
    return summary
