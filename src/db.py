"""SQLite database helpers for Morning Brief."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    url_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    author TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    full_text TEXT,
    summary TEXT,
    summary_model TEXT,
    content_hash TEXT,
    UNIQUE(url_hash)
);

CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL,
    value REAL,
    change_pct REAL,
    extra_json TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    status_code INTEGER,
    response_ms REAL,
    is_up INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT,
    date TEXT,
    medium TEXT,
    image_url TEXT,
    source_url TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_url_hash ON articles(url_hash);
CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
CREATE INDEX IF NOT EXISTS idx_articles_fetched ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_market_data_symbol ON market_data(symbol);
CREATE INDEX IF NOT EXISTS idx_health_checks_url ON health_checks(url);
"""


class Database:
    """Simple SQLite wrapper for Morning Brief data."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Open the database connection and ensure schema exists."""
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        logger.info("Database connected: %s", self.db_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def insert_article(
        self,
        url: str,
        url_hash: str,
        title: str,
        source: str,
        category: str,
        author: str | None = None,
        published_at: str | None = None,
        full_text: str | None = None,
        content_hash: str | None = None,
    ) -> bool:
        """Insert an article. Returns True if inserted, False if duplicate."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                """INSERT INTO articles
                   (url, url_hash, title, source, category, author,
                    published_at, fetched_at, full_text, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, url_hash, title, source, category, author,
                 published_at, now, full_text, content_hash),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def insert_market_data(
        self,
        symbol: str,
        data_type: str,
        value: float | None = None,
        change_pct: float | None = None,
        extra_json: str | None = None,
    ) -> None:
        """Insert a market data point."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO market_data
               (symbol, data_type, value, change_pct, extra_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, data_type, value, change_pct, extra_json, now),
        )
        self.conn.commit()

    def insert_health_check(
        self,
        url: str,
        name: str,
        status_code: int | None,
        response_ms: float | None,
        is_up: bool,
    ) -> None:
        """Insert a health check result."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO health_checks
               (url, name, status_code, response_ms, is_up, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (url, name, status_code, response_ms, int(is_up), now),
        )
        self.conn.commit()

    def get_unsummarized_articles(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get articles that haven't been summarized yet."""
        rows = self.conn.execute(
            """SELECT id, url, title, source, category, full_text
               FROM articles
               WHERE summary IS NULL AND full_text IS NOT NULL
               ORDER BY fetched_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_summary(self, article_id: int, summary: str, model: str) -> None:
        """Update an article with its summary."""
        self.conn.execute(
            "UPDATE articles SET summary = ?, summary_model = ? WHERE id = ?",
            (summary, model, article_id),
        )
        self.conn.commit()

    def get_todays_articles(self) -> list[dict[str, Any]]:
        """Get all articles fetched today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            """SELECT * FROM articles
               WHERE fetched_at >= ?
               ORDER BY category, fetched_at DESC""",
            (today,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_market_data(self) -> list[dict[str, Any]]:
        """Get the most recent market data for each symbol."""
        rows = self.conn.execute(
            """SELECT m.* FROM market_data m
               INNER JOIN (
                   SELECT symbol, MAX(fetched_at) as max_fetched
                   FROM market_data GROUP BY symbol
               ) latest ON m.symbol = latest.symbol
                       AND m.fetched_at = latest.max_fetched
               ORDER BY m.data_type, m.symbol""",
        ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_health_checks(self) -> list[dict[str, Any]]:
        """Get the most recent health check for each endpoint."""
        rows = self.conn.execute(
            """SELECT h.* FROM health_checks h
               INNER JOIN (
                   SELECT url, MAX(checked_at) as max_checked
                   FROM health_checks GROUP BY url
               ) latest ON h.url = latest.url
                       AND h.checked_at = latest.max_checked
               ORDER BY h.name""",
        ).fetchall()
        return [dict(row) for row in rows]
