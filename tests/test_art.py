"""Tests for daily artwork fetcher."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.fetchers.art import (
    _date_seed,
    _parse_artwork,
    _pick_object_id,
    fetch_daily_artwork,
)

# --- _date_seed ---


def test_date_seed_deterministic():
    """Same date + salt always produces the same seed."""
    s1 = _date_seed("2026-03-27", "0_0")
    s2 = _date_seed("2026-03-27", "0_0")
    assert s1 == s2


def test_date_seed_varies_by_date():
    """Different dates produce different seeds."""
    s1 = _date_seed("2026-03-27")
    s2 = _date_seed("2026-03-28")
    assert s1 != s2


def test_date_seed_varies_by_salt():
    """Different salts produce different seeds."""
    s1 = _date_seed("2026-03-27", "0_0")
    s2 = _date_seed("2026-03-27", "1_0")
    assert s1 != s2


# --- _pick_object_id ---


def test_pick_object_id_deterministic():
    """Same inputs always pick the same object."""
    ids = [100, 200, 300, 400, 500]
    p1 = _pick_object_id(ids, "2026-03-27")
    p2 = _pick_object_id(ids, "2026-03-27")
    assert p1 == p2
    assert p1 in ids


def test_pick_object_id_varies_by_date():
    """Different dates may pick different objects (probabilistic but near-certain with many IDs)."""
    ids = list(range(1, 10001))
    p1 = _pick_object_id(ids, "2026-03-27")
    p2 = _pick_object_id(ids, "2026-03-28")
    # With 10K IDs, collision probability is ~0.01%
    assert p1 != p2


# --- _parse_artwork ---


def test_parse_artwork_full():
    """All fields populated from API response."""
    data = {
        "title": "Water Lilies",
        "artistDisplayName": "Claude Monet",
        "objectDate": "1906",
        "medium": "Oil on canvas",
        "primaryImageSmall": "https://example.com/small.jpg",
        "primaryImage": "https://example.com/large.jpg",
        "objectURL": "https://www.metmuseum.org/art/collection/search/12345",
        "department": "European Paintings",
    }
    result = _parse_artwork(data)
    assert result["title"] == "Water Lilies"
    assert result["artist"] == "Claude Monet"
    assert result["date"] == "1906"
    assert result["medium"] == "Oil on canvas"
    assert result["image_url"] == "https://example.com/small.jpg"
    assert result["source_url"].startswith("https://www.metmuseum.org")


def test_parse_artwork_missing_fields():
    """Missing fields get sensible defaults."""
    result = _parse_artwork({})
    assert result["title"] == "Untitled"
    assert result["artist"] == "Unknown"
    assert result["date"] == "Unknown date"
    assert result["medium"] == "Unknown medium"


# --- fetch_daily_artwork ---


def _mock_response(json_data, status_code=200):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_fetch_daily_artwork_success():
    """Fetches two artworks (highlight + ceramics) on success."""
    search_result = {"objectIDs": [1001, 1002, 1003]}
    object_result = {
        "title": "Blue Vase",
        "artistDisplayName": "Test Artist",
        "objectDate": "1900",
        "medium": "Ceramic",
        "primaryImageSmall": "https://example.com/img.jpg",
        "objectURL": "https://www.metmuseum.org/art/collection/search/1001",
        "department": "Asian Art",
    }

    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[
            _mock_response(search_result),  # search: painting
            _mock_response(object_result),  # object detail
            _mock_response(search_result),  # search: ceramics
            _mock_response(object_result),  # object detail
        ]
    )

    date = datetime(2026, 3, 27, tzinfo=timezone.utc)
    artworks = await fetch_daily_artwork(client, date=date)
    assert len(artworks) == 2
    assert artworks[0]["title"] == "Blue Vase"
    assert "search_category" in artworks[0]


@pytest.mark.asyncio
async def test_fetch_daily_artwork_no_results():
    """Returns empty list when search yields no results."""
    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[
            _mock_response({"objectIDs": None}),  # painting: no results
            _mock_response({"objectIDs": None}),  # ceramics: no results
        ]
    )

    artworks = await fetch_daily_artwork(client)
    assert artworks == []


@pytest.mark.asyncio
async def test_fetch_daily_artwork_retries_on_no_image():
    """Retries picking when object has no image."""
    search_result = {"objectIDs": [1, 2, 3, 4, 5]}
    no_image_obj = {
        "title": "No Image",
        "primaryImageSmall": "",
    }
    good_obj = {
        "title": "Good Art",
        "artistDisplayName": "Artist",
        "primaryImageSmall": "https://example.com/img.jpg",
        "objectURL": "https://example.com",
    }

    client = AsyncMock()
    client.get = AsyncMock(
        side_effect=[
            _mock_response(search_result),  # search: painting
            _mock_response(no_image_obj),  # attempt 1: no image → returns None
            _mock_response(good_obj),  # attempt 2: good
            _mock_response(search_result),  # search: ceramics
            _mock_response(good_obj),  # attempt 1: good
        ]
    )

    artworks = await fetch_daily_artwork(client)
    assert len(artworks) == 2
    assert artworks[0]["title"] == "Good Art"
