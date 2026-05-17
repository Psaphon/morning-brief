"""Tests for daily briefing — prompt construction, DB storage, and skip logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import OllamaConfig
from src.db import Database
from src.summarizers.local import (
    _build_briefing_prompt,
    _parse_briefing_response,
    generate_daily_briefing,
)

# --- DB: save_briefing / get_briefing ---


def test_save_and_get_briefing(tmp_path: Path):
    """Briefing is stored and retrieved by date."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.save_briefing("2026-03-29", "Today was eventful.", "qwen2.5")
        result = db.get_briefing("2026-03-29")
        assert result is not None
        assert result["content"] == "Today was eventful."
        assert result["segment_map"] is None
    finally:
        db.close()


def test_get_briefing_missing_returns_none(tmp_path: Path):
    """get_briefing returns None when no entry exists for the date."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        assert db.get_briefing("2026-01-01") is None
    finally:
        db.close()


def test_save_briefing_replaces_existing(tmp_path: Path):
    """Saving a second briefing for the same date overwrites the first."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.save_briefing("2026-03-29", "First version.", "qwen2.5")
        db.save_briefing("2026-03-29", "Updated version.", "qwen2.5")
        result = db.get_briefing("2026-03-29")
        assert result is not None
        assert result["content"] == "Updated version."
    finally:
        db.close()


def test_save_briefing_with_segment_map(tmp_path: Path):
    """Briefing stored with a segment map is round-tripped correctly."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        segments = [
            {"topic": "Markets", "text": "Stocks fell.", "source_article_ids": [1, 2]},
            {"topic": "Politics", "text": "Bill passed.", "source_article_ids": [3]},
        ]
        db.save_briefing(
            "2026-03-29",
            "Stocks fell.\n\nBill passed.",
            "qwen2.5",
            json.dumps(segments),
        )
        result = db.get_briefing("2026-03-29")
        assert result is not None
        assert result["content"] == "Stocks fell.\n\nBill passed."
        assert result["segment_map"] == segments
    finally:
        db.close()


def test_daily_briefings_table_exists(tmp_path: Path):
    """Schema includes the daily_briefings table."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        assert "daily_briefings" in [t["name"] for t in tables]
    finally:
        db.close()


def test_daily_briefings_has_segment_map_column(tmp_path: Path):
    """Schema includes segment_map column in daily_briefings."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        cols = db.conn.execute("PRAGMA table_info(daily_briefings)").fetchall()
        col_names = [c["name"] for c in cols]
        assert "segment_map" in col_names
    finally:
        db.close()


# --- _build_briefing_prompt ---


def test_build_briefing_prompt_returns_string():
    """Function returns a non-empty string given categories with summaries."""
    summaries = {
        "US Politics": [(1, "Senate passed a bill."), (2, "President signed executive order.")],
        "Crypto / Web3": [(3, "Bitcoin hit $100k."), (4, "ETH gas fees dropped.")],
    }
    result = _build_briefing_prompt(summaries)
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_briefing_prompt_includes_categories():
    """Prompt contains each category name."""
    summaries = {
        "World News": [(10, "Major earthquake in Turkey.")],
        "Software Dev": [(11, "New Python 4.0 released.")],
    }
    result = _build_briefing_prompt(summaries)
    assert "World News" in result
    assert "Software Dev" in result


def test_build_briefing_prompt_includes_summaries():
    """Prompt contains individual article summaries."""
    summaries = {"Tech": [(42, "AI model beats humans at chess.")]}
    result = _build_briefing_prompt(summaries)
    assert "AI model beats humans at chess." in result


def test_build_briefing_prompt_includes_article_ids():
    """Prompt embeds article IDs so the model can reference them."""
    summaries = {"Markets": [(7, "Dow Jones fell 2%."), (8, "Oil prices rose.")]}
    result = _build_briefing_prompt(summaries)
    assert "[7]" in result
    assert "[8]" in result


def test_build_briefing_prompt_requests_json():
    """Prompt instructs the model to output JSON."""
    summaries = {"Markets": [(1, "Dow Jones fell 2%."), (2, "Ceasefire announced.")]}
    result = _build_briefing_prompt(summaries)
    assert "segments" in result
    assert "source_article_ids" in result


# --- _parse_briefing_response ---


def test_parse_briefing_response_valid_json():
    """Valid JSON with segments returns rendered text and segment JSON."""
    segments = [
        {"topic": "Lead", "text": "Markets dropped sharply.", "source_article_ids": [1]},
        {"topic": "Looking Ahead", "text": "Watch the Fed.", "source_article_ids": [2]},
    ]
    raw = json.dumps({"segments": segments})
    rendered, seg_json = _parse_briefing_response(raw)
    assert rendered == "Markets dropped sharply.\n\nWatch the Fed."
    assert seg_json is not None
    assert json.loads(seg_json) == segments


def test_parse_briefing_response_strips_markdown_fences():
    """JSON wrapped in markdown code fences is still parsed correctly."""
    segments = [{"topic": "News", "text": "Big story today.", "source_article_ids": [3]}]
    raw = "```json\n" + json.dumps({"segments": segments}) + "\n```"
    rendered, seg_json = _parse_briefing_response(raw)
    assert rendered == "Big story today."
    assert seg_json is not None


def test_parse_briefing_response_invalid_json_fallback():
    """Malformed JSON triggers fallback: raw text returned, no segment map."""
    raw = "This is just plain text, not JSON at all."
    rendered, seg_json = _parse_briefing_response(raw)
    assert rendered == raw
    assert seg_json is None


def test_parse_briefing_response_missing_segments_key_fallback():
    """JSON without 'segments' key triggers fallback."""
    raw = json.dumps({"result": "something unexpected"})
    rendered, seg_json = _parse_briefing_response(raw)
    assert rendered == raw
    assert seg_json is None


def test_parse_briefing_response_empty_segments_fallback():
    """Empty segments list triggers fallback."""
    raw = json.dumps({"segments": []})
    rendered, seg_json = _parse_briefing_response(raw)
    assert rendered == raw
    assert seg_json is None


def test_parse_briefing_response_segment_ids_preserved():
    """source_article_ids are preserved in the segment map."""
    segments = [
        {"topic": "Tech", "text": "AI advances.", "source_article_ids": [5, 12, 99]},
    ]
    raw = json.dumps({"segments": segments})
    _, seg_json = _parse_briefing_response(raw)
    assert seg_json is not None
    parsed = json.loads(seg_json)
    assert parsed[0]["source_article_ids"] == [5, 12, 99]


# --- generate_daily_briefing ---


@pytest.fixture
def ollama_config():
    return OllamaConfig(host="http://localhost:11434", model="qwen2.5:7b-instruct-q4_K_M")


@pytest.fixture
def db_with_summaries(tmp_path: Path):
    """A connected database with today's summarized articles."""
    db = Database(tmp_path / "test.db")
    db.connect()
    db.insert_article(
        url="https://example.com/1",
        url_hash="h1",
        title="Article One",
        source="BBC",
        category="World News",
        full_text="Full text here.",
    )
    row = db.get_unsummarized_articles()[0]
    db.update_summary(row["id"], "A concise summary of article one.", "qwen2.5")
    return db


