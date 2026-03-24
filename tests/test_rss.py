"""Tests for RSS feed parser."""

from pathlib import Path
from textwrap import dedent

from src.fetchers.rss import parse_feeds_md


def test_parse_feeds_md(tmp_path: Path):
    feeds_file = tmp_path / "FEEDS.md"
    feeds_file.write_text(dedent("""\
        # Feeds

        ## RSS Feeds

        ### US Politics

        | Source | Feed URL | Status | Notes |
        |--------|----------|--------|-------|
        | AP Politics | `https://apnews.com/politics.rss` | untested | Wire service |
        | Broken Feed | `https://broken.example.com/rss` | broken | Do not use |

        ### Crypto

        | Source | Feed URL | Status | Notes |
        |--------|----------|--------|-------|
        | CoinDesk | `https://www.coindesk.com/arc/outboundfeeds/rss/` | active | Major |
    """))

    feeds = parse_feeds_md(feeds_file)

    assert len(feeds) == 2  # broken feed excluded
    assert feeds[0]["source"] == "AP Politics"
    assert feeds[0]["category"] == "US Politics"
    assert feeds[0]["url"] == "https://apnews.com/politics.rss"
    assert feeds[1]["source"] == "CoinDesk"
    assert feeds[1]["category"] == "Crypto"


def test_parse_feeds_md_missing_file(tmp_path: Path):
    result = parse_feeds_md(tmp_path / "nonexistent.md")
    assert result == []
