"""Daily artwork fetcher — Met Museum Open Access API.

Selects a deterministic-random artwork each day using the date as a seed.
Includes a ceramics search alongside general art to match user interests.
No API key required — the Met's API is fully open.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MET_API_BASE = "https://collectionapi.metmuseum.org/public/collection/v1"

# Search queries — one general, one ceramics-focused
SEARCH_QUERIES = [
    {"q": "painting", "hasImages": "true", "isHighlight": "true"},
    {"q": "ceramics", "hasImages": "true"},
]


async def _search_objects(client: httpx.AsyncClient, params: dict) -> list[int]:
    """Search the Met collection and return object IDs."""
    try:
        resp = await client.get(f"{MET_API_BASE}/search", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("objectIDs") or []
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Met search failed for %s: %s", params.get("q"), e)
        return []


async def _get_object(client: httpx.AsyncClient, object_id: int) -> dict[str, Any] | None:
    """Fetch a single object's details."""
    try:
        resp = await client.get(f"{MET_API_BASE}/objects/{object_id}", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Only return if it has a usable image
        if data.get("primaryImageSmall"):
            return data
        return None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Met object %d fetch failed: %s", object_id, e)
        return None


def _date_seed(date_str: str, salt: str = "") -> int:
    """Generate a deterministic seed from a date string."""
    h = hashlib.md5(f"{date_str}{salt}".encode()).hexdigest()
    return int(h, 16)


def _pick_object_id(object_ids: list[int], date_str: str, salt: str = "") -> int:
    """Pick a deterministic-random object ID based on date."""
    seed = _date_seed(date_str, salt)
    return object_ids[seed % len(object_ids)]


def _parse_artwork(data: dict[str, Any]) -> dict[str, Any]:
    """Extract display fields from a Met API object response."""
    return {
        "title": data.get("title", "Untitled"),
        "artist": data.get("artistDisplayName") or "Unknown",
        "date": data.get("objectDate") or "Unknown date",
        "medium": data.get("medium") or "Unknown medium",
        "image_url": data.get("primaryImageSmall") or data.get("primaryImage", ""),
        "source_url": data.get("objectURL", ""),
        "department": data.get("department", ""),
    }


async def fetch_daily_artwork(
    client: httpx.AsyncClient,
    date: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch today's artwork selections — one highlight and one ceramics piece.

    Uses the date as a seed so the same day always returns the same artworks.
    Returns a list of artwork dicts (typically 2, fewer if a search fails).
    """
    if date is None:
        date = datetime.now(timezone.utc)
    date_str = date.strftime("%Y-%m-%d")

    artworks = []
    for i, params in enumerate(SEARCH_QUERIES):
        query_name = params.get("q", "art")
        object_ids = await _search_objects(client, params)
        if not object_ids:
            logger.warning("No results for '%s' search", query_name)
            continue

        # Try up to 3 picks in case an object lacks images
        for attempt in range(3):
            pick_id = _pick_object_id(object_ids, date_str, salt=f"{i}_{attempt}")
            obj = await _get_object(client, pick_id)
            if obj:
                artwork = _parse_artwork(obj)
                artwork["search_category"] = query_name
                artworks.append(artwork)
                logger.info(
                    "Daily %s artwork: '%s' by %s",
                    query_name,
                    artwork["title"],
                    artwork["artist"],
                )
                break
        else:
            logger.warning(
                "Could not find artwork with image for '%s' after 3 attempts", query_name
            )

    return artworks
