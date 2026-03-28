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

        output_path = Path(output) if output else config.output_dir / "dashboard.html"
        render_dashboard(
            articles=articles,
            market_data=market_data,
            health_checks=health_checks,
            artworks=artworks,
            output_path=output_path,
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
