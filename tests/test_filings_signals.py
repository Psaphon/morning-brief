"""filings-signals: the signals-filings.json writer (SIGNAL-SCHEMA §12).

Seeds a temp DB with synthetic filings and facts (made-up CIKs and accessions).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from src.cli import cli
from src.config import Config
from src.db import Database
from src.publishers.signals import load_ticker_map
from src.publishers.signals_filings import emit_filings_signals

ROOT = Path(__file__).parent.parent
SCHEMA_PATH = ROOT / "docs" / "schemas" / "signals-filings.schema.json"
TICKER_MAP_PATH = ROOT / "config" / "ticker_map.toml"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "brief_fixture" / "signals-filings.golden.json"
SIGNALS_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "brief_fixture" / "signals.golden.json"

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
CUTOFF = "2026-11-01"

TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
CIK_RE = re.compile(r"^[0-9]{10}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ENUMS = {"earnings", "guidance", "acquisition", "agreement", "exec_change", "edgar", "filings"}

EVENT_KEYS = {
    "ticker",
    "cik",
    "event",
    "origin",
    "accession",
    "wire_id",
    "knowable_at",
    "period_end",
    "direction",
    "direction_n",
    "components",
    "in_model_window",
}
COMPONENT_KEYS = {"eps_yoy", "rev_yoy", "rev_accel", "guidance_change"}


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


@pytest.fixture(scope="module")
def ticker_map():
    return load_ticker_map(TICKER_MAP_PATH)


def make_config(cutoff: str = CUTOFF, emit_days: int = 30) -> Config:
    return Config(filings_model_cutoff=cutoff, filings_emit_days=emit_days)


def add(db, n, ticker, accepted, *, fetched=None, status="ok", items=("2.02",), facts=True, **kw):
    """Insert one filing (accession 0000000001-26-00000N) and, unless facts=False, its facts."""
    accession = f"0000000001-26-{n:06d}"
    db.insert_filing(
        accession=accession,
        cik="0000000001",
        ticker=ticker,
        form="8-K",
        items=list(items),
        accepted_at=accepted,
        fetched_at=fetched or accepted,
        primary_doc_url="http://x.test/p.htm",
        primary_html=b"<p>x</p>",
        exhibits=[],
        primary_text="x",
        exhibit_text="x",
    )
    if facts:
        row = {
            "accession": accession,
            "status": status,
            "unverified_count": 0,
            "direction_n": 0,
            "extracted_at": "2026-11-22T11:00:00Z",
        }
        row.update(kw)
        db.save_filing_facts(row)
    return accession


def seed_fixed(db) -> None:
    """The rows behind the golden file."""
    add(  # fetched two days after acceptance
        db,
        1,
        "AAPL",
        "2026-11-05T20:31:07Z",
        fetched="2026-11-07T04:15:00Z",
        eps_yoy=12.5,
        rev_yoy=8.0,
        rev_accel=1.5,
        guidance_change=1,
        direction=0.62,
        direction_n=4,
    )
    add(
        db,
        2,
        "MSFT",
        "2026-10-29T20:15:00Z",
        status="partial",
        eps_yoy=20.0,
        direction=0.46,
        direction_n=1,
    )
    add(db, 3, "NVDA", "2026-11-12T21:02:33Z", status="failed")
    add(
        db,
        4,
        "NVDA",
        "2026-10-30T21:00:00Z",
        eps_yoy=-4.0,
        rev_yoy=2.0,
        direction=-0.05,
        direction_n=2,
    )
    add(
        db, 5, "AAPL", "2026-10-22T12:00:00Z", eps_yoy=1.0, direction=0.04, direction_n=1
    )  # 31 days
    add(db, 6, "MSFT", "2026-11-10T20:00:00Z", facts=False)
    add(
        db,
        7,
        "MSFT",
        "2026-11-11T20:00:00Z",
        items=("7.01",),
        eps_yoy=1.0,
        direction=0.1,
        direction_n=1,
    )


def emit(db, ticker_map, tmp_path, config=None, now=NOW) -> Path | None:
    return emit_filings_signals(db, tmp_path / "signals", ticker_map, config or make_config(), now)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_strings(v)


def atrade_rules(doc: dict, universe: set[str]) -> None:
    """The field rules atrade's reader enforces (§12.3/§12.4); raises AssertionError."""
    assert set(doc) == {"schema_version", "emitted_at", "universe_ref", "lane", "events"}
    assert SEMVER_RE.match(doc["schema_version"]) and TIMESTAMP_RE.match(doc["emitted_at"])
    assert doc["lane"] == "filings" and isinstance(doc["events"], list)
    for ev in doc["events"]:
        assert set(ev) == EVENT_KEYS
        assert ev["ticker"] in universe and CIK_RE.match(ev["cik"])
        assert ev["event"] == "earnings" and ev["origin"] == "edgar"
        assert ACCESSION_RE.match(ev["accession"]) and ev["wire_id"] is None
        assert TIMESTAMP_RE.match(ev["knowable_at"])
        assert ev["period_end"] is None or DATE_RE.match(ev["period_end"])
        n = ev["direction_n"]
        assert type(n) is int and 0 <= n <= 4
        d = ev["direction"]
        if n == 0:
            assert d is None
        if d is not None:
            assert type(d) in (int, float) and -1 <= d <= 1
        assert type(ev["in_model_window"]) is bool
        comps = ev["components"]
        assert set(comps) == COMPONENT_KEYS
        for key in ("eps_yoy", "rev_yoy", "rev_accel"):
            assert comps[key] is None or type(comps[key]) in (int, float)
        gc = comps["guidance_change"]
        assert gc is None or (type(gc) is int and gc in (-1, 0, 1))


