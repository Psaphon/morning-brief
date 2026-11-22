"""CLI entry point for Morning Brief.

Provides commands to run the pipeline and view the dashboard.
"""

from __future__ import annotations

import logging
import sys

import click

from .config import load_config
from .db import Database

logger = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """Morning Brief — automated morning intelligence dashboard."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@cli.command()
def run() -> None:
    """Run the full pipeline (fetch, process, summarize)."""
    import asyncio

    from .main import run_pipeline

    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted")
        sys.exit(130)


@cli.command()
@click.option("--output", "-o", type=click.Path(), help="Write HTML to file")
def dashboard(output: str | None) -> None:
    """Render the HTML dashboard from today's data."""
    from datetime import datetime, timezone
    from pathlib import Path

    from .publishers.html import render_dashboard

    config = load_config()
    db = Database(config.database_path)

    try:
        db.connect()
        articles = db.get_todays_articles()
        market_data = db.get_latest_market_data()
        health_checks = db.get_latest_health_checks()
        artworks = db.get_todays_artwork()

        if not articles and not market_data:
            click.echo("No data found. Run the pipeline first: python -m src.main")
            sys.exit(1)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bref_record = db.get_briefing(today_str)
        briefing = bref_record["content"] if bref_record else None
        briefing_segments = bref_record["segment_map"] if bref_record else None

        output_path = Path(output) if output else config.output_dir / "dashboard.html"
        render_dashboard(
            articles=articles,
            market_data=market_data,
            health_checks=health_checks,
            artworks=artworks,
            briefing=briefing,
            briefing_segments=briefing_segments,
            output_path=output_path,
            hmac_key=config.dashboard_hmac_key,
            brief_id=today_str,
            contact_email=config.contact_email,
        )
        click.echo(f"Dashboard written to {output_path} ({len(articles)} articles)")

    finally:
        db.close()


@cli.command()
@click.option("--limit", "-n", default=5, help="Max articles per category")
def view(limit: int) -> None:
    """View today's briefing in the terminal."""
    from .publishers.terminal import render_terminal

    config = load_config()
    db = Database(config.database_path)

    try:
        db.connect()
        articles = db.get_todays_articles()
        market_data = db.get_latest_market_data()
        health_checks = db.get_latest_health_checks()
        artworks = db.get_todays_artwork()

        if not articles and not market_data:
            click.echo("No data found. Run the pipeline first: python -m src.main")
            sys.exit(1)

        render_terminal(
            articles=articles,
            market_data=market_data,
            health_checks=health_checks,
            artworks=artworks,
            max_articles_per_category=limit,
        )

    finally:
        db.close()


@cli.command()
@click.option("--days", "-d", default=7, show_default=True, help="Days of history to show")
def history(days: int) -> None:
    """View past daily briefings from the archive."""
    config = load_config()
    db = Database(config.database_path)

    try:
        db.connect()
        briefings = db.get_briefing_history(days=days)

        if not briefings:
            click.echo(f"No briefings found in the last {days} day(s).")
            return

        for entry in briefings:
            meta = entry["briefing_metadata"] or {}
            article_count = meta.get("article_count", "?")
            categories = ", ".join(meta.get("categories_covered", [])) or "—"
            top_sources = ", ".join(meta.get("top_sources", [])) or "—"
            click.echo(f"\n{'=' * 60}")
            click.echo(f"Date:        {entry['date']}")
            click.echo(f"Model:       {entry['model']}")
            click.echo(f"Generated:   {entry['generated_at']}")
            click.echo(f"Articles:    {article_count}")
            click.echo(f"Categories:  {categories}")
            click.echo(f"Top sources: {top_sources}")
            click.echo(f"{'=' * 60}")
            click.echo(entry["content"])

    finally:
        db.close()


@cli.command("audit-sources")
@click.option("--days", "-d", default=7, show_default=True, help="Days of feed items to audit")
def audit_sources(days: int) -> None:
    """Report per-feed extracted-text quality (read-only; does not touch the DB)."""
    import asyncio
    import os
    from pathlib import Path

    from .fetchers.rss import parse_feeds_md
    from .processors.lanes import load_lanes_for_run
    from .processors.source_audit import audit_feeds, load_paywall_markers, write_report

    config = load_config()
    min_words = int(os.environ.get("AUDIT_MIN_WORDS", "150"))
    # Same feed set and kinds the pipeline uses: FEEDS.md plus overlay-added feeds,
    # each stamped with its kind so post feeds are judged as posts (ok-post).
    lanes = load_lanes_for_run(config.feeds_overlay)
    feeds = parse_feeds_md(config.feeds_path)
    known = {f["source"] for f in feeds}
    feeds += [dict(f) for f in lanes.extra_feeds if f["source"] not in known]
    for feed in feeds:
        feed["kind"] = lanes.kind_for(feed["source"])
    markers = load_paywall_markers()
    audits = asyncio.run(audit_feeds(feeds, days, min_words, markers))

    click.echo(
        f"{'Feed':<28} {'Items':>5} {'MedWords':>8} {'Short%':>7} "
        f"{'Paywall%':>8} {'Errors':>6}  Verdict"
    )
    for a in audits:
        click.echo(
            f"{a.source[:28]:<28} {a.items:>5} {a.median_words:>8.0f} "
            f"{a.short_share * 100:>6.0f}% {a.paywall_share * 100:>7.0f}% "
            f"{a.fetch_errors:>6}  {a.verdict}"
        )
    path = write_report(audits, Path("data"), days, min_words)
    click.echo(f"Wrote {path}")


