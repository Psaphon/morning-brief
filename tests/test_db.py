"""Tests for database helpers."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import Database


def test_database_connect_creates_schema(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        # Verify tables exist
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "articles" in table_names
        assert "market_data" in table_names
        assert "health_checks" in table_names
        assert "artworks" in table_names
    finally:
        db.close()


def test_insert_article(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        inserted = db.insert_article(
            url="https://example.com/article",
            url_hash="abc123",
            title="Test Article",
            source="Test Source",
            category="Test",
            full_text="This is the article body.",
        )
        assert inserted is True

        # Duplicate should return False
        duplicate = db.insert_article(
            url="https://example.com/article",
            url_hash="abc123",
            title="Test Article",
            source="Test Source",
            category="Test",
        )
        assert duplicate is False
    finally:
        db.close()


def test_insert_and_get_health_check(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.insert_health_check(
            url="https://example.com",
            name="Example",
            status_code=200,
            response_ms=142.5,
            is_up=True,
        )
        results = db.get_latest_health_checks()
        assert len(results) == 1
        assert results[0]["name"] == "Example"
        assert results[0]["is_up"] == 1
    finally:
        db.close()


def _insert_article_with_timestamps(
    db: Database,
    url_hash: str,
    fetched_at: str,
    last_seen_at: str | None = None,
) -> None:
    """Helper to insert an article with explicit timestamps for retention tests."""
    db.conn.execute(
        """INSERT INTO articles
           (url, url_hash, title, source, category, fetched_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"https://example.com/{url_hash}",
            url_hash,
            "Title",
            "Src",
            "Cat",
            fetched_at,
            last_seen_at,
        ),
    )
    db.conn.commit()


