"""edgar-fetcher: 8-K polling, storage, rate limiting, backoff and contact handling.

EDGAR is faked at the HTTP boundary by a local stub server serving the synthetic files in
tests/fixtures/edgar/; the real client, limiter and parser run throughout. Time is faked
through the injectable clock/sleep so no test waits on a real backoff.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import zlib
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

import src.fetchers.edgar as edgar
import src.http as shttp
from src.cli import cli
from src.config import Config, load_config
from src.db import Database
from src.fetchers.edgar import (
    InvalidAcceptanceTime,
    fetch_filings,
    parse_acceptance_time,
    parse_exhibit_links,
)

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
# Built at runtime so this file never contains a literal address.
ADDR = "-".join(["alias", "9c2d"]) + "@" + "example.test"
NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

CIK_A, CIK_B = "0000111111", "0000222222"
FOLDER_1 = "/Archives/edgar/data/111111/000000000126000001/"
FOLDER_7 = "/Archives/edgar/data/111111/000000000126000007/"
SUBMISSIONS_A = f"/submissions/CIK{CIK_A}.json"
SUBMISSIONS_B = f"/submissions/CIK{CIK_B}.json"

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
former_ciks = ["0000999999"]
aliases = ["Synthetic Co"]

[[tickers]]
symbol = "BBB"
asset_class = "stock"
cik = "{CIK_B}"
aliases = ["Quiet Co"]
"""


class FakeClock:
    """Monotonic clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class StubSec:
    """Serves fixture files; records (path, user-agent, fake-clock time) per request."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.requests: list[tuple[str, str | None, float]] = []
        self.files: dict[str, bytes] = {}
        self.script: dict[str, deque[int]] = {}  # path -> statuses to answer with first
        self.always: dict[str, int] = {}  # path -> status for every request
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub.requests.append((self.path, self.headers.get("User-Agent"), stub.clock()))
                status = stub.always.get(self.path)
                if status is None and stub.script.get(self.path):
                    status = stub.script[self.path].popleft()
                body = b""
                if status is None or status == 200:
                    status = 200 if self.path in stub.files else 404
                    body = stub.files.get(self.path, b"")
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def paths(self) -> list[str]:
        return [p for p, _, _ in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _load(stub: StubSec) -> None:
    for name, path in (
        ("submissions.json", SUBMISSIONS_A),
        ("submissions_quiet.json", SUBMISSIONS_B),
    ):
        stub.files[path] = (FIXTURES / name).read_bytes()
    for sub, folder in (("filing_1", FOLDER_1), ("filing_7", FOLDER_7)):
        for f in (FIXTURES / sub).iterdir():
            stub.files[folder + f.name] = f.read_bytes()


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
def stub(clock):
    s = StubSec(clock)
    _load(s)
    yield s
    s.close()


@pytest.fixture
def ticker_map(tmp_path):
    p = tmp_path / "ticker_map.toml"
    p.write_text(TICKER_MAP)
    return p


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


def _config(**overrides) -> Config:
    return Config(contact_email=ADDR, **overrides)


async def _run(stub, db, ticker_map, clock, config=None, **kwargs):
    return await fetch_filings(
        config or _config(),
        db,
        ticker_map_path=ticker_map,
        now=lambda: NOW,
        data_base=stub.url,
        www_base=stub.url,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0.5,
        **kwargs,
    )


def _row(db, accession):
    return db.conn.execute("SELECT * FROM filings WHERE accession = ?", (accession,)).fetchone()


# --- time ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_value", "eastern_index_page"),
    [
        # EDT (UTC-4): XOM earnings 8-K, API 10:32:50Z == index page 06:32:50 Eastern.
        ("2026-10-30T10:32:50.000Z", "2026-10-30 06:32:50"),
        # EST (UTC-5): XOM earnings 8-K, API 11:31:24Z == index page 06:31:24 Eastern.
        ("2026-01-30T11:31:24.000Z", "2026-01-30 06:31:24"),
    ],
)
def test_acceptance_time_is_utc_on_both_sides_of_dst(api_value, eastern_index_page):
    accepted = parse_acceptance_time(api_value, NOW)
    assert accepted.utcoffset().total_seconds() == 0
    assert accepted.strftime("%Y-%m-%dT%H:%M:%SZ") == api_value.replace(".000", "")
    local = accepted.astimezone(ZoneInfo("America/New_York"))
    assert local.strftime("%Y-%m-%d %H:%M:%S") == eastern_index_page


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("2026-11-21T10:00:00", "no zone"),
        ("2026-11-21", "no zone"),
        ("2026-11-23T10:00:00.000Z", "in the future"),
        ("not a time", "unparseable"),
        ("", "missing"),
        (None, "missing"),
    ],
)
def test_unusable_acceptance_times_are_rejected(value, reason):
    with pytest.raises(InvalidAcceptanceTime, match=reason):
        parse_acceptance_time(value, NOW)


