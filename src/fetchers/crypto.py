"""Crypto data fetcher — CoinGecko, DeFi Llama, Etherscan.

Fetches cryptocurrency and DeFi market data:
- CoinGecko: Top crypto prices (BTC, ETH + watchlist) — requires free demo key
- DeFi Llama: DeFi TVL snapshot — no key required
- Etherscan: ETH gas prices — requires free key

DeFi Llama is always available (no key). Others degrade gracefully without keys.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0

# Coins to track (CoinGecko IDs)
WATCHLIST = ["bitcoin", "ethereum", "solana"]


@dataclass
class CryptoDataPoint:
    """A single crypto data observation."""

    symbol: str
    data_type: str
    value: float | None
    change_pct: float | None = None
    extra: dict | None = None


async def fetch_coingecko_prices(
    client: httpx.AsyncClient,
    api_key: str = "",
) -> list[CryptoDataPoint]:
    """Fetch crypto prices from CoinGecko.

    CoinGecko demo tier: 10,000 req/month, 30/min.
    Endpoint: GET /api/v3/simple/price?ids=X&vs_currencies=usd&include_24hr_change=true
    Demo key goes in x-cg-demo-key header.
    """
    if not api_key:
        logger.info("CoinGecko API key not configured, skipping crypto prices")
        return []

    results: list[CryptoDataPoint] = []

    try:
        ids = ",".join(WATCHLIST)
        resp = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": ids,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_market_cap": "true",
            },
            headers={"x-cg-demo-key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        for coin_id in WATCHLIST:
            coin_data = data.get(coin_id)
            if not coin_data:
                continue

            price = coin_data.get("usd")
            change_24h = coin_data.get("usd_24h_change")
            market_cap = coin_data.get("usd_market_cap")

            ticker = coin_id[:3].upper()  # bitcoin -> BIT -> BTC convention below
            ticker_map = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
            ticker = ticker_map.get(coin_id, ticker)

            results.append(
                CryptoDataPoint(
                    symbol=ticker,
                    data_type="crypto_price",
                    value=price,
                    change_pct=change_24h,
                    extra={"market_cap": market_cap, "coin_id": coin_id},
                )
            )
            logger.debug(
                "%s: $%.2f (%+.2f%%)",
                ticker,
                price or 0,
                change_24h or 0,
            )

    except httpx.HTTPError as e:
        logger.warning("Failed to fetch CoinGecko prices: %s", e)

    return results


async def fetch_defi_llama_tvl(
    client: httpx.AsyncClient,
) -> list[CryptoDataPoint]:
    """Fetch DeFi TVL from DeFi Llama.

    DeFi Llama: unlimited, no key required.
    Endpoint: GET https://api.llama.fi/v2/historicalChainTvl
    Returns array of {date, tvl} for total DeFi TVL.
    Also: GET /v2/chains for per-chain breakdown.
    """
    results: list[CryptoDataPoint] = []

    # Total DeFi TVL
    try:
        resp = await client.get(
            "https://api.llama.fi/v2/historicalChainTvl",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if data and isinstance(data, list):
            latest = data[-1]
            tvl = latest.get("tvl")
            if tvl is not None:
                # Calculate change from previous day
                change_pct = None
                if len(data) >= 2:
                    prev_tvl = data[-2].get("tvl")
                    if prev_tvl and prev_tvl != 0:
                        change_pct = ((tvl - prev_tvl) / prev_tvl) * 100

                results.append(
                    CryptoDataPoint(
                        symbol="DEFI_TVL",
                        data_type="defi_tvl",
                        value=tvl,
                        change_pct=change_pct,
                        extra={"date": latest.get("date")},
                    )
                )
                logger.debug("Total DeFi TVL: $%.2fB", tvl / 1e9)

    except httpx.HTTPError as e:
        logger.warning("Failed to fetch DeFi Llama TVL: %s", e)

    # Top chains by TVL
    try:
        resp = await client.get(
            "https://api.llama.fi/v2/chains",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        chains = resp.json()

        # Top 5 chains
        if isinstance(chains, list):
            sorted_chains = sorted(chains, key=lambda c: c.get("tvl", 0), reverse=True)
            for chain in sorted_chains[:5]:
                name = chain.get("name", "Unknown")
                tvl = chain.get("tvl")
                if tvl is not None:
                    results.append(
                        CryptoDataPoint(
                            symbol=f"CHAIN_{name.upper()}",
                            data_type="chain_tvl",
                            value=tvl,
                            extra={"chain": name},
                        )
                    )

    except httpx.HTTPError as e:
        logger.warning("Failed to fetch DeFi Llama chains: %s", e)

    return results


async def fetch_etherscan_gas(
    client: httpx.AsyncClient,
    api_key: str = "",
) -> list[CryptoDataPoint]:
    """Fetch ETH gas prices from Etherscan.

    Etherscan free tier: 100,000 req/day.
    Endpoint: GET /api?module=gastracker&action=gasoracle&apikey=KEY
    Returns: SafeGasPrice, ProposeGasPrice, FastGasPrice (in Gwei)
    """
    if not api_key:
        logger.info("Etherscan API key not configured, skipping gas prices")
        return []

    results: list[CryptoDataPoint] = []

    try:
        resp = await client.get(
            "https://api.etherscan.io/api",
            params={
                "module": "gastracker",
                "action": "gasoracle",
                "apikey": api_key,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "1":
            result = data.get("result", {})
            safe = result.get("SafeGasPrice")
            propose = result.get("ProposeGasPrice")
            fast = result.get("FastGasPrice")

            if propose is not None:
                results.append(
                    CryptoDataPoint(
                        symbol="ETH_GAS",
                        data_type="gas_price",
                        value=float(propose),
                        extra={
                            "safe": safe,
                            "propose": propose,
                            "fast": fast,
                            "unit": "gwei",
                        },
                    )
                )
                logger.debug("ETH Gas: %s/%s/%s gwei (safe/std/fast)", safe, propose, fast)
        else:
            logger.warning("Etherscan gas API error: %s", data.get("message"))

    except httpx.HTTPError as e:
        logger.warning("Failed to fetch Etherscan gas: %s", e)

    return results


async def fetch_all_crypto(
    client: httpx.AsyncClient,
    coingecko_key: str = "",
    etherscan_key: str = "",
) -> list[CryptoDataPoint]:
    """Fetch all crypto data from configured sources.

    DeFi Llama always runs (no key needed). Others require keys.
    """
    import asyncio

    tasks = [
        fetch_coingecko_prices(client, coingecko_key),
        fetch_defi_llama_tvl(client),
        fetch_etherscan_gas(client, etherscan_key),
    ]

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: list[CryptoDataPoint] = []
    for result in results_lists:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Crypto fetch error: %s", result)

    logger.info("Fetched %d crypto data points", len(all_results))
    return all_results