def test_cleanup_deletes_old_articles(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        _insert_article_with_timestamps(db, "old1", old, old)
        count = db.cleanup_old_articles(max_age_days=2)
        assert count == 1
        rows = db.conn.execute("SELECT * FROM articles WHERE url_hash = 'old1'").fetchall()
        assert len(rows) == 0
    finally:
        db.close()


def test_cleanup_preserves_recent_articles(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        recent = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        _insert_article_with_timestamps(db, "recent1", recent, recent)
        count = db.cleanup_old_articles(max_age_days=2)
        assert count == 0
        rows = db.conn.execute("SELECT * FROM articles WHERE url_hash = 'recent1'").fetchall()
        assert len(rows) == 1
    finally:
        db.close()


def test_cleanup_does_not_touch_daily_briefings(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.save_briefing("2020-01-01", "Old briefing content", "qwen2.5")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        _insert_article_with_timestamps(db, "oldarticle", old, old)
        db.cleanup_old_articles(max_age_days=2)
        briefing = db.get_briefing("2020-01-01")
        assert briefing is not None
        assert briefing["content"] == "Old briefing content"
    finally:
        db.close()


def test_refetch_updates_last_seen_at(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        # Insert article with old last_seen_at via direct SQL
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        _insert_article_with_timestamps(db, "reseen1", old, old)

        # Re-insert via insert_article — should update last_seen_at
        result = db.insert_article(
            url="https://example.com/reseen1",
            url_hash="reseen1",
            title="Title",
            source="Src",
            category="Cat",
        )
        assert result is False  # existing article

        row = db.conn.execute(
            "SELECT last_seen_at FROM articles WHERE url_hash = 'reseen1'"
        ).fetchone()
        updated_at = datetime.fromisoformat(row["last_seen_at"])
        assert updated_at > datetime.now(timezone.utc) - timedelta(seconds=5)
    finally:
        db.close()


def test_updated_last_seen_survives_cleanup(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        # Article has old fetched_at but fresh last_seen_at (re-fetched today)
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_article_with_timestamps(db, "survivor1", old, recent)

        count = db.cleanup_old_articles(max_age_days=2)
        assert count == 0
        rows = db.conn.execute("SELECT * FROM articles WHERE url_hash = 'survivor1'").fetchall()
        assert len(rows) == 1
    finally:
        db.close()


def test_unsummarized_articles(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.insert_article(
            url="https://example.com/1",
            url_hash="hash1",
            title="Article With Text",
            source="Src",
            category="Cat",
            full_text="Some content here.",
        )
        db.insert_article(
            url="https://example.com/2",
            url_hash="hash2",
            title="Article Without Text",
            source="Src",
            category="Cat",
            full_text=None,
        )

        # Only articles with full_text and no summary are returned
        unsummarized = db.get_unsummarized_articles()
        assert len(unsummarized) == 1
        assert unsummarized[0]["title"] == "Article With Text"

        # Summarize it — should no longer appear
        db.update_summary(unsummarized[0]["id"], "A summary.", "qwen2.5")
        assert len(db.get_unsummarized_articles()) == 0
    finally:
        db.close()


def test_briefing_metadata_stored_and_retrieved(tmp_path: Path):
    """save_briefing persists briefing_metadata; get_briefing returns it parsed."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        metadata = {
            "article_count": 42,
            "categories_covered": ["tech", "politics"],
            "top_sources": ["Reuters", "BBC"],
        }
        db.save_briefing("2026-01-01", "Briefing content", "qwen2.5", briefing_metadata=metadata)
        row = db.conn.execute(
            "SELECT briefing_metadata FROM daily_briefings WHERE date = '2026-01-01'"
        ).fetchone()
        assert row is not None
        parsed = json.loads(row["briefing_metadata"])
        assert parsed["article_count"] == 42
        assert "tech" in parsed["categories_covered"]
        assert "Reuters" in parsed["top_sources"]
    finally:
        db.close()


def test_get_briefing_history_returns_recent(tmp_path: Path):
    """get_briefing_history returns briefings within the requested window."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d")

        db.save_briefing(today, "Today's briefing", "qwen2.5")
        db.save_briefing(yesterday, "Yesterday's briefing", "qwen2.5")
        db.save_briefing(old, "Old briefing", "qwen2.5")

        history = db.get_briefing_history(days=30)
        dates = [e["date"] for e in history]
        assert today in dates
        assert yesterday in dates
        assert old not in dates
    finally:
        db.close()


def test_get_briefing_history_newest_first(tmp_path: Path):
    """get_briefing_history returns entries ordered newest first."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        db.save_briefing(yesterday, "Yesterday", "qwen2.5")
        db.save_briefing(today, "Today", "qwen2.5")

        history = db.get_briefing_history(days=7)
        assert len(history) >= 2
        assert history[0]["date"] >= history[1]["date"]
    finally:
        db.close()


def test_get_briefing_history_includes_parsed_metadata(tmp_path: Path):
    """get_briefing_history parses briefing_metadata JSON into a dict."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        metadata = {
            "article_count": 10,
            "categories_covered": ["crypto"],
            "top_sources": ["CoinDesk"],
        }
        db.save_briefing(today, "Content", "qwen2.5", briefing_metadata=metadata)

        history = db.get_briefing_history(days=1)
        assert len(history) == 1
        assert history[0]["briefing_metadata"]["article_count"] == 10
        assert history[0]["briefing_metadata"]["categories_covered"] == ["crypto"]
    finally:
        db.close()


def test_briefing_archive_survives_article_cleanup(tmp_path: Path):
    """daily_briefings rows are never deleted by cleanup_old_articles."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        # Insert a very old briefing
        db.save_briefing("2020-06-01", "Ancient briefing", "qwen2.5")

        # Insert and clean up old articles
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        _insert_article_with_timestamps(db, "stale1", old, old)
        deleted = db.cleanup_old_articles(max_age_days=2)
        assert deleted == 1

        # Briefing must still be present
        history = db.get_briefing_history(days=99999)
        dates = [e["date"] for e in history]
        assert "2020-06-01" in dates
    finally:
        db.close()


def test_briefing_archive_no_metadata(tmp_path: Path):
    """Briefings saved without metadata return None for briefing_metadata."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.save_briefing(today, "No meta", "qwen2.5")

        history = db.get_briefing_history(days=1)
        assert len(history) == 1
        assert history[0]["briefing_metadata"] is None
    finally:
        db.close()


def test_unsummarized_articles_ordered_by_score(tmp_path: Path):
    """get_unsummarized_articles returns articles ordered by score descending."""
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        db.insert_article(
            url="https://example.com/low",
            url_hash="low",
            title="Low Score",
            source="Src",
            category="Cat",
            full_text="Body text here.",
        )
        db.insert_article(
            url="https://example.com/high",
            url_hash="high",
            title="High Score",
            source="Src",
            category="Cat",
            full_text="Body text here.",
        )
        low_id = db.conn.execute("SELECT id FROM articles WHERE url_hash='low'").fetchone()["id"]
        high_id = db.conn.execute("SELECT id FROM articles WHERE url_hash='high'").fetchone()["id"]
        db.update_score(low_id, 0.2)
        db.update_score(high_id, 0.9)

        result = db.get_unsummarized_articles()
        assert len(result) == 2
        assert result[0]["title"] == "High Score"
        assert result[1]["title"] == "Low Score"
    finally:
        db.close()
