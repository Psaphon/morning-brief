"""Morning Brief pipeline entry point.

Runs all stages in order:
1. Fetch — RSS feeds, APIs, health checks
2. Process — extract full text, deduplicate
3. Summarize — LLM per-article summaries
4. Publish — render HTML dashboard, terminal output
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from .config import load_config
from .db import Database
from .fetchers.rss import fetch_all_feeds
from .processors.extractor import extract_articles
from .processors.dedup import deduplicate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("morning-brief")


async def run_pipeline() -> None:
    """Execute the full Morning Brief pipeline."""
    config = load_config()
    db = Database(config.database_path)

    try:
        db.connect()
        start = datetime.now(timezone.utc)
        logger.info("Pipeline started at %s", start.isoformat())

        # Stage 1: Fetch RSS feeds
        logger.info("Stage 1: Fetching RSS feeds...")
        raw_articles = await fetch_all_feeds(config.feeds_path)
        logger.info("Fetched %d raw articles", len(raw_articles))

        # Stage 2: Process — extract and deduplicate
        logger.info("Stage 2: Processing articles...")
        extracted = await extract_articles(raw_articles)
        logger.info("Extracted %d articles", len(extracted))

        new_count = 0
        for article in deduplicate(extracted):
            inserted = db.insert_article(**article)
            if inserted:
                new_count += 1
        logger.info("Stored %d new articles (%d duplicates skipped)",
                     new_count, len(extracted) - new_count)

        # Stage 3: Summarize (requires Ollama — skip if unavailable)
        # TODO: implement in Phase 2

        # Stage 4: Publish
        # TODO: implement in Phase 4

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("Pipeline completed in %.1fs", elapsed)

    finally:
        db.close()


def main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
