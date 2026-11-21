"""Tests for article-extraction: schema, trimming, request shape and pipeline behaviour."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from src.config import OllamaConfig, load_config
from src.summarizers.local import (
    _briefing_summaries,
    extraction_max_chars,
    summarize_single,
)
from src.summarizers.schema import (
    EXTRACTION_SCHEMA,
    ExtractionError,
    parse_extraction,
    trim_words,
)
from tests.stub_ollama import BRIEFING_MARKER, EXTRACTION_MARKER, StubOllama
from tests.test_signals_golden import (
    GOLDEN_PATH,
    load_fixture_articles,
    run_pipeline_on,
    strip_ignored,
)

VALID = {
    "what_happened": "Example Corp bought a plant. It paid $1.5 million.",
    "entities": ["Example Corp"],
    "numbers": ["$1.5 million"],
    "event_date": "2020-01-02",
    "status": "happened",
    "is_roundup": False,
}


def _raw(**overrides) -> str:
    return json.dumps({**VALID, **overrides})


def _without(field: str) -> str:
    return json.dumps({k: v for k, v in VALID.items() if k != field})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", "")


def _run(tmp_path: Path, monkeypatch, **stub_kwargs):
    """Run the pipeline on the fixture with a configured stub; return (artifact, stub, db path)."""
    monkeypatch.setattr("tests.test_signals_golden.StubOllama", lambda: StubOllama(**stub_kwargs))
    artifact, stub = run_pipeline_on(load_fixture_articles(), tmp_path / "run")
    return artifact, stub, tmp_path / "run" / "data" / "morning_brief.db"


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _summarizable(db_path: Path) -> int:
    """Articles that reach the model: the fixture's same-story pair is deduped to one."""
    return len(_rows(db_path, "SELECT 1 FROM articles WHERE duplicate_of IS NULL"))


def _briefing_prompts(stub: StubOllama) -> list[str]:
    return [
        c["body"]["prompt"] for c in stub.generate_calls if BRIEFING_MARKER in c["body"]["prompt"]
    ]


# --- trim_words -------------------------------------------------------------


def test_trim_under_limit_is_unchanged():
    assert trim_words("One short sentence. And another.", 50) == "One short sentence. And another."


def test_trim_cuts_at_last_sentence_boundary_within_limit():
    text = "Alpha beta gamma. Delta epsilon zeta eta theta."
    assert trim_words(text, 5) == "Alpha beta gamma."


def test_trim_keeps_at_most_two_sentences():
    assert trim_words("One. Two. Three.", 50) == "One. Two."


def test_trim_hard_cuts_a_single_overlong_sentence():
    text = "one two three four five six seven eight nine ten."
    assert trim_words(text, 4) == "one two three four"


def test_trim_does_not_split_on_abbreviations():
    assert trim_words("The U.S. said it would act. Then it did.", 50).startswith(
        "The U.S. said it would act."
    )


# --- parse_extraction --------------------------------------------------------


def test_parse_accepts_valid_and_null_date():
    assert parse_extraction(_raw(), 50)["event_date"] == "2020-01-02"
    assert parse_extraction(_raw(event_date=None), 50)["event_date"] is None


def test_parse_trims_what_happened_to_max_words():
    out = parse_extraction(_raw(what_happened="One two three. Four five six."), 3)
    assert out["what_happened"] == "One two three."


@pytest.mark.parametrize("field", list(EXTRACTION_SCHEMA["required"]))
def test_parse_rejects_missing_field(field):
    with pytest.raises(ExtractionError, match=field):
        parse_extraction(_without(field), 50)


@pytest.mark.parametrize(
    "overrides",
    [
        {"what_happened": 5},
        {"what_happened": "   "},
        {"entities": "Example Corp"},
        {"entities": [1, 2]},
        {"numbers": None},
        {"is_roundup": "false"},
        {"is_roundup": 0},
        {"event_date": 20200102},
    ],
)
def test_parse_rejects_wrong_types(overrides):
    with pytest.raises(ExtractionError):
        parse_extraction(_raw(**overrides), 50)


