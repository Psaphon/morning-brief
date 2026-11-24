"""SQLite database helpers for Morning Brief."""

from __future__ import annotations

import json
import logging
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .timeutil import UTC_Z_FORMAT, normalise_timestamp

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
    score REAL,
    last_seen_at TEXT,
    duplicate_of INTEGER,
    UNIQUE(url_hash)
);

CREATE TABLE IF NOT EXISTS extractions (
    article_id INTEGER PRIMARY KEY REFERENCES articles(id),
    json TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS daily_briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT UNIQUE NOT NULL,
    content TEXT NOT NULL,
    model TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    segment_map TEXT,
    briefing_metadata TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Filings lane (SIGNAL-SCHEMA section 12). Deliberately outside the 2-day article retention.
CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT UNIQUE NOT NULL,
    cik TEXT NOT NULL,
    ticker TEXT NOT NULL,
    form TEXT NOT NULL,
    items TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'edgar',
    fetched_at TEXT NOT NULL,
    primary_doc_url TEXT,
    primary_html BLOB,
    exhibits BLOB,
    primary_text TEXT,
    exhibit_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker, accepted_at);

-- Verified earnings numbers and code-computed direction (one row per 2.02 filing).
-- Values are base units (dollars, dollars per share). The model's raw output is not kept.
CREATE TABLE IF NOT EXISTS filing_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession TEXT UNIQUE NOT NULL REFERENCES filings(accession),
    status TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'failed')),
    period_label TEXT,
    eps_diluted_current REAL,
    eps_diluted_prior REAL,
    revenue_current REAL,
    revenue_prior REAL,
    guidance_metric TEXT,
    guidance_period_label TEXT,
    guidance_low REAL,
    guidance_high REAL,
    guidance_basis TEXT,
    guidance_base REAL,
    guidance_low_pct REAL,
    guidance_high_pct REAL,
    unverified_count INTEGER NOT NULL DEFAULT 0,
    eps_yoy REAL,
    rev_yoy REAL,
    rev_accel REAL,
    guidance_change INTEGER,
    direction REAL,
    direction_n INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    extracted_at TEXT NOT NULL,
    error TEXT
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
        self._migrate()
        logger.info("Database connected: %s", self.db_path)

    def _migrate(self) -> None:
        """Apply idempotent schema migrations for columns added after initial release."""
        migrations = [
            ("ALTER TABLE articles ADD COLUMN score REAL", "score"),
            ("ALTER TABLE articles ADD COLUMN last_seen_at TEXT", "last_seen_at"),
            ("ALTER TABLE articles ADD COLUMN duplicate_of INTEGER", "duplicate_of"),
            (
                "ALTER TABLE articles ADD COLUMN extraction_failed INTEGER NOT NULL DEFAULT 0",
                "extraction_failed",
            ),
            (
                "ALTER TABLE articles ADD COLUMN is_roundup INTEGER NOT NULL DEFAULT 0",
                "is_roundup",
            ),
            (
                "ALTER TABLE daily_briefings ADD COLUMN segment_map TEXT",
                "daily_briefings.segment_map",
            ),
            (
                "ALTER TABLE daily_briefings ADD COLUMN briefing_metadata TEXT",
                "daily_briefings.briefing_metadata",
            ),
            ("ALTER TABLE filing_facts ADD COLUMN guidance_basis TEXT", "guidance_basis"),
            ("ALTER TABLE filing_facts ADD COLUMN guidance_base REAL", "guidance_base"),
            ("ALTER TABLE filing_facts ADD COLUMN guidance_low_pct REAL", "guidance_low_pct"),
            ("ALTER TABLE filing_facts ADD COLUMN guidance_high_pct REAL", "guidance_high_pct"),
        ]
        for sql, name in migrations:
            try:
                self.conn.execute(sql)
                self.conn.commit()
                logger.debug("Migration: added %s column to articles", name)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Create indexes for migrated columns (must run after columns exist)
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(score);
            CREATE INDEX IF NOT EXISTS idx_articles_last_seen ON articles(last_seen_at);
        """)
        self._normalise_published_at()

    def _normalise_published_at(self) -> int:
        """Rewrite legacy RFC 822 published_at values as ISO-8601 UTC 'Z'.

        Rows written before normalisation moved into insert_article hold the
        feed's own format, which cannot be ordered against fetched_at. This
        converts what it can prove and returns the number of rows changed.

        Deliberately conservative: a value that will not parse is **left
        alone**, not nulled. Refusing to store an unparseable timestamp going
        forward is a policy choice about new data; destroying one already on
        disk is not the same thing, and a human can still inspect it.

        Idempotent -- rows already in the canonical format are skipped, so this
        is a no-op on every connect after the first.
        """
        rows = self.conn.execute(
            "SELECT id, published_at FROM articles WHERE published_at IS NOT NULL"
        ).fetchall()

        updates: list[tuple[str, int]] = []
        unparseable = 0
        for row in rows:
            raw = row["published_at"]
            try:
                datetime.strptime(raw, UTC_Z_FORMAT)
                continue  # already canonical
            except (ValueError, TypeError):
                pass
            normalised = normalise_timestamp(raw)
            if normalised is None:
                unparseable += 1
                continue
            updates.append((normalised, row["id"]))

        if updates:
            self.conn.executemany("UPDATE articles SET published_at = ? WHERE id = ?", updates)
            self.conn.commit()
            logger.info("Migration: normalised %d published_at value(s) to UTC Z", len(updates))
        if unparseable:
            logger.warning(
                "Migration: left %d unparseable published_at value(s) untouched", unparseable
            )
        return len(updates)

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
        """Insert an article. Returns True if inserted, False if duplicate.

        published_at is normalised to ISO-8601 UTC 'Z' before storage. Feeds
        deliver RFC 822 ('Tue, 14 Jul 2026 00:00:00 GMT'), which cannot be
        ordered against the ISO-8601 fetched_at written below. Normalising at
        this single choke point is what lets every consumer compare the two.
        An unparseable value becomes NULL, which the schema already allows and
        which means the same thing to a reader: no usable publish time.
        """
        if published_at is not None:
            normalised = normalise_timestamp(published_at)
            if normalised is None:
                logger.warning(
                    "Unparseable published_at %r for %s; storing NULL", published_at, url
                )
            published_at = normalised
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO articles
               (url, url_hash, title, source, category, author,
                published_at, fetched_at, full_text, content_hash, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url_hash) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (
                url,
                url_hash,
                title,
                source,
                category,
                author,
                published_at,
                now,
                full_text,
                content_hash,
                now,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT fetched_at FROM articles WHERE url_hash = ?", (url_hash,)
        ).fetchone()
        return row["fetched_at"] == now

    def has_filing(self, accession: str) -> bool:
        """True when this accession number is already stored."""
        row = self.conn.execute(
            "SELECT 1 FROM filings WHERE accession = ?", (accession,)
        ).fetchone()
        return row is not None

    def insert_filing(
        self,
        *,
        accession: str,
        cik: str,
        ticker: str,
        form: str,
        items: list[str],
        accepted_at: str,
        fetched_at: str,
        primary_doc_url: str,
        primary_html: bytes,
        exhibits: list[dict[str, str]],
        primary_text: str,
        exhibit_text: str,
        origin: str = "edgar",
    ) -> bool:
        """Store one filing. Returns False (and changes nothing) if the accession exists.

        ``primary_html`` and ``exhibits`` (a JSON list of {type, document, url, html}) are
        zlib-compressed; the text columns hold the plain-text rendering.
        """
        exhibits_blob = zlib.compress(json.dumps(exhibits).encode("utf-8"))
        cursor = self.conn.execute(
            """INSERT INTO filings
               (accession, cik, ticker, form, items, accepted_at, origin, fetched_at,
                primary_doc_url, primary_html, exhibits, primary_text, exhibit_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(accession) DO NOTHING""",
            (
                accession,
                cik,
                ticker,
                form,
                ",".join(items),
                accepted_at,
                origin,
                fetched_at,
                primary_doc_url,
                zlib.compress(primary_html),
                exhibits_blob,
                primary_text,
                exhibit_text,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def filings_for_extraction(self, *, redo: bool = False) -> list[sqlite3.Row]:
        """Stored 8-Ks with item 2.02, oldest first; without facts unless ``redo``."""
        rows = self.conn.execute(
            """SELECT f.* FROM filings f
               LEFT JOIN filing_facts ff ON ff.accession = f.accession
               WHERE (? OR ff.accession IS NULL)
               ORDER BY f.accepted_at, f.id""",
            (1 if redo else 0,),
        ).fetchall()
        return [r for r in rows if "2.02" in str(r["items"]).split(",")]

    def previous_earnings_facts(self, ticker: str, accepted_at: str) -> sqlite3.Row | None:
        """The same ticker's latest earlier 2.02 filing (facts columns NULL if none stored)."""
        rows = self.conn.execute(
            """SELECT f.accession, f.accepted_at, f.items, ff.rev_yoy
               FROM filings f LEFT JOIN filing_facts ff ON ff.accession = f.accession
               WHERE f.ticker = ? AND f.accepted_at < ?
               ORDER BY f.accepted_at DESC, f.id DESC""",
            (ticker, accepted_at),
        ).fetchall()
        return next((r for r in rows if "2.02" in str(r["items"]).split(",")), None)

    def earlier_guidance(self, ticker: str, metric: str, accepted_at: str) -> list[sqlite3.Row]:
        """Earlier guidance rows for the ticker and metric, most recent first."""
        return self.conn.execute(
            """SELECT f.accepted_at, ff.guidance_period_label, ff.guidance_low, ff.guidance_high
               FROM filing_facts ff JOIN filings f ON f.accession = ff.accession
               WHERE f.ticker = ? AND ff.guidance_metric = ? AND f.accepted_at < ?
                 AND ff.guidance_low IS NOT NULL AND ff.guidance_high IS NOT NULL
               ORDER BY f.accepted_at DESC, f.id DESC""",
            (ticker, metric, accepted_at),
        ).fetchall()

    def save_filing_facts(self, row: dict[str, Any]) -> None:
        """Insert or replace the facts row for ``row['accession']``."""
        cols = list(row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO filing_facts ({', '.join(cols)}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(row.values()),
        )
        self.conn.commit()

    def cleanup_old_articles(self, max_age_days: int = 2) -> int:
        """Delete articles not seen in the last max_age_days days. Returns count deleted."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        cursor = self.conn.execute(
            """DELETE FROM articles
               WHERE (last_seen_at IS NOT NULL AND last_seen_at < ?)
                  OR (last_seen_at IS NULL AND fetched_at < ?)""",
            (cutoff, cutoff),
        )
        self.conn.commit()
        count = cursor.rowcount
        if count:
            logger.info("Deleted %d old articles (max_age_days=%d)", count, max_age_days)
        return count

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

    def get_unsummarized_articles(self) -> list[dict[str, Any]]:
        """Get today's unsummarized articles ordered by relevance score descending.

        Returns all of today's unsummarized articles with full_text, ordered by
        score so that callers can apply category-balanced selection on top.
        Articles without a score are ranked last.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            """SELECT id, url, title, source, category, full_text, score,
                      published_at, fetched_at
               FROM articles
               WHERE summary IS NULL
                 AND extraction_failed = 0
                 AND duplicate_of IS NULL
                 AND full_text IS NOT NULL
                 AND fetched_at >= ?
               ORDER BY COALESCE(score, 0) DESC""",
            (today,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_duplicates(self, duplicates: dict[int, int]) -> None:
        """Record ``duplicate_of`` for each {duplicate_id: kept_id}. Never deletes rows."""
        if not duplicates:
            return
        self.conn.executemany(
            "UPDATE articles SET duplicate_of = ? WHERE id = ?",
            [(kept, dup) for dup, kept in duplicates.items()],
        )
        self.conn.commit()

    def update_summary(self, article_id: int, summary: str, model: str) -> None:
        """Update an article with its summary."""
        self.conn.execute(
            "UPDATE articles SET summary = ?, summary_model = ? WHERE id = ?",
            (summary, model, article_id),
        )
        self.conn.commit()

    def save_extraction(self, article_id: int, extraction: dict[str, Any], model: str) -> None:
        """Store an article's validated extraction and mirror it onto the article row.

        ``summary`` holds ``what_happened`` so everything that reads summaries keeps working.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO extractions (article_id, json, model, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(article_id) DO UPDATE SET
                   json = excluded.json,
                   model = excluded.model,
                   created_at = excluded.created_at""",
            (article_id, json.dumps(extraction), model, now),
        )
        self.conn.execute(
            """UPDATE articles
               SET summary = ?, summary_model = ?, is_roundup = ?, extraction_failed = 0
               WHERE id = ?""",
            (extraction["what_happened"], model, int(extraction["is_roundup"]), article_id),
        )
        self.conn.commit()

    def get_extraction_details(self, article_ids: list[int]) -> dict[int, list[str]]:
        """Surviving ``details`` sentences per article id, read from the stored extractions.

        Articles without an extraction, or with one saved before ``details`` existed,
        map to an empty list.
        """
        out: dict[int, list[str]] = {}
        for i in range(0, len(article_ids), 500):  # stay under SQLite's variable limit
            chunk = article_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT article_id, json FROM extractions WHERE article_id IN ({marks})",
                chunk,
            ).fetchall()
            for row in rows:
                try:
                    details = json.loads(row["json"]).get("details", [])
                except (json.JSONDecodeError, AttributeError):
                    details = []
                out[row["article_id"]] = [d for d in details if isinstance(d, str)]
        return out

    def mark_extraction_failed(self, article_id: int) -> None:
        """Flag an article whose extraction failed validation twice, so it is not retried."""
        self.conn.execute(
            "UPDATE articles SET extraction_failed = 1 WHERE id = ?",
            (article_id,),
        )
        self.conn.commit()

    def update_score(self, article_id: int, score: float) -> None:
        """Update an article's relevance score."""
        self.conn.execute(
            "UPDATE articles SET score = ? WHERE id = ?",
            (score, article_id),
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

    def get_articles_for_date(self, date: str) -> list[dict[str, Any]]:
        """All articles fetched on the UTC date *date* (YYYY-MM-DD)."""
        rows = self.conn.execute(
            """SELECT * FROM articles
               WHERE substr(fetched_at, 1, 10) = ?
               ORDER BY category, fetched_at DESC""",
            (date,),
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

    def insert_artwork(
        self,
        title: str,
        artist: str | None = None,
        date: str | None = None,
        medium: str | None = None,
        image_url: str | None = None,
        source_url: str | None = None,
    ) -> None:
        """Insert an artwork record."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO artworks
               (title, artist, date, medium, image_url, source_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, artist, date, medium, image_url, source_url, now),
        )
        self.conn.commit()

    def get_todays_artwork(self) -> list[dict[str, Any]]:
        """Get artworks fetched today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM artworks WHERE fetched_at >= ? ORDER BY id",
            (today,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_briefing(
        self,
        date: str,
        content: str,
        model: str,
        segment_map: str | None = None,
        briefing_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert or replace the daily briefing for a given date.

        Args:
            date: ISO date string (YYYY-MM-DD).
            content: Rendered briefing text (joined from segments or plain text).
            model: Name of the model that generated the briefing.
            segment_map: JSON-encoded list of segment dicts, or None if unavailable.
            briefing_metadata: Dict with article count, categories covered, top sources.
        """
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(briefing_metadata) if briefing_metadata is not None else None
        self.conn.execute(
            """INSERT OR REPLACE INTO daily_briefings
               (date, content, model, generated_at, segment_map, briefing_metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (date, content, model, now, segment_map, metadata_json),
        )
        self.conn.commit()

    def record_api_usage(
        self, model: str, tokens_in: int, tokens_out: int, cost_usd: float, ts: str | None = None
    ) -> None:
        """Record one paid API call (the spend guard sums these per UTC month)."""
        ts = ts or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO api_usage (ts, model, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, model, tokens_in, tokens_out, cost_usd),
        )
        self.conn.commit()

    def month_to_date_cost(self, now: datetime | None = None) -> float:
        """Total ``cost_usd`` recorded in the UTC month of *now* (default: the current one)."""
        month = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m")
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE substr(ts, 1, 7) = ?",
            (month,),
        ).fetchone()
        return float(row[0])

    def claim_marker(self, key: str) -> bool:
        """Set the meta marker *key*; True only for the call that set it (first time)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_briefing(self, date: str) -> dict[str, Any] | None:
        """Return the daily briefing for a given date, or None if not found.

        Returns a dict with keys:
            content (str): The rendered briefing text.
            segment_map (list | None): Parsed segment list, or None if not stored.
        """
        row = self.conn.execute(
            "SELECT content, segment_map FROM daily_briefings WHERE date = ?",
            (date,),
        ).fetchone()
        if not row:
            return None
        segments = None
        if row["segment_map"]:
            try:
                segments = json.loads(row["segment_map"])
            except json.JSONDecodeError:
                logger.warning("Failed to parse segment_map JSON for date %s", date)
        return {"content": row["content"], "segment_map": segments}

    def get_briefing_history(self, days: int = 30) -> list[dict[str, Any]]:
        """Return daily briefings from the past N days, newest first.

        Each entry includes date, content, model, generated_at, and parsed
        briefing_metadata (or None if not stored).
        """
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            """SELECT date, content, model, generated_at, briefing_metadata
               FROM daily_briefings
               WHERE date >= ?
               ORDER BY date DESC""",
            (cutoff,),
        ).fetchall()
        results = []
        for row in rows:
            metadata = None
            if row["briefing_metadata"]:
                try:
                    metadata = json.loads(row["briefing_metadata"])
                except json.JSONDecodeError:
                    logger.warning(
                        "Failed to parse briefing_metadata JSON for date %s", row["date"]
                    )
            results.append(
                {
                    "date": row["date"],
                    "content": row["content"],
                    "model": row["model"],
                    "generated_at": row["generated_at"],
                    "briefing_metadata": metadata,
                }
            )
        return results

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
