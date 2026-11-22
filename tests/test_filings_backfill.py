"""filings-backfill: paging, predecessor CIKs, resumability, the time window, the history file.

EDGAR is faked by the stub server from tests/test_edgar_fetcher.py and the model by the HTTP-level
Ollama stub; the real client, limiter, extractor and emitter run throughout.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

import src.http as shttp
from src.cli import cli
from src.config import Config, FilingFactsConfig, OllamaConfig
from src.db import Database
from src.processors.filings_backfill import (
    backfill_counts,
    in_window,
    parse_window,
    run_backfill,
    write_history,
)
from src.publishers.signals import load_ticker_map
from src.publishers.signals_filings import SCHEMA_PATH, build_events
from tests.stub_ollama import StubOllama
from tests.test_edgar_fetcher import ADDR, FakeClock, StubSec
from tests.test_filing_facts import BASIC, basic_reply

CIK_A, CIK_FORMER, CIK_B = "0000111111", "0000999999", "0000222222"
PAGE_OLD = "CIK0000111111-submissions-001.json"  # filingTo before --since: never requested
PAGE_NEW = "CIK0000111111-submissions-002.json"  # top-level shape

TICKER_MAP = f"""
schema_version = "1.0.0"
universe_ref = "test"

[[tickers]]
symbol = "SPY"
asset_class = "etf"
aliases = ["S&P 500"]

[[tickers]]
symbol = "AAA"
asset_class = "stock"
cik = "{CIK_A}"
former_ciks = ["{CIK_FORMER}"]
aliases = ["Synthetic Co"]

