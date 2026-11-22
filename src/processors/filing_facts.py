"""Earnings-release fact extraction for the filings lane.

The local model only *copies* figures as written in the release. Code then:

1. verifies every figure against the full source text (unverified -> null, counted),
2. computes ``eps_yoy``, ``rev_yoy``, ``rev_accel``, ``guidance_change`` and ``direction``
   from the verified values (see ``filing_direction``).

Nothing the model returns can set a component, ``direction`` or the status: keys outside
``FILING_FACTS_SCHEMA`` are ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Config
from ..db import Database
from ..summarizers.local import MAX_RETRIES, REQUEST_TIMEOUT, RETRY_DELAY
from ..timeutil import UTC_Z_FORMAT
from . import filing_direction as fd

logger = logging.getLogger(__name__)

# --- Schema -------------------------------------------------------------------------

SCALES = ("units", "thousands", "millions", "billions")
GUIDANCE_METRICS = ("revenue", "eps")
VALUE_FIELDS = ("eps_diluted_current", "eps_diluted_prior", "revenue_current", "revenue_prior")

_VALUE = {
    "type": "object",
    "properties": {
        "as_written": {"type": ["string", "null"]},
        "scale": {"type": ["string", "null"], "enum": [*SCALES, None]},
    },
    "required": ["as_written", "scale"],
    "additionalProperties": False,
}

FILING_FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{name: _VALUE for name in VALUE_FIELDS},
        "period_label": {"type": ["string", "null"]},
        "guidance": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": list(GUIDANCE_METRICS)},
                        "period_label": {"type": "string"},
                        "low": _VALUE,
                        "high": _VALUE,
                    },
                    "required": ["metric", "period_label", "low", "high"],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": [*VALUE_FIELDS, "period_label", "guidance"],
    "additionalProperties": False,
}

# Marker used by tests/stub_ollama.py to recognise this call.
PROMPT_MARKER = "extract the reported figures from the following earnings release"

# Wording is deliberately neutral: the model copies figures, it never judges them.
FILING_FACTS_PROMPT = """You {marker}. Copy values exactly as written. Do not calculate, \
convert, round or infer anything. Use null for anything that is not stated.

Return JSON with these fields:
- eps_diluted_current: diluted earnings per share (also called "per diluted share" or \
"earnings per share assuming dilution") for the current period.
- eps_diluted_prior: the same figure for the same period one year earlier.
- revenue_current: total revenue (also "net revenue", "net sales", "total revenues") for the \
current period.
- revenue_prior: the same figure for the same period one year earlier.
- period_label: the current period as the release words it, for example "third quarter \
fiscal 2026".
- guidance: the company's own outlook for a future period, or null if the release gives none. \
Give metric ("revenue" or "eps"), period_label (the future period as written), and low and \
high (for a single figure, use it for both).

Each value is an object with as_written and scale.
- as_written: the number exactly as printed, including any parentheses or minus sign and \
any word such as "billion" that is attached to it. Example: "$2.10", "(0.45)", "12.1 billion".
- scale: "units", "thousands", "millions" or "billions", taken from the number's own wording \
or from a table heading such as "in millions". Use "units" for per-share amounts.

Rules:
- Use the GAAP figures only. Never use a figure labelled adjusted, non-GAAP, \
core or constant currency.
- Use the three-month period named in the release headline, not a year-to-date column.
- If a figure is not in the text, use null for both as_written and scale.

