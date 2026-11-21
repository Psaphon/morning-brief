"""summary-bakeoff: score a local model's section writing against the stored Claude sections.

Ollama and the Claude API are HTTP stubs on 127.0.0.1; the Claude stub proves the
bakeoff never calls the API.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from src import cli as cli_mod
from src.config import Config, OllamaConfig, WriterConfig
from src.db import Database
from src.summarizers.bakeoff import (
    BakeoffError,
    format_table,
    load_day,
    report_path,
    run_bakeoff,
    write_report,
)
from src.summarizers.claude_writer import WriterRun, build_request
from src.summarizers.writer import write_sections
from src.validators.grounding import ValidatorRun
from tests.stub_claude import Reply, StubClaude
from tests.stub_ollama import StubOllama

DATE = "2026-03-10"
MODEL = "test-model:7b"
SECTIONS = {"Tech": (1, 2), "World": (3, 4)}


def _grounded(n: int) -> str:
    return f"Orion Corp reported {n}00 widgets shipped to Lisbon. [[{n}]]"


# Tech: two grounded, cited chunks (8 words each).
TECH_REPLY = f"{_grounded(1)} {_grounded(2)}"
# World: one good chunk (8 words), one wrong number (999 is in no article), one uncited tail.
WORLD_REPLY = (
    f"{_grounded(3)} Orion Corp reported 999 widgets shipped to Lisbon. [[4]] "
    "Lisbon saw more widgets arrive."
)
CLAUDE_TECH_TEXT = " ".join(["word"] * 20)
DROPPED = {"uncited": 1, "unknown_id": 0, "number": 0, "name": 0, "banned": 0}


def _segments(with_stats: bool = True) -> list[dict]:
    claude = {
        "topic": "Tech",
        "text": CLAUDE_TECH_TEXT,
        "cited_text": f"{CLAUDE_TECH_TEXT} [[1]]",
        "source_article_ids": [1],
        "writer": "claude",
    }
    if with_stats:
        claude["stats"] = {"chunks": 4, "dropped": DROPPED}
    fallback = {
        "topic": "World",
        "text": "Plain fallback.",
        "cited_text": "Plain fallback. [[3]]",
        "source_article_ids": [3],
        "writer": "local fallback",
    }
    return [claude, fallback]


def _seed(
    db: Database, *, with_stats: bool = True, briefing: bool = True, fetched: str | None = None
) -> None:
    fetched = fetched or f"{DATE}T05:00:00+00:00"
    for section, ids in SECTIONS.items():
        for n in ids:
            db.conn.execute(
                "INSERT INTO articles (url, url_hash, title, source, category, fetched_at, "
                "full_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"https://news.example.test/{n}",
                    f"h{n}",
                    f"Widget report {n}",
                    f"Source {n}",
                    section,
                    fetched,
                    "text",
                ),
            )
    db.conn.commit()
    for n in range(1, 5):
        db.save_extraction(
            n,
            {
                "what_happened": f"Orion Corp reported {n}00 widgets shipped to Lisbon.",
                "details": [f"Delta Labs expects {n}1 more gadgets."],
                "entities": [],
                "numbers": [],
                "event_date": None,
                "status": "happened",
                "is_roundup": False,
            },
            "m",
        )
    if briefing:
        segments = _segments(with_stats)
        db.save_briefing(DATE, "rendered", "claude-test", json.dumps(segments))


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db")
    database.connect()
    _seed(database)
    yield database
    database.close()


@pytest.fixture
def ollama():
    with StubOllama() as stub:
        stub.section_reply("Tech", TECH_REPLY)
        stub.section_reply("World", WORLD_REPLY)
        yield stub


def _config(ollama: StubOllama, **writer) -> Config:
    return Config(ollama=OllamaConfig(host=ollama.url), writer=WriterConfig(**writer))


async def test_scores_local_and_claude(db, ollama):
    report = await run_bakeoff(db, _config(ollama), MODEL, DATE)
    tech, world = report["sections"]["Tech"], report["sections"]["World"]

    assert tech["local"] == {"pass_rate": 1.0, "coverage": 1.0, "words": 16, "fallback": False}
    # 1 of 3 chunks kept: two-thirds dropped is over the 0.3 limit, so it would fall back.
    assert world["local"]["pass_rate"] == pytest.approx(1 / 3)
    assert world["local"]["coverage"] == 0.5
    assert world["local"]["words"] == 8
    assert world["local"]["fallback"] is True

    assert tech["claude"] == {"pass_rate": 0.75, "coverage": 0.5, "words": 20, "fallback": False}
    assert world["claude"] == {"pass_rate": None, "coverage": None, "words": None, "fallback": True}

    local_t, claude_t = report["totals"]["local"], report["totals"]["claude"]
    assert local_t["pass_rate"] == pytest.approx((1 + 1 / 3) / 2)
    assert (local_t["coverage"], local_t["words"], local_t["fallbacks"]) == (0.75, 12, 1)
    assert claude_t == {"pass_rate": 0.75, "coverage": 0.5, "words": 20, "fallbacks": 1}


async def test_local_request_matches_claude_request(db, ollama):
    config = _config(ollama)
    await run_bakeoff(db, config, MODEL, DATE)
    sections = load_day(db, DATE, config)

    calls = ollama.generate_calls
    assert len(calls) == 2
    for call in calls:
        body = call["body"]
        section = body["prompt"].splitlines()[0].removeprefix("Section: ")
        claude = build_request(config.writer, section, sections[section])
        assert body["model"] == MODEL
        assert body["system"] == claude["system"]
        assert body["prompt"] == claude["messages"][0]["content"]
        assert "format" not in body
        assert body["think"] is False


async def test_higher_drop_limit_clears_fallback(db, ollama):
    report = await run_bakeoff(db, _config(ollama, max_drop_share=0.7), MODEL, DATE)
    assert report["sections"]["World"]["local"]["fallback"] is False
    assert report["totals"]["local"]["fallbacks"] == 0


async def test_failed_call_is_a_fallback(db):
    config = Config(ollama=OllamaConfig(host="http://127.0.0.1:1"))
    report = await run_bakeoff(db, config, MODEL, DATE)
    assert report["sections"]["Tech"]["local"] == {
        "pass_rate": None,
        "coverage": 0.0,
        "words": 0,
        "fallback": True,
    }
    assert report["totals"]["local"]["fallbacks"] == 2


async def test_segment_without_stats_has_no_claude_pass_rate(tmp_path, ollama):
    database = Database(tmp_path / "old.db")
    database.connect()
    _seed(database, with_stats=False)
    try:
        report = await run_bakeoff(database, _config(ollama), MODEL, DATE)
    finally:
        database.close()
    tech = report["sections"]["Tech"]["claude"]
    assert tech["pass_rate"] is None
    assert (tech["coverage"], tech["words"]) == (0.5, 20)
    assert "n/a" in format_table(report)


async def test_no_claude_sections_still_scores_local(tmp_path, ollama):
    database = Database(tmp_path / "none.db")
    database.connect()
    _seed(database, briefing=False)
    try:
        report = await run_bakeoff(database, _config(ollama), MODEL, DATE)
    finally:
        database.close()
    assert report["sections"]["Tech"]["claude"] is None
    assert report["totals"]["claude"] == {
        "pass_rate": None,
        "coverage": None,
        "words": None,
        "fallbacks": None,
    }
    assert report["sections"]["Tech"]["local"]["pass_rate"] == 1.0
    assert "n/a" in format_table(report)


def test_load_day_applies_live_rules(db):
    db.conn.execute("UPDATE articles SET is_roundup = 1 WHERE id = 2")
    db.conn.execute("UPDATE articles SET extraction_failed = 1 WHERE id = 4")
    db.conn.execute("UPDATE articles SET fetched_at = '2026-03-09T23:59:59+00:00' WHERE id = 3")
    db.conn.commit()
    sections = load_day(db, DATE, Config())
    assert {k: [a["id"] for a in v] for k, v in sections.items()} == {"Tech": [1]}
    assert sections["Tech"][0]["details"] == ["Delta Labs expects 11 more gadgets."]


def test_load_day_without_extractions_raises(db):
    with pytest.raises(BakeoffError, match="No stored extractions for 2026-01-01"):
        load_day(db, "2026-01-01", Config())


def test_report_file_name_is_sanitized(tmp_path):
    assert report_path(tmp_path, DATE, "hf.co/org/model:q4") == (
        tmp_path / f"bakeoff-{DATE}-hf.co_org_model_q4.json"
    )


@pytest.fixture
def cli_env(tmp_path, monkeypatch, db, ollama):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("OLLAMA_HOST", ollama.url)
    return tmp_path


def test_cli_prints_table_and_writes_json(cli_env, ollama):
    result = CliRunner().invoke(cli_mod.cli, ["bakeoff", "--model", MODEL, "--date", DATE])
    assert result.exit_code == 0, result.output

    header = next(line for line in result.output.splitlines() if line.startswith("section"))
    assert header.split() == ["section"] + ["pass%", "cov%", "words", "fb"] * 2
    assert f"local: {MODEL}" in result.output and "claude" in result.output
    tech = next(line for line in result.output.splitlines() if line.startswith("Tech"))
    assert tech.split() == ["Tech", "100%", "100%", "16", "0", "75%", "50%", "20", "0"]
    world = next(line for line in result.output.splitlines() if line.startswith("World"))
    assert world.split() == ["World", "33%", "50%", "8", "1", "n/a", "n/a", "n/a", "1"]
    total = next(line for line in result.output.splitlines() if line.startswith("TOTAL"))
    assert total.split() == ["TOTAL", "67%", "75%", "12", "1", "75%", "50%", "20", "1"]

    path = cli_env / "data" / "bakeoff-2026-03-10-test-model_7b.json"
    report = json.loads(path.read_text())
    assert report["model"] == MODEL and report["date"] == DATE
    assert report["sections"]["Tech"]["local"]["words"] == 16
    assert report["sections"]["Tech"]["claude"]["pass_rate"] == 0.75
    assert report["totals"]["local"]["fallbacks"] == 1


def test_cli_writes_no_briefing(cli_env, db):
    before = db.conn.execute("SELECT date, content, segment_map FROM daily_briefings").fetchall()
    result = CliRunner().invoke(cli_mod.cli, ["bakeoff", "--model", MODEL, "--date", DATE])
    assert result.exit_code == 0, result.output
    fresh = Database(cli_env / "t.db")
    fresh.connect()
    after = fresh.conn.execute("SELECT date, content, segment_map FROM daily_briefings").fetchall()
    fresh.close()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_cli_date_without_extractions_exits_1(cli_env):
    result = CliRunner().invoke(cli_mod.cli, ["bakeoff", "--model", MODEL, "--date", "2026-01-01"])
    assert result.exit_code == 1
    assert "No stored extractions for 2026-01-01" in result.output
    assert not (cli_env / "data").exists()


def test_cli_rejects_a_bad_date(cli_env):
    result = CliRunner().invoke(cli_mod.cli, ["bakeoff", "--model", MODEL, "--date", "10/03/2026"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


def test_cli_makes_no_api_call(cli_env, monkeypatch):
    with StubClaude() as claude:
        monkeypatch.setenv("WRITER_API_URL", claude.url)
        monkeypatch.setenv("SUMMARY_WRITER", "claude")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cli_env / "creds"))
        result = CliRunner().invoke(cli_mod.cli, ["bakeoff", "--model", MODEL, "--date", DATE])
        assert result.exit_code == 0, result.output
        assert claude.requests == []


def test_write_report_creates_the_directory(tmp_path):
    report = {"date": DATE, "model": MODEL, "sections": {}, "totals": {}}
    path = write_report(report, tmp_path / "out")
    assert json.loads(path.read_text())["model"] == MODEL


async def test_writer_stores_chunk_stats(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "anthropic_api_key").write_text("sk-test-NOT-A-REAL-KEY", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    now = datetime.now(timezone.utc)
    database = Database(tmp_path / "w.db")
    database.connect()
    _seed(database, briefing=False, fetched=now.isoformat())
    try:
        with StubClaude() as claude, StubOllama() as ollama:
            # Tech: 4 chunks, one uncited tail (25% dropped, kept). World: all good.
            claude.reply("Tech", Reply(f"{TECH_REPLY} {_grounded(1)} Loose words."))
            config = Config(
                ollama=OllamaConfig(host=ollama.url),
                writer=WriterConfig(
                    mode="claude", api_url=claude.url, price_in_per_mtok=3, price_out_per_mtok=15
                ),
            )
            await write_sections(database, config, ValidatorRun.from_config(config), WriterRun())
        segments = database.get_briefing(now.strftime("%Y-%m-%d"))["segment_map"]
    finally:
        database.close()
    by_topic = {s["topic"]: s for s in segments}
    assert by_topic["Tech"]["writer"] == "claude"
    assert by_topic["Tech"]["stats"] == {"chunks": 4, "dropped": DROPPED}
    assert by_topic["World"]["stats"]["chunks"] == 4
    assert sum(by_topic["World"]["stats"]["dropped"].values()) == 0
