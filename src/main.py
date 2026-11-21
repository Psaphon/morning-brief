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
import time
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
from .processors.dedupe import find_duplicates
from .processors.extractor import extract_articles
from .processors.interest import filter_by_feed_minimum, load_interests
from .processors.lanes import ReaderLaneDB, filter_lane, load_lanes_for_run
from .processors.posts import group_posts
from .publishers.html import render_dashboard
from .publishers.terminal import render_terminal
from .summarizers.cloud import generate_daily_briefing_claude
from .summarizers.local import (
    extract_post,
    extraction_max_chars,
    generate_daily_briefing,
    select_balanced_articles,
    summarize_articles,
    truncate_text,
)
from .validators.grounding import ValidatorRun

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("morning-brief")


# The stage timer's clock; tests monkeypatch it to simulate a slow run.
_clock = time.monotonic


class StageTimer:
    """Times consecutive pipeline stages: each ``lap`` closes the stage since the last one."""

    def __init__(self, clock=None) -> None:
        self._clock = clock or _clock  # resolved now, so a patched module clock is honoured
        self._start = self._last = self._clock()
        self.stages: dict[str, float] = {}

    def lap(self, stage: str) -> None:
        now = self._clock()
        self.stages[stage] = now - self._last
        self._last = now

    @property
    def total(self) -> float:
        return self._last - self._start

    def report(self, deadline_minutes: int) -> dict:
        """Log the one-line timing summary (plus a deadline warning); return the status payload."""
        stages = ", ".join(f"{name} {secs:.1f}s" for name, secs in self.stages.items())
        logger.info("Run timing: total %.1fs (%s)", self.total, stages)
        if self.total > deadline_minutes * 60:
            slowest = max(self.stages, key=self.stages.__getitem__)
            logger.warning(
                "Run took %.1fs, past the %d-minute deadline (RUN_DEADLINE_MINUTES); "
                "slowest stage: %s (%.1fs)",
                self.total,
                deadline_minutes,
                slowest,
                self.stages[slowest],
            )
        return {
            "total_s": round(self.total, 1),
            "stages": {name: round(secs, 1) for name, secs in self.stages.items()},
        }


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
    validator = ValidatorRun.from_config(config)

    try:
        db.connect()
        cleaned = db.cleanup_old_articles()
        if cleaned:
            logger.info("Retention cleanup: removed %d old articles", cleaned)
        start = datetime.now(timezone.utc)
        logger.info("Pipeline started at %s", start.isoformat())
        timer = StageTimer()

        # Feed lanes decide where each feed's articles are used (loaded once per run).
        lanes = load_lanes_for_run(config.feeds_overlay)

        # Stage 1: Fetch all data sources in parallel
        logger.info("Stage 1: Fetching data sources...")

        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "MorningBrief/0.1"},
        ) as client:
            rss_task = fetch_all_feeds(config.feeds_path, lanes.extra_feeds, lanes.kinds)
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
        timer.lap("fetch")

        # Stage 2: Process — extract and deduplicate
        logger.info("Stage 2: Processing articles...")
        raw_articles = group_posts(raw_articles, config.post_group_hours)
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

        timer.lap("process")

        # Stage 2.5: Score articles by relevance
        logger.info("Scoring articles by relevance...")
        from .processors.scorer import score_articles, select_top_articles

        todays = db.get_todays_articles()
        scored = score_articles(todays)
        for sa in scored:
            db.update_score(sa.article["id"], sa.score)
            sa.article["score"] = sa.score  # keep in-memory dicts in sync
        top = select_top_articles(scored)
        logger.info(
            "Scored %d articles, selected top %d for summarization",
            len(scored),
            len(top),
        )

        timer.lap("score")

        # Stage 2.6: Emit per-ticker signals for atrade (optional, non-fatal — §2)
        if config.signals_enabled:
            try:
                from .publishers.signals import emit_signals, load_ticker_map

                _ticker_map_path = Path("config/ticker_map.toml")
                _ticker_map = load_ticker_map(_ticker_map_path)
                emit_signals(
                    articles=filter_lane(todays, "signals", lanes),
                    ticker_map=_ticker_map,
                    signals_dir=config.signals_dir,
                    universe_ref=_ticker_map.universe_ref,
                )
            except Exception:
                logger.exception("Signal emission failed; pipeline continues")
        else:
            logger.debug("Signal emission disabled (set SIGNALS_ENABLED=true to enable)")

        timer.lap("signals")

        # Stage 3: Summarize (requires Ollama — skips gracefully if unavailable)
        logger.info("Stage 3: Summarizing articles...")
        candidates = filter_lane(db.get_unsummarized_articles(), "reader", lanes)
        # Dedupe before selecting so duplicates never use an LLM call and the
        # freed slot is refilled from the category's next article.
        interests = load_interests()
        candidates = filter_by_feed_minimum(candidates, interests)
        duplicates = find_duplicates(candidates, config.dedupe_window_hours)
        db.mark_duplicates(duplicates)
        candidates = [a for a in candidates if a["id"] not in duplicates]
        unsummarized = select_balanced_articles(
            candidates,
            per_category=config.reader_max_per_category,
            total=config.reader_max_total,
            interests=interests,
        )
        logger.info("Found %d unsummarized articles (balanced selection)", len(unsummarized))

        if unsummarized:
            # Posts are their own extraction (no model call); the rest go to Ollama.
            is_post = [lanes.kind_for(a.get("source", "")) == "post" for a in unsummarized]
            post_results = [extract_post(a) for a, p in zip(unsummarized, is_post) if p]
            results, metrics = await summarize_articles(
                config.ollama, [a for a, p in zip(unsummarized, is_post) if not p]
            )
            results = post_results + results
            by_id = {a["id"]: a for a in unsummarized}
            saved = 0
            for result in results:
                if result.success and result.extraction:
                    extraction = result.extraction
                    if validator.mode != "off":
                        article = by_id[result.article_id]
                        cap = extraction_max_chars(config.ollama)
                        text = truncate_text(article.get("full_text", ""), cap)
                        source = f"{article.get('title', '')}\n{text}"
                        kept = validator.apply(
                            extraction["what_happened"], source, f"article {result.article_id}"
                        )
                        if not kept:
                            # Every sentence was ungrounded: leave the article out of the
                            # briefing (same flag as a failed extraction), save no summary.
                            db.mark_extraction_failed(result.article_id)
                            continue
                        # Details are held to the same source; the article stands or falls
                        # on what_happened alone.
                        details = [
                            d
                            for d in (
                                validator.apply(d, source, f"article {result.article_id} detail")
                                for d in extraction["details"]
                            )
                            if d
                        ]
                        extraction = {**extraction, "what_happened": kept, "details": details}
                    db.save_extraction(result.article_id, extraction, result.model)
                    saved += 1
                elif result.extraction_failed:
                    db.mark_extraction_failed(result.article_id)
            logger.info("Saved %d summaries to database", saved)
            if metrics.total_articles > 0:
                logger.info(
                    "Summarization timing: %.1fs total, %.1f articles/min (%d/%d succeeded)",
                    metrics.total_seconds,
                    metrics.articles_per_minute,
                    metrics.succeeded,
                    metrics.total_articles,
                )

        timer.lap("summarize")

        # Generate daily briefing (after summarization, before publish)
        # Try Claude first if API key is configured, fall back to Ollama.
        logger.info("Generating daily briefing...")
        reader_db = ReaderLaneDB(db, lanes)  # briefing reads reader-lane articles only
        briefing = None
        if config.api_keys.anthropic:
            logger.info("ANTHROPIC_API_KEY set — attempting Claude synthesis")
            briefing = await generate_daily_briefing_claude(
                reader_db, config.api_keys.anthropic, validator
            )
            if briefing:
                logger.info("Claude briefing ready (%d chars)", len(briefing))
            else:
                logger.info("Claude synthesis failed or skipped — falling back to Ollama")

        if briefing is None:
            briefing = await generate_daily_briefing(reader_db, config.ollama, validator)
            if briefing:
                logger.info("Daily briefing ready (%d chars)", len(briefing))
            else:
                logger.info("No briefing generated (Ollama unavailable or no summaries)")

        # Retrieve structured segment map for interactive HTML
        briefing_segments = None
        if briefing:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            bref_record = db.get_briefing(today_str)
            briefing_segments = bref_record["segment_map"] if bref_record else None

        timer.lap("briefing")

        # Stage 4: Publish HTML dashboard
        logger.info("Stage 4: Rendering dashboard...")
        all_articles = filter_lane(db.get_todays_articles(), "reader", lanes)
        market = db.get_latest_market_data()
        health = db.get_latest_health_checks()
        daily_art = db.get_todays_artwork()

        output_path = config.output_dir / "dashboard.html"
        today_brief_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        render_dashboard(
            articles=all_articles,
            market_data=market,
            health_checks=health,
            artworks=daily_art,
            briefing=briefing,
            briefing_segments=briefing_segments,
            interests=interests,
            output_path=output_path,
            hmac_key=config.dashboard_hmac_key,
            brief_id=today_brief_id,
        )
        logger.info("Dashboard published with %d articles", len(all_articles))

        render_terminal(
            articles=all_articles,
            market_data=market,
            health_checks=health,
            artworks=daily_art,
            briefing=briefing,
        )

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
        timer.lap("publish")

        logger.info(
            "Validator (%s): checked %d sentences, dropped %d (number=%d, name=%d, banned=%d)",
            validator.mode,
            validator.checked,
            sum(validator.dropped.values()),
            validator.dropped["number"],
            validator.dropped["name"],
            validator.dropped["banned"],
        )

        timing = timer.report(config.run_deadline_minutes)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("Pipeline completed in %.1fs", elapsed)

        _write_status(
            data_dir,
            status="ok",
            message=f"Pipeline completed in {elapsed:.1f}s",
            articles=len(all_articles),
            elapsed_seconds=round(elapsed, 1),
            validator=validator.metadata(),
            timing=timing,
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