Release ({ticker}, filed {filed}):
{text}
"""

# --- Trimming -----------------------------------------------------------------------

CHARS_PER_TOKEN = 3
PROMPT_RESERVE_TOKENS = 1200  # prompt instructions + framing around the release text
HEAD_CHARS = 3000

_KEEP_RE = re.compile(
    r"per\s+share|diluted|assuming\s+dilution|revenue|net\s+sales|outlook|guidance|expects?"
    r"|\bin\s+(?:thousands|millions|billions)\b",
    re.IGNORECASE,
)


def release_budget_chars(num_ctx: int, num_predict: int) -> int:
    """Characters of release text that fit the context after output and prompt reserves."""
    return max(0, (num_ctx - num_predict - PROMPT_RESERVE_TOKENS) * CHARS_PER_TOKEN)


def trim_release(text: str, max_chars: int) -> str:
    """Opening highlights plus every line mentioning EPS, revenue or guidance, in order.

    Keeps the first ~3,000 characters, then (from the next line break) whole lines, which
    covers paragraphs and table rows, that match the financial terms, until the budget is
    used. A line that does not fit is skipped so shorter ones after it can still be kept.
    """
    if len(text) <= max_chars:
        return text
    head = text[: min(HEAD_CHARS, max_chars)]
    used = len(head)
    newline = text.find("\n", HEAD_CHARS)
    kept = [head]
    if newline != -1 and used < max_chars:
        for line in text[newline + 1 :].splitlines():
            stripped = line.strip()
            if not stripped or not _KEEP_RE.search(stripped):
                continue
            if used + 1 + len(stripped) > max_chars:
                continue
            kept.append(stripped)
            used += 1 + len(stripped)
    return "\n".join(kept)


# --- Verification -------------------------------------------------------------------

_WORDS = {
    "thousand": 1e3,
    "k": 1e3,
    "million": 1e6,
    "mm": 1e6,
    "m": 1e6,
    "billion": 1e9,
    "bn": 1e9,
    "b": 1e9,
}
_SCALE_MULT = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9, None: 1.0}
_SCALE_ROOT = {1e3: "thousand", 1e6: "million", 1e9: "billion"}
_WORD_ALT = "thousand|million|billion|mm|bn|k|m|b"
_AS_WRITTEN_RE = re.compile(
    rf"""^\s*(?P<open>[(\-]?)\s*\$?\s*(?P<open2>[(\-]?)\s*
        (?P<num>\d[\d,]*(?:\.\d+)?|\.\d+)\s*
        (?P<word>{_WORD_ALT})?s?\s*\)?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_TRAILING_WORD_RE = re.compile(r"\s*(thousand|million|billion)s?\b", re.IGNORECASE)


def _normalise_text(text: str) -> str:
    text = text.replace("−", "-").replace("–", "-").replace("\xa0", " ")
    text = text.replace("$", "")
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


def _scale_is_declared(source: str, root: str) -> bool:
    """True if the source states a table scale such as "in millions" or "$ millions"."""
    pattern = (
        rf"\bin\s+{root}s\b|\b{root}s\s+of\b|\b{root}s,?\s+except\b|\$\s*{root}s?\b"
        rf"|\(\s*{root}s\b"
    )
    return re.search(pattern, source, re.IGNORECASE) is not None


def has_scale_word(as_written: str) -> bool:
    m = _AS_WRITTEN_RE.match(_normalise_text(as_written))
    return bool(m and m.group("word"))


def verify_value(as_written: str | None, scale: str | None, source_text: str) -> float | None:
    """Return ``as_written`` in base units if it appears in ``source_text``, else None.

    The number must occur in the source as written (after dropping ``$``, thousands commas
    and spaces). Parentheses or a leading minus mean negative, and the source must show the
    same sign form. A word attached to the number ("12.1 billion") must also follow it in
    the source; otherwise ``scale`` is used, and a scale above "units" must be declared in
    the source (a table heading such as "in millions"). A figure the source
    prints with a different scale word than the one claimed is rejected.
    """
    if not as_written or scale not in _SCALE_MULT:
        return None
    written = _normalise_text(as_written)
    m = _AS_WRITTEN_RE.match(written)
    if not m:
        return None
    number = m.group("num")
    negative = bool(m.group("open") or m.group("open2"))
    word = (m.group("word") or "").lower()
    mult = _WORDS[word] if word else _SCALE_MULT[scale]
    digits = number.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None

    source = _normalise_text(source_text)
    declared = word or mult == 1.0 or _scale_is_declared(source, _SCALE_ROOT[mult])
    body = re.escape(digits)
    if digits.startswith((".", "0.")):
        body = r"0?" + re.escape(digits.lstrip("0"))
    for hit in re.finditer(rf"(?<![\d.]){body}(?!\.?\d)", source):
        before = source[: hit.start()].rstrip()
        after = source[hit.end() :]
        minus = before.endswith("-") and (len(before) == 1 or before[-2] in " \t\n($:=,;")
        paren = before.endswith("(") and after.lstrip().startswith(")")
        if (minus or paren) != negative:
            continue
        trailing = _TRAILING_WORD_RE.match(after)
        printed = _WORDS[trailing.group(1).lower()] if trailing else None
        if word:
            # "12.1 billion": the source must print the same scale word after the number.
            if printed != mult:
                continue
        elif printed is not None:
            if printed != mult:
                continue
        elif not declared:
            continue
        return -value * mult if negative else value * mult
    return None


# --- Model call ---------------------------------------------------------------------


class FactsError(ValueError):
    """The model's reply is not a usable extraction."""


def _check_value(raw: Any, name: str) -> dict[str, str | None]:
    if raw is None:
        return {"as_written": None, "scale": None}
    if not isinstance(raw, dict):
        raise FactsError(f"{name} is not an object")
    written, scale = raw.get("as_written"), raw.get("scale")
    if written is not None and not isinstance(written, str):
        raise FactsError(f"{name}.as_written is not a string")
    if scale is not None and scale not in SCALES:
        raise FactsError(f"{name}.scale is not one of {SCALES}")
    return {"as_written": written, "scale": scale}


def parse_reply(raw: str) -> dict[str, Any]:
    """Parse and shape-check the reply. Keys outside the schema are dropped here."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FactsError(f"invalid JSON: {e.msg}") from None
    if not isinstance(data, dict):
        raise FactsError("reply is not a JSON object")
    label = data.get("period_label")
    if label is not None and not isinstance(label, str):
        raise FactsError("period_label is not a string")
    parsed: dict[str, Any] = {name: _check_value(data.get(name), name) for name in VALUE_FIELDS}
    parsed["period_label"] = label
    guidance = data.get("guidance")
    if guidance is not None:
        if not isinstance(guidance, dict):
            raise FactsError("guidance is not an object")
        metric, glabel = guidance.get("metric"), guidance.get("period_label")
        if metric not in GUIDANCE_METRICS:
            raise FactsError("guidance.metric is not revenue or eps")
        if not isinstance(glabel, str) or not glabel.strip():
            raise FactsError("guidance.period_label is not a string")
        guidance = {
            "metric": metric,
            "period_label": glabel,
            "low": _check_value(guidance.get("low"), "guidance.low"),
            "high": _check_value(guidance.get("high"), "guidance.high"),
        }
    parsed["guidance"] = guidance
    return parsed


def build_prompt(
    row: sqlite3.Row | dict[str, Any], source_text: str, num_ctx: int, num_predict: int
) -> str:
    text = trim_release(source_text, release_budget_chars(num_ctx, num_predict))
    return FILING_FACTS_PROMPT.format(
        marker=PROMPT_MARKER,
        ticker=row["ticker"],
        filed=str(row["accepted_at"])[:10],
        text=text,
    )


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class OllamaUnavailable(RuntimeError):
    """The model server could not be reached after retries."""


async def call_model(
    client: httpx.AsyncClient, config: Config, prompt: str
) -> tuple[dict[str, Any] | None, str | None]:
    """One extraction call. Returns ``(parsed, None)`` or ``(None, reason)``.

    A reply that is not valid is retried once; a second invalid reply returns the reason.
    Transport errors are retried like ``summarize_single`` and then raise ``OllamaUnavailable``.
    """
    facts = config.filing_facts
    payload = {
        "model": config.ollama.model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": FILING_FACTS_SCHEMA,
        "options": {
            "num_ctx": facts.num_ctx,
            "num_predict": facts.num_predict,
            "temperature": 0,
            "seed": facts.seed,
        },
    }
    invalid = 0
    transport = 0
    while True:
        try:
            resp = await client.post(
                f"{config.ollama.host}/api/generate", json=payload, timeout=REQUEST_TIMEOUT
            )
            resp.raise_for_status()
            raw = str(resp.json().get("response", "")).strip()
            try:
                if not raw:
                    raise FactsError("empty response")
                return parse_reply(raw), None
            except FactsError as e:
                invalid += 1
                if invalid < 2:
                    logger.info("Invalid filing-facts reply (%s), retrying once", e)
                    continue
                return None, str(e)[:200]
        except httpx.HTTPError as e:
            if transport < MAX_RETRIES:
                transport += 1
                logger.warning("Ollama error on filing extraction: %s (retry %d)", e, transport)
                await _sleep(RETRY_DELAY)
                continue
            raise OllamaUnavailable(str(e)) from e


# --- Verify + compute + store -------------------------------------------------------


@dataclass
class FactsSummary:
    ok: int = 0
    partial: int = 0
    failed: int = 0
    unverified: int = 0

    def line(self) -> str:
        return (
            f"filings extract: {self.ok} extracted, {self.partial} partial, "
            f"{self.failed} failed, {self.unverified} unverified values"
        )


def _verify_field(
    value: dict[str, str | None], source: str, *, per_share: bool, counter: list[int]
) -> float | None:
    """Verified base-unit value, or None. Counts a value the model gave but code rejected."""
    written = value["as_written"]
    if written is None:
        return None
    if per_share and has_scale_word(written):
        result = None
    else:
        result = verify_value(written, "units" if per_share else value["scale"], source)
    if result is None:
        counter[0] += 1
        logger.info("Unverified value %r dropped", written)
    return result


def verify_facts(parsed: dict[str, Any], source: str) -> tuple[dict[str, Any], int]:
    """Verify every value in a parsed reply. Returns (verified fields, unverified count)."""
    counter = [0]
    out: dict[str, Any] = {"period_label": (parsed.get("period_label") or "").strip() or None}
    for name in VALUE_FIELDS:
        out[name] = _verify_field(
            parsed[name], source, per_share=name.startswith("eps"), counter=counter
        )
    out.update(
        guidance_metric=None, guidance_period_label=None, guidance_low=None, guidance_high=None
    )
    guidance = parsed.get("guidance")
    if guidance:
        per_share = guidance["metric"] == "eps"
        low = _verify_field(guidance["low"], source, per_share=per_share, counter=counter)
        high = _verify_field(guidance["high"], source, per_share=per_share, counter=counter)
        if low is not None and high is not None:
            out.update(
                guidance_metric=guidance["metric"],
                guidance_period_label=guidance["period_label"].strip(),
                guidance_low=min(low, high),
                guidance_high=max(low, high),
            )
    return out, counter[0]


def compute_components(
    db: Database, config: Config, row: sqlite3.Row, facts: dict[str, Any]
) -> dict[str, float | int | None]:
    """eps_yoy, rev_yoy, rev_accel, guidance_change for one filing from verified values."""
    eps_yoy = fd.pct_change(facts["eps_diluted_current"], facts["eps_diluted_prior"])
    rev_yoy = fd.pct_change(facts["revenue_current"], facts["revenue_prior"])

    previous = db.previous_earnings_facts(row["ticker"], row["accepted_at"])
    accel = None
    if previous is not None:
        accel = fd.rev_accel(
            rev_yoy, previous["rev_yoy"], previous["accepted_at"], row["accepted_at"]
        )

    change = None
    if facts["guidance_metric"]:
        wanted = fd.normalise_period(facts["guidance_period_label"])
        current_mid = fd.midpoint(facts["guidance_low"], facts["guidance_high"])
        for earlier in db.earlier_guidance(
            row["ticker"], facts["guidance_metric"], row["accepted_at"]
        ):
            if fd.normalise_period(earlier["guidance_period_label"]) == wanted:
                previous_mid = fd.midpoint(earlier["guidance_low"], earlier["guidance_high"])
                change = fd.guidance_change(
                    current_mid, previous_mid, config.filing_facts.guidance_tolerance
                )
                break
    return {"eps_yoy": eps_yoy, "rev_yoy": rev_yoy, "rev_accel": accel, "guidance_change": change}


def release_text(row: sqlite3.Row) -> str:
    """The text the numbers must be found in: the exhibits, else the 8-K body."""
    return (row["exhibit_text"] or "").strip() or (row["primary_text"] or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_Z_FORMAT)


def _failed_row(row: sqlite3.Row, config: Config, reason: str) -> dict[str, Any]:
    return {
        "accession": row["accession"],
        "status": "failed",
        "unverified_count": 0,
        "direction_n": 0,
        "model": config.ollama.model,
        "extracted_at": _now(),
        "error": reason[:200],
    }


async def extract_filing(
    client: httpx.AsyncClient, config: Config, db: Database, row: sqlite3.Row
) -> dict[str, Any]:
    """Extract, verify and score one filing; returns the row to store (not yet saved)."""
    source = release_text(row)
    if not source:
        return _failed_row(row, config, "no release text stored")
    facts_cfg = config.filing_facts
    prompt = build_prompt(row, source, facts_cfg.num_ctx, facts_cfg.num_predict)
    parsed, reason = await call_model(client, config, prompt)
    if parsed is None:
        return _failed_row(row, config, reason or "invalid reply")

    facts, unverified = verify_facts(parsed, source)
    components = compute_components(db, config, row, facts)
    direction, direction_n = fd.compute_direction(
        components, facts_cfg.weights(), facts_cfg.scales()
    )
    complete = all(facts[name] is not None for name in VALUE_FIELDS)
    return {
        "accession": row["accession"],
        "status": "ok" if complete and unverified == 0 else "partial",
        **facts,
        "unverified_count": unverified,
        **components,
        "direction": direction,
        "direction_n": direction_n,
        "model": config.ollama.model,
        "extracted_at": _now(),
        "error": None,
    }


async def extract_filings(
    config: Config,
    db: Database,
    *,
    redo: bool = False,
    client: httpx.AsyncClient | None = None,
) -> FactsSummary:
    """Extract every stored 2.02 filing that has no facts yet (all of them with ``redo``).

    Oldest first, so ``rev_accel`` and guidance comparisons find the earlier rows. If the
    model server is unreachable the run stops without storing anything for the rest, so
    they are tried again next time.
    """
    summary = FactsSummary()
    rows = db.filings_for_extraction(redo=redo)
    own_client = client is None
    http = client or httpx.AsyncClient()
    try:
        for row in rows:
            start = time.monotonic()
            try:
                result = await extract_filing(http, config, db, row)
            except OllamaUnavailable as e:
                logger.warning("Model unreachable (%s); stopping filing extraction", e)
                summary.failed += len(rows) - (summary.ok + summary.partial + summary.failed)
                break
            db.save_filing_facts(result)
            summary.unverified += result["unverified_count"]
            if result["status"] == "ok":
                summary.ok += 1
            elif result["status"] == "partial":
                summary.partial += 1
            else:
                summary.failed += 1
            logger.info(
                "%s %s: %s (direction=%s, n=%s) in %.1fs",
                row["ticker"],
                row["accession"],
                result["status"],
                result.get("direction"),
                result["direction_n"],
                time.monotonic() - start,
            )
    finally:
        if own_client:
            await http.aclose()
    return summary
