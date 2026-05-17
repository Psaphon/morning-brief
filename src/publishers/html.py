"""HTML dashboard publisher — Jinja2 rendering.

Loads today's data from the database and renders a mobile-friendly
HTML dashboard using the Jinja2 template.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import time
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


def compute_brief_token(hmac_key: str, brief_id: str) -> str:
    """Compute a dashboard authentication token for the given brief.

    Token format: "{timestamp_s}:{hmac_hex}"
    HMAC input:   "{brief_id}:{timestamp_s}"

    The token embeds a Unix timestamp so the Worker can enforce a 48-hour
    expiry window and reject replays of old briefs.
    """
    timestamp_s = int(time.time())
    message = f"{brief_id}:{timestamp_s}"
    sig = _hmac.new(hmac_key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp_s}:{sig}"


def compute_article_sig(
    hmac_key: str, article_id: int, title: str, source: str, summary: str
) -> str:
    """Compute a per-article HMAC signature.

    HMAC input: canonical JSON (no spaces) of {id, title, source, summary}
    in that exact key order, matching JSON.stringify({id, title, source, summary})
    in the Worker's auth.js.
    """
    canonical = json.dumps(
        {"id": article_id, "title": title, "source": source, "summary": summary},
        separators=(",", ":"),
    )
    return _hmac.new(hmac_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _group_articles_by_category(articles: list[dict[str, Any]]) -> OrderedDict:
    """Group articles by category in display order.

    Within each category, summarized articles appear first (sorted by score
    descending), followed by unsummarized articles as title-only links.
    """
    grouped: dict[str, list] = {}
    for article in articles:
        cat = article.get("category", "Other")
        grouped.setdefault(cat, []).append(article)

    # Within each category: summarized first (by score), then unsummarized
    for cat, cat_articles in grouped.items():
        summarized = [a for a in cat_articles if a.get("summary")]
        unsummarized = [a for a in cat_articles if not a.get("summary")]
        summarized.sort(key=lambda a: a.get("score") or 0, reverse=True)
        grouped[cat] = summarized + unsummarized

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
    briefing_segments: list[dict[str, Any]] | None = None,
    template_dir: Path = Path("templates"),
    output_path: Path | None = None,
    hmac_key: str = "",
    brief_id: str = "",
) -> str:
    """Render the HTML dashboard.

    Returns the rendered HTML string and optionally writes it to a file.

    Args:
        briefing_segments: Parsed segment list from the structured briefing, or
            None if unavailable. Passed to the template as JSON for JavaScript
            expand/collapse UI.
        hmac_key: Pipeline HMAC key (DASHBOARD_HMAC_KEY env var). When set,
            a dashboard authentication token is embedded in the HTML and each
            article receives a per-article signature so the Worker can verify
            that article content was generated by the pipeline.
        brief_id: Identifier for this brief (today's UTC date, YYYY-MM-DD).
            Used as input to the dashboard token HMAC.
    """
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    template = env.get_template("dashboard.html")

    now = datetime.now(timezone.utc)
    categories = _group_articles_by_category(articles)
    formatted_market = _format_market_data(market_data)

    # Build articles_by_id — include per-article HMAC sigs when key is present
    articles_by_id: dict[str, dict[str, Any]] = {}
    for a in articles:
        if a.get("id") is None:
            continue
        art_id = a["id"]
        title = a.get("title") or ""
        source = a.get("source") or ""
        summary = a.get("summary") or ""

        entry: dict[str, Any] = {
            "id": art_id,
            "title": title,
            "url": a.get("url") or "",
            "source": source,
            "summary": summary,
        }

        if hmac_key:
            entry["sig"] = compute_article_sig(hmac_key, art_id, title, source, summary)

        articles_by_id[str(art_id)] = entry

    # Compute per-brief dashboard token when key is present
    dashboard_token = ""
    if hmac_key and brief_id:
        dashboard_token = compute_brief_token(hmac_key, brief_id)

    html = template.render(
        date=now.strftime("%A, %B %-d, %Y"),
        generated_at=now.strftime("%Y-%m-%d %H:%M UTC"),
        total_articles=len(articles),
        categories=categories,
        market_data=formatted_market,
        health_checks=health_checks,
        artworks=artworks or [],
        briefing=briefing,
        briefing_segments_json=json.dumps(briefing_segments or []),
        articles_by_id_json=json.dumps(articles_by_id),
        dashboard_token=dashboard_token,
    )

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html)
        logger.info("Dashboard written to %s", output_path)

    return html
