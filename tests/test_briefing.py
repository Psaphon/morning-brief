"""Tests for daily briefing — prompt construction, DB storage, and skip logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import OllamaConfig
from src.db import Database
from src.summarizers.local import _build_briefing_prompt, generate_daily_briefing

# --- DB: save_briefing / get_briefing ---


def test_save_and_get_briefing(tmp_path: Path):
    """Briefing is stored and retrieved by date."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.save_briefing("2026-03-29", "Today was eventful.", "qwen2.5")
        result = db.get_briefing("2026-03-29")
        assert result == "Today was eventful."
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
        assert db.get_briefing("2026-03-29") == "Updated version."
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


# --- _build_briefing_prompt ---


def test_build_briefing_prompt_returns_string():
    """Function returns a non-empty string given categories with summaries."""
    summaries = {
        "US Politics": ["Senate passed a bill.", "President signed executive order."],
        "Crypto / Web3": ["Bitcoin hit $100k.", "ETH gas fees dropped."],
    }
    result = _build_briefing_prompt(summaries)
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_briefing_prompt_includes_categories():
    """Prompt contains each category name."""
    summaries = {
        "World News": ["Major earthquake in Turkey."],
        "Software Dev": ["New Python 4.0 released."],
    }
    result = _build_briefing_prompt(summaries)
    assert "World News" in result
    assert "Software Dev" in result


def test_build_briefing_prompt_includes_summaries():
    """Prompt contains individual article summaries."""
    summaries = {"Tech": ["AI model beats humans at chess."]}
    result = _build_briefing_prompt(summaries)
    assert "AI model beats humans at chess." in result


def test_build_briefing_prompt_mentions_briefing():
    """Prompt includes the briefing instruction (500-1000 words or similar)."""
    summaries = {"Markets": ["Dow Jones fell 2%."], "World": ["Ceasefire announced."]}
    result = _build_briefing_prompt(summaries)
    # Should contain some form of the briefing instruction
    assert any(word in result.lower() for word in ["briefing", "summary", "500", "themes"])


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
async def test_generate_briefing_success(db_with_summaries, ollama_config):
    """Successful briefing is stored in DB and returned."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "Today's key themes were X and Y."}
    mock_resp.raise_for_status = lambda: None

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local._build_briefing_prompt", return_value="A real prompt"),
        patch("httpx.AsyncClient") as mock_client_cls,
    ):
        mock_check.return_value = True
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await generate_daily_briefing(db_with_summaries, ollama_config)

    assert result == "Today's key themes were X and Y."

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert db_with_summaries.get_briefing(today) == "Today's key themes were X and Y."
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
