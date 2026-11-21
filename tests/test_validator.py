"""Output validator: unit checks plus pipeline runs against the Ollama stub."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from src.config import load_config
from src.summarizers.local import validate_briefing
from src.validators.grounding import (
    ValidatorRun,
    check_banned,
    check_names,
    check_numbers,
    load_banned_phrases,
    load_name_allowlist,
    split_sentences,
    validate_text,
)
from tests.stub_ollama import UNGROUNDED_SENTENCE, StubOllama, canned_extraction
from tests.test_extraction import _briefing_prompts, _rows, _summarizable
from tests.test_signals_golden import (
    GOLDEN_PATH,
    load_fixture_articles,
    run_pipeline_on,
    strip_ignored,
)

ALLOW = load_name_allowlist()
PHRASES = load_banned_phrases(overlay=Path("/nonexistent/overlay.txt"))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", "")
    monkeypatch.delenv("VALIDATOR_MODE", raising=False)
    monkeypatch.delenv("MORNING_BRIEF_BANNED_OVERLAY", raising=False)


# --- sentences --------------------------------------------------------------------


def test_split_keeps_abbreviations_together():
    assert split_sentences("U.S. officials met. They left early.") == [
        "U.S. officials met.",
        "They left early.",
    ]


# --- numbers ----------------------------------------------------------------------


def test_number_scale_suffix_matches():
    assert check_numbers("It paid $44.6 million.", "The deal was worth $44.6M in total.")


def test_number_wrong_digit_fails():
    assert not check_numbers("It paid $45.6 million.", "The deal was worth $44.6M in total.")


def test_number_commas_stripped_and_years_count():
    assert check_numbers("It sold 12,500 units in 2024.", "Sales: 12500 units, up in 2024")
    assert not check_numbers("It sold 12,500 units in 2023.", "Sales: 12500 units, up in 2024")


def test_number_is_not_a_substring_match():
    assert not check_numbers("It rose 4 points.", "It rose 44.6 points.")


def test_sentence_without_numbers_passes():
    assert check_numbers("Nothing numeric here.", "")


# --- names ------------------------------------------------------------------------


def test_name_prefix_match_democrats_vs_democratic():
    assert check_names("Senators said Democrats agreed.", "The Democratic caucus agreed.", ALLOW)


def test_name_missing_from_source_fails_with_possessive_stripped():
    assert not check_names("Bids rose at Christie's.", "Bids rose at the auction house.", ALLOW)
    assert check_names("Bids rose at Christie's.", "Christie’s reported bids.", ALLOW)


def test_name_sentence_initial_short_and_allowlisted_are_skipped():
    assert check_names("Zorblax rose.", "nothing", ALLOW)  # sentence-initial
    assert check_names("Shares in Ox rose.", "shares rose", ALLOW)  # under 3 characters
    assert check_names("It met on Monday in the U.S. and EU.", "it met", ALLOW)


def test_name_check_is_case_insensitive():
    assert check_names("Shares of Acme rose.", "ACME corp shares rose.", ALLOW)


# --- banned phrases ---------------------------------------------------------------


def test_banned_phrase_fails_case_insensitively():
    assert not check_banned("This highlights the volatility.", PHRASES)
    assert not check_banned("IT IS WORTH NOTING that sales fell.", PHRASES)
    assert check_banned("Sales fell sharply.", PHRASES)


def test_seed_phrases_loaded():
    assert {"highlights", "delve", "in today's", "robust"} <= set(PHRASES)
    assert len(PHRASES) == 20


def test_missing_overlay_is_not_an_error(caplog):
    with caplog.at_level(logging.INFO):
        phrases = load_banned_phrases(overlay=Path("/nonexistent/overlay.txt"))
    assert phrases == PHRASES
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_overlay_adds_phrases_via_env(tmp_path, monkeypatch):
    overlay = tmp_path / "banned.txt"
    overlay.write_text("# mine\nsynergy\n\n", encoding="utf-8")
    monkeypatch.setenv("MORNING_BRIEF_BANNED_OVERLAY", str(overlay))
    phrases = load_banned_phrases()
    assert "synergy" in phrases and "highlights" in phrases


def test_allowlist_ignores_comments():
    assert "monday" in ALLOW and "u.s" in ALLOW
    assert not any(w.startswith("#") for w in ALLOW)


# --- validate_text ----------------------------------------------------------------


def test_validate_text_reports_first_failing_reason_in_order():
    source = "Acme bought a plant for $5 million."
    text = (
        "Acme bought a plant. "  # kept
        "It cost $6 million. "  # number
        "It was bought by Zorblax. "  # name
        "This highlights the deal. "  # banned
        "It cost $7 million and highlights Zorblax."  # number wins over name and banned
    )
    result = validate_text(text, source, allowlist=ALLOW, phrases=PHRASES)
    assert result.kept == ["Acme bought a plant."]
    assert [d.reason for d in result.dropped] == ["number", "name", "banned", "number"]
    assert result.counts == {"number": 2, "name": 1, "banned": 1}
    assert result.checked == 5
    assert result.text == "Acme bought a plant."


def test_validate_text_preserves_paragraphs():
    source = "Acme grew. Beta shrank."
    result = validate_text(
        "Acme grew. It cost $9.\n\nBeta shrank.", source, allowlist=ALLOW, phrases=PHRASES
    )
    assert result.text == "Acme grew.\n\nBeta shrank."


# --- config -----------------------------------------------------------------------


def test_validator_mode_config(monkeypatch, caplog):
    assert load_config().validator_mode == "enforce"
    for mode in ("warn", "off", "enforce"):
        monkeypatch.setenv("VALIDATOR_MODE", mode)
        assert load_config().validator_mode == mode
    monkeypatch.setenv("VALIDATOR_MODE", "sometimes")
    with caplog.at_level(logging.WARNING):
        assert load_config().validator_mode == "enforce"
    assert any("VALIDATOR_MODE" in r.getMessage() for r in caplog.records)


def test_overlay_path_config(monkeypatch):
    assert load_config().banned_overlay is None
    monkeypatch.setenv("MORNING_BRIEF_BANNED_OVERLAY", "/x/banned.txt")
    assert load_config().banned_overlay == Path("/x/banned.txt")


# --- briefing segments (unit) -----------------------------------------------------

ARTICLES = [
    {"id": 1, "title": "Acme buys plant", "summary": "Acme paid $5 million.", "category": "A"},
    {"id": 2, "title": "Beta cuts jobs", "summary": "Beta cut 40 jobs.", "category": "A"},
    {"id": 3, "title": "Gamma", "summary": "Roundup.", "category": "A", "is_roundup": 1},
]


def _segments(*segs: tuple[str, str, list]) -> tuple[str, str]:
    items = [{"topic": t, "text": x, "source_article_ids": ids} for t, x, ids in segs]
    return "\n\n".join(s["text"] for s in items), json.dumps(items)


def test_segment_checked_against_cited_articles_only():
    rendered, seg_json = _segments(("T", "Acme paid $5 million. Beta cut 40 jobs.", [1]))
    run = ValidatorRun(allowlist=ALLOW, phrases=PHRASES)
    out, out_json = validate_briefing(rendered, seg_json, ARTICLES, run)
    assert out == "Acme paid $5 million."  # "40" is only in article 2
    assert json.loads(out_json)[0]["text"] == out
    assert run.dropped["number"] == 1 and run.checked == 2


def test_fully_dropped_segment_removed_from_text_and_map():
    rendered, seg_json = _segments(
        ("Keep", "Acme paid $5 million.", [1]),
        ("Drop", "This highlights the deal.", [1]),
    )
    out, out_json = validate_briefing(
        rendered, seg_json, ARTICLES, ValidatorRun(allowlist=ALLOW, phrases=PHRASES)
    )
    assert out == "Acme paid $5 million."
    assert [s["topic"] for s in json.loads(out_json)] == ["Keep"]


def test_segment_without_known_ids_uses_all_briefing_input():
    rendered, seg_json = _segments(
        ("Ok", "Beta cut 40 jobs.", [999]),
        ("Roundup id", "Reports say Gamma cut 40 jobs.", [3]),  # id 3 is not briefing input
    )
    out, out_json = validate_briefing(
        rendered, seg_json, ARTICLES, ValidatorRun(allowlist=ALLOW, phrases=PHRASES)
    )
    # "Gamma" is not in any briefing input either, but its id 3 is unknown, so the
    # whole input is the source: the name check drops it.
    assert out == "Beta cut 40 jobs."
    assert [s["topic"] for s in json.loads(out_json)] == ["Ok"]


def test_nothing_surviving_returns_empty():
    rendered, seg_json = _segments(("T", "It cost $1.", [1]))
    out, out_json = validate_briefing(
        rendered, seg_json, ARTICLES, ValidatorRun(allowlist=ALLOW, phrases=PHRASES)
    )
    assert (out, out_json) == ("", None)


def test_plain_text_fallback_is_checked_against_all_input():
    run = ValidatorRun(allowlist=ALLOW, phrases=PHRASES)
    out, out_json = validate_briefing("Beta cut 40 jobs. It cost $1.", None, ARTICLES, run)
    assert (out, out_json) == ("Beta cut 40 jobs.", None)


def test_warn_and_off_leave_briefing_untouched():
    rendered, seg_json = _segments(("T", "It cost $1.", [1]))
    for mode in ("warn", "off"):
        run = ValidatorRun(mode=mode, allowlist=ALLOW, phrases=PHRASES)
        assert validate_briefing(rendered, seg_json, ARTICLES, run) == (rendered, seg_json)
    assert validate_briefing(rendered, seg_json, ARTICLES, None) == (rendered, seg_json)


# --- pipeline runs ----------------------------------------------------------------


def _run(tmp_path: Path, monkeypatch, mode: str | None = None, **stub_kwargs):
    if mode:
        monkeypatch.setenv("VALIDATOR_MODE", mode)
    monkeypatch.setattr("tests.test_signals_golden.StubOllama", lambda: StubOllama(**stub_kwargs))
    artifact, stub = run_pipeline_on(load_fixture_articles(), tmp_path / "run")
    data = tmp_path / "run" / "data"
    status = json.loads((data / "last_run.json").read_text(encoding="utf-8"))
    return artifact, stub, data / "morning_brief.db", status


def _victim() -> str:
    return load_fixture_articles()[0]["title"]


def _article(db: Path, title: str) -> sqlite3.Row:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM articles WHERE title = ?", (title,)).fetchone()
    finally:
        conn.close()


def _summary_of(db: Path, title: str) -> str | None:
    return _article(db, title)["summary"]


def test_enforce_drops_ungrounded_sentence_and_reports_it(tmp_path, monkeypatch):
    _, stub, db, status = _run(tmp_path, monkeypatch, "enforce", ungrounded_titles={_victim()})
    assert _summary_of(db, _victim()) == f"Canned fact about {_victim()}."
    assert "$987" not in " ".join(r["json"] for r in _rows(db, "SELECT json FROM extractions"))
    assert "$987" not in " ".join(_briefing_prompts(stub))

    validator = status["validator"]
    assert validator["mode"] == "enforce"
    assert validator["dropped"] == {"number": 1, "name": 0, "banned": 0}
    # One sentence per article plus the extra one, plus the two canned briefing segments.
    assert validator["checked"] == _summarizable(db) + 1 + 2
    assert status["status"] == "ok"


def test_warn_keeps_sentence_and_logs_warning(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        _, _, db, status = _run(tmp_path, monkeypatch, "warn", ungrounded_titles={_victim()})
    assert UNGROUNDED_SENTENCE in _summary_of(db, _victim())
    assert any(
        r.levelno == logging.WARNING and "(number)" in r.getMessage() and "$987" in r.getMessage()
        for r in caplog.records
    )
    assert status["validator"]["mode"] == "warn"
    assert status["validator"]["dropped"]["number"] == 1


def test_off_changes_nothing(tmp_path, monkeypatch):
    _, _, db, status = _run(tmp_path, monkeypatch, "off", ungrounded_titles={_victim()})
    assert UNGROUNDED_SENTENCE in _summary_of(db, _victim())
    assert status["validator"] == {
        "mode": "off",
        "checked": 0,
        "dropped": {"number": 0, "name": 0, "banned": 0},
    }


def test_default_mode_is_enforce_and_clean_run_drops_nothing(tmp_path, monkeypatch):
    _, _, _, status = _run(tmp_path, monkeypatch)
    assert status["validator"]["mode"] == "enforce"
    assert status["validator"]["dropped"] == {"number": 0, "name": 0, "banned": 0}
    assert status["validator"]["checked"] > 0


def test_all_dropped_extraction_is_left_out_of_briefing(tmp_path, monkeypatch):
    victim = _victim()

    def fake(title: str = "", **kw):
        out = canned_extraction(title, **kw)
        if title == victim:  # its only sentence has a figure that is in no article
            out["what_happened"] = UNGROUNDED_SENTENCE
        return out

    monkeypatch.setattr("tests.stub_ollama.canned_extraction", fake)
    _, stub, db, status = _run(tmp_path, monkeypatch, "enforce")

    row = _article(db, victim)
    assert row["summary"] is None and row["extraction_failed"] == 1
    assert _rows(db, f"SELECT 1 FROM extractions WHERE article_id = {row['id']}") == []
    (prompt,) = _briefing_prompts(stub)
    assert f"[{row['id']}] " not in prompt
    assert "Canned fact about" in prompt  # the other articles are still there
    assert status["validator"]["dropped"]["number"] == 1


BAD_BRIEFING = {
    "segments": [
        {
            "topic": "Lead",
            "text": "Canned lead segment. Spending reached $987 million.",
            "source_article_ids": [1],
        },
        {"topic": "Gone", "text": "Revenue hit $987 million.", "source_article_ids": [2]},
        {"topic": "Ahead", "text": "Canned look-ahead segment.", "source_article_ids": [9999]},
    ]
}


def test_briefing_segment_removed_from_text_and_segment_map(tmp_path, monkeypatch):
    _, _, db, status = _run(tmp_path, monkeypatch, "enforce", briefing=BAD_BRIEFING)
    row = _rows(db, "SELECT content, segment_map FROM daily_briefings")[0]
    segments = json.loads(row["segment_map"])
    assert [s["topic"] for s in segments] == ["Lead", "Ahead"]
    assert segments[0]["text"] == "Canned lead segment."
    assert row["content"] == "Canned lead segment.\n\nCanned look-ahead segment."
    assert "$987" not in row["content"] + row["segment_map"]
    assert status["validator"]["dropped"]["number"] == 2


def test_briefing_untouched_when_off(tmp_path, monkeypatch):
    _, _, db, _ = _run(tmp_path, monkeypatch, "off", briefing=BAD_BRIEFING)
    row = _rows(db, "SELECT content, segment_map FROM daily_briefings")[0]
    assert len(json.loads(row["segment_map"])) == 3
    assert "$987" in row["content"]


# --- signals ----------------------------------------------------------------------


def test_signals_unaffected_by_validation(tmp_path, monkeypatch):
    artifact, _, _, _ = _run(tmp_path, monkeypatch, "enforce", ungrounded_titles={_victim()})
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert strip_ignored(artifact) == strip_ignored(golden)