[[tickers]]
symbol = "BBB"
asset_class = "stock"
cik = "{CIK_B}"
aliases = ["Quiet Co"]
"""

IN_WINDOW = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
PAST_WINDOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
SINCE = "2023-01-01"

# (accession, acceptance time, form, items) per file.
REC_1 = ("0000000001-26-000001", "2026-10-30T10:32:50.000Z", "8-K", "2.02,9.01")
REC_2 = ("0000000001-25-000010", "2025-07-30T20:05:00.000Z", "8-K", "2.02")
REC_SKIP = [
    ("0000000001-26-000002", "2026-09-01T12:00:00.000Z", "8-K", "5.02"),
    ("0000000001-26-000003", "2026-10-30T11:00:00.000Z", "8-K/A", "2.02"),
]
PAGE_1 = ("0000000001-24-000020", "2024-05-01T20:00:00.000Z", "8-K", "2.02")
PAGE_BEFORE_SINCE = ("0000000001-22-000030", "2022-02-01T21:00:00.000Z", "8-K", "2.02")
FORMER_1 = ("0000000002-23-000040", "2023-02-01T21:30:00.000Z", "8-K", "2.02")
STORED = [REC_1, REC_2, PAGE_1, FORMER_1]  # what a full run stores (oldest first: reversed)


def columns(filings: list[tuple[str, str, str, str]]) -> dict:
    return {
        "accessionNumber": [f[0] for f in filings],
        "acceptanceDateTime": [f[1] for f in filings],
        "form": [f[2] for f in filings],
        "items": [f[3] for f in filings],
        "primaryDocument": ["p.htm" for _ in filings],
    }


def folder(cik: str, accession: str) -> str:
    return f"/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"


def serve_filing(stub: StubSec, cik: str, accession: str) -> None:
    base = folder(cik, accession)
    stub.files[base + "p.htm"] = b"<p>See exhibit 99.1.</p>"
    stub.files[base + "ex.htm"] = b"<p>" + BASIC.replace("\n\n", "</p><p>").encode() + b"</p>"
    stub.files[base + f"{accession}-index.htm"] = (
        "<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>"
        f'<tr><td>1</td><td>8-K</td><td><a href="{base}p.htm">p.htm</a></td><td>8-K</td></tr>'
        f'<tr><td>2</td><td>PR</td><td><a href="{base}ex.htm">ex.htm</a></td><td>EX-99.1</td></tr>'
        "</table>"
    ).encode()


def build_stub(stub: StubSec) -> None:
    def put(path: str, payload: dict) -> None:
        stub.files[path] = json.dumps(payload).encode()

    put(
        f"/submissions/CIK{CIK_A}.json",
        {
            "filings": {
                "recent": columns([REC_1, *REC_SKIP, REC_2]),
                "files": [
                    {"name": PAGE_OLD, "filingFrom": "2005-10-04", "filingTo": "2020-01-01"},
                    {"name": PAGE_NEW, "filingFrom": "2021-01-01", "filingTo": "2024-06-30"},
                ],
            }
        },
    )
    put(f"/submissions/{PAGE_NEW}", columns([PAGE_1, PAGE_BEFORE_SINCE]))  # top level
    put(f"/submissions/CIK{CIK_FORMER}.json", {"filings": {"recent": columns([FORMER_1])}})
    put(f"/submissions/CIK{CIK_B}.json", {"filings": {"recent": columns([])}})
    for cik, filing in (
        (CIK_A, REC_1),
        (CIK_A, REC_2),
        (CIK_A, PAGE_1),
        (CIK_A, PAGE_BEFORE_SINCE),
        (CIK_FORMER, FORMER_1),
        (CIK_A, REC_SKIP[0]),
        (CIK_A, REC_SKIP[1]),
    ):
        serve_filing(stub, cik, filing[0])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    shttp._warned = False
    for var in ("CONTACT_EMAIL", "CONTACT_URL", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sec(clock):
    s = StubSec(clock)
    build_stub(s)
    yield s
    s.close()


@pytest.fixture
def ollama():
    with StubOllama() as s:
        s.set_filing_reply(basic_reply())
        yield s


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


@pytest.fixture
def ticker_map(tmp_path):
    p = tmp_path / "ticker_map.toml"
    p.write_text(TICKER_MAP)
    return p


@pytest.fixture
def history(tmp_path):
    return tmp_path / "out" / "filings-history.jsonl"


def make_config(ollama: StubOllama, history: Path, **overrides) -> Config:
    fields = {
        "contact_email": ADDR,
        "ollama": OllamaConfig(host=ollama.url, model="gemma4:e4b"),
        "filing_facts": FilingFactsConfig(),
        "filings_history_path": history,
        "filings_model_cutoff": "2025-02-01",
    }
    return Config(**{**fields, **overrides})


async def go(sec, db, ticker_map, clock, config, *, now=IN_WINDOW, **kwargs):
    kwargs.setdefault("since", SINCE)
    return await run_backfill(
        config,
        db,
        ticker_map_path=ticker_map,
        now=now if callable(now) else (lambda: now),
        data_base=sec.url,
        www_base=sec.url,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0.5,
        **kwargs,
    )


def stored(db) -> dict[str, tuple[str, str, str]]:
    rows = db.conn.execute("SELECT accession, cik, ticker, origin FROM filings").fetchall()
    return {r["accession"]: (r["cik"], r["ticker"], r["origin"]) for r in rows}


def document_requests(sec) -> list[str]:
    return [p for p in sec.paths() if p.startswith("/Archives/")]


def history_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- paging and predecessor CIKs ----------------------------------------------------


async def test_recent_and_top_level_paged_file_are_both_read(
    sec, ollama, db, ticker_map, history, clock
):
    summary = await go(sec, db, ticker_map, clock, make_config(ollama, history))

    assert set(stored(db)) == {f[0] for f in STORED}
    paths = sec.paths()
    assert f"/submissions/{PAGE_NEW}" in paths
    assert f"/submissions/{PAGE_OLD}" not in paths  # filingTo is before --since
    # Not 2.02, not an 8-K, or accepted before --since: never stored, never fetched.
    skipped = [REC_SKIP[0][0], REC_SKIP[1][0], PAGE_BEFORE_SINCE[0]]
    assert not any(a.replace("-", "") in p for a in skipped for p in document_requests(sec))
    assert (summary.stocks, summary.ciks, summary.earnings) == (2, 3, 4)
    assert (summary.new, summary.already, summary.stopped) == (4, 0, None)


async def test_since_filters_recent_rows_by_acceptance_time(
    sec, ollama, db, ticker_map, history, clock
):
    await go(sec, db, ticker_map, clock, make_config(ollama, history), since="2025-01-01")
    assert set(stored(db)) == {REC_1[0], REC_2[0]}
    assert f"/submissions/{PAGE_NEW}" not in sec.paths()  # filingTo 2024-06-30 < 2025-01-01


async def test_former_cik_filings_are_stored_with_their_own_filer_cik(
    sec, ollama, db, ticker_map, history, clock
):
    await go(sec, db, ticker_map, clock, make_config(ollama, history))
    rows = stored(db)
    assert rows[FORMER_1[0]] == (CIK_FORMER, "AAA", "edgar")
    assert rows[REC_1[0]] == (CIK_A, "AAA", "edgar")
    assert rows[PAGE_1[0]] == (CIK_A, "AAA", "edgar")
    assert f"/submissions/CIK{CIK_FORMER}.json" in sec.paths()
    # The history record carries the real filer CIK too.
    by_accession = {r["accession"]: r for r in history_records(history)}
    assert by_accession[FORMER_1[0]]["cik"] == CIK_FORMER
    assert by_accession[REC_1[0]]["cik"] == CIK_A


async def test_stored_exhibits_follow_the_daily_rules(sec, ollama, db, ticker_map, history, clock):
    await go(sec, db, ticker_map, clock, make_config(ollama, history))
    row = db.conn.execute(
        "SELECT items, accepted_at, exhibit_text FROM filings WHERE accession = ?", (REC_1[0],)
    ).fetchone()
    assert row["items"] == "2.02"  # 9.01 is not in FILINGS_ITEMS, as in the daily fetch
    assert row["accepted_at"] == "2026-10-30T10:32:50Z"
    assert row["exhibit_text"].startswith("[EX-99.1]")


# --- resumable ----------------------------------------------------------------------


async def test_second_run_requests_no_documents_and_creates_no_facts(
    sec, ollama, db, ticker_map, history, clock
):
    config = make_config(ollama, history)
    first = await go(sec, db, ticker_map, clock, config)
    docs_before, calls_before = len(document_requests(sec)), len(ollama.generate_calls)
    facts_before = db.conn.execute("SELECT id, extracted_at FROM filing_facts").fetchall()
    assert first.new == 4 and len(facts_before) == 4

    second = await go(sec, db, ticker_map, clock, config)

    assert len(document_requests(sec)) == docs_before
    assert len(ollama.generate_calls) == calls_before
    assert [tuple(r) for r in db.conn.execute("SELECT id, extracted_at FROM filing_facts")] == [
        tuple(r) for r in facts_before
    ]
    assert (second.new, second.already) == (0, 4)
    assert second.line().startswith("backfill: 2 stocks, 3 CIKs, 4 earnings 8-Ks (0 new, 4 already")
    assert len(history_records(history)) == 4


async def test_failed_document_is_retried_on_the_next_run(
    sec, ollama, db, ticker_map, history, clock
):
    config = make_config(ollama, history, sec_max_retries=0)
    broken = folder(CIK_A, PAGE_1[0]) + "p.htm"
    sec.always[broken] = 500
    first = await go(sec, db, ticker_map, clock, config)
    assert first.failed == 1 and PAGE_1[0] not in stored(db)
    del sec.always[broken]
    second = await go(sec, db, ticker_map, clock, config)
    assert second.new == 1 and PAGE_1[0] in stored(db)


# --- the window ---------------------------------------------------------------------


def test_parse_window_and_midnight_crossing():
    window = parse_window("05:00-07:45")
    assert in_window(window, datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc))
    assert in_window(window, datetime(2026, 1, 1, 7, 44, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 1, 1, 7, 45, tzinfo=timezone.utc))
    assert not in_window(window, datetime(2026, 1, 1, 4, 59, tzinfo=timezone.utc))
    night = parse_window("22:00-02:00")
    assert in_window(night, datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))
    assert in_window(night, datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc))
    assert not in_window(night, datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc))
    for bad in ("", "05:00", "5-7", "05:00-05:00", "aa:bb-cc:dd"):
        with pytest.raises(ValueError):
            parse_window(bad)


async def test_clock_past_window_end_stops_at_the_next_filing(
    sec, ollama, db, ticker_map, history, clock
):
    config = make_config(ollama, history)

    def now() -> datetime:  # the window ends once two filings' documents have been requested
        started = len([p for p in document_requests(sec) if p.endswith("p.htm")])
        return IN_WINDOW if started < 2 else PAST_WINDOW

    summary = await go(sec, db, ticker_map, clock, config, now=now)

    assert summary.stopped == "window end"
    assert len(stored(db)) == 2  # the item in flight finished, nothing after it started
    assert len([p for p in document_requests(sec) if p.endswith("p.htm")]) == 2
    assert ollama.generate_calls == []  # extraction does not start past the window
    assert "stopped: window end" in summary.line()

    resumed = await go(sec, db, ticker_map, clock, config)  # a later window finishes the job
    assert resumed.stopped is None and resumed.new == 2 and resumed.already == 2
    assert "stopped: done" in resumed.line()
    assert len(stored(db)) == 4


async def test_clock_past_window_end_stops_extraction_at_the_next_item(
    sec, ollama, db, ticker_map, history, clock
):
    config = make_config(ollama, history)

    def now() -> datetime:  # ends after the first extraction call
        return IN_WINDOW if len(ollama.generate_calls) < 1 else PAST_WINDOW

    summary = await go(sec, db, ticker_map, clock, config, now=now)

    assert summary.stopped == "window end"
    assert summary.new == 4  # fetching finished inside the window
    assert len(ollama.generate_calls) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM filing_facts").fetchone()[0] == 1
    assert summary.history_lines == 1  # what exists is still written
    assert len(history_records(history)) == 1
    # The oldest filing is the one extracted (oldest first).
    assert history_records(history)[0]["accession"] == FORMER_1[0]

    resumed = await go(sec, db, ticker_map, clock, config)
    assert resumed.stopped is None and len(ollama.generate_calls) == 4
    assert [r["knowable_at"] for r in history_records(history)] == sorted(
        r["knowable_at"] for r in history_records(history)
    )


async def test_outside_the_window_does_nothing_and_logs_one_info(
    sec, ollama, db, ticker_map, history, clock, caplog
):
    config = make_config(ollama, history)
    with caplog.at_level(logging.INFO, logger="src"):
        summary = await go(sec, db, ticker_map, clock, config, now=PAST_WINDOW)

    assert summary.outside_window
    assert sec.requests == [] and ollama.requests == []
    assert stored(db) == {} and not history.exists()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "BACKFILL_WINDOW_UTC" in infos[0].getMessage()


async def test_ignore_window_runs_outside_it(sec, ollama, db, ticker_map, history, clock):
    summary = await go(
        sec,
        db,
        ticker_map,
        clock,
        make_config(ollama, history),
        now=PAST_WINDOW,
        ignore_window=True,
    )
    assert summary.new == 4 and summary.stopped is None and history.exists()


async def test_shared_client_sends_user_agent_and_obeys_the_rate_limit(
    sec, ollama, db, ticker_map, history, clock
):
    await go(sec, db, ticker_map, clock, make_config(ollama, history, sec_max_rps=2))
    assert sec.requests
    assert all(ua and ADDR in ua for _, ua, _ in sec.requests)
    times = [t for _, _, t in sec.requests]
    assert len(times) > 2  # something to limit
    for t in times:
        assert len([u for u in times if t <= u < t + 1.0]) <= 2


async def test_without_contact_nothing_is_requested(sec, ollama, db, ticker_map, history, clock):
    config = make_config(ollama, history, contact_email="")
    summary = await go(sec, db, ticker_map, clock, config)
    assert summary.no_contact and sec.requests == []


# --- the history file ---------------------------------------------------------------


def schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator({"$ref": "#/$defs/history_line", "$defs": schema["$defs"]})


async def test_history_lines_validate_sort_and_match_build_events(
    sec, ollama, db, ticker_map, history, clock
):
    config = make_config(ollama, history)
    summary = await go(sec, db, ticker_map, clock, config)

    text = history.read_text(encoding="utf-8")
    assert text.endswith("\n") and "\r" not in text
    records = history_records(history)
    assert summary.history_lines == len(records) == 4
    validator = schema_validator()
    assert all(validator.is_valid(r) for r in records)
    assert [r["knowable_at"] for r in records] == sorted(r["knowable_at"] for r in records)

    expected = build_events(db, load_ticker_map(ticker_map), config, IN_WINDOW, windowed=False)
    assert records == [{"schema_version": "1.1.0", **e} for e in expected]
    assert not history.with_name(history.name + ".tmp").exists()


async def test_in_model_window_comes_from_the_cutoff_and_old_events_are_kept(
    sec, ollama, db, ticker_map, history, clock
):
    await go(
        sec, db, ticker_map, clock, make_config(ollama, history, filings_model_cutoff="2025-02-01")
    )
    flags = {r["accession"]: r["in_model_window"] for r in history_records(history)}
    # Events from 2023 and 2024 are far outside the daily emit window and still present.
    assert flags == {FORMER_1[0]: True, PAGE_1[0]: True, REC_2[0]: False, REC_1[0]: False}

    await go(sec, db, ticker_map, clock, make_config(ollama, history, filings_model_cutoff=""))
    assert all(r["in_model_window"] for r in history_records(history))


def good_line(**overrides) -> dict:
    line = {
        "schema_version": "1.1.0",
        "ticker": "AAA",
        "cik": CIK_A,
        "event": "earnings",
        "origin": "edgar",
        "accession": "0000000001-26-000001",
        "wire_id": None,
        "knowable_at": "2026-10-30T10:32:50Z",
        "period_end": None,
        "direction": 0.4,
        "direction_n": 2,
        "components": {"eps_yoy": 1.0, "rev_yoy": 2.0, "rev_accel": None, "guidance_change": None},
        "in_model_window": False,
    }
    return {**line, **overrides}


def test_one_invalid_line_writes_nothing_and_logs_error(tmp_path, caplog):
    path = tmp_path / "filings-history.jsonl"
    assert write_history(path, [good_line()]) is True
    before = path.read_text(encoding="utf-8")

    bad = good_line(accession="0000000001-26-000002", headline="free text is not allowed")
    with caplog.at_level(logging.ERROR):
        assert write_history(path, [good_line(), bad]) is False
    assert path.read_text(encoding="utf-8") == before  # the previous file is untouched
    assert not path.with_name(path.name + ".tmp").exists()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1

    fresh = tmp_path / "new" / "h.jsonl"
    assert write_history(fresh, [bad]) is False and not fresh.exists()


async def test_run_writes_nothing_when_a_built_line_is_invalid(
    sec, ollama, db, ticker_map, history, clock, monkeypatch
):
    import src.processors.filings_backfill as backfill

    real = backfill.history_lines

    def broken(*args, **kwargs):
        lines = real(*args, **kwargs)
        lines[0]["knowable_at"] = "2026-10-30"  # a date without a time breaks the contract
        return lines

    monkeypatch.setattr(backfill, "history_lines", broken)
    summary = await go(sec, db, ticker_map, clock, make_config(ollama, history))
    assert summary.history_lines is None and not history.exists()
    assert "history not written" in summary.line()


# --- --counts and the CLI -----------------------------------------------------------


def seed(db: Database, accession: str, ticker: str, accepted: str, values: int, n: int) -> None:
    db.insert_filing(
        accession=accession,
        cik=CIK_A,
        ticker=ticker,
        form="8-K",
        items=["2.02"],
        accepted_at=accepted,
        fetched_at="2026-11-22T12:00:00Z",
        primary_doc_url="http://x.test/p.htm",
        primary_html=b"<p>x</p>",
        exhibits=[],
        primary_text="x",
        exhibit_text="x",
    )
    fields = ("eps_diluted_current", "eps_diluted_prior", "revenue_current", "revenue_prior")
    row = {name: (1.0 if i < values else None) for i, name in enumerate(fields)}
    db.save_filing_facts(
        {
            "accession": accession,
            "status": "ok",
            **row,
            "unverified_count": 0,
            "direction": 0.5 if n else None,
            "direction_n": n,
            "model": "m",
            "extracted_at": "2026-11-22T12:00:00Z",
        }
    )


def test_counts_cli_on_a_seeded_db(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "ticker_map.toml").write_text(TICKER_MAP)
    db_path = tmp_path / "counts.db"
    d = Database(db_path)
    d.connect()
    seed(d, "0000000001-24-000001", "AAA", "2024-05-01T20:00:00Z", 4, 4)
    seed(d, "0000000001-24-000002", "AAA", "2024-08-01T20:00:00Z", 2, 2)
    seed(d, "0000000001-25-000003", "AAA", "2025-05-01T20:00:00Z", 0, 0)
    seed(d, "0000000001-25-000004", "BBB", "2025-06-01T20:00:00Z", 2, 2)
    d.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("FILINGS_MODEL_CUTOFF", "2025-02-01")
    monkeypatch.setenv("CONTACT_EMAIL", ADDR)

    result = CliRunner().invoke(cli, ["filings", "backfill", "--counts"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "events: 4" in out
    assert "  AAA: 3" in out and "  BBB: 1" in out
    assert "verification rate: 50.0% (8 of 16 values verified)" in out
    assert "  0: 1\n  1: 0\n  2: 2\n  3: 0\n  4: 1" in out
    assert "in model window: 2, outside: 2" in out
    assert "filings-history" not in out and ADDR not in out


def test_counts_function_flags_an_unset_cutoff(tmp_path, db, ticker_map):
    seed(db, "0000000001-24-000001", "AAA", "2024-05-01T20:00:00Z", 4, 4)
    text = backfill_counts(db, load_ticker_map(ticker_map), Config())
    assert "in model window: 1, outside: 0" in text and "FILINGS_MODEL_CUTOFF is not set" in text


def test_cli_rejects_a_bad_since_and_a_bad_window(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    assert CliRunner().invoke(cli, ["filings", "backfill", "--since", "yesterday"]).exit_code == 2
    monkeypatch.setenv("BACKFILL_WINDOW_UTC", "noon")
    result = CliRunner().invoke(cli, ["filings", "backfill"])
    assert result.exit_code != 0 and "BACKFILL_WINDOW_UTC" in result.output


def test_cli_without_contact_prints_the_summary_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("FILINGS_HISTORY_PATH", str(tmp_path / "h.jsonl"))
    result = CliRunner().invoke(cli, ["filings", "backfill", "--ignore-window"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("backfill: 0 stocks, 0 CIKs, 0 earnings 8-Ks")
    assert "CONTACT_EMAIL is not set" in result.output
    assert not (tmp_path / "h.jsonl").exists()


def test_config_defaults_and_environment(monkeypatch):
    from src.config import load_config

    monkeypatch.delenv("SIGNALS_DIR", raising=False)
    cfg = load_config()
    assert cfg.backfill_since == "2016-01-01" and cfg.backfill_window_utc == "05:00-07:45"
    assert cfg.filings_history_path == Path("data/filings-history.jsonl")
    monkeypatch.setenv("SIGNALS_DIR", "/srv/mb/signals")
    assert load_config().filings_history_path == Path("/srv/mb/filings-history.jsonl")
    monkeypatch.setenv("FILINGS_HISTORY_PATH", "/tmp/h.jsonl")
    monkeypatch.setenv("FILINGS_BACKFILL_SINCE", "2020-01-01")
    monkeypatch.setenv("BACKFILL_WINDOW_UTC", "01:00-02:00")
    cfg = load_config()
    assert (str(cfg.filings_history_path), cfg.backfill_since) == ("/tmp/h.jsonl", "2020-01-01")
    assert cfg.backfill_window_utc == "01:00-02:00"
