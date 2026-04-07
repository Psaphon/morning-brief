# AI Development Prompt — article-retention

**Branch:** `feature/article-retention`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` for full acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Add article retention cleanup so the database doesn't grow unbounded. Old articles (not seen for 2+ days) are deleted at pipeline start. A new `last_seen_at` column tracks when articles were last fetched, enabling smarter retention that doesn't break deduplication.

### 1. Schema migration in `src/db.py`

Add a `last_seen_at TEXT` column to the `articles` table schema. Handle migration for existing databases:

```python
# After executescript(SCHEMA), run:
# ALTER TABLE articles ADD COLUMN last_seen_at TEXT;
# Wrap in try/except to handle "duplicate column" on re-runs (idempotent).
```

Add an index: `CREATE INDEX IF NOT EXISTS idx_articles_last_seen ON articles(last_seen_at);`

### 2. Update `insert_article()` in `src/db.py`

When an article already exists (IntegrityError on insert), update its `last_seen_at` timestamp instead of just returning False. Use `INSERT ... ON CONFLICT(url_hash) DO UPDATE SET last_seen_at = ?` instead of the current try/except pattern. Still return True for new inserts and False for existing (updated) articles.

### 3. Add `cleanup_old_articles()` method to `Database` class in `src/db.py`

```python
def cleanup_old_articles(self, max_age_days: int = 2) -> int:
    """Delete articles not seen in the last max_age_days days. Returns count deleted."""
```

- Delete from `articles` where `last_seen_at` is older than `max_age_days` days ago
- Also delete articles where `last_seen_at IS NULL` and `fetched_at` is older than `max_age_days` (handles pre-migration rows)
- Do NOT touch `daily_briefings`, `market_data`, `health_checks`, or `artworks` tables
- Log the count of deleted articles
- Return the count

### 4. Call cleanup in `src/main.py`

Add cleanup call right after `db.connect()` and before Stage 1 (fetching):

```python
db.connect()
cleaned = db.cleanup_old_articles()
if cleaned:
    logger.info("Retention cleanup: removed %d old articles", cleaned)
```

### 5. Tests in `tests/test_db.py`

Add tests for:
- `cleanup_old_articles` deletes articles older than 2 days
- `cleanup_old_articles` preserves articles seen within 2 days
- `cleanup_old_articles` does not touch `daily_briefings`
- Re-fetching an existing article updates `last_seen_at`
- Articles with updated `last_seen_at` survive cleanup even if `fetched_at` is old

## Commit message

```
feat: add article retention cleanup with last_seen_at tracking

Delete articles not seen for 2+ days at pipeline start. Track
last_seen_at on re-fetch to prevent dedup gaps after cleanup.
Preserves daily_briefings table for archival.
```

## Rules

- Run `ruff check . && ruff format --check .` before committing
- Run `pytest tests/ -v` before committing
- Do NOT push
- Do NOT modify files outside `src/db.py`, `src/main.py`, and `tests/test_db.py`
- Do NOT delete or modify the `daily_briefings` table or its data
