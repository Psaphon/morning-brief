"""reader-coverage: configurable reader caps, validated `details`, briefing input, run timing."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.config import load_config
from src.main import StageTimer
from src.processors.interest import load_interests
from src.summarizers.local import select_balanced_articles
from src.summarizers.schema import DETAIL_MAX_WORDS, MAX_DETAILS, parse_extraction
from tests.stub_ollama import EXTRACTION_MARKER, UNGROUNDED_SENTENCE, StubOllama, canned_extraction
from tests.test_extraction import VALID, _briefing_prompts, _raw, _rows
from tests.test_signals_golden import load_fixture_articles, run_pipeline_on

STAGES = ["fetch", "process", "score", "signals", "summarize", "briefing", "publish"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", "")
    for name in (
        "READER_MAX_PER_CATEGORY",
        "READER_MAX_TOTAL",
        "RUN_DEADLINE_MINUTES",
        "VALIDATOR_MODE",
        "MORNING_BRIEF_BANNED_OVERLAY",
    ):
        monkeypatch.delenv(name, raising=False)


def _run(tmp_path: Path, monkeypatch, **stub_kwargs):
    """Run the pipeline on the fixture; return (stub, db path, last_run.json)."""
    monkeypatch.setattr("tests.test_signals_golden.StubOllama", lambda: StubOllama(**stub_kwargs))
    _, stub = run_pipeline_on(load_fixture_articles(), tmp_path / "run")
    data = tmp_path / "run" / "data"
    status = json.loads((data / "last_run.json").read_text(encoding="utf-8"))
    return stub, data / "morning_brief.db", status


def _victim() -> str:
    return load_fixture_articles()[0]["title"]


def _extraction_calls(stub: StubOllama) -> int:
    return sum(EXTRACTION_MARKER in c["body"]["prompt"] for c in stub.generate_calls)


def _stored(db: Path, title: str) -> dict | None:
    rows = _rows(
        db,
        "SELECT e.json FROM extractions e JOIN articles a ON a.id = e.article_id"
        f" WHERE a.title = '{title}'",
    )
    return json.loads(rows[0]["json"]) if rows else None


# --- config -----------------------------------------------------------------------


def test_config_defaults():
    config = load_config()
    assert config.reader_max_per_category == 15
    assert config.reader_max_total == 120
    assert config.run_deadline_minutes == 60
    assert config.ollama.extract_max_words == 120
    assert config.ollama.extract_num_predict == 900


def test_config_reads_environment(monkeypatch):
    monkeypatch.setenv("READER_MAX_PER_CATEGORY", "12")
    monkeypatch.setenv("READER_MAX_TOTAL", "30")
    monkeypatch.setenv("RUN_DEADLINE_MINUTES", "5")
    config = load_config()
    assert (config.reader_max_per_category, config.reader_max_total) == (12, 30)
    assert config.run_deadline_minutes == 5


# --- selection --------------------------------------------------------------------


def _article(i: int, category: str, title: str) -> dict:
    return {
        "id": i,
        "title": title,
        "category": category,
        "source": "Feed",
        "full_text": "",
        "score": 0.0,
        "url": f"https://example.com/{i}",
    }


def _interests(tmp_path: Path, count: int):
    keywords = "\n".join(f"topic{i} = {i + 1}" for i in range(count))
    path = tmp_path / "interests.toml"
    path.write_text(f"[keywords]\n{keywords}\n", encoding="utf-8")
    return load_interests(str(path))


def test_per_category_cap_from_config_picks_highest_interest(monkeypatch, tmp_path):
    monkeypatch.setenv("READER_MAX_PER_CATEGORY", "12")
    config = load_config()
    interests = _interests(tmp_path, 20)
    articles = [_article(i, "Tech", f"Story about topic{i}") for i in range(20)]

    chosen = select_balanced_articles(
        articles,
        per_category=config.reader_max_per_category,
        total=config.reader_max_total,
        interests=interests,
    )

    assert [a["id"] for a in chosen] == list(range(19, 7, -1))  # 12 articles, best first


def test_total_cap_from_config_applies_across_categories(monkeypatch, tmp_path):
    monkeypatch.setenv("READER_MAX_PER_CATEGORY", "10")
    monkeypatch.setenv("READER_MAX_TOTAL", "15")
    config = load_config()
    articles = [_article(c * 100 + i, f"Cat{c}", f"Story {i}") for c in range(3) for i in range(10)]

    chosen = select_balanced_articles(
        articles,
        per_category=config.reader_max_per_category,
        total=config.reader_max_total,
    )

    assert len(chosen) == 15


def test_pipeline_passes_reader_caps_to_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("READER_MAX_PER_CATEGORY", "1")
    stub, db, _ = _run(tmp_path, monkeypatch)
    categories = _rows(db, "SELECT DISTINCT category FROM articles WHERE duplicate_of IS NULL")
    assert _extraction_calls(stub) == len(categories) > 1

    monkeypatch.setenv("READER_MAX_TOTAL", "2")
    stub, _, _ = _run(tmp_path / "second", monkeypatch)
    assert _extraction_calls(stub) == 2


# --- extraction: details ----------------------------------------------------------


def test_five_details_keep_four():
    details = [f"Detail number {n} here." for n in range(5)]
    out = parse_extraction(_raw(details=details), 120)
    assert out["details"] == details[:MAX_DETAILS] and MAX_DETAILS == 4


def test_overlong_detail_is_trimmed_to_thirty_words():
    long_detail = " ".join(f"word{n}" for n in range(45)) + "."
    out = parse_extraction(_raw(details=[long_detail]), 120)
    assert len(out["details"][0].split()) == DETAIL_MAX_WORDS == 30


def test_detail_is_one_sentence_and_empty_details_are_dropped():
    out = parse_extraction(_raw(details=["First fact. Second fact.", "  ", "", "Third fact."]), 120)
    assert out["details"] == ["First fact.", "Third fact."]


@pytest.mark.parametrize("bad", ["a string", [1, 2], None])
def test_details_must_be_a_list_of_strings(bad):
    from src.summarizers.schema import ExtractionError

    with pytest.raises(ExtractionError, match="details"):
        parse_extraction(_raw(details=bad), 120)


def test_what_happened_may_now_run_to_120_words():
    sentence = " ".join(f"w{n}" for n in range(100)) + "."
    assert parse_extraction(_raw(what_happened=sentence), 120)["what_happened"] == sentence
    assert VALID["details"]  # the shared fixture exercises the new field


def test_details_are_stored_and_summary_stays_what_happened(tmp_path, monkeypatch):
    _, db, _ = _run(tmp_path, monkeypatch)
    stored = _stored(db, _victim())
    assert stored["details"] == [
        f"Canned detail about {_victim()}.",
        f"Canned follow-up about {_victim()}.",
    ]
    summary = _rows(db, f"SELECT summary FROM articles WHERE title = '{_victim()}'")[0][0]
    assert summary == stored["what_happened"] == f"Canned fact about {_victim()}."


def test_ungrounded_detail_is_dropped_and_counted(tmp_path, monkeypatch):
    victim = _victim()

    def fake(title: str = "", **kw):
        out = canned_extraction(title, **kw)
        if title == victim:
            out["details"] = [*out["details"], UNGROUNDED_SENTENCE]
        return out

    monkeypatch.setattr("tests.stub_ollama.canned_extraction", fake)
    _, db, status = _run(tmp_path, monkeypatch)

    stored = _stored(db, victim)
    assert UNGROUNDED_SENTENCE not in stored["details"] and len(stored["details"]) == 2
    assert stored["what_happened"] == f"Canned fact about {victim}."
    assert status["validator"]["dropped"] == {"number": 1, "name": 0, "banned": 0}


def test_warn_mode_keeps_ungrounded_detail(tmp_path, monkeypatch):
    victim = _victim()

    def fake(title: str = "", **kw):
        out = canned_extraction(title, **kw)
        if title == victim:
            out["details"] = [UNGROUNDED_SENTENCE]
        return out

    monkeypatch.setattr("tests.stub_ollama.canned_extraction", fake)
    monkeypatch.setenv("VALIDATOR_MODE", "warn")
    _, db, status = _run(tmp_path, monkeypatch)
    assert _stored(db, victim)["details"] == [UNGROUNDED_SENTENCE]
    assert status["validator"]["dropped"]["number"] == 1


def test_good_details_do_not_save_a_fully_dropped_what_happened(tmp_path, monkeypatch):
    victim = _victim()

    def fake(title: str = "", **kw):
        out = canned_extraction(title, **kw)
        if title == victim:
            out["what_happened"] = UNGROUNDED_SENTENCE
        return out

    monkeypatch.setattr("tests.stub_ollama.canned_extraction", fake)
    _, db, _ = _run(tmp_path, monkeypatch)

    assert _stored(db, victim) is None
    row = _rows(db, f"SELECT summary, extraction_failed FROM articles WHERE title = '{victim}'")[0]
    assert row["summary"] is None and row["extraction_failed"] == 1


# --- briefing input ---------------------------------------------------------------


def test_briefing_prompt_contains_surviving_details_only(tmp_path, monkeypatch):
    victim = _victim()

    def fake(title: str = "", **kw):
        out = canned_extraction(title, **kw)
        if title == victim:
            out["details"] = [*out["details"], UNGROUNDED_SENTENCE]
        return out

    monkeypatch.setattr("tests.stub_ollama.canned_extraction", fake)
    stub, db, _ = _run(tmp_path, monkeypatch)

    (prompt,) = _briefing_prompts(stub)
    article_id = _rows(db, f"SELECT id FROM articles WHERE title = '{victim}'")[0][0]
    line = next(ln for ln in prompt.splitlines() if ln.strip().startswith(f"[{article_id}] "))
    assert f"Canned fact about {victim}." in line
    assert f"Canned detail about {victim}." in line
    assert f"Canned follow-up about {victim}." in line
    assert "$987" not in prompt


def test_get_extraction_details_handles_missing_and_old_rows(tmp_path):
    from src.db import Database

    db = Database(tmp_path / "t.db")
    db.connect()
    try:
        for n in (1, 2, 3):
            db.conn.execute(
                "INSERT INTO articles (url, url_hash, title, source, category, fetched_at)"
                f" VALUES ('u{n}', 'h{n}', 't', 's', 'c', '2999-01-01')"
            )
        db.conn.commit()
        db.save_extraction(1, parse_extraction(_raw(details=["Kept fact."]), 120), "m")
        old = {k: v for k, v in VALID.items() if k != "details"}  # saved before details existed
        db.save_extraction(2, old, "m")
        assert db.get_extraction_details([1, 2, 3]) == {1: ["Kept fact."], 2: []}
        assert db.get_extraction_details([]) == {}
    finally:
        db.close()


# --- timing -----------------------------------------------------------------------


def test_last_run_records_total_and_every_stage(tmp_path, monkeypatch):
    _, _, status = _run(tmp_path, monkeypatch)
    timing = status["timing"]
    assert isinstance(timing["total_s"], float)
    assert list(timing["stages"]) == STAGES
    assert all(isinstance(v, float) and v >= 0 for v in timing["stages"].values())


def test_timing_line_is_logged_and_no_warning_within_deadline(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run(tmp_path, monkeypatch)
    line = next(r.getMessage() for r in caplog.records if "Run timing" in r.getMessage())
    assert "total" in line and all(stage in line for stage in STAGES)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "deadline" in r.msg]


def test_deadline_warning_names_deadline_and_slowest_stage(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RUN_DEADLINE_MINUTES", "60")
    # Clock reads: timer start, then one per stage; summarize takes 4000 s of the 4060 s.
    ticks = iter([0, 10, 20, 30, 40, 4040, 4050, 4060])
    monkeypatch.setattr("src.main._clock", lambda: next(ticks))

    with caplog.at_level(logging.INFO):
        _, _, status = _run(tmp_path, monkeypatch)

    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING and "deadline" in r.msg]
    message = warning.getMessage()
    assert "60-minute deadline" in message and "summarize" in message
    assert status["timing"]["total_s"] == 4060.0
    assert status["timing"]["stages"]["summarize"] == 4000.0


def test_stage_timer_unit(caplog):
    ticks = iter([100.0, 101.5, 104.0])
    timer = StageTimer(clock=lambda: next(ticks))
    timer.lap("fetch")
    timer.lap("process")
    with caplog.at_level(logging.INFO):
        payload = timer.report(deadline_minutes=1)
    assert payload == {"total_s": 4.0, "stages": {"fetch": 1.5, "process": 2.5}}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    with caplog.at_level(logging.WARNING):
        timer.report(deadline_minutes=0)
    assert "process" in caplog.records[-1].getMessage()