# --- acceptance ---------------------------------------------------------------------------


def test_output_validates_and_strings_are_restricted(db, ticker_map, tmp_path):
    seed_fixed(db)
    doc = load(emit(db, ticker_map, tmp_path))
    errors = list(Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(doc))
    assert errors == []

    universe = {s for s, e in ticker_map.tickers.items() if e.asset_class == "stock"}
    allowed_keys = EVENT_KEYS | COMPONENT_KEYS | set(doc)
    for s in walk_strings(doc):
        assert (
            s in ENUMS
            or s in universe
            or s in allowed_keys
            or s == ticker_map.universe_ref
            or s == doc["schema_version"]
            or TIMESTAMP_RE.match(s)
            or DATE_RE.match(s)
            or ACCESSION_RE.match(s)
            or CIK_RE.match(s)
        ), s


def test_golden_file(db, ticker_map, tmp_path):
    seed_fixed(db)
    path = emit(db, ticker_map, tmp_path)
    assert path.name == "signals-filings.json"
    assert path.read_text(encoding="utf-8") == GOLDEN_PATH.read_text(encoding="utf-8")
    assert load(path)["emitted_at"] == "2026-11-22T12:00:00Z"  # from the injected now


def test_knowable_at_is_acceptance_not_fetch_or_run_time(db, ticker_map, tmp_path):
    add(db, 1, "AAPL", "2026-11-05T20:31:07Z", fetched="2026-11-07T04:15:00Z", direction_n=0)
    doc = load(emit(db, ticker_map, tmp_path))
    assert [e["knowable_at"] for e in doc["events"]] == ["2026-11-05T20:31:07Z"]
    assert doc["emitted_at"] == "2026-11-22T12:00:00Z"


def test_emit_window_excludes_31_day_old_filing(db, ticker_map, tmp_path):
    add(db, 1, "AAPL", "2026-10-22T11:59:59Z")  # 31 days and 1 second old
    add(db, 2, "MSFT", "2026-10-23T12:00:00Z")  # 30 days old: kept
    doc = load(emit(db, ticker_map, tmp_path))
    assert [e["ticker"] for e in doc["events"]] == ["MSFT"]


