"""Resumable EDGAR earnings backfill and ``filings-history.jsonl`` (SIGNAL-SCHEMA §12.2).

For every universe stock and each of its CIKs (``cik`` plus ``former_ciks``) this lists
8-Ks with item 2.02 accepted on or after ``--since``, across ``filings.recent`` and the older
paged submissions files, stores the ones not yet stored with the daily fetcher's code, runs
the daily facts extraction oldest first, and rewrites the history file with the daily
emitter's ``build_events`` (no emit-day window).

It only runs inside ``BACKFILL_WINDOW_UTC`` unless told otherwise. The clock is checked before
every submissions request, every filing and every extraction, so an ending window stops the
run at the next item; everything already stored stays stored, so a rerun resumes.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..config import Config
from ..db import Database
from ..fetchers.edgar import (
    ACCESSION_RE,
    SEC_DATA_BASE,
    SEC_WWW_BASE,
    TICKER_MAP_PATH,
    EdgarBlocked,
    EdgarClient,
    EdgarRequestError,
    InvalidAcceptanceTime,
    _candidates,
    load_stock_ciks,
    parse_acceptance_time,
    store_filing,
    submission_columns,
)
from ..publishers.signals import TickerMap, load_ticker_map
from ..publishers.signals_filings import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    build_events,
    parse_model_cutoff,
)
from .filing_facts import VALUE_FIELDS, extract_filings

logger = logging.getLogger(__name__)

EARNINGS_ITEM = "2.02"
HISTORY_LINE_REF = "#/$defs/history_line"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


def parse_window(raw: str) -> tuple[time, time]:
    """Parse ``HH:MM-HH:MM`` (UTC). Raises ValueError on anything else."""
    try:
        start_raw, end_raw = raw.strip().split("-")
        start = time.fromisoformat(start_raw.strip())
        end = time.fromisoformat(end_raw.strip())
    except ValueError:
        raise ValueError(f"BACKFILL_WINDOW_UTC {raw!r} is not HH:MM-HH:MM") from None
    if start == end:
        raise ValueError(f"BACKFILL_WINDOW_UTC {raw!r} is an empty window")
    return start, end


def in_window(window: tuple[time, time], moment: datetime) -> bool:
    """True when *moment* (UTC) is in ``[start, end)``; a window may cross midnight."""
    start, end = window
    at = moment.astimezone(timezone.utc).time()
    return start <= at < end if start < end else at >= start or at < end


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass
class BackfillSummary:
    stocks: int = 0
    ciks: int = 0
    earnings: int = 0  # earnings 8-Ks listed (new + already stored + failed)
    new: int = 0
    already: int = 0
    failed: int = 0  # requests that gave up; retried on the next run
    rejected: int = 0  # unusable acceptance time
    ok: int = 0
    partial: int = 0
    extract_failed: int = 0
    history_lines: int | None = None  # None: the history file was not written
    stopped: str | None = None
    no_contact: bool = False
    outside_window: bool = False

    def line(self) -> str:
        listed = f"{self.new} new, {self.already} already stored"
        if self.failed:
            listed += f", {self.failed} failed"
        if self.rejected:
            listed += f", {self.rejected} rejected"
        extracted = self.ok + self.partial + self.extract_failed
        history = "not written" if self.history_lines is None else f"{self.history_lines} lines"
        return (
            f"backfill: {self.stocks} stocks, {self.ciks} CIKs, {self.earnings} earnings 8-Ks "
            f"({listed}), {extracted} extracted ({self.ok} ok, {self.partial} partial, "
            f"{self.extract_failed} failed), history {history}, "
            f"stopped: {self.stopped or 'done'}"
        )


# ---------------------------------------------------------------------------
# History file
# ---------------------------------------------------------------------------


def history_lines(
    db: Database, ticker_map: TickerMap, config: Config, now: datetime
) -> list[dict[str, Any]]:
    """One record per stored earnings filing with facts: ``build_events`` plus schema_version."""
    events = build_events(db, ticker_map, config, now, windowed=False)
    return [{"schema_version": SCHEMA_VERSION, **event} for event in events]


def write_history(path: Path, lines: list[dict[str, Any]]) -> bool:
    """Validate every line against the committed schema, then write atomically.

    If any line fails (or the schema cannot be loaded) nothing is written, one ERROR is logged
    and False is returned; the previous file, if any, is left alone.
    """
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("Cannot load filings schema %s: %s; history not written", SCHEMA_PATH, exc)
        return False
    validator = Draft202012Validator({"$ref": HISTORY_LINE_REF, "$defs": schema["$defs"]})
    bad = [
        (n, next(iter(validator.iter_errors(line))))
        for n, line in enumerate(lines, 1)
        if not validator.is_valid(line)
    ]
    if bad:
        first_n, first_err = bad[0]
        logger.error(
            "History not written: %d of %d line(s) failed schema validation "
            "(first: line %d at %s: %s)",
            len(bad),
            len(lines),
            first_n,
            "/".join(str(p) for p in first_err.path) or "<root>",
            first_err.message,
        )
        return False

    tmp = path.with_name(path.name + ".tmp")
    payload = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("Failed to write %s: %s", path, exc)
        return False
    logger.info("Wrote %d history line(s) to %s", len(lines), path)
    return True


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _needed_pages(submissions: dict, since: date) -> list[str]:
    """Names of paged files that can hold filings on or after *since* (``filingTo`` >= since)."""
    names: list[str] = []
    files = submissions.get("filings", {}).get("files", [])
    for entry in files if isinstance(files, list) else []:
        name = str(entry.get("name", "")) if isinstance(entry, dict) else ""
        if not name or "/" in name or name.startswith("."):
            continue
        try:
            if date.fromisoformat(str(entry["filingTo"])[:10]) < since:
                continue
        except (KeyError, ValueError):
            pass  # no usable range: fetch it rather than miss filings
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_backfill(
    config: Config,
    db: Database,
    *,
    since: str | None = None,
    ignore_window: bool = False,
    ticker_map_path: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
    data_base: str | None = None,
    www_base: str | None = None,
    **client_options: object,
) -> BackfillSummary:
    """Backfill earnings 8-Ks, extract facts oldest first, write the history file.

    Never raises for EDGAR problems. ``now`` (UTC datetime) drives the window and the
    acceptance-time check; ``client_options`` (clock, sleep, jitter, backoff_base_s) are for
    tests and reach the shared EdgarClient.
    """
    summary = BackfillSummary()
    since_date = date.fromisoformat(since or config.backfill_since)
    window = parse_window(config.backfill_window_utc)

    def window_over() -> bool:
        return not ignore_window and not in_window(window, now())

    if window_over():
        summary.outside_window = True
        summary.stopped = "outside window"
        logger.info(
            "Outside BACKFILL_WINDOW_UTC (%s); nothing done (use --ignore-window to override)",
            config.backfill_window_utc,
        )
        return summary
    if not config.contact_email:
        summary.no_contact = True
        summary.stopped = "CONTACT_EMAIL is not set"
        logger.warning("CONTACT_EMAIL is not set; skipping the EDGAR backfill (SEC requires it)")
        return summary

    ticker_map_path = ticker_map_path or TICKER_MAP_PATH
    filers = load_stock_ciks(ticker_map_path, include_former=True)
    summary.stocks = len({ticker for ticker, _ in filers})
    summary.ciks = len(filers)
    since_dt = datetime.combine(since_date, time.min, timezone.utc)
    keep_items = set(config.filings_items) | {EARNINGS_ITEM}
    data_base = (data_base or SEC_DATA_BASE).rstrip("/")
    www_base = (www_base or SEC_WWW_BASE).rstrip("/")
    seen: set[str] = set()

    async with EdgarClient(
        config.contact_email,
        max_rps=config.sec_max_rps,
        max_retries=config.sec_max_retries,
        **client_options,  # type: ignore[arg-type]
    ) as client:
        try:
            for ticker, cik in filers:
                if window_over():
                    summary.stopped = "window end"
                    break
                await _backfill_cik(
                    client, db, ticker, cik, summary, seen, since_date, since_dt, keep_items,
                    now, window_over, data_base, www_base,
                )  # fmt: skip
                if summary.stopped:
                    break
        except EdgarBlocked:
            summary.stopped = "SEC 403"
            logger.error("EDGAR kept returning 403; stopping the backfill")

    if summary.stopped is None or summary.stopped == "window end":
        await _extract(config, db, summary, window_over)

    try:
        ticker_map = load_ticker_map(ticker_map_path)
    except Exception as exc:  # SignalEmitError: nothing sensible to write without the universe
        logger.error("Cannot load the ticker map (%s); history not written", exc)
        return summary
    lines = history_lines(db, ticker_map, config, now())
    if write_history(config.filings_history_path, lines):
        summary.history_lines = len(lines)
    return summary


async def _backfill_cik(
    client: EdgarClient,
    db: Database,
    ticker: str,
    cik: str,
    summary: BackfillSummary,
    seen: set[str],
    since_date: date,
    since_dt: datetime,
    keep_items: set[str],
    clock: Callable[[], datetime],
    window_over: Callable[[], bool],
    data_base: str,
    www_base: str,
) -> None:
    """List this CIK's earnings 8-Ks (recent + needed paged files) and store the new ones."""
    pages: list[dict] = []
    try:
        main = json.loads(await client.get(f"{data_base}/submissions/CIK{cik}.json"))
        pages.append(submission_columns(main))
        for name in _needed_pages(main, since_date):
            if window_over():
                summary.stopped = "window end"
                return
            try:
                pages.append(
                    submission_columns(
                        json.loads(await client.get(f"{data_base}/submissions/{name}"))
                    )
                )
            except (EdgarRequestError, ValueError) as exc:
                summary.failed += 1
                logger.warning("%s: paged file %s failed (%s)", ticker, name, exc)
    except (EdgarRequestError, ValueError) as exc:
        summary.failed += 1
        logger.warning("%s: submissions fetch failed (%s)", ticker, exc)
        return

    candidates: list[tuple[datetime, dict, list[str]]] = []
    for page in pages:
        for row in _candidates(page):
            accession = str(row["accessionNumber"])
            if not ACCESSION_RE.match(accession):
                logger.warning("%s: ignoring malformed accession number", ticker)
                continue
            try:
                accepted = parse_acceptance_time(row["acceptanceDateTime"], clock())
            except InvalidAcceptanceTime as exc:
                summary.rejected += 1
                logger.warning("%s %s: rejected acceptance time (%s)", ticker, accession, exc)
                continue
            raw_items = [i.strip() for i in str(row["items"] or "").split(",")]
            if accepted < since_dt or EARNINGS_ITEM not in raw_items:
                continue
            if accession in seen:
                continue
            seen.add(accession)
            candidates.append((accepted, row, [i for i in raw_items if i in keep_items]))

    for accepted, row, items in sorted(candidates, key=lambda c: c[0]):
        accession = str(row["accessionNumber"])
        if db.has_filing(accession):
            summary.earnings += 1
            summary.already += 1
            continue
        if window_over():
            summary.stopped = "window end"
            return
        summary.earnings += 1
        primary_name = str(row["primaryDocument"] or "")
        if not primary_name:
            logger.warning("%s %s: no primary document listed", ticker, accession)
            summary.failed += 1
            continue
        stored = await store_filing(
            client, db, ticker, cik, accession, accepted, items, primary_name, clock, www_base
        )
        if stored is None:
            summary.failed += 1
        elif stored:
            summary.new += 1
            logger.info("%s: stored 8-K %s from CIK %s", ticker, accession, cik)
        else:
            summary.already += 1


