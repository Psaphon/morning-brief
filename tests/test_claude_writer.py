"""claude-section-writer: cited section summaries from validated extractions.

The Claude API and Ollama are HTTP stubs on 127.0.0.1, so the real request, parsing,
validation, spend-guard and fallback code runs unmodified.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.config import Config, OllamaConfig, WriterConfig, load_config
from src.db import Database
from src.summarizers.claude_writer import (
    WriterRun,
    build_request,
    load_api_key,
    split_cited_chunks,
)
from src.summarizers.local import _briefing_articles, with_details
from src.summarizers.writer import write_sections
from src.validators.grounding import ValidatorRun, check_chunk
from tests.stub_claude import TOKENS_IN, TOKENS_OUT, Reply, StubClaude, good_chunks
from tests.stub_ollama import StubOllama
from tests.test_run_pipeline import _run as run_pipeline_script

FAKE_KEY = "sk-test-NOT-A-REAL-KEY-123"
PRICE_IN, PRICE_OUT = 3.0, 15.0  # fake test prices, USD per million tokens
CALL_COST = (TOKENS_IN * PRICE_IN + TOKENS_OUT * PRICE_OUT) / 1_000_000
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MONTH = datetime.now(timezone.utc).strftime("%Y-%m")

# id -> (section, title). Two articles per section; ids 1-2 Tech, 3-4 World, 5-6 Markets.
SECTIONS = {"Tech": (1, 2), "World": (3, 4), "Markets": (5, 6)}


def _full_text(n: int) -> str:
    return (
        f"FULLTEXT-MARKER-{n} " + "A long distinctive paragraph about nothing in particular. " * 40
    )


def _url(n: int) -> str:
    return f"https://news.example.test/story-{n}"


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db")
    database.connect()
    now = datetime.now(timezone.utc).isoformat()
    for section, ids in SECTIONS.items():
        for n in ids:
            database.conn.execute(
                "INSERT INTO articles (url, url_hash, title, source, category, fetched_at, "
                "full_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _url(n),
                    f"h{n}",
                    f"Widget report {n}",
                    f"Source {n}",
                    section,
                    now,
                    _full_text(n),
                ),
            )
    database.conn.commit()
    for n in range(1, 7):
        database.save_extraction(
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
    yield database
    database.close()


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "creds"
    directory.mkdir()
    (directory / "anthropic_api_key").write_text(f"  {FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    return directory


@pytest.fixture
def claude():
    with StubClaude() as stub:
        yield stub


@pytest.fixture
def ollama():
    with StubOllama() as stub:
        yield stub


def _config(claude: StubClaude, ollama: StubOllama | None, **writer) -> Config:
    kwargs = {
        "mode": "claude",
        "api_url": claude.url,
        "price_in_per_mtok": PRICE_IN,
        "price_out_per_mtok": PRICE_OUT,
        **writer,
    }
    host = ollama.url if ollama else "http://127.0.0.1:1"
    return Config(ollama=OllamaConfig(host=host), writer=WriterConfig(**kwargs))


async def _write(db, config: Config) -> tuple[WriterRun, dict[str, dict]]:
    run = WriterRun()
    await write_sections(db, config, ValidatorRun.from_config(config), run)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    segments = db.get_briefing(today)["segment_map"]
    return run, {s["topic"]: s for s in segments}


def _usage_rows(db) -> list[dict]:
    return [dict(r) for r in db.conn.execute("SELECT * FROM api_usage ORDER BY id")]


def _reset_briefing(db) -> None:
    db.conn.execute("DELETE FROM daily_briefings")
    db.conn.commit()


# --- valid output -----------------------------------------------------------------


async def test_valid_cited_output_is_saved_and_priced(db, credentials, claude, ollama):
    run, segments = await _write(db, _config(claude, ollama))

    assert sorted(segments) == ["Markets", "Tech", "World"]
    tech = segments["Tech"]
    assert tech["writer"] == "claude"
    assert "[[" not in tech["text"]
    assert tech["cited_text"].count("[[1]]") == 2 and tech["cited_text"].count("[[2]]") == 2
    assert tech["text"] == tech["cited_text"].replace(" [[1]]", "").replace(" [[2]]", "")
    assert tech["source_article_ids"] == [1, 2]

    assert len(claude.requests) == 3  # one call per section
    assert ollama.generate_calls == []  # nothing fell back
    request = claude.requests[0]
    assert request["headers"]["x-api-key"] == FAKE_KEY  # stripped of whitespace
    assert request["headers"]["anthropic-version"] == "2023-06-01"

    rows = _usage_rows(db)
    assert len(rows) == 3
    assert rows[0]["tokens_in"] == TOKENS_IN and rows[0]["tokens_out"] == TOKENS_OUT
    assert rows[0]["cost_usd"] == pytest.approx(CALL_COST)
    assert rows[0]["model"] == "claude-sonnet-5-5"
    assert db.month_to_date_cost() == pytest.approx(3 * CALL_COST)

    meta = run.metadata()
    assert meta["sections"] == 3 and meta["claude"] == 3 and meta["fallback"] == 0
    assert meta["tokens_in"] == 3 * TOKENS_IN and meta["tokens_out"] == 3 * TOKENS_OUT
    assert meta["cost_usd"] == pytest.approx(3 * CALL_COST)
    assert sum(meta["dropped"].values()) == 0


async def test_paragraph_breaks_survive(db, credentials, claude, ollama):
    para = f"{good_chunks(1)}\n\n{good_chunks(2)}"
    claude.reply("Tech", para)
    _, segments = await _write(db, _config(claude, ollama))
    assert "\n\n" in segments["Tech"]["text"]
    assert segments["Tech"]["cited_text"] == para


# --- chunk checks -----------------------------------------------------------------


async def test_bad_chunks_are_dropped_and_counted_by_reason(db, credentials, claude, ollama):
    text = " ".join(
        [
            good_chunks(1, 7),
            "Orion Corp reported 100 widgets shipped to Lisbon. [[99]]",  # unknown id
            "Orion Corp reported 777 widgets shipped to Lisbon. [[1]]",  # wrong number
            "Orion Corp did something nobody cited.",  # uncited (trailing, no marker)
        ]
    )
    claude.reply("Tech", text)  # 3 bad of 10 chunks: exactly the 0.3 limit, so it stays
    run, segments = await _write(db, _config(claude, ollama))

    assert segments["Tech"]["writer"] == "claude"
    assert run.dropped == {"uncited": 1, "unknown_id": 1, "number": 1, "name": 0, "banned": 0}
    assert "777" not in segments["Tech"]["text"] and "nobody" not in segments["Tech"]["text"]
    assert segments["Tech"]["text"].count("Lisbon") == 7


async def test_name_and_banned_chunks_are_dropped(db, credentials, claude, ollama, tmp_path):
    overlay = tmp_path / "banned.txt"
    overlay.write_text("game-changing\n", encoding="utf-8")
    config = _config(claude, ollama)
    validator = ValidatorRun.from_config(config)
    validator.phrases = [*validator.phrases, "game-changing"]
    text = " ".join(
        [
            good_chunks(1, 8),
            "Orion Corp reported 100 widgets shipped to Zanzibar. [[1]]",  # name
            "Orion Corp reported 100 widgets, a game-changing result. [[1]]",  # banned
        ]
    )
    claude.reply("Tech", text)
    run = WriterRun()
    await write_sections(db, config, validator, run)
    assert run.dropped["name"] == 1 and run.dropped["banned"] == 1


async def test_chunk_may_combine_two_cited_sources(db, credentials, claude, ollama):
    text = "Orion Corp reported 100 widgets while Delta Labs expects 21 more gadgets. [[1,2]]"
    claude.reply("Tech", "Orion Corp reported 200 widgets shipped to Lisbon. [[2]] " + text)
    run, segments = await _write(db, _config(claude, ollama))
    # 21 comes from article 2's detail, 100 from article 1's what_happened: union passes
    assert segments["Tech"]["writer"] == "claude"
    assert "[[1,2]]" in segments["Tech"]["cited_text"]
    assert sum(run.dropped.values()) == 0


# --- fallbacks --------------------------------------------------------------------


async def test_drop_share_above_limit_falls_back_that_section_only(db, credentials, claude, ollama):
    claude.reply("Tech", good_chunks(1, 3) + " Uncited one. Uncited two.")  # 1 of 4 uncited = .25
    claude.reply("World", good_chunks(3, 3) + " Orion Corp reported 777 widgets. [[3]]")  # .25
    claude.reply("Markets", good_chunks(5, 2) + " Orion Corp reported 777 widgets. [[5]]")  # .33
    run, segments = await _write(db, _config(claude, ollama))

    assert segments["Markets"]["writer"] == "local fallback"
    assert segments["Tech"]["writer"] == "claude"
    assert segments["World"]["writer"] == "claude"
    assert (run.claude, run.fallback) == (2, 1)
    assert len(ollama.generate_calls) == 1  # one local call, for Markets only
    assert "## Markets" in ollama.generate_calls[0]["body"]["prompt"]
    assert "## Tech" not in ollama.generate_calls[0]["body"]["prompt"]
    # the spend of the rejected reply is still recorded
    assert len(_usage_rows(db)) == 3


async def test_api_500_falls_back_per_section(db, credentials, claude, ollama, caplog):
    caplog.set_level(logging.INFO)
    claude.reply("World", Reply(status=500, error_body=f"upstream said {FAKE_KEY}"))
    run, segments = await _write(db, _config(claude, ollama))
    assert segments["World"]["writer"] == "local fallback"
    assert segments["Tech"]["writer"] == segments["Markets"]["writer"] == "claude"
    assert len(_usage_rows(db)) == 2  # no usage for the failed call
    assert "HTTP 500" in caplog.text
    assert FAKE_KEY not in caplog.text


async def test_timeout_falls_back_per_section(db, credentials, claude, ollama):
    claude.reply("Tech", Reply(good_chunks(1), delay=1.5))
    config = _config(claude, ollama, timeout_s=0.3)
    run, segments = await _write(db, config)
    assert segments["Tech"]["writer"] == "local fallback"
    assert segments["World"]["writer"] == "claude"


@pytest.mark.parametrize(
    "reply",
    [Reply(raw=b"not json"), Reply(raw=b'{"content": []}'), Reply(raw=b'{"usage": {}}')],
    ids=["bad-json", "empty-content", "no-text"],
)
async def test_bad_reply_falls_back(db, credentials, claude, ollama, reply):
    claude.reply("Tech", reply)
    run, segments = await _write(db, _config(claude, ollama))
    assert segments["Tech"]["writer"] == "local fallback"
    assert run.claude == 2


async def test_local_fallback_without_ollama_is_a_plain_cited_list(db, credentials, claude):
    claude.reply("Tech", Reply(status=500))
    _, segments = await _write(db, _config(claude, None))
    tech = segments["Tech"]
    assert tech["writer"] == "local fallback"
    assert tech["text"] == (
        "Orion Corp reported 100 widgets shipped to Lisbon.\n\n"
        "Orion Corp reported 200 widgets shipped to Lisbon."
    )
    assert "widgets shipped to Lisbon. [[1]]" in tech["cited_text"]
    assert tech["source_article_ids"] == [1, 2]


# --- spend guard ------------------------------------------------------------------


async def test_cap_switches_to_local_and_alerts_once(db, credentials, claude, ollama, caplog):
    caplog.set_level(logging.INFO)
    db.record_api_usage("claude-sonnet-5-5", 1, 1, 10.0)  # at the cap (default $10)
    config = _config(claude, ollama)

    run, segments = await _write(db, config)
    assert claude.requests == []
    assert {s["writer"] for s in segments.values()} == {"local fallback"}
    assert len(ollama.generate_calls) == 3
    assert run.alerts == [f"writer spend cap reached {MONTH}"]
    capped = [r for r in caplog.records if "spend cap" in r.getMessage()]
    assert len(capped) == 1 and capped[0].levelno == logging.WARNING

    _reset_briefing(db)  # same month, a second run: no second alert
    run2, _ = await _write(db, config)
    assert run2.alerts == []
    assert claude.requests == []


async def test_cap_reached_mid_run_leaves_remaining_sections_local(db, credentials, claude, ollama):
    db.record_api_usage("m", 1, 1, 10 - CALL_COST * 1.5)  # room for two calls... then over
    _, segments = await _write(db, _config(claude, ollama))
    writers = [seg["writer"] for seg in segments.values()]  # in the order they were written
    assert writers == ["claude", "claude", "local fallback"]
    assert len(claude.requests) == 2


def test_cap_alert_reaches_last_run_json(tmp_path):
    from src.main import _write_status

    _write_status(tmp_path, "ok", "m", alerts=[f"writer spend cap reached {MONTH}"])
    status = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))
    assert status["alerts"] == [f"writer spend cap reached {MONTH}"]


def test_month_to_date_is_utc_month_only(db):
    db.record_api_usage("m", 1, 1, 4.0, ts="2020-01-31T23:59:59+00:00")
    db.record_api_usage("m", 1, 1, 1.5, ts="2020-02-01T00:00:00+00:00")
    db.record_api_usage("m", 1, 1, 2.0, ts="2020-02-15T12:00:00+00:00")
    feb = datetime(2020, 2, 20, tzinfo=timezone.utc)
    assert db.month_to_date_cost(feb) == pytest.approx(3.5)
    assert db.month_to_date_cost(datetime(2020, 3, 1, tzinfo=timezone.utc)) == 0


def test_claim_marker_is_true_once(db):
    assert db.claim_marker("k") is True
    assert db.claim_marker("k") is False
    assert db.claim_marker("other") is True


# --- credential and prices --------------------------------------------------------


def _assert_local_whole_brief(db, claude, ollama):
    assert claude.requests == []
    assert any("daily intelligence briefing" in c["body"]["prompt"] for c in ollama.generate_calls)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert db.get_briefing(today)["segment_map"][0]["topic"] == "Lead"


@pytest.mark.parametrize("case", ["no-variable", "no-file", "empty-file", "unreadable"])
async def test_missing_credential_uses_local(
    db, claude, ollama, tmp_path, monkeypatch, caplog, case
):
    caplog.set_level(logging.INFO)
    directory = tmp_path / "creds"
    directory.mkdir()
    if case == "no-variable":
        monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    else:
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    if case == "empty-file":
        (directory / "anthropic_api_key").write_text(" \n", encoding="utf-8")
    if case == "unreadable":
        (directory / "anthropic_api_key").mkdir()  # reading a directory raises OSError
    await _write_raw(db, _config(claude, ollama))
    _assert_local_whole_brief(db, claude, ollama)
    lines = [r for r in caplog.records if "anthropic_api_key" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno >= logging.INFO
    assert lines[0].exc_info is None


async def _write_raw(db, config: Config) -> None:
    await write_sections(db, config, ValidatorRun.from_config(config), WriterRun())


@pytest.mark.parametrize("missing", ["in", "out", "both"])
async def test_missing_prices_refuse_to_run(db, credentials, claude, ollama, caplog, missing):
    caplog.set_level(logging.INFO)
    overrides = {}
    if missing in ("in", "both"):
        overrides["price_in_per_mtok"] = None
    if missing in ("out", "both"):
        overrides["price_out_per_mtok"] = None
    await _write_raw(db, _config(claude, ollama, **overrides))
    _assert_local_whole_brief(db, claude, ollama)
    warnings = [r for r in caplog.records if "WRITER_PRICE" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert FAKE_KEY not in caplog.text


def test_load_api_key_strips_whitespace_and_never_logs_it(credentials, caplog):
    caplog.set_level(logging.DEBUG)
    assert load_api_key() == FAKE_KEY
    assert FAKE_KEY not in caplog.text


async def test_key_never_appears_in_logs_on_any_failure(db, credentials, claude, ollama, caplog):
    caplog.set_level(logging.DEBUG)
    for section in SECTIONS:
        claude.reply(section, Reply(status=401, error_body=f"invalid x-api-key {FAKE_KEY}"))
    await _write_raw(db, _config(claude, ollama))
    assert FAKE_KEY not in caplog.text
    assert all(r.exc_info is None for r in caplog.records if r.name.endswith("writer"))


# --- what leaves the box ----------------------------------------------------------


async def test_requests_hold_extractions_only(db, credentials, claude, ollama):
    await _write(db, _config(claude, ollama))
    assert len(claude.requests) == 3
    for request in claude.requests:
        raw = json.dumps(request["body"])
        for n in range(1, 7):
            assert f"FULLTEXT-MARKER-{n}" not in raw
            assert "distinctive paragraph" not in raw
            assert _url(n) not in raw
        assert "http" not in raw and "example.test" not in raw
        assert set(request["body"]) == {"model", "max_tokens", "system", "messages"}
    contents = [r["body"]["messages"][0]["content"] for r in claude.requests]
    content = next(c for c in contents if c.startswith("Section: Tech"))
    assert "[1] Widget report 1" in content and "Source: Source 1" in content
    assert "What happened: Orion Corp reported 100 widgets shipped to Lisbon." in content
    assert "- Delta Labs expects 11 more gadgets." in content
    system = claude.requests[0]["body"]["system"]
    assert "[[id]]" in system and "250 to 450 words" in system


def test_build_request_uses_configured_words_and_model(db):
    articles = with_details(db, db.get_todays_articles())
    config = WriterConfig(min_words=100, max_words=200, model="m-x")
    body = build_request(config, "Tech", _briefing_articles(articles)[:1])
    assert body["model"] == "m-x" and "100 to 200 words" in body["system"]


# --- local mode is unchanged ------------------------------------------------------


async def test_local_mode_makes_no_api_request(db, credentials, claude, ollama):
    config = _config(claude, ollama, mode="local")
    await _write_raw(db, config)
    assert claude.requests == []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    segments = db.get_briefing(today)["segment_map"]
    assert [s["topic"] for s in segments] == ["Lead", "Ahead"]
    assert all("writer" not in s for s in segments)
    assert _usage_rows(db) == []


def test_defaults_and_overrides(monkeypatch, caplog):
    for name in (
        "SUMMARY_WRITER", "WRITER_MODEL", "WRITER_API_URL", "WRITER_MIN_WORDS",
        "WRITER_MAX_WORDS", "WRITER_MAX_DROP_SHARE", "WRITER_TIMEOUT_S",
        "WRITER_MONTHLY_CAP_USD", "WRITER_PRICE_IN_PER_MTOK", "WRITER_PRICE_OUT_PER_MTOK",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    writer = load_config().writer
    assert writer == WriterConfig()
    assert (writer.mode, writer.model) == ("local", "claude-sonnet-5-5")
    assert writer.api_url == "https://api.anthropic.com/v1/messages"
    assert (writer.min_words, writer.max_words, writer.max_drop_share) == (250, 450, 0.3)
    assert (writer.timeout_s, writer.monthly_cap_usd) == (120, 10)
    assert writer.price_in_per_mtok is None and writer.price_out_per_mtok is None

    monkeypatch.setenv("SUMMARY_WRITER", "Claude")
    monkeypatch.setenv("WRITER_PRICE_IN_PER_MTOK", "1.5")
    assert load_config().writer.mode == "claude"
    assert load_config().writer.price_in_per_mtok == 1.5

    monkeypatch.setenv("SUMMARY_WRITER", "gpt")
    with caplog.at_level(logging.WARNING):
        assert load_config().writer.mode == "local"
    assert "SUMMARY_WRITER" in caplog.text


def test_env_api_key_path_is_gone(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert not hasattr(load_config().api_keys, "anthropic")
    assert not (PROJECT_ROOT / "src" / "summarizers" / "cloud.py").exists()


# --- chunk splitting and the grounding helper -------------------------------------


def test_split_cited_chunks():
    text = "One fact. [[1]] Two facts, combined. [[2, 3]]\n\nNew paragraph. [[4]] Dangling text."
    assert split_cited_chunks(text) == [
        ("One fact.", [1]),
        ("Two facts, combined.", [2, 3]),
        ("\n\nNew paragraph.", [4]),
        ("Dangling text.", []),
    ]


def test_split_cited_chunks_edge_cases():
    assert split_cited_chunks("") == []
    assert split_cited_chunks("Just prose.") == [("Just prose.", [])]
    assert split_cited_chunks("A. [[1]] [[2]]") == [("A.", [1, 2])]
    assert split_cited_chunks("A. [[x]]") == [("A.", ["x"])]
    assert split_cited_chunks("A. [[]]") == [("A.", [])]


def test_check_chunk_uses_the_union_of_sources():
    sources = ["Orion Corp ships 100 widgets.", "Delta Labs expects 21 gadgets."]
    assert check_chunk("Orion Corp ships 100 widgets and Delta Labs 21.", sources) is None
    assert check_chunk("Orion Corp ships 101 widgets.", sources) == "number"
    assert check_chunk("Orion Corp ships 100 widgets in Zanzibar.", sources) == "name"
    assert check_chunk("It is a big deal.", sources, phrases=["big deal"]) == "banned"
    assert check_chunk("Fine. Orion Corp ships 5 widgets.", sources) == "number"  # any sentence


# --- container switch -------------------------------------------------------------


def test_run_script_adds_credentials_override_only_when_file_exists(tmp_path):
    creds = tmp_path / "creds"
    creds.mkdir()
    base = {"FAKE_SECURITY_OPTIONS": "[]"}

    plain = run_pipeline_script(tmp_path, **base).stdout
    assert "ARGS=compose up --build --abort-on-container-exit" in plain

    unset_file = run_pipeline_script(tmp_path, CREDENTIALS_DIRECTORY=str(creds), **base).stdout
    assert "docker-compose.credentials.yml" not in unset_file
    assert "ARGS=compose up --build" in unset_file

    (creds / "anthropic_api_key").write_text(FAKE_KEY, encoding="utf-8")
    out = run_pipeline_script(tmp_path, CREDENTIALS_DIRECTORY=str(creds), **base)
    assert (
        "ARGS=compose -f docker-compose.yml -f docker-compose.credentials.yml up --build "
        "--abort-on-container-exit --exit-code-from morning-brief"
    ) in out.stdout
    assert FAKE_KEY not in out.stdout + out.stderr


def test_credentials_override_mounts_read_only_and_sets_the_variable():
    text = (PROJECT_ROOT / "docker-compose.credentials.yml").read_text(encoding="utf-8")
    assert "${CREDENTIALS_DIRECTORY}:/run/credentials:ro" in text
    assert "CREDENTIALS_DIRECTORY=/run/credentials" in text
    assert "sk-" not in text  # no key material in the repo


# --- health-check alerts ----------------------------------------------------------


def _executable_script(path: Path, body: str) -> bool:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    probe = path.with_name(path.name + ".probe")  # test exec on a no-op copy, not the real one
    probe.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    probe.chmod(0o700)
    try:
        return subprocess.run([str(probe)], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _health(tmp_path: Path, status: dict | None, alert_bin: Path) -> subprocess.CompletedProcess:
    data = tmp_path / "data"
    (data / "output").mkdir(parents=True, exist_ok=True)
    (data / "output" / "dashboard.html").write_text("<html></html>", encoding="utf-8")
    if status is not None:
        (data / "last_run.json").write_text(json.dumps(status), encoding="utf-8")
    env = {**os.environ, "HOME": str(tmp_path), "HUB_ALERT": str(alert_bin)}
    return subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "health-check.sh"), str(data)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_health_check_sends_each_alert_once(tmp_path):
    alert = tmp_path / "hub-alert"
    sent = tmp_path / "sent.log"
    if not _executable_script(alert, f'echo "$1|$2|$3" >> "{sent}"\n'):
        pytest.skip("cannot execute scripts from tmp_path (noexec mount)")
    status = {
        "status": "ok",
        "timestamp": "2026-11-21T04:30:00+00:00",
        "alerts": [f"writer spend cap reached {MONTH}", "second alert"],
    }
    result = _health(tmp_path, status, alert)
    assert result.returncode == 0, result.stderr
    lines = sent.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert f"writer spend cap reached {MONTH}" in lines[0] and "second alert" in lines[1]

    assert _health(tmp_path, status, alert).returncode == 0  # same file again: nothing re-sent
    assert len(sent.read_text(encoding="utf-8").splitlines()) == 2


def test_health_check_skips_silently_without_alerts_or_publisher(tmp_path):
    alert = tmp_path / "hub-alert"
    sent = tmp_path / "sent.log"
    if not _executable_script(alert, f'echo "$1" >> "{sent}"\n'):
        pytest.skip("cannot execute scripts from tmp_path (noexec mount)")
    for status in ({"status": "ok"}, {"status": "ok", "alerts": []}, None):
        result = _health(tmp_path, status, alert)
        assert result.returncode == 0 and "alert" not in result.stderr.lower()
    assert not sent.exists()

    missing = tmp_path / "no-such-hub-alert"
    result = _health(tmp_path, {"status": "ok", "alerts": ["x"], "timestamp": "t"}, missing)
    assert result.returncode == 0 and result.stderr == ""
    not_exec = tmp_path / "plain"
    not_exec.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    not_exec.chmod(0o600)
    result = _health(tmp_path, {"status": "ok", "alerts": ["x"], "timestamp": "t"}, not_exec)
    assert result.returncode == 0 and result.stderr == ""


def test_health_check_existing_checks_unchanged(tmp_path):
    alert = tmp_path / "hub-alert"  # never created: publisher absent
    data = tmp_path / "data"
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / "health-check.sh"), str(data)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), "HUB_ALERT": str(alert)},
        check=False,
    )
    assert result.returncode == 1 and "dashboard not found" in result.stderr


# --- pipeline wiring --------------------------------------------------------------


@pytest.fixture
def _pipeline_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", "")
    for name in ("SUMMARY_WRITER", "CREDENTIALS_DIRECTORY", "VALIDATOR_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_default_pipeline_status_has_no_writer_keys(_pipeline_env, tmp_path, monkeypatch):
    from tests.test_reader_coverage import _run

    stub, _, status = _run(tmp_path, monkeypatch)
    assert "writer" not in status and "alerts" not in status
    assert any("daily intelligence briefing" in c["body"]["prompt"] for c in stub.generate_calls)


def test_pipeline_claude_without_credential_runs_local(
    _pipeline_env, tmp_path, monkeypatch, caplog
):
    from tests.test_reader_coverage import _run

    monkeypatch.setenv("SUMMARY_WRITER", "claude")
    monkeypatch.setenv("WRITER_PRICE_IN_PER_MTOK", "3")
    monkeypatch.setenv("WRITER_PRICE_OUT_PER_MTOK", "15")
    with caplog.at_level(logging.INFO):
        stub, _, status = _run(tmp_path, monkeypatch)
    assert status["status"] == "ok"
    assert status["writer"]["claude"] == 0 and "alerts" not in status
    assert "anthropic_api_key" in caplog.text
    assert any("daily intelligence briefing" in c["body"]["prompt"] for c in stub.generate_calls)


def test_compose_reads_optional_host_writer_env():
    """Writer settings (choice, cap, prices) come from a host-only file, never git."""
    import yaml

    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_file.read_text())
    env_files = compose["services"]["morning-brief"]["env_file"]
    entry = next(e for e in env_files if isinstance(e, dict) and "writer.env" in e["path"])
    assert entry["path"] == "${HOME}/.config/morning-brief/writer.env"
    assert entry["required"] is False