def test_filing_without_facts_is_skipped(db, ticker_map, tmp_path):
    add(db, 1, "AAPL", "2026-11-05T20:00:00Z", facts=False)
    assert load(emit(db, ticker_map, tmp_path))["events"] == []


def test_failed_facts_emit_null_direction(db, ticker_map, tmp_path):
    add(db, 1, "AAPL", "2026-11-05T20:00:00Z", status="failed")
    (event,) = load(emit(db, ticker_map, tmp_path))["events"]
    assert event["direction"] is None and event["direction_n"] == 0
    assert event["components"] == dict.fromkeys(COMPONENT_KEYS)


def test_partial_facts_keep_verified_components(db, ticker_map, tmp_path):
    add(
        db,
        1,
        "AAPL",
        "2026-11-05T20:00:00Z",
        status="partial",
        eps_yoy=9.0,
        direction=0.3,
        direction_n=1,
    )
    (event,) = load(emit(db, ticker_map, tmp_path))["events"]
    assert event["components"]["eps_yoy"] == 9.0 and event["components"]["rev_yoy"] is None
    assert event["direction"] == 0.3 and event["direction_n"] == 1


def test_events_sorted_by_knowable_at_then_ticker(db, ticker_map, tmp_path):
    add(db, 1, "NVDA", "2026-11-05T20:00:00Z")
    add(db, 2, "MSFT", "2026-11-05T20:00:00Z")
    add(db, 3, "AAPL", "2026-11-06T20:00:00Z")
    add(db, 4, "AAPL", "2026-11-01T20:00:00Z")
    doc = load(emit(db, ticker_map, tmp_path))
    assert [(e["knowable_at"][:10], e["ticker"]) for e in doc["events"]] == [
        ("2026-11-01", "AAPL"),
        ("2026-11-05", "MSFT"),
        ("2026-11-05", "NVDA"),
        ("2026-11-06", "AAPL"),
    ]


def test_cutoff_unset_is_conservative_with_one_warning(db, ticker_map, tmp_path, caplog):
    seed_fixed(db)
    with caplog.at_level(logging.WARNING):
        doc = load(emit(db, ticker_map, tmp_path, make_config(cutoff="")))
    assert doc["events"] and all(e["in_model_window"] is True for e in doc["events"])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "FILINGS_MODEL_CUTOFF" in warnings[0].getMessage()


def test_cutoff_set_splits_either_side(db, ticker_map, tmp_path, caplog):
    add(db, 1, "AAPL", "2026-10-31T23:59:59Z")
    add(db, 2, "MSFT", "2026-11-01T00:00:00Z")  # exactly at the cutoff: not earlier
    add(db, 3, "NVDA", "2026-11-02T00:00:00Z")
    with caplog.at_level(logging.WARNING):
        doc = load(emit(db, ticker_map, tmp_path, make_config(cutoff="2026-11-01")))
    assert {e["ticker"]: e["in_model_window"] for e in doc["events"]} == {
        "AAPL": True,
        "MSFT": False,
        "NVDA": False,
    }
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_unknown_or_etf_ticker_is_skipped(db, ticker_map, tmp_path):
    etf = next(s for s, e in ticker_map.tickers.items() if e.asset_class != "stock")
    add(db, 1, "ZZZ", "2026-11-05T20:00:00Z")
    add(db, 2, etf, "2026-11-05T20:00:00Z")
    assert load(emit(db, ticker_map, tmp_path))["events"] == []


