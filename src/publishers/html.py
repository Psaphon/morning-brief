"""HTML dashboard publisher — Jinja2 rendering.

Loads today's data from the database and renders a mobile-friendly
HTML dashboard using the Jinja2 template.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# Category display order
CATEGORY_ORDER = [
    "US Politics",
    "Florida Politics",
    "World News",
    "Crypto / Web3 / ReFi",
    "Software Dev / DevOps / AI / ML",
    "Art / Ceramics / Visual",
]


def _group_articles_by_category(articles: list[dict[str, Any]]) -> OrderedDict:
    """Group articles by category in display order."""
    grouped: dict[str, list] = {}
    for article in articles:
        cat = article.get("category", "Other")
        grouped.setdefault(cat, []).append(article)

    # Sort by defined order, then alphabetically for any extras
    ordered = OrderedDict()
    for cat in CATEGORY_ORDER:
        # Match partial category names (e.g., "US Politics" matches full name)
        for key in list(grouped.keys()):
            if key.startswith(cat):
                ordered[key] = grouped.pop(key)
    # Append any remaining categories
    for key in sorted(grouped.keys()):
        ordered[key] = grouped[key]

    return ordered


def _format_market_data(market_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format market data for template rendering."""
    formatted = []
    for item in market_data:
        extra = {}
        if item.get("extra_json"):
            try:
                extra = json.loads(item["extra_json"])
            except (json.JSONDecodeError, TypeError):
                pass

        display_symbol = item["symbol"]
        # Clean up display names
        symbol_labels = {
            "DGS2": "2Y Treasury",
            "DGS10": "10Y Treasury",
            "VIXCLS": "VIX",
            "DEFI_TVL": "DeFi TVL",
            "ETH_GAS": "ETH Gas",
        }
        display_symbol = symbol_labels.get(display_symbol, display_symbol)

        # Strip CHAIN_ prefix
        if display_symbol.startswith("CHAIN_"):
            display_symbol = display_symbol[6:].title()

        formatted.append(
            {
                "symbol": display_symbol,
                "value": item.get("value", 0),
                "change_pct": item.get("change_pct"),
                "data_type": item.get("data_type"),
                "extra": extra,
            }
        )

    return formatted


def render_dashboard(
    articles: list[dict[str, Any]],
    market_data: list[dict[str, Any]],
    health_checks: list[dict[str, Any]],
    artworks: list[dict[str, Any]] | None = None,
    briefing: str | None = None,
    template_dir: Path = Path("templates"),
    output_path: Path | None = None,
) -> str:
    """Render the HTML dashboard.

    Returns the rendered HTML string and optionally writes it to a file.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    template = env.get_template("dashboard.html")

    now = datetime.now(timezone.utc)
    categories = _group_articles_by_category(articles)
    formatted_market = _format_market_data(market_data)

    html = template.render(
        date=now.strftime("%A, %B %-d, %Y"),
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        total_articles=len(articles),
        categories=categories,
        market_data=formatted_market,
        health_checks=health_checks,
        artworks=artworks or [],
        briefing=briefing,
    )

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html)
        logger.info("Dashboard written to %s", output_path)

    return html