@pytest.mark.parametrize("bad", ["02/01/2020", "2020-1-2", "2020-02-30", "2020-13-01", ""])
def test_parse_rejects_bad_event_date(bad):
    with pytest.raises(ExtractionError, match="event_date"):
        parse_extraction(_raw(event_date=bad), 50)


@pytest.mark.parametrize("bad", ["done", "", None, "Happened"])
def test_parse_rejects_bad_status(bad):
    with pytest.raises(ExtractionError, match="status"):
        parse_extraction(_raw(status=bad), 50)


@pytest.mark.parametrize("raw", ["", "not json", '{"what_happened": "cut', "[]", "null"])
def test_parse_rejects_non_json_or_non_object(raw):
    with pytest.raises(ExtractionError):
        parse_extraction(raw, 50)


# --- config -------------------------------------------------------------------


def test_config_defaults(monkeypatch):
    for name in (
        "OLLAMA_NUM_CTX",
        "EXTRACT_TEMPERATURE",
        "EXTRACT_SEED",
        "EXTRACT_MAX_WORDS",
        "EXTRACT_NUM_PREDICT",
        "CHARS_PER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    o = load_config().ollama
    assert (o.num_ctx, o.extract_temperature, o.extract_seed) == (8192, 0.1, 42)
    assert (o.extract_max_words, o.extract_num_predict, o.chars_per_token) == (50, 600, 4)


def test_max_chars_fits_context():
    assert extraction_max_chars(OllamaConfig()) == (8192 - 600 - 600) * 4
    small = OllamaConfig(num_ctx=4096, extract_num_predict=500, chars_per_token=3)
    assert extraction_max_chars(small) == (4096 - 500 - 600) * 3


# --- summarize_single (direct) -------------------------------------------------


class _Resp:
    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"response": self._text}


class _Client:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls.append(json)
        return _Resp(self.replies.pop(0))


def _article(**kw) -> dict:
    return {
        "id": 7,
        "title": "T",
        "source": "S",
        "category": "C",
        "full_text": "word " * 100,
        "published_at": "2020-01-02T03:04:05Z",
        **kw,
    }


@pytest.mark.asyncio
async def test_retry_once_then_success():
    client = _Client(["{bad", _raw()])
    result = await summarize_single(client, OllamaConfig(), _article())
    assert result.success and not result.extraction_failed
    assert result.summary == result.extraction["what_happened"]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_two_invalid_replies_fail_without_fallback_to_text(caplog):
    client = _Client(["{bad", _without("status"), _raw()])
    with caplog.at_level(logging.WARNING):
        result = await summarize_single(client, OllamaConfig(), _article())
    assert result.extraction_failed and not result.success
    assert result.summary == "" and result.extraction is None
    assert len(client.calls) == 2  # exactly one retry
    assert "article 7" in caplog.text


@pytest.mark.asyncio
async def test_prompt_uses_fetched_at_when_no_published_at_and_caps_text():
    cfg = OllamaConfig(num_ctx=2048, extract_num_predict=100, chars_per_token=2)
    client = _Client([_raw()])
    art = _article(
        published_at=None, fetched_at="2021-05-06T00:00:00+00:00", full_text="x" * 50_000
    )
    await summarize_single(client, cfg, art)
    prompt = client.calls[0]["prompt"]
    assert "Published: 2021-05-06" in prompt
    assert prompt.count("x") <= extraction_max_chars(cfg) + 5


# --- pipeline: request shape ---------------------------------------------------


def test_request_carries_schema_config_options_and_published_date(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "6144")
    monkeypatch.setenv("EXTRACT_TEMPERATURE", "0.2")
    monkeypatch.setenv("EXTRACT_SEED", "7")
    monkeypatch.setenv("EXTRACT_NUM_PREDICT", "333")
    _, stub, db = _run(tmp_path, monkeypatch)

    calls = [c["body"] for c in stub.generate_calls if EXTRACTION_MARKER in c["body"]["prompt"]]
    assert len(calls) == _summarizable(db)
    for body in calls:
        assert body["format"] == EXTRACTION_SCHEMA
        assert body["think"] is False
        opts = body["options"]
        assert (opts["num_ctx"], opts["temperature"], opts["seed"], opts["num_predict"]) == (
            6144,
            0.2,
            7,
            333,
        )
    prompts = " ".join(b["prompt"] for b in calls)
    for art in load_fixture_articles():
        assert f"Published: {art['published_at'][:10]}" in prompts