def test_offset_times_are_converted_to_utc():
    got = parse_acceptance_time("2026-10-30T06:32:50-04:00", NOW)
    assert got == datetime(2026, 10, 30, 10, 32, 50, tzinfo=timezone.utc)


# --- storage and filtering ----------------------------------------------------------


async def test_stores_new_8ks_with_filtering(stub, db, ticker_map, clock):
    summary = await _run(stub, db, ticker_map, clock)

    # acc 1 (2.02) and 7 (7.01, 8.01); 8-K/A, item 9.01 only, old, Form 4 are not candidates.
    assert (summary.ciks_polled, summary.new, summary.skipped, summary.rejected) == (2, 2, 0, 2)
    stored = [r["accession"] for r in db.conn.execute("SELECT accession FROM filings ORDER BY id")]
    assert stored == ["0000000001-26-000001", "0000000001-26-000007"]

    row = _row(db, "0000000001-26-000001")
    assert row["ticker"] == "AAA"
    assert row["cik"] == CIK_A
    assert row["form"] == "8-K"
    assert row["items"] == "2.02"  # 9.01 is not in FILINGS_ITEMS
    assert row["origin"] == "edgar"
    assert row["accepted_at"] == "2026-11-20T21:05:10Z"
    assert row["fetched_at"] == "2026-11-22T12:00:00Z"
    assert row["primary_doc_url"] == f"{stub.url}{FOLDER_1}primary1.htm"
    assert "Item 2.02 Results of Operations." in row["primary_text"]
    assert zlib.decompress(row["primary_html"]) == (FIXTURES / "filing_1/primary1.htm").read_bytes()
    assert _row(db, "0000000001-26-000007")["items"] == "7.01,8.01"


async def test_exhibits_are_found_by_type_not_file_name(stub, db, ticker_map, clock):
    await _run(stub, db, ticker_map, clock)

    paths = stub.paths()
    assert FOLDER_1 + "qtrrelease8k.htm" in paths  # EX-99.1, arbitrary name
    assert FOLDER_1 + "slidedeck.htm" in paths  # EX-99.2
    assert FOLDER_1 + "synth-20261120.xsd" not in paths  # EX-101.SCH
    assert FOLDER_1 + "synth-20261120.xml" not in paths  # EX-101.INS
    assert FOLDER_1 + "cover.htm" not in paths  # EX-104

    row = _row(db, "0000000001-26-000001")
    assert row["exhibit_text"].startswith("[EX-99.1]\nSynthetic Co reports quarterly results.")
    assert "[EX-99.2]\nSynthetic Co investor slides." in row["exhibit_text"]
    import json

    blob = json.loads(zlib.decompress(row["exhibits"]))
    assert [(e["type"], e["document"]) for e in blob] == [
        ("EX-99.1", "qtrrelease8k.htm"),
        ("EX-99.2", "slidedeck.htm"),
    ]
    assert _row(db, "0000000001-26-000007")["exhibit_text"] == ""


def test_parse_exhibit_links_ignores_foreign_links_and_unwraps_viewer():
    page = f"""<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th>
    <th>Size</th></tr>
    <tr><td>1</td><td>x</td><td><a href="/ix?doc={FOLDER_1}a.htm">a.htm</a></td><td>EX-99.1</td>
    <td>1</td></tr>
    <tr><td>2</td><td>x</td><td><a href="/Archives/edgar/data/999/other/b.htm">b</a></td>
    <td>EX-99.2</td><td>1</td></tr>
    <tr><td>3</td><td>x</td><td><a href="{FOLDER_1}c.htm">c</a></td><td>EX-991</td><td>1</td></tr>
    </table>"""
    assert parse_exhibit_links(page, FOLDER_1) == [("EX-99.1", FOLDER_1 + "a.htm")]


