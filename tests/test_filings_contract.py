"""Contract tests for the filings lane (docs/SIGNAL-SCHEMA.md §12).

No producer exists yet. These tests pin the JSON schema, the synthetic fixtures, the
documented version and the CIK data in the real ``config/ticker_map.toml``.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
SCHEMA_PATH = ROOT / "docs" / "schemas" / "signals-filings.schema.json"
DOC_PATH = ROOT / "docs" / "SIGNAL-SCHEMA.md"
TICKER_MAP_PATH = ROOT / "config" / "ticker_map.toml"
FIXTURES = Path(__file__).parent / "fixtures" / "filings"
VALID_DAILY = FIXTURES / "signals-filings.valid.json"
VALID_HISTORY = FIXTURES / "filings-history.valid.jsonl"
INVALID_DIR = FIXTURES / "invalid"

CIK_RE = re.compile(r"^[0-9]{10}$")
ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
WIRE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
UNIVERSE_REF_RE = re.compile(r"^[A-Za-z0-9_./@-]+$")

EVENTS = {"earnings", "guidance", "acquisition", "agreement", "exec_change"}
ORIGINS = {"edgar", "wire", "both"}
ENUM_VALUES = EVENTS | ORIGINS | {"filings"}

# fixture file -> a word that must appear in the validation error for it
INVALID_REASONS = {
    "unknown-event": "event",
    "direction-out-of-range": "direction",
    "knowable-at-no-zone": "knowable_at",
    "free-text-title": "title",
    "bad-accession-pattern": "accession",
    "edgar-null-accession": "accession",
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _daily_validator() -> Draft202012Validator:
    return Draft202012Validator(_schema())


def _history_validator() -> Draft202012Validator:
    schema = _schema()
    wrapper = {
        "$schema": schema["$schema"],
        "$ref": "#/$defs/history_line",
        "$defs": schema["$defs"],
    }
    return Draft202012Validator(wrapper)


def _history_lines() -> list[dict]:
    text = VALID_HISTORY.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ticker_map() -> dict:
    return tomllib.loads(TICKER_MAP_PATH.read_text(encoding="utf-8"))


def _walk_strings(node: object):
    """Yield every string *value* (dict keys are structure, not content)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


# --- schema and fixtures -----------------------------------------------------------------


def test_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(_schema())


def test_valid_daily_fixture_validates():
    doc = json.loads(VALID_DAILY.read_text(encoding="utf-8"))
    errors = list(_daily_validator().iter_errors(doc))
    assert errors == []


def test_valid_history_fixture_validates():
    lines = _history_lines()
    assert len(lines) >= 3
    validator = _history_validator()
    for line in lines:
        assert list(validator.iter_errors(line)) == []


def test_daily_fixture_covers_every_event_origin_and_edge_case():
    events = json.loads(VALID_DAILY.read_text(encoding="utf-8"))["events"]
    assert {e["event"] for e in events} == EVENTS
    assert {e["origin"] for e in events} == ORIGINS
    assert any(e["direction"] is None for e in events)
    assert any(e["in_model_window"] is True for e in events)


@pytest.mark.parametrize("name", sorted(INVALID_REASONS))
def test_invalid_fixture_is_rejected(name: str):
    doc = json.loads((INVALID_DIR / f"{name}.json").read_text(encoding="utf-8"))
    errors = list(_daily_validator().iter_errors(doc))
    assert errors, f"{name} should fail validation"
    assert INVALID_REASONS[name] in " ".join(f"{list(e.path)} {e.message}" for e in errors)


def test_every_invalid_fixture_is_covered_by_a_case():
    on_disk = {p.stem for p in INVALID_DIR.glob("*.json")}
    assert on_disk == set(INVALID_REASONS)


def test_history_line_rejects_free_text_and_missing_version():
    validator = _history_validator()
    line = _history_lines()[0]
    assert list(validator.iter_errors({**line, "title": "x"}))
    without_version = {k: v for k, v in line.items() if k != "schema_version"}
    assert list(validator.iter_errors(without_version))


def test_directionless_events_must_carry_null_direction():
    events = json.loads(VALID_DAILY.read_text(encoding="utf-8"))["events"]
    acquisition = next(e for e in events if e["event"] == "acquisition")
    doc = json.loads(VALID_DAILY.read_text(encoding="utf-8"))
    doc["events"] = [{**acquisition, "direction": 0.5, "direction_n": 1}]
    assert list(_daily_validator().iter_errors(doc))


# --- no free text ------------------------------------------------------------------------


@pytest.mark.parametrize("path", [VALID_DAILY, VALID_HISTORY], ids=lambda p: p.name)
def test_every_string_in_valid_fixtures_is_allowed(path: Path):
    tickers = {t["symbol"] for t in _ticker_map()["tickers"]}
    if path == VALID_DAILY:
        docs = [json.loads(path.read_text(encoding="utf-8"))]
    else:
        docs = _history_lines()
    patterns = (
        CIK_RE,
        ACCESSION_RE,
        WIRE_ID_RE,
        TIMESTAMP_RE,
        DATE_RE,
        SEMVER_RE,
        UNIVERSE_REF_RE,
    )
    for doc in docs:
        for value in _walk_strings(doc):
            allowed = (
                value in ENUM_VALUES or value in tickers or any(p.match(value) for p in patterns)
            )
            assert allowed, f"free-text-looking string in fixture: {value!r}"


def test_fixture_events_use_a_cik_belonging_to_their_ticker():
    entries = {t["symbol"]: t for t in _ticker_map()["tickers"]}
    daily = json.loads(VALID_DAILY.read_text(encoding="utf-8"))["events"]
    for event in daily + _history_lines():
        entry = entries[event["ticker"]]
        assert entry["asset_class"] == "stock"
        assert event["cik"] in [entry["cik"], *entry.get("former_ciks", [])]


# --- real ticker_map.toml ----------------------------------------------------------------


def test_every_stock_has_a_ten_digit_cik_and_no_etf_does():
    for entry in _ticker_map()["tickers"]:
        if entry["asset_class"] == "stock":
            cik = entry.get("cik")
            assert isinstance(cik, str) and CIK_RE.match(cik), entry["symbol"]
        else:
            assert "cik" not in entry, entry["symbol"]
            assert "former_ciks" not in entry, entry["symbol"]


def test_former_ciks_are_lists_of_ten_digit_strings():
    for entry in _ticker_map()["tickers"]:
        if "former_ciks" not in entry:
            continue
        former = entry["former_ciks"]
        assert isinstance(former, list) and former, entry["symbol"]
        assert all(isinstance(c, str) and CIK_RE.match(c) for c in former), entry["symbol"]
        assert entry["cik"] not in former, entry["symbol"]


def test_stock_ciks_are_unique():
    entries = [t for t in _ticker_map()["tickers"] if t["asset_class"] == "stock"]
    all_ciks = [t["cik"] for t in entries] + [c for t in entries for c in t.get("former_ciks", [])]
    assert len(all_ciks) == len(set(all_ciks))


def test_ticker_map_version_and_universe_ref_are_unchanged():
    raw = _ticker_map()
    assert raw["schema_version"] == "1.0.0"
    assert raw["universe_ref"].endswith("@82c959e13d5d55f3b3871dbd496b39c023b57230")


# --- versions ----------------------------------------------------------------------------


def test_doc_header_and_schema_versions_agree():
    match = re.search(r"^\*\*Schema version:\*\* `([^`]+)`", DOC_PATH.read_text("utf-8"), re.M)
    assert match, "SIGNAL-SCHEMA.md has no Schema version header"
    assert match.group(1) == _schema()["x-schema-version"] == "1.1.0"
