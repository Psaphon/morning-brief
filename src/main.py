"""Morning Brief pipeline entry point.

Runs all stages in order:
1. Fetch — RSS feeds, APIs, health checks
2. Process — extract full text, deduplicate
3. Summarize — LLM per-article summaries
4. Publish — render HTML dashboard, terminal output
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import load_config
from .db import Database
from .fetchers.art import fetch_daily_artwork
from .fetchers.crypto import fetch_all_crypto
from .fetchers.financial import fetch_all_financial
from .fetchers.rss import fetch_all_feeds
from .processors.dedup import deduplicate
from .processors.extractor import extract_articles
from .publishers.html import render_dashboard
from .summarizers.local import summarize_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("morning-brief")


def _write_status(data_dir: Path, status: str, message: str, **extra) -> None:
    """Write a JSON status file so health checks and alerting scripts can read it.

    Written to data/last_run.json — checked by scripts/healthcheck.sh.
    """
    status_path = data_dir / "last_run.json"
    payload = {
        "status": status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(payload, indent=2))
    except OSError as e:
        logger.warning("Could not write status file: %s", e)


async def run_pipeline() -> None:
    """Execute the full Morning Brief pipeline."""
    config = load_config()
    db = Database(config.database_path)
    data_dir = config.database_path.parent

    try:
        db.connect()
        start = datetime.now(timezone.utc)
        logger.info("Pipeline started at %s", start.isoformat())

        # Stage 1: Fetch all data sources in parallel
        logger.info("Stage 1: Fetching data sources...")

        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "MorningBrief/0.1"},
        ) as client:
            rss_task = fetch_all_feeds(config.feeds_path)
            financial_task = fetch_all_financial(
                client,
                finnhub_key=config.api_keys.finnhub,
                fred_key=config.api_keys.fred,
            )
            crypto_task = fetch_all_crypto(
                client,
                coingecko_key=config.api_keys.coingecko,
                etherscan_key=config.api_keys.etherscan,
            )

            art_task = fetch_daily_artwork(client)

            raw_articles, financial_data, crypto_data, artworks = await asyncio.gather(
                rss_task, financial_task, crypto_task, art_task
            )

        logger.info("Fetched %d raw articles", len(raw_articles))

        # Store financial data
        for point in financial_data:
            extra_json = json.dumps(point.extra) if point.extra else None
            db.insert_market_data(
                symbol=point.symbol,
                data_type=point.data_type,
                value=point.value,
                change_pct=point.change_pct,
                extra_json=extra_json,
            )
        if financial_data:
            logger.info("Stored %d financial data points", len(financial_data))

        # Store crypto data
        for point in crypto_data:
            extra_json = json.dumps(point.extra) if point.extra else None
            db.insert_market_data(
                symbol=point.symbol,
                data_type=point.data_type,
                value=point.value,
                change_pct=point.change_pct,
                extra_json=extra_json,
            )
        if crypto_data:
            logger.info("Stored %d crypto data points", len(crypto_data))

        # Store artwork
        for artwork in artworks:
            db.insert_artwork(
                title=artwork["title"],
                artist=artwork.get("artist"),
                date=artwork.get("date"),
                medium=artwork.get("medium"),
                image_url=artwork.get("image_url"),
                source_url=artwork.get("source_url"),
            )
        if artworks:
            logger.info("Stored %d daily artworks", len(artworks))

        # Stage 2: Process — extract and deduplicate
        logger.info("Stage 2: Processing articles...")
        extracted = await extract_articles(raw_articles)
        logger.info("Extracted %d articles", len(extracted))

        new_count = 0
        for article in deduplicate(extracted):
            inserted = db.insert_article(**article)
            if inserted:
                new_count += 1
        logger.info(
            "Stored %d new articles (%d duplicates skipped)", new_count, len(extracted) - new_count
        )

        # Stage 3: Summarize (requires Ollama — skips gracefully if unavailable)
        logger.info("Stage 3: Summarizing articles...")
        unsummarized = db.get_unsummarized_articles()
        logger.info("Found %d unsummarized articles", len(unsummarized))

        if unsummarized:
            results = await summarize_articles(config.ollama, unsummarized)
            saved = 0
            for result in results:
                if result.success and result.summary:
                    db.update_summary(result.article_id, result.summary, result.model)
                    saved += 1
            logger.info("Saved %d summaries to database", saved)

        # Stage 4: Publish HTML dashboard
        logger.info("Stage 4: Rendering dashboard...")
        all_articles = db.get_todays_articles()
        market = db.get_latest_market_data()
        health = db.get_latest_health_checks()
        daily_art = db.get_todays_artwork()

        output_path = config.output_dir / "dashboard.html"
        render_dashboard(
            articles=all_articles,
            market_data=market,
            health_checks=health,
            artworks=daily_art,
            output_path=output_path,
        )
        logger.info("Dashboard published with %d articles", len(all_articles))

        # Stage 5: Deploy to gh-pages (if enabled)
        if config.deploy_enabled:
            logger.info("Stage 5: Deploying dashboard...")
            deploy_script = Path("scripts/deploy-dashboard.sh")
            if deploy_script.exists():
                result = subprocess.run(
                    [str(deploy_script), str(output_path)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    logger.info("Dashboard deployed to %s branch", config.deploy_branch)
                else:
                    logger.warning("Deploy failed: %s", result.stderr.strip())
            else:
                logger.warning("Deploy script not found at %s", deploy_script)
        else:
            logger.info("Deploy disabled (set DEPLOY_ENABLED=true to enable)")

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("Pipeline completed in %.1fs", elapsed)

        _write_status(
            data_dir,
            status="ok",
            message=f"Pipeline completed in {elapsed:.1f}s",
            articles=len(all_articles),
            elapsed_seconds=round(elapsed, 1),
        )

    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        _write_status(data_dir, status="error", message=str(exc))
        raise

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
