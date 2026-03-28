"""Tests for the crypto data fetcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.fetchers.crypto import (
    fetch_all_crypto,
    fetch_coingecko_prices,
    fetch_defi_llama_tvl,
    fetch_etherscan_gas,
)


@pytest.fixture
def mock_client():
    return AsyncMock()


# --- fetch_coingecko_prices ---


@pytest.mark.asyncio
async def test_coingecko_no_key(mock_client):
    """Skips gracefully when no API key."""
    results = await fetch_coingecko_prices(mock_client, "")
    assert results == []


@pytest.mark.asyncio
async def test_coingecko_success(mock_client):
    """Parses CoinGecko price response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "bitcoin": {"usd": 67432.50, "usd_24h_change": 2.35, "usd_market_cap": 1.3e12},
        "ethereum": {"usd": 3456.78, "usd_24h_change": -1.20, "usd_market_cap": 4.1e11},
        "solana": {"usd": 145.60, "usd_24h_change": 5.10, "usd_market_cap": 6.5e10},
    }
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_coingecko_prices(mock_client, "demo-key")
    assert len(results) == 3

    btc = next(r for r in results if r.symbol == "BTC")
    assert btc.value == 67432.50
    assert btc.change_pct == 2.35
    assert btc.data_type == "crypto_price"

    eth = next(r for r in results if r.symbol == "ETH")
    assert eth.value == 3456.78


@pytest.mark.asyncio
async def test_coingecko_http_error(mock_client):
    """Handles API errors gracefully."""
    import httpx

    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    results = await fetch_coingecko_prices(mock_client, "demo-key")
    assert results == []


# --- fetch_defi_llama_tvl ---


@pytest.mark.asyncio
async def test_defi_llama_tvl(mock_client):
    """Parses DeFi Llama historical TVL."""
    # First call: historical TVL
    tvl_resp = MagicMock()
    tvl_resp.json.return_value = [
        {"date": 1711411200, "tvl": 95_000_000_000},
        {"date": 1711497600, "tvl": 96_500_000_000},
    ]
    tvl_resp.raise_for_status = lambda: None

    # Second call: chains
    chains_resp = MagicMock()
    chains_resp.json.return_value = [
        {"name": "Ethereum", "tvl": 55_000_000_000},
        {"name": "Tron", "tvl": 12_000_000_000},
        {"name": "BSC", "tvl": 8_000_000_000},
        {"name": "Solana", "tvl": 6_000_000_000},
        {"name": "Arbitrum", "tvl": 4_000_000_000},
        {"name": "Other", "tvl": 1_000_000_000},
    ]
    chains_resp.raise_for_status = lambda: None

    mock_client.get = AsyncMock(side_effect=[tvl_resp, chains_resp])

    results = await fetch_defi_llama_tvl(mock_client)

    # 1 total TVL + 5 top chains
    assert len(results) == 6
    total = next(r for r in results if r.symbol == "DEFI_TVL")
    assert total.value == 96_500_000_000
    assert total.change_pct is not None  # Calculated from prev day

    chains = [r for r in results if r.data_type == "chain_tvl"]
    assert len(chains) == 5
    assert chains[0].symbol == "CHAIN_ETHEREUM"


@pytest.mark.asyncio
async def test_defi_llama_http_error(mock_client):
    """Handles DeFi Llama errors gracefully."""
    import httpx

    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    results = await fetch_defi_llama_tvl(mock_client)
    assert results == []


# --- fetch_etherscan_gas ---


@pytest.mark.asyncio
async def test_etherscan_no_key(mock_client):
    """Skips gracefully when no API key."""
    results = await fetch_etherscan_gas(mock_client, "")
    assert results == []


@pytest.mark.asyncio
async def test_etherscan_success(mock_client):
    """Parses gas price response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "1",
        "result": {
            "SafeGasPrice": "15",
            "ProposeGasPrice": "20",
            "FastGasPrice": "30",
        },
    }
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    results = await fetch_etherscan_gas(mock_client, "test-key")
    assert len(results) == 1
    assert results[0].symbol == "ETH_GAS"
    assert results[0].value == 20.0
    assert results[0].extra["unit"] == "gwei"


# --- fetch_all_crypto ---


@pytest.mark.asyncio
async def test_fetch_all_crypto_defi_llama_always_runs():
    """DeFi Llama runs even without any API keys."""
    import httpx

    # DeFi Llama doesn't need a key, so it should attempt a request
    # even when all keys are empty. We mock it to fail with a network error
    # to prove it tried.
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("no network"))

    results = await fetch_all_crypto(mock_client, "", "")
    # CoinGecko and Etherscan skip (no keys), DeFi Llama tries and fails
    assert results == []
    # DeFi Llama makes 2 calls (historical + chains), both fail
    assert mock_client.get.call_count == 2