# --- pipeline: storage --------------------------------------------------------


def test_valid_extractions_stored_one_per_article(tmp_path, monkeypatch):
    _, stub, db = _run(tmp_path, monkeypatch)
    n = _summarizable(db)
    assert n < len(load_fixture_articles())  # the same-story pair is deduped
    rows = _rows(
        db,
        "SELECT e.json, a.summary, a.summary_model, a.is_roundup, a.extraction_failed"
        " FROM extractions e JOIN articles a ON a.id = e.article_id",
    )
    assert len(rows) == n
    assert len(_rows(db, "SELECT article_id FROM extractions")) == n
    for row in rows:
        assert row["summary"] == json.loads(row["json"])["what_happened"]
        assert row["summary_model"] == "gemma4:e4b"
        assert (row["is_roundup"], row["extraction_failed"]) == (0, 0)
    assert _briefing_prompts(stub)  # briefing still generated from the summaries


def test_malformed_twice_marks_failed_and_run_continues(tmp_path, monkeypatch, caplog):
    victim = load_fixture_articles()[0]["title"]
    with caplog.at_level(logging.WARNING):
        _, stub, db = _run(tmp_path, monkeypatch, malformed_titles={victim})

    failed = _rows(db, "SELECT id, title FROM articles WHERE extraction_failed = 1")
    assert [r["title"] for r in failed] == [victim]
    fid = failed[0]["id"]
    assert _rows(db, f"SELECT 1 FROM extractions WHERE article_id = {fid}") == []
    assert _rows(db, f"SELECT summary FROM articles WHERE id = {fid}")[0]["summary"] is None
    # Everything else was extracted.
    assert len(_rows(db, "SELECT 1 FROM extractions")) == _summarizable(db) - 1
    # Exactly one retry for the victim, no more.
    assert (
        sum(f"Article title: {victim}\n" in c["body"]["prompt"] for c in stub.generate_calls) == 2
    )
    # Absent from the briefing input; others present.
    prompts = _briefing_prompts(stub)
    assert len(prompts) == 1
    assert f"[{fid}] " not in prompts[0]
    assert "Canned fact about" in prompts[0]
    # Logged by id at WARNING.
    assert any(
        r.levelno == logging.WARNING and f"article {fid}" in r.getMessage() for r in caplog.records
    )


def test_failed_article_is_not_retried_next_run(tmp_path, monkeypatch):
    victim = load_fixture_articles()[0]["title"]
    _, _, db_path = _run(tmp_path, monkeypatch, malformed_titles={victim})
    from src.db import Database

    db = Database(db_path)
    db.connect()
    try:
        assert victim not in [a["title"] for a in db.get_unsummarized_articles()]
        assert any(a["extraction_failed"] for a in db.get_todays_articles())
    finally:
        db.close()


def test_malformed_next_counts_calls(tmp_path, monkeypatch):
    # Four malformed replies = two articles fail twice each; the run still finishes.
    monkeypatch.setattr("tests.test_signals_golden.StubOllama", lambda: _next_stub(4))
    run_pipeline_on(load_fixture_articles(), tmp_path / "run")
    db = tmp_path / "run" / "data" / "morning_brief.db"
    assert len(_rows(db, "SELECT 1 FROM articles WHERE extraction_failed = 1")) == 2


def _next_stub(n: int) -> StubOllama:
    stub = StubOllama()
    stub.malformed_next(n)
    return stub


# --- roundups -------------------------------------------------------------------


