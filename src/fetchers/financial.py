"""Financial data fetcher — Finnhub and FRED.

Fetches market data from free-tier APIs:
- Finnhub: Stock quotes (SPY, QQQ, DIA), market status
- FRED: Treasury yields (DGS2, DGS10), VIX (VIXCLS)

All APIs require free API keys. Gracefully skips if keys are not configured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15.0

# Indexes to track
STOCK_SYMBOLS = ["SPY", "QQQ", "DIA"]

# FRED series IDs
FRED_SERIES = {
    "DGS2": "2-Year Treasury Yield",
    "DGS10": "10-Year Treasury Yield",
    "VIXCLS": "VIX (Volatility Index)",
}


@dataclass
class MarketDataPoint:
    """A single market data observation."""

    symbol: str
    data_type: str
    value: float | None
    change_pct: float | None = None
    extra: dict | None = None


async def fetch_finnhub_quotes(
    client: httpx.AsyncClient,
    api_key: str,
) -> list[MarketDataPoint]:
    """Fetch stock quotes from Finnhub.

    Finnhub free tier: 60 req/min, no daily cap.
    Endpoint: GET /api/v1/quote?symbol=X&token=KEY
    Returns: c (current), d (change), dp (change %), h (high), l (low), o (open), pc (prev close)
    """
    if not api_key:
        logger.info("Finnhub API key not configured, skipping stock quotes")
        return []

    results: list[MarketDataPoint] = []

    for symbol in STOCK_SYMBOLS:
        try:
            resp = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": symbol, "token": api_key},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            current = data.get("c")
            change_pct = data.get("dp")

            if current is not None and current != 0:
                results.append(
                    MarketDataPoint(
                        symbol=symbol,
                        data_type="stock_quote",
                        value=current,
                        change_pct=change_pct,
                        extra={
                            "open": data.get("o"),
                            "high": data.get("h"),
                            "low": data.get("l"),
                            "prev_close": data.get("pc"),
                            "change": data.get("d"),
                        },
                    )
                )
                logger.debug(
                    "%s: $%.2f (%+.2f%%)",
                    symbol,
                    current,
                    change_pct or 0,
                )
            else:
                logger.warning("Finnhub returned no data for %s (market may be closed)", symbol)

        except httpx.HTTPError as e:
            logger.warning("Failed to fetch %s from Finnhub: %s", symbol, e)

    return results


async def fetch_fred_series(
    client: httpx.AsyncClient,
    api_key: str,
) -> list[MarketDataPoint]:
    """Fetch economic data from FRED (Federal Reserve Economic Data).

    FRED free tier: 120 req/min, no daily cap.
    Endpoint: GET /fred/series/observations?series_id=X&api_key=KEY&sort_order=desc&limit=1
    """
    if not api_key:
        logger.info("FRED API key not configured, skipping economic data")
        return []

    results: list[MarketDataPoint] = []

    for series_id, description in FRED_SERIES.items():
        try:
            resp = await client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 2,  # Get last 2 for change calculation
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            observations = data.get("observations", [])

            if not observations:
                logger.warning("No FRED data for %s", series_id)
                continue

            # Latest observation
            latest = observations[0]
            value_str = latest.get("value", ".")

            if value_str == ".":
                logger.debug("FRED %s: value pending (reported as '.')", series_id)
                continue

            value = float(value_str)

            # Calculate change from previous observation
            change_pct = None
            if len(observations) > 1:
                prev_str = observations[1].get("value", ".")
                if prev_str != ".":
                    prev_value = float(prev_str)
                    if prev_value != 0:
                        change_pct = ((value - prev_value) / prev_value) * 100

            results.append(
                MarketDataPoint(
                    symbol=series_id,
                    data_type="fred_series",
                    value=value,
                    change_pct=change_pct,
                    extra={
                        "description": description,
                        "date": latest.get("date"),
                    },
                )
            )
            logger.debug("%s (%s): %.3f", series_id, description, value)

        except httpx.HTTPError as e:
            logger.warning("Failed to fetch %s from FRED: %s", series_id, e)
        except (ValueError, KeyError) as e:
            logger.warning("Failed to parse FRED data for %s: %s", series_id, e)

    return results


async def fetch_all_financial(
    client: httpx.AsyncClient,
    finnhub_key: str = "",
    fred_key: str = "",
) -> list[MarketDataPoint]:
    """Fetch all financial data from configured sources.

    Runs Finnhub and FRED fetches concurrently.
    """
    import asyncio

    tasks = [
        fetch_finnhub_quotes(client, finnhub_key),
        fetch_fred_series(client, fred_key),
    ]

    results_lists = await asyncio.gather(*tasks, return_exceptions=True)

    all_results: list[MarketDataPoint] = []
    for result in results_lists:
        if isinstance(result, list):
            all_results.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Financial fetch error: %s", result)

    logger.info("Fetched %d financial data points", len(all_results))
    return all_results
