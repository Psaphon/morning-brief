"""Tests for article deduplication."""

from src.processors.dedup import _normalize_title, deduplicate


def test_normalize_title_strips_prefixes():
    assert _normalize_title("Breaking: Big news today") == "big news today"
    assert _normalize_title("UPDATED: Something happened") == "something happened"


def test_normalize_title_removes_punctuation():
    assert _normalize_title("What's next? A look ahead.") == "whats next a look ahead"


def test_dedup_by_url_hash():
    articles = [
        {"url_hash": "abc123", "title": "Article 1", "content_hash": None},
        {"url_hash": "abc123", "title": "Article 1 copy", "content_hash": None},
        {"url_hash": "def456", "title": "Article 2", "content_hash": None},
    ]
    result = deduplicate(articles)
    assert len(result) == 2


def test_dedup_by_title():
    articles = [
        {"url_hash": "aaa", "title": "Big news today", "content_hash": None},
        {"url_hash": "bbb", "title": "Breaking: Big news today", "content_hash": None},
    ]
    result = deduplicate(articles)
    assert len(result) == 1


def test_dedup_by_content_hash():
    articles = [
        {"url_hash": "aaa", "title": "Title A", "content_hash": "same123"},
        {"url_hash": "bbb", "title": "Title B", "content_hash": "same123"},
    ]
    result = deduplicate(articles)
    assert len(result) == 1


def test_dedup_preserves_unique():
    articles = [
        {"url_hash": "aaa", "title": "Article A", "content_hash": "hash_a"},
        {"url_hash": "bbb", "title": "Article B", "content_hash": "hash_b"},
        {"url_hash": "ccc", "title": "Article C", "content_hash": "hash_c"},
    ]
    result = deduplicate(articles)
    assert len(result) == 3