def _check_date(ctx: click.Context, param: click.Parameter, value: str | None) -> str:
    from datetime import datetime, timezone

    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise click.BadParameter("use YYYY-MM-DD") from None


@cli.command()
@click.option("--model", required=True, help="Local Ollama model to score, e.g. gemma4:e4b")
@click.option("--date", "date", callback=_check_date, help="UTC date to score (default: today)")
def bakeoff(model: str, date: str) -> None:
    """Score a local model's section writing against the day's stored Claude sections.

    Reruns each section on MODEL with the same input, citation format and chunk validator
    as the Claude writer. Publishes nothing, saves no briefing and makes no Claude API call.
    """
    import asyncio
    from pathlib import Path

    from .summarizers.bakeoff import BakeoffError, format_table, run_bakeoff, write_report

    config = load_config()
    db = Database(config.database_path)
    try:
        db.connect()
        report = asyncio.run(run_bakeoff(db, config, model, date))
    except BakeoffError as e:
        click.echo(str(e), err=True)
        sys.exit(1)
    finally:
        db.close()

    click.echo(format_table(report))
    click.echo(f"Wrote {write_report(report, Path('data'))}")


@cli.group()
def filings() -> None:
    """Filings lane: SEC EDGAR 8-K filings."""


@filings.command("fetch")
def filings_fetch() -> None:
    """Fetch new 8-Ks for the universe's stocks from EDGAR and store them.

    Needs CONTACT_EMAIL; without it nothing is requested and the exit code is still 0.
    Works whether or not FILINGS_ENABLED is set (that only gates the daily run).
    """
    import asyncio

    from .fetchers.edgar import fetch_filings

    config = load_config()
    db = Database(config.database_path)
    try:
        db.connect()
        summary = asyncio.run(fetch_filings(config, db))
    finally:
        db.close()
    click.echo(summary.line())


@filings.command("extract")
@click.option(
    "--redo", is_flag=True, help="Re-extract every stored 2.02 filing, not just new ones."
)
def filings_extract(redo: bool) -> None:
    """Extract and verify earnings numbers from stored 2.02 filings (local model).

    Oldest first, so rev_accel and guidance changes can find earlier rows. Prints one
    summary line. Works whether or not FILINGS_ENABLED is set.
    """
    import asyncio

    from .processors.filing_facts import extract_filings

    config = load_config()
    db = Database(config.database_path)
    try:
        db.connect()
        summary = asyncio.run(extract_filings(config, db, redo=redo))
    finally:
        db.close()
    click.echo(summary.line())


@filings.command("emit")
def filings_emit() -> None:
    """Write signals-filings.json from stored filings and facts (manual run).

    Works whether or not FILINGS_ENABLED is set. Prints the path, or a notice if nothing
    was written.
    """
    from pathlib import Path

    from .publishers.signals import load_ticker_map
    from .publishers.signals_filings import emit_filings_signals

    config = load_config()
    db = Database(config.database_path)
    try:
        db.connect()
        path = emit_filings_signals(
            db, config.signals_dir, load_ticker_map(Path("config/ticker_map.toml")), config
        )
    finally:
        db.close()
    click.echo(f"Wrote {path}" if path else "No filings signals written (see log)")


def _check_since(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """--since as YYYY-MM-DD; unset stays None so FILINGS_BACKFILL_SINCE applies."""
    return None if value is None else _check_date(ctx, param, value)


@filings.command("backfill")
@click.option(
    "--since", "since", callback=_check_since, help="Earliest acceptance date (YYYY-MM-DD)."
)
@click.option("--ignore-window", is_flag=True, help="Run outside BACKFILL_WINDOW_UTC (supervised).")
@click.option("--counts", is_flag=True, help="Print the review counts from the DB; no fetching.")
def filings_backfill(since: str | None, ignore_window: bool, counts: bool) -> None:
    """Backfill historical earnings 8-Ks, extract facts, write filings-history.jsonl.

    Resumable: stored accessions and facts rows are skipped. Only runs inside
    BACKFILL_WINDOW_UTC unless --ignore-window is given; --since defaults to
    FILINGS_BACKFILL_SINCE. Needs CONTACT_EMAIL (exit code 0 without it).
    """
    import asyncio
    from pathlib import Path

    from .processors.filings_backfill import backfill_counts, run_backfill

    config = load_config()
    db = Database(config.database_path)
    try:
        db.connect()
        if counts:
            from .publishers.signals import load_ticker_map

            ticker_map = load_ticker_map(Path("config/ticker_map.toml"))
            click.echo(backfill_counts(db, ticker_map, config))
            return
        try:
            summary = asyncio.run(
                run_backfill(config, db, since=since, ignore_window=ignore_window)
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        db.close()
    if not summary.outside_window:
        click.echo(summary.line())


@cli.command()
def deploy() -> None:
    """Deploy the latest dashboard to gh-pages branch."""
    import subprocess
    from pathlib import Path

    config = load_config()
    dashboard = config.output_dir / "dashboard.html"
    script = Path("scripts/deploy-dashboard.sh")

    if not dashboard.exists():
        click.echo(f"Dashboard not found at {dashboard}. Run the pipeline first.")
        sys.exit(1)

    if not script.exists():
        click.echo(f"Deploy script not found at {script}.")
        sys.exit(1)

    result = subprocess.run([str(script), str(dashboard)], text=True)
    sys.exit(result.returncode)


if __name__ == "__main__":
    cli()