def test_roundup_left_out_of_briefing_but_kept_in_signals(tmp_path, monkeypatch):
    articles = load_fixture_articles()
    roundup_titles = {a["title"] for a in articles[::2]}
    artifact, stub, db = _run(tmp_path, monkeypatch, roundup_titles=roundup_titles)

    by_title = {
        r["title"]: r
        for r in _rows(db, "SELECT id, title, is_roundup FROM articles WHERE duplicate_of IS NULL")
    }
    roundup_titles &= set(by_title)  # a deduped duplicate never reaches the model
    assert roundup_titles
    roundup_ids = {by_title[t]["id"] for t in roundup_titles}
    assert all(by_title[t]["is_roundup"] == 1 for t in roundup_titles)
    assert all(r["is_roundup"] == 0 for t, r in by_title.items() if t not in roundup_titles)
    # Roundups still have a stored extraction and summary (page keeps working).
    assert len(_rows(db, "SELECT 1 FROM extractions")) == _summarizable(db)

    prompt = _briefing_prompts(stub)[0]
    for rid in roundup_ids:
        assert f"[{rid}] " not in prompt
    for t, r in by_title.items():
        if t not in roundup_titles:
            assert f"[{r['id']}] " in prompt

    # Signals are unchanged by roundup flags, and include roundup articles.
    provenance_ids = {p["article_id"] for s in artifact["signals"] for p in s["provenance"]}
    assert provenance_ids & roundup_ids
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert strip_ignored(artifact) == strip_ignored(golden)


def test_briefing_input_filter_unit():
    arts = [
        {"id": 1, "category": "A", "summary": "keep"},
        {"id": 2, "category": "A", "summary": "roundup", "is_roundup": 1},
        {"id": 3, "category": "A", "summary": "failed", "extraction_failed": 1},
        {"id": 4, "category": "B", "summary": None},
        {"id": 5, "category": "B", "summary": "keep too", "is_roundup": 0},
    ]
    assert _briefing_summaries(arts) == {"A": [(1, "keep")], "B": [(5, "keep too")]}


def test_reader_lane_wrapper_still_passes_flags_through(tmp_path):
    from src.db import Database
    from src.processors.lanes import FeedLanes, ReaderLaneDB

    db = Database(tmp_path / "t.db")
    db.connect()
    try:
        db.conn.execute(
            "INSERT INTO articles (url, url_hash, title, source, category, fetched_at,"
            " summary, is_roundup) VALUES ('u', 'h', 't', 'Fixture', 'C', '2999-01-01', 's', 1)"
        )
        db.conn.commit()
        wrapped = ReaderLaneDB(db, FeedLanes(default=frozenset({"reader"}), feeds={}))
        (row,) = wrapped.get_todays_articles()
        assert row["is_roundup"] == 1 and row["extraction_failed"] == 0
        assert wrapped.get_briefing("1999-01-01") is None  # delegated attribute
    finally:
        db.close()


def test_save_extraction_upserts(tmp_path):
    from src.db import Database

    db = Database(tmp_path / "t.db")
    db.connect()
    try:
        db.conn.execute(
            "INSERT INTO articles (url, url_hash, title, source, category, fetched_at)"
            " VALUES ('u', 'h', 't', 's', 'c', '2999-01-01')"
        )
        db.conn.commit()
        db.mark_extraction_failed(1)
        db.save_extraction(1, parse_extraction(_raw(), 50), "m1")
        db.save_extraction(1, parse_extraction(_raw(is_roundup=True), 50), "m2")
        rows = db.conn.execute("SELECT * FROM extractions").fetchall()
        assert len(rows) == 1 and rows[0]["model"] == "m2"
        art = db.conn.execute("SELECT * FROM articles").fetchone()
        assert (art["summary_model"], art["is_roundup"], art["extraction_failed"]) == ("m2", 1, 0)
    finally:
        db.close()


def test_lists_are_capped_so_replies_fit_num_predict():
    """Uncapped lists ran past num_predict on long articles (7/100 cut off on hub)."""
    from src.summarizers.schema import EXTRACTION_SCHEMA, MAX_LIST_ITEMS

    props = EXTRACTION_SCHEMA["properties"]
    assert props["entities"]["maxItems"] == MAX_LIST_ITEMS
    assert props["numbers"]["maxItems"] == MAX_LIST_ITEMS


def test_roundup_rule_defaults_to_false():
    """A false roundup drops a real story from the briefing; the prompt must lean false."""
    from src.summarizers.local import EXTRACTION_PROMPT

    assert "ONLY if" in EXTRACTION_PROMPT
    assert "When unsure, use false" in EXTRACTION_PROMPT