async def test_rerun_is_idempotent_and_fetches_no_documents(stub, db, ticker_map, clock):
    await _run(stub, db, ticker_map, clock)
    first_count = len(stub.requests)

    summary = await _run(stub, db, ticker_map, clock)

    assert (summary.new, summary.skipped) == (0, 2)
    assert db.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 2
    second = stub.paths()[first_count:]
    assert second == [SUBMISSIONS_A, SUBMISSIONS_B]  # no document or index requests


async def test_items_and_lookback_follow_config(stub, db, ticker_map, clock):
    for folder, name in (("000000000126000003", "only901.htm"), ("000000000126000004", "old.htm")):
        stub.files[f"/Archives/edgar/data/111111/{folder}/{name}"] = b"<p>x</p>"
        stub.files[
            f"/Archives/edgar/data/111111/{folder}/0000000001-26-{folder[-6:]}-index.htm"
        ] = b"<table></table>"
    cfg = _config(filings_items=("9.01",), filings_lookback_days=30)
    summary = await _run(stub, db, ticker_map, clock, cfg)
    # Only 9.01 counts as a wanted item now, and the 30-day lookback reaches back further.
    stored = {r["accession"]: r["items"] for r in db.conn.execute("SELECT * FROM filings")}
    assert stored == {
        "0000000001-26-000001": "9.01",
        "0000000001-26-000003": "9.01",
        "0000000001-26-000007": "9.01",
    }
    assert summary.new == 3

    db2 = Database(db.db_path.parent / "other.db")
    db2.connect()
    try:
        cfg = _config(filings_items=("5.02",), filings_lookback_days=30)
        await _run(stub, db2, ticker_map, clock, cfg)
        assert [r["accession"] for r in db2.conn.execute("SELECT accession FROM filings")] == [
            "0000000001-26-000004"
        ]
    finally:
        db2.close()


async def test_unusable_acceptance_times_are_rejected_and_logged(
    stub, db, ticker_map, clock, caplog
):
    caplog.set_level(logging.INFO)
    summary = await _run(stub, db, ticker_map, clock)

    assert summary.rejected == 2
    assert _row(db, "0000000001-26-000006") is None  # no zone
    assert _row(db, "0000000001-26-000008") is None  # later than fetch time
    text = caplog.text
    assert "0000000001-26-000006: rejected acceptance time (no zone)" in text.replace(" AAA ", ": ")
    assert "in the future" in text
    # Rejected filings never cause a document request.
    assert not any("nozone.htm" in p or "future.htm" in p for p in stub.paths())


async def test_only_stock_ciks_are_polled_and_former_ciks_ignored(stub, db, ticker_map, clock):
    await _run(stub, db, ticker_map, clock)
    polled = [p for p in stub.paths() if p.startswith("/submissions/")]
    assert polled == [SUBMISSIONS_A, SUBMISSIONS_B]
    assert not any("0000999999" in p for p in stub.paths())


# --- requests: User-Agent, rate limit, retries ---------------------------------------


async def test_every_request_carries_the_user_agent(stub, db, ticker_map, clock):
    await _run(stub, db, ticker_map, clock)
    assert len(stub.requests) > 5
    for path, ua, _ in stub.requests:
        assert ua and ua.startswith("MorningBrief/"), path
        assert f"(contact: {ADDR})" in ua, path


@pytest.mark.parametrize("rps", [1, 3, 5])
async def test_no_one_second_window_exceeds_max_rps(stub, db, ticker_map, clock, rps):
    await _run(stub, db, ticker_map, clock, _config(sec_max_rps=rps))

    times = [t for _, _, t in stub.requests]
    assert len(times) > rps  # the limit actually had something to limit
    for start in times:
        in_window = [t for t in times if start <= t < start + 1.0]
        assert len(in_window) <= rps
    assert clock.t > 0  # the limiter did sleep


async def test_429_then_200_succeeds_after_backoff(stub, db, ticker_map, clock):
    stub.script[SUBMISSIONS_A] = deque([429])
    summary = await _run(stub, db, ticker_map, clock)

    assert summary.new == 2
    assert stub.paths().count(SUBMISSIONS_A) == 2
    assert clock.sleeps[0] == pytest.approx(1.0 * (1 + 0.5))  # base * 2**0 * (1 + jitter)