@pytest.mark.asyncio
async def test_generate_briefing_skip_if_exists(db_with_summaries, ollama_config):
    """Returns existing briefing without calling Ollama if one already exists for today."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db_with_summaries.save_briefing(today, "Pre-existing briefing.", ollama_config.model)

    mock_target = "src.summarizers.local.check_ollama_available"
    with patch(mock_target, new_callable=AsyncMock) as mock_check:
        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result == "Pre-existing briefing."
    mock_check.assert_not_called()
    db_with_summaries.close()


@pytest.mark.asyncio
async def test_generate_briefing_ollama_unavailable(db_with_summaries, ollama_config):
    """Returns None gracefully when Ollama is unreachable."""
    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local._build_briefing_prompt", return_value="Some prompt"),
    ):
        mock_check.return_value = False
        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result is None
    db_with_summaries.close()


@pytest.mark.asyncio
async def test_generate_briefing_no_summaries(tmp_path, ollama_config):
    """Returns None when there are no summarized articles today."""
    db = Database(tmp_path / "empty.db")
    db.connect()
    result = await generate_daily_briefing(db, ollama_config)
    assert result is None
    db.close()


@pytest.mark.asyncio
async def test_generate_briefing_success_structured(db_with_summaries, ollama_config):
    """Successful structured JSON briefing is parsed, stored with segment map, and returned."""
    from datetime import datetime, timezone

    articles = db_with_summaries.get_todays_articles()
    aid = articles[0]["id"]

    segments = [
        {"topic": "Lead", "text": "Big things happened.", "source_article_ids": [aid]},
        {"topic": "Looking Ahead", "text": "Watch this space.", "source_article_ids": [aid]},
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": json.dumps({"segments": segments})}
    mock_resp.raise_for_status = lambda: None

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_check.return_value = True
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result == "Big things happened.\n\nWatch this space."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = db_with_summaries.get_briefing(today)
    assert record is not None
    assert record["content"] == "Big things happened.\n\nWatch this space."
    assert record["segment_map"] == segments
    db_with_summaries.close()


@pytest.mark.asyncio
async def test_generate_briefing_fallback_on_malformed_json(db_with_summaries, ollama_config):
    """Malformed JSON from Ollama stores plain text with no segment map (graceful fallback)."""
    from datetime import datetime, timezone

    plain_text = "Today was interesting. Markets moved. Watch tomorrow."
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": plain_text}
    mock_resp.raise_for_status = lambda: None

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_check.return_value = True
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result == plain_text

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = db_with_summaries.get_briefing(today)
    assert record is not None
    assert record["content"] == plain_text
    assert record["segment_map"] is None
    db_with_summaries.close()


@pytest.mark.asyncio
async def test_generate_briefing_http_error_returns_none(db_with_summaries, ollama_config):
    """HTTP errors from Ollama are caught and return None."""
    import httpx

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local._build_briefing_prompt", return_value="A prompt"),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_check.return_value = True
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result is None
    db_with_summaries.close()
