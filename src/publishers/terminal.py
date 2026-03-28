"""Rich terminal dashboard publisher.

Renders a quick summary of today's briefing in the terminal
using Rich tables and panels.
"""

from __future__ import annotations

import logging
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

logger = logging.getLogger(__name__)


def _change_color(pct: float | None) -> str:
    """Return color name for a percent change value."""
    if pct is None:
        return "dim"
    return "green" if pct >= 0 else "red"


def _format_change(pct: float | None) -> str:
    """Format a percent change value."""
    if pct is None:
        return "—"
    return f"{pct:+.2f}%"


def render_terminal(
    articles: list[dict[str, Any]],
    market_data: list[dict[str, Any]],
    health_checks: list[dict[str, Any]],
    max_articles_per_category: int = 5,
) -> None:
    """Render the briefing to the terminal using Rich."""
    console = Console()
    console.print()

    # Header
    console.rule("[bold blue]Morning Brief[/bold blue]")
    console.print()

    # Market overview
    if market_data:
        market_table = Table(title="Markets", show_header=True, header_style="bold")
        market_table.add_column("Symbol", style="bold")
        market_table.add_column("Value", justify="right")
        market_table.add_column("Change", justify="right")

        for item in market_data:
            symbol = item["symbol"]
            value = item.get("value")
            change_pct = item.get("change_pct")

            # Format value
            if value is None:
                val_str = "—"
            elif value >= 1_000_000_000:
                val_str = f"${value / 1e9:.1f}B"
            elif value >= 1000:
                val_str = f"${value:,.0f}"
            elif value >= 1:
                val_str = f"${value:,.2f}"
            else:
                val_str = f"{value:,.2f}"

            color = _change_color(change_pct)
            change_str = _format_change(change_pct)

            market_table.add_row(symbol, val_str, Text(change_str, style=color))

        console.print(market_table)
        console.print()

    # Articles by category
    categories: dict[str, list] = {}
    for article in articles:
        cat = article.get("category", "Other")
        categories.setdefault(cat, []).append(article)

    for category, cat_articles in categories.items():
        lines = []
        for article in cat_articles[:max_articles_per_category]:
            source = article.get("source", "?")
            title = article.get("title", "Untitled")
            summary = article.get("summary", "")

            lines.append(f"[bold]{title}[/bold]")
            lines.append(f"  [dim]{source}[/dim]")
            if summary:
                # Truncate long summaries for terminal
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                lines.append(f"  {summary}")
            lines.append("")

        remaining = len(cat_articles) - max_articles_per_category
        if remaining > 0:
            lines.append(f"  [dim]+ {remaining} more articles[/dim]")

        content = "\n".join(lines)
        console.print(Panel(content, title=f"[bold]{category}[/bold]", border_style="blue"))
        console.print()

    # Health checks
    if health_checks:
        health_table = Table(title="Service Health", show_header=True, header_style="bold")
        health_table.add_column("Service")
        health_table.add_column("Status", justify="center")
        health_table.add_column("Response", justify="right")

        for check in health_checks:
            name = check.get("name", "?")
            is_up = check.get("is_up", False)
            response_ms = check.get("response_ms")

            status = Text("UP", style="green") if is_up else Text("DOWN", style="red")
            time_str = f"{response_ms:.0f}ms" if response_ms else "—"

            health_table.add_row(name, status, time_str)

        console.print(health_table)
        console.print()

    # Summary
    console.rule("[dim]End of briefing[/dim]")
    console.print(f"[dim]{len(articles)} articles across {len(categories)} categories[/dim]")
    console.print()