async def test_backoff_grows_exponentially(stub, db, ticker_map, clock):
    stub.script[SUBMISSIONS_A] = deque([503, 429, 500])
    await _run(stub, db, ticker_map, clock)
    assert clock.sleeps[:3] == pytest.approx([1.5, 3.0, 6.0])


async def test_persistent_5xx_skips_that_cik_and_continues(stub, db, ticker_map, clock, caplog):
    stub.always[SUBMISSIONS_A] = 503
    summary = await _run(stub, db, ticker_map, clock, _config(sec_max_retries=2))

    assert stub.paths().count(SUBMISSIONS_A) == 3
    assert SUBMISSIONS_B in stub.paths()
    assert (summary.failed, summary.blocked, summary.new) == (1, False, 0)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_persistent_403_stops_the_whole_run_with_one_error(
    stub, db, ticker_map, clock, caplog
):
    caplog.set_level(logging.INFO)
    stub.always[SUBMISSIONS_A] = 403
    summary = await _run(stub, db, ticker_map, clock, _config(sec_max_retries=2))

    assert summary.blocked
    assert stub.paths() == [SUBMISSIONS_A] * 3  # 1 try + 2 backoff retries, then stop
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0


async def test_403_on_a_document_stops_the_run(stub, db, ticker_map, clock, caplog):
    stub.always[FOLDER_1 + "primary1.htm"] = 403
    summary = await _run(stub, db, ticker_map, clock, _config(sec_max_retries=1))

    assert summary.blocked
    assert SUBMISSIONS_B not in stub.paths()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


async def test_failed_document_is_not_stored_and_retried_next_run(stub, db, ticker_map, clock):
    stub.always[FOLDER_1 + "qtrrelease8k.htm"] = 404
    summary = await _run(stub, db, ticker_map, clock)
    assert (summary.new, summary.failed) == (1, 1)
    assert _row(db, "0000000001-26-000001") is None

    del stub.always[FOLDER_1 + "qtrrelease8k.htm"]
    summary = await _run(stub, db, ticker_map, clock)
    assert (summary.new, summary.skipped) == (1, 1)
    assert _row(db, "0000000001-26-000001") is not None


# --- contact handling and logging -----------------------------------------------------


def _cli_env(monkeypatch, tmp_path, stub, ticker_map, **extra):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("SEC_MAX_RPS", "10")
    monkeypatch.setattr(edgar, "SEC_DATA_BASE", stub.url)
    monkeypatch.setattr(edgar, "SEC_WWW_BASE", stub.url)
    monkeypatch.setattr(edgar, "TICKER_MAP_PATH", ticker_map)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_without_contact_email_nothing_is_sent(monkeypatch, tmp_path, stub, ticker_map, caplog):
    _cli_env(monkeypatch, tmp_path, stub, ticker_map, CONTACT_URL="https://ops.example.test/")
    caplog.set_level(logging.DEBUG)

    result = CliRunner().invoke(cli, ["filings", "fetch"])

    assert result.exit_code == 0, result.output
    assert stub.requests == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "CONTACT_EMAIL" in warnings[0].getMessage()
    assert "0 CIKs polled" in result.output
    db = Database(tmp_path / "cli.db")
    db.connect()
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0
    finally:
        db.close()


def test_cli_fetch_prints_one_summary_line(monkeypatch, tmp_path, stub, ticker_map):
    _cli_env(monkeypatch, tmp_path, stub, ticker_map, CONTACT_EMAIL=ADDR)

    result = CliRunner().invoke(cli, ["filings", "fetch"])

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.startswith("Filings fetch:")]
    assert lines == ["Filings fetch: 2 CIKs polled, 2 new, 0 skipped, 2 rejected"]
    assert all(ua and ADDR in ua for _, ua, _ in stub.requests)


