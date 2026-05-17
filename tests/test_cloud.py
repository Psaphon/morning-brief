"""Tests for the Claude API cloud synthesizer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.summarizers.cloud import (  # noqa: E402
    CLAUDE_MODEL,
    MAX_PROMPT_CHARS,
    generate_daily_briefing_claude,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(articles=None, existing_briefing=None):
    """Return a mock Database with configurable behaviour."""
    db = MagicMock()
    db.get_briefing.return_value = existing_briefing
    db.get_todays_articles.return_value = articles or []
    db.save_briefing.return_value = None
    return db


def _make_article(article_id: int, category: str = "Tech", summary: str = "A summary.") -> dict:
    return {
        "id": article_id,
        "title": f"Article {article_id}",
        "source": "Test Source",
        "category": category,
        "summary": summary,
    }


def _claude_response(text: str) -> dict:
    """Build a minimal Claude Messages API response payload."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": CLAUDE_MODEL,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }


# ---------------------------------------------------------------------------
# No API key — skip immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_api_key_returns_none():
    """Returns None without making any HTTP calls when key is empty."""
    db = _make_db()
    result = await generate_daily_briefing_claude(db, anthropic_api_key="")
    assert result is None
    db.get_briefing.assert_not_called()


# ---------------------------------------------------------------------------
# Existing briefing — reuse without calling API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reuses_existing_briefing():
    """Returns cached briefing text without calling the Claude API."""
    db = _make_db(existing_briefing={"content": "Cached briefing text."})
    result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")
    assert result == "Cached briefing text."


# ---------------------------------------------------------------------------
# No summaries — skip gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_summaries_returns_none():
    """Returns None when there are no article summaries."""
    db = _make_db(articles=[{"id": 1, "title": "No summary", "category": "Tech", "summary": ""}])
    result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")
    assert result is None


# ---------------------------------------------------------------------------
# Successful synthesis
# ---------------------------------------------------------------------------

_VALID_SEGMENTS_JSON = (
    '{"segments": ['
    '{"topic": "Lead", "text": "Big things happened.", "source_article_ids": [1, 2]},'
    '{"topic": "Outlook", "text": "Watch for more.", "source_article_ids": [3]}'
    "]}"
)


@pytest.mark.asyncio
async def test_successful_synthesis():
    """Happy-path: Claude returns valid JSON, briefing is saved and returned."""
    articles = [_make_article(i) for i in range(1, 4)]
    db = _make_db(articles=articles)

    mock_resp = MagicMock()
    mock_resp.json.return_value = _claude_response(_VALID_SEGMENTS_JSON)
    mock_resp.raise_for_status = lambda: None

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    assert result is not None
    assert "Big things happened." in result
    assert "Watch for more." in result
    db.save_briefing.assert_called_once()
    # Verify model recorded
    call_args = db.save_briefing.call_args
    assert call_args[0][2] == CLAUDE_MODEL


# ---------------------------------------------------------------------------
# Prompt construction — verify API call payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_construction_includes_article_ids():
    """The API request body contains article IDs from summaries."""
    articles = [
        _make_article(42, category="Crypto", summary="Bitcoin hit ATH."),
        _make_article(99, category="Politics", summary="Election results in."),
    ]
    db = _make_db(articles=articles)

    mock_resp = MagicMock()
    mock_resp.json.return_value = _claude_response(_VALID_SEGMENTS_JSON)
    mock_resp.raise_for_status = lambda: None

    captured_payload = {}

    async def fake_post(url, headers, json, timeout):
        captured_payload.update(json)
        return mock_resp

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    assert "messages" in captured_payload
    prompt_text = captured_payload["messages"][0]["content"]
    assert "[42]" in prompt_text
    assert "[99]" in prompt_text
    assert "Bitcoin hit ATH." in prompt_text
    assert "Election results in." in prompt_text


# ---------------------------------------------------------------------------
# Prompt truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_truncated_when_too_long():
    """Oversized prompts are truncated to MAX_PROMPT_CHARS before sending."""
    long_summary = "A" * MAX_PROMPT_CHARS  # guarantees total prompt exceeds limit
    articles = [_make_article(1, summary=long_summary)]
    db = _make_db(articles=articles)

    mock_resp = MagicMock()
    mock_resp.json.return_value = _claude_response(_VALID_SEGMENTS_JSON)
    mock_resp.raise_for_status = lambda: None

    captured_payload = {}

    async def fake_post(url, headers, json, timeout):
        captured_payload.update(json)
        return mock_resp

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    sent_prompt = captured_payload["messages"][0]["content"]
    assert len(sent_prompt) <= MAX_PROMPT_CHARS


# ---------------------------------------------------------------------------
# HTTP error handling — graceful failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_status_error_returns_none():
    """Returns None (not an exception) when Claude API returns an HTTP error."""
    articles = [_make_article(1)]
    db = _make_db(articles=articles)

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "429 Too Many Requests",
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                response=httpx.Response(429),
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    assert result is None
    db.save_briefing.assert_not_called()


@pytest.mark.asyncio
async def test_connect_error_returns_none():
    """Returns None when the network connection fails."""
    articles = [_make_article(1)]
    db = _make_db(articles=articles)

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    assert result is None


# ---------------------------------------------------------------------------
# Empty API response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_content_returns_none():
    """Returns None when Claude returns no text content blocks."""
    articles = [_make_article(1)]
    db = _make_db(articles=articles)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"content": [], "model": CLAUDE_MODEL}
    mock_resp.raise_for_status = lambda: None

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    assert result is None
    db.save_briefing.assert_not_called()


# ---------------------------------------------------------------------------
# Malformed JSON response — graceful fallback to plain text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_stores_plain_text():
    """Malformed JSON from Claude is stored as plain text (no segments)."""
    articles = [_make_article(1)]
    db = _make_db(articles=articles)
    plain_response = "This is just a plain text briefing, not JSON."

    mock_resp = MagicMock()
    mock_resp.json.return_value = _claude_response(plain_response)
    mock_resp.raise_for_status = lambda: None

    with patch("src.summarizers.cloud.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await generate_daily_briefing_claude(db, anthropic_api_key="sk-test")

    # Plain text is returned; save_briefing is called with None segment map
    assert result == plain_response
    db.save_briefing.assert_called_once()
    call_args = db.save_briefing.call_args[0]
    # 4th positional arg is segment_map_json — should be None for plain text
    assert call_args[3] is None