def test_validation_failure_writes_nothing(db, ticker_map, tmp_path, caplog):
    add(db, 1, "AAPL", "2026-11-05T20:00:00Z")
    db.conn.execute("UPDATE filings SET cik = 'not-a-cik'")
    db.conn.commit()
    with caplog.at_level(logging.ERROR):
        assert emit(db, ticker_map, tmp_path) is None
    assert not (tmp_path / "signals" / "signals-filings.json").exists()
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_write_is_atomic_and_leaves_no_tmp(db, ticker_map, tmp_path):
    seed_fixed(db)
    out = tmp_path / "signals"
    out.mkdir()
    (out / "signals-filings.json").write_text("old")
    emit(db, ticker_map, tmp_path)
    assert sorted(p.name for p in out.iterdir()) == ["signals-filings.json"]
    assert load(out / "signals-filings.json")["lane"] == "filings"


def test_atrade_field_rules(db, ticker_map, tmp_path):
    seed_fixed(db)
    doc = load(emit(db, ticker_map, tmp_path))
    universe = {s for s, e in ticker_map.tickers.items() if e.asset_class == "stock"}
    assert len(doc["events"]) == 4
    atrade_rules(doc, universe)


def test_atrade_rules_catch_violations(db, ticker_map, tmp_path):
    seed_fixed(db)
    doc = load(emit(db, ticker_map, tmp_path))
    universe = set(ticker_map.tickers)
    bad = json.loads(json.dumps(doc))
    bad["events"][0]["extra"] = 1
    with pytest.raises(AssertionError):
        atrade_rules(bad, universe)
    bad = json.loads(json.dumps(doc))
    bad["events"][0]["direction_n"] = 0
    bad["events"][0]["direction"] = 0.5
    with pytest.raises(AssertionError):
        atrade_rules(bad, universe)
    bad = json.loads(json.dumps(doc))
    bad["events"][0]["in_model_window"] = 1
    with pytest.raises(AssertionError):
        atrade_rules(bad, universe)


# --- CLI and pipeline wiring ----------------------------------------------------------------


def test_cli_filings_emit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    shutil.copy(TICKER_MAP_PATH, tmp_path / "config" / "ticker_map.toml")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("SIGNALS_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("FILINGS_MODEL_CUTOFF", CUTOFF)
    d = Database(tmp_path / "t.db")
    d.connect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    add(d, 1, "AAPL", now, direction_n=0)
    d.close()
    result = CliRunner().invoke(cli, ["filings", "emit"])
    assert result.exit_code == 0, result.output
    assert len(load(tmp_path / "out" / "signals-filings.json")["events"]) == 1


def _run_pipeline(monkeypatch, filings_enabled: bool) -> tuple[dict, Path]:
    from tests.test_signals_golden import load_fixture_articles, run_pipeline_on

    monkeypatch.setenv("FILINGS_ENABLED", "true" if filings_enabled else "")
    monkeypatch.setenv("CONTACT_EMAIL", "")  # the fetcher requests nothing without it
    with tempfile.TemporaryDirectory(prefix="brief-filings-") as tmp:
        workdir = Path(tmp) / "run"
        artifact, _ = run_pipeline_on(load_fixture_articles(), workdir)
        data = workdir / "data"
        files = sorted(p.name for p in data.iterdir() if p.name.startswith("signals"))
        return artifact, files


def test_lane_disabled_writes_no_file_and_signals_unchanged(monkeypatch):
    artifact, files = _run_pipeline(monkeypatch, filings_enabled=False)
    assert "signals-filings.json" not in files
    golden = json.loads(SIGNALS_GOLDEN_PATH.read_text(encoding="utf-8"))
    strip = lambda a: {k: v for k, v in a.items() if k != "emitted_at"}  # noqa: E731
    assert strip(artifact) == strip(golden)


def test_lane_enabled_pipeline_writes_filings_file(monkeypatch):
    artifact, files = _run_pipeline(monkeypatch, filings_enabled=True)
    assert "signals-filings.json" in files
    golden = json.loads(SIGNALS_GOLDEN_PATH.read_text(encoding="utf-8"))
    strip = lambda a: {k: v for k, v in a.items() if k != "emitted_at"}  # noqa: E731
    assert strip(artifact) == strip(golden)
