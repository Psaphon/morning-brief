"""Endpoint health checker for Morning Brief.

Checks HTTP endpoints for availability, response time, and status.
Originally from the Pulse Monitor concept — merged into Morning Brief
as another data source for the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 10.0


@dataclass
class HealthResult:
    """Result of a single health check."""

    url: str
    name: str
    status_code: int | None
    response_ms: float | None
    is_up: bool
    error: str | None = None


# Default endpoints to check — can be overridden via config
DEFAULT_ENDPOINTS: list[dict[str, str]] = [
    {"url": "https://github.com", "name": "GitHub"},
]


async def check_endpoint(
    client: httpx.AsyncClient,
    url: str,
    name: str,
) -> HealthResult:
    """Check a single HTTP endpoint."""
    start = time.monotonic()
    try:
        response = await client.get(url, timeout=CHECK_TIMEOUT)
        elapsed_ms = (time.monotonic() - start) * 1000
        is_up = 200 <= response.status_code < 400

        logger.info(
            "Health check %s: %d (%s, %.0fms)",
            name,
            response.status_code,
            "UP" if is_up else "DOWN",
            elapsed_ms,
        )
        return HealthResult(
            url=url,
            name=name,
            status_code=response.status_code,
            response_ms=round(elapsed_ms, 1),
            is_up=is_up,
        )
    except httpx.HTTPError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.warning("Health check %s: FAILED (%.0fms) — %s", name, elapsed_ms, e)
        return HealthResult(
            url=url,
            name=name,
            status_code=None,
            response_ms=round(elapsed_ms, 1),
            is_up=False,
            error=str(e),
        )


async def check_all_endpoints(
    endpoints: list[dict[str, str]] | None = None,
) -> list[HealthResult]:
    """Check all configured endpoints concurrently."""
    endpoints = endpoints or DEFAULT_ENDPOINTS
    if not endpoints:
        return []

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "MorningBrief-HealthCheck/0.1"},
    ) as client:
        tasks = [check_endpoint(client, ep["url"], ep["name"]) for ep in endpoints]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    health_results: list[HealthResult] = []
    for result in results:
        if isinstance(result, HealthResult):
            health_results.append(result)
        elif isinstance(result, Exception):
            logger.warning("Health check error: %s", result)

    up_count = sum(1 for r in health_results if r.is_up)
    logger.info(
        "Health checks: %d/%d endpoints up",
        up_count,
        len(health_results),
    )
    return health_results
