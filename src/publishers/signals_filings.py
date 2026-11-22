"""Filings-lane artifact emitter: ``signals-filings.json`` for atrade (SIGNAL-SCHEMA §12).

One record per stored earnings 8-K that has extracted facts. EDGAR only for now, so every
record has ``origin = "edgar"``, an accession and ``wire_id = null``.

Rules this module protects:
  - ``knowable_at`` is the filing's EDGAR acceptance time, never fetch, run or emit time (§12.5).
  - No free text: only enums, numbers, booleans, timestamps, dates, tickers and IDs (§12.6).
  - The whole document is validated against the committed JSON schema before it is written;
    on failure nothing is written.
  - The file is written atomically: .tmp, fsync, os.replace (§12.2).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..config import Config
from ..db import Database
from ..timeutil import parse_timestamp, to_utc_z
from .signals import TickerMap

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.1.0"
FILENAME = "signals-filings.json"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "docs" / "schemas" / "signals-filings.schema.json"
)
COMPONENT_KEYS = ("eps_yoy", "rev_yoy", "rev_accel", "guidance_change")


def parse_model_cutoff(raw: str | None) -> datetime | None:
    """Parse FILINGS_MODEL_CUTOFF (ISO date or datetime) to an aware UTC datetime.

    A bare date means 00:00 UTC that day. Returns None when unset or unparseable.
    """
    value = (raw or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
    except ValueError:
        pass
    parsed = parse_timestamp(value)
    if parsed is None:
        logger.warning("FILINGS_MODEL_CUTOFF %r is not an ISO date; treating it as unset", value)
    return parsed


def _components(row: Any) -> dict[str, float | int | None]:
    """The four stored components; NULL columns stay null. guidance_change is an int."""
    out: dict[str, float | int | None] = {}
    for key in COMPONENT_KEYS:
        value = row[key]
        out[key] = (
            None if value is None else (int(value) if key == "guidance_change" else float(value))
        )
    return out


def build_events(
    db: Database, ticker_map: TickerMap, config: Config, now: datetime
) -> list[dict[str, Any]]:
    """Event records for every stored 2.02 filing with a facts row inside the emit window."""
    cutoff = parse_model_cutoff(config.filings_model_cutoff)
    if cutoff is None:
        logger.warning(
            "FILINGS_MODEL_CUTOFF is not set; every filings record is emitted with "
            "in_model_window=true (the model may have seen the outcome)"
        )
    window_start = now - timedelta(days=config.filings_emit_days)

    rows = db.conn.execute(
        """SELECT f.accession, f.cik, f.ticker, f.items, f.accepted_at, f.fetched_at,
                  ff.direction, ff.direction_n,
                  ff.eps_yoy, ff.rev_yoy, ff.rev_accel, ff.guidance_change
           FROM filings f JOIN filing_facts ff ON ff.accession = f.accession
           ORDER BY f.accepted_at, f.ticker, f.accession"""
    ).fetchall()

    events: list[dict[str, Any]] = []
    for row in rows:
        if "2.02" not in str(row["items"]).split(","):
            continue
        entry = ticker_map.tickers.get(row["ticker"])
        if entry is None or entry.asset_class != "stock":
            logger.warning("Skipping %s: ticker is not a stock in the universe", row["accession"])
            continue
        knowable = parse_timestamp(row["accepted_at"])
        if knowable is None:
            logger.error(
                "Skipping %s: unparseable accepted_at %r", row["accession"], row["accepted_at"]
            )
            continue
        fetched = parse_timestamp(row["fetched_at"])
        if fetched is not None and knowable > fetched:
            logger.error("Skipping %s: accepted_at is later than fetch time", row["accession"])
            continue
        if knowable < window_start or knowable > now:
            continue

        direction_n = int(row["direction_n"] or 0)
        direction = row["direction"]
        events.append(
            {
                "ticker": row["ticker"],
                "cik": row["cik"],
                "event": "earnings",
                "origin": "edgar",
                "accession": row["accession"],
                "wire_id": None,
                "knowable_at": to_utc_z(knowable),
                # Not extracted yet; the schema allows null for period_end.
                "period_end": None,
                "direction": None if direction_n == 0 or direction is None else float(direction),
                "direction_n": direction_n,
                "components": _components(row),
                "in_model_window": True if cutoff is None else knowable < cutoff,
            }
        )
    events.sort(key=lambda e: (e["knowable_at"], e["ticker"]))
    return events


def emit_filings_signals(
    db: Database,
    signals_dir: Path,
    ticker_map: TickerMap,
    config: Config,
    now: datetime | None = None,
) -> Path | None:
    """Write ``signals_dir/signals-filings.json``; returns its path, or None if not written.

    Validation or I/O failures are logged at ERROR and write nothing (never raises for them).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    document = {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": to_utc_z(now),
        "universe_ref": ticker_map.universe_ref,
        "lane": "filings",
        "events": build_events(db, ticker_map, config, now),
    }

    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document), key=lambda e: list(e.path)
        )
    except (OSError, ValueError) as exc:
        logger.error("Cannot load filings schema %s: %s; nothing written", SCHEMA_PATH, exc)
        return None
    if errors:
        for err in errors[:5]:
            logger.error(
                "signals-filings.json failed schema validation at %s: %s",
                "/".join(str(p) for p in err.path) or "<root>",
                err.message,
            )
        logger.error("Filings signals not written (%d validation error(s))", len(errors))
        return None

    target = signals_dir / FILENAME
    tmp = signals_dir / f"{FILENAME}.tmp"
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    try:
        signals_dir.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        logger.error("Failed to write %s: %s", target, exc)
        return None

    logger.info("Emitted %d filings event(s) to %s", len(document["events"]), target)
    return target
