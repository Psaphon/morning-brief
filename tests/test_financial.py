"""Tests for the financial data fetcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.fetchers.financial import (
    MarketDataPoint,
    fetch_all_financial,
    fetch_finnhub_quotes,
    fetch_fred_series,
)


@pytest.fixture
def mock_client():
    return AsyncMock()


# --- fetch_finnhub_quotes ---


@pytest.mark.asyncio
async def test_finnhub_no_key(mock_client):
    """Skips gracefully when no API key."""
    results = await fetch_finnhub_quotes(mock_client, "")
    assert results == []
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_finnhub_success(mock_client):
    """Parses stock quote response correctly."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "c": 450.25,
        "d": 3.50,
        "dp": 0.78,
        "h": 452.00,
        "l": 447.10,
        "o": 448.00,
        "pc": 446.75,
    }
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_finnhub_quotes(mock_client, "test-key")
    assert len(results) == 3  # SPY, QQQ, DIA
    assert all(isinstance(r, MarketDataPoint) for r in results)
    assert results[0].data_type == "stock_quote"
    assert results[0].value == 450.25
    assert results[0].change_pct == 0.78


@pytest.mark.asyncio
async def test_finnhub_market_closed(mock_client):
    """Handles market-closed response (zero values)."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0}
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_finnhub_quotes(mock_client, "test-key")
    assert results == []


@pytest.mark.asyncio
async def test_finnhub_http_error(mock_client):
    """Handles HTTP errors gracefully."""
    import httpx

    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    results = await fetch_finnhub_quotes(mock_client, "test-key")
    assert results == []


# --- fetch_fred_series ---


@pytest.mark.asyncio
async def test_fred_no_key(mock_client):
    """Skips gracefully when no API key."""
    results = await fetch_fred_series(mock_client, "")
    assert results == []


@pytest.mark.asyncio
async def test_fred_success(mock_client):
    """Parses FRED observation data correctly."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "observations": [
            {"date": "2026-03-27", "value": "4.25"},
            {"date": "2026-03-26", "value": "4.20"},
        ]
    }
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_fred_series(mock_client, "test-key")
    assert len(results) == 3  # DGS2, DGS10, VIXCLS
    assert results[0].data_type == "fred_series"
    assert results[0].value == 4.25
    assert results[0].change_pct is not None


@pytest.mark.asyncio
async def test_fred_pending_value(mock_client):
    """Handles FRED's '.' pending value marker."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"observations": [{"date": "2026-03-27", "value": "."}]}
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_fred_series(mock_client, "test-key")
    assert results == []


# --- fetch_all_financial ---


@pytest.mark.asyncio
async def test_fetch_all_no_keys():
    """Returns empty when no API keys configured."""
    async with AsyncMock() as mock_client:
        results = await fetch_all_financial(mock_client, "", "")
    assert results == []