async def _extract(
    config: Config, db: Database, summary: BackfillSummary, window_over: Callable[[], bool]
) -> None:
    """Run the daily extraction over the stored filings, oldest first, inside the window."""
    stopped_by_window = False

    def should_stop() -> bool:
        nonlocal stopped_by_window
        if window_over():
            stopped_by_window = True
        return stopped_by_window

    facts = await extract_filings(config, db, should_stop=should_stop)
    summary.ok, summary.partial, summary.extract_failed = facts.ok, facts.partial, facts.failed
    if stopped_by_window:
        summary.stopped = "window end"
    elif db.filings_for_extraction():
        summary.stopped = "model unreachable"


# ---------------------------------------------------------------------------
# --counts
# ---------------------------------------------------------------------------


def backfill_counts(
    db: Database, ticker_map: TickerMap, config: Config, now: datetime | None = None
) -> str:
    """The [HUMAN] review numbers, read from the DB (no fetching, no model).

    Events are exactly the history file's records, so the counts describe that file.
    """
    events = history_lines(db, ticker_map, config, now or _utc_now())
    out = [f"events: {len(events)}"]

    per_ticker: dict[str, int] = {}
    for event in events:
        per_ticker[event["ticker"]] = per_ticker.get(event["ticker"], 0) + 1
    out.append("events per ticker:")
    out.extend(f"  {t}: {n}" for t, n in sorted(per_ticker.items()))

    accessions = [e["accession"] for e in events]
    verified = 0
    for start in range(0, len(accessions), 500):
        chunk = accessions[start : start + 500]
        rows = db.conn.execute(
            f"SELECT {', '.join(VALUE_FIELDS)} FROM filing_facts "  # noqa: S608
            f"WHERE accession IN ({', '.join('?' for _ in chunk)})",
            chunk,
        ).fetchall()
        verified += sum(1 for row in rows for name in VALUE_FIELDS if row[name] is not None)
    possible = len(accessions) * len(VALUE_FIELDS)
    rate = f"{verified / possible:.1%}" if possible else "n/a"
    out.append(f"verification rate: {rate} ({verified} of {possible} values verified)")

    out.append("direction_n distribution:")
    out.extend(f"  {n}: {sum(1 for e in events if e['direction_n'] == n)}" for n in range(5))

    inside = sum(1 for e in events if e["in_model_window"])
    out.append(f"in model window: {inside}, outside: {len(events) - inside}")
    if parse_model_cutoff(config.filings_model_cutoff) is None:
        out.append("(FILINGS_MODEL_CUTOFF is not set: every event counts as in-window)")
    return "\n".join(out)


__all__ = [
    "BackfillSummary",
    "backfill_counts",
    "history_lines",
    "in_window",
    "parse_window",
    "run_backfill",
    "write_history",
]
