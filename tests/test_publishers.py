"""Tests for HTML and terminal publishers."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from src.publishers.html import (
    _format_market_data,
    _group_articles_by_category,
    render_dashboard,
)

# --- _group_articles_by_category ---


def _article(category: str, title: str = "Test") -> dict:
    return {"category": category, "title": title, "source": "Test", "url": "#"}


def test_group_preserves_order():
    """Articles are grouped in defined display order."""
    articles = [
        _article("Software Dev / DevOps / AI / ML"),
        _article("US Politics (factual, non-opinion)"),
        _article("World News"),
    ]
    grouped = _group_articles_by_category(articles)
    keys = list(grouped.keys())
    assert keys.index("US Politics (factual, non-opinion)") < keys.index("World News")
    assert keys.index("World News") < keys.index("Software Dev / DevOps / AI / ML")


def test_group_handles_unknown_category():
    """Unknown categories appear after known ones."""
    articles = [
        _article("Unknown Category"),
        _article("World News"),
    ]
    grouped = _group_articles_by_category(articles)
    keys = list(grouped.keys())
    assert keys[-1] == "Unknown Category"


def test_group_empty():
    """Empty input returns empty OrderedDict."""
    result = _group_articles_by_category([])
    assert isinstance(result, OrderedDict)
    assert len(result) == 0


# --- _format_market_data ---


def test_format_market_data_renames_symbols():
    """Symbols get human-readable labels."""
    base = {"data_type": "fred_series", "extra_json": None}
    data = [
        {"symbol": "DGS10", "value": 4.25, "change_pct": 0.05, **base},
        {"symbol": "VIXCLS", "value": 18.5, "change_pct": -1.2, **base},
        {
            "symbol": "CHAIN_ETHEREUM",
            "value": 55e9,
            "change_pct": None,
            "data_type": "chain_tvl",
            "extra_json": None,
        },
    ]
    formatted = _format_market_data(data)
    symbols = [f["symbol"] for f in formatted]
    assert "10Y Treasury" in symbols
    assert "VIX" in symbols
    assert "Ethereum" in symbols


def test_format_market_data_parses_extra_json():
    """Extra JSON field is parsed into dict."""
    data = [
        {
            "symbol": "SPY",
            "value": 450.0,
            "change_pct": 0.5,
            "data_type": "stock_quote",
            "extra_json": '{"open": 448.0, "high": 452.0}',
        }
    ]
    formatted = _format_market_data(data)
    assert formatted[0]["extra"]["open"] == 448.0


def test_format_market_data_handles_bad_json():
    """Bad JSON in extra field doesn't crash."""
    data = [
        {
            "symbol": "SPY",
            "value": 450.0,
            "change_pct": None,
            "data_type": "stock_quote",
            "extra_json": "not json",
        }
    ]
    formatted = _format_market_data(data)
    assert formatted[0]["extra"] == {}


# --- render_dashboard ---


def test_render_dashboard_produces_html(tmp_path):
    """Renders valid HTML with articles and market data."""
    articles = [
        {
            "category": "World News",
            "title": "Test Headline",
            "source": "BBC",
            "url": "https://example.com",
            "summary": "A brief summary.",
        }
    ]
    market_data = [
        {
            "symbol": "SPY",
            "value": 450.0,
            "change_pct": 1.5,
            "data_type": "stock_quote",
            "extra_json": None,
        }
    ]

    template_dir = Path("templates")
    output = tmp_path / "test_dashboard.html"

    html = render_dashboard(
        articles=articles,
        market_data=market_data,
        health_checks=[],
        template_dir=template_dir,
        output_path=output,
    )

    assert "Morning Brief" in html
    assert "Test Headline" in html
    assert "SPY" in html
    assert "450" in html
    assert output.exists()


def test_render_dashboard_empty_data(tmp_path):
    """Renders without error when data is empty."""
    template_dir = Path("templates")
    html = render_dashboard(
        articles=[],
        market_data=[],
        health_checks=[],
        template_dir=template_dir,
    )
    assert "Morning Brief" in html
    assert "0 articles" in html


def test_render_dashboard_with_health_checks(tmp_path):
    """Health checks section renders."""
    template_dir = Path("templates")
    html = render_dashboard(
        articles=[],
        market_data=[],
        health_checks=[
            {"name": "API Server", "is_up": True, "response_ms": 42.5},
            {"name": "Database", "is_up": False, "response_ms": None},
        ],
        template_dir=template_dir,
    )
    assert "API Server" in html
    assert "Database" in html