def test_contact_address_and_user_agent_never_logged(
    stub, db, ticker_map, clock, caplog, monkeypatch, tmp_path
):
    caplog.set_level(logging.DEBUG)
    stub.script[SUBMISSIONS_A] = deque([429])
    asyncio.run(_run(stub, db, ticker_map, clock))
    stub.always[SUBMISSIONS_B] = 403
    asyncio.run(_run(stub, db, ticker_map, clock, _config(sec_max_retries=1)))
    _cli_env(monkeypatch, tmp_path, stub, ticker_map, CONTACT_EMAIL=ADDR)
    result = CliRunner().invoke(cli, ["filings", "fetch"])

    assert result.exit_code == 0, result.output
    assert "Filings fetch:" in result.output  # the CLI leg really ran
    assert any("retry" in r.getMessage() for r in caplog.records)
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert ADDR not in caplog.text
    assert "MorningBrief/" not in caplog.text
    assert "contact:" not in caplog.text


# --- config ---------------------------------------------------------------------------


def test_config_defaults_and_ceiling(monkeypatch, caplog):
    for var in (
        "FILINGS_ENABLED",
        "SEC_MAX_RPS",
        "SEC_MAX_RETRIES",
        "FILINGS_ITEMS",
        "FILINGS_LOOKBACK_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.filings_enabled is False
    assert (cfg.sec_max_rps, cfg.sec_max_retries, cfg.filings_lookback_days) == (5, 3, 7)
    assert cfg.filings_items == ("2.02", "7.01", "8.01", "1.01", "2.01", "5.02")

    monkeypatch.setenv("SEC_MAX_RPS", "50")
    monkeypatch.setenv("FILINGS_ITEMS", "2.02, 5.02")
    monkeypatch.setenv("FILINGS_ENABLED", "true")
    with caplog.at_level(logging.WARNING):
        cfg = load_config()
    assert cfg.sec_max_rps == 10
    assert cfg.filings_items == ("2.02", "5.02")
    assert cfg.filings_enabled is True
    assert any("SEC_MAX_RPS" in r.getMessage() for r in caplog.records)


# --- database -------------------------------------------------------------------------


def test_accession_is_unique(db):
    kwargs = {
        "accession": "0000000001-26-000099",
        "cik": CIK_A,
        "ticker": "AAA",
        "form": "8-K",
        "items": ["2.02"],
        "accepted_at": "2026-11-20T21:05:10Z",
        "fetched_at": "2026-11-22T12:00:00Z",
        "primary_doc_url": "http://x.test/p.htm",
        "primary_html": b"<p>x</p>",
        "exhibits": [],
        "primary_text": "x",
        "exhibit_text": "",
    }
    assert db.insert_filing(**kwargs) is True
    assert db.insert_filing(**kwargs) is False
    assert db.has_filing(kwargs["accession"])


def test_article_retention_leaves_filings_alone(db):
    db.insert_filing(
        accession="0000000001-26-000098",
        cik=CIK_A,
        ticker="AAA",
        form="8-K",
        items=["2.02"],
        accepted_at="2025-01-02T14:00:00Z",
        fetched_at="2025-01-02T15:00:00Z",
        primary_doc_url="http://x.test/p.htm",
        primary_html=b"<p>x</p>",
        exhibits=[],
        primary_text="x",
        exhibit_text="",
    )
    db.insert_article(url="http://x.test/a", url_hash="h", title="t", source="s", category="c")
    db.conn.execute("UPDATE articles SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    db.conn.commit()

    assert db.cleanup_old_articles(max_age_days=2) == 1  # the prune does run
    assert db.conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1


# --- pipeline -------------------------------------------------------------------------


def test_fetch_failure_in_run_pipeline_is_logged_and_the_brief_completes(
    monkeypatch, tmp_path, caplog
):
    from tests.test_signals_golden import load_fixture_articles, run_pipeline_on

    async def boom(*args, **kwargs):
        raise RuntimeError("edgar exploded")

    monkeypatch.setenv("FILINGS_ENABLED", "true")
    monkeypatch.setattr(edgar, "fetch_filings", boom)
    caplog.set_level(logging.INFO)

    artifact, _ = run_pipeline_on(load_fixture_articles(), tmp_path / "work")

    assert artifact  # the brief (and its signals file) was still produced
    assert "Filings fetch failed; pipeline continues" in caplog.text
    assert "edgar exploded" in caplog.text
    import json

    status = json.loads((tmp_path / "work" / "data" / "last_run.json").read_text())
    assert status["status"] == "ok"
    assert "filings" in status["timing"]["stages"]  # counts toward RUN_DEADLINE_MINUTES
