"""Tests for database helpers."""

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

        unsummarized = db.get_unsummarized_articles()
        assert len(unsummarized) == 1
        assert unsummarized[0]["title"] == "Article With Text"

        # Summarize it
        db.update_summary(unsummarized[0]["id"], "A summary.", "qwen2.5")
        assert len(db.get_unsummarized_articles()) == 0
    finally:
        db.close()
