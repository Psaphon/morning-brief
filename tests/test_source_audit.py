"""Tests for the source audit."""

import json
import sqlite3

import httpx
from click.testing import CliRunner

from src.cli import cli
from src.processors.source_audit import audit_feeds, load_paywall_markers

WORDS = " ".join(f"word{i}" for i in range(400))
TEASER = " ".join(f"tease{i}" for i in range(40)) + " Subscribe to continue reading."


def _rss(base: str, n: int = 4) -> str:
    items = "".join(
        f"<item><title>T{i}</title><link>https://{base}/a{i}</link></item>" for i in range(n)
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'
    )


def _page(text: str) -> str:
    return f"<html><body><article><h1>Title</h1><p>{text}</p></article></body></html>"


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "err.test":
        return httpx.Response(500)
    if path == "/feed":
        return httpx.Response(200, text=_rss(host))
    return httpx.Response(200, text=_page(TEASER if host == "wall.test" else WORDS))


FEEDS = [
    {"source": "Full", "url": "https://full.test/feed", "category": "c"},
    {"source": "Wall", "url": "https://wall.test/feed", "category": "c"},
    {"source": "Err", "url": "https://err.test/feed", "category": "c"},
]


async def _run():
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    async with client:
        audits = await audit_feeds(FEEDS, days=7, min_words=150, client=client)
    return {a.source: a for a in audits}


async def test_verdicts():
    audits = await _run()
    assert audits["Full"].verdict == "ok"
    assert audits["Full"].median_words >= 150
    assert audits["Wall"].verdict == "paywalled"
    assert audits["Wall"].paywall_share == 1.0
    assert audits["Wall"].short_share == 1.0
    assert audits["Err"].verdict == "failing"
    assert audits["Err"].fetch_errors >= 1


def test_markers_file_loads():
    markers = load_paywall_markers()
    assert "subscribe to continue" in markers
    assert "sign in to read" in markers


def test_cli_prints_rows_writes_json_and_is_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paywall_markers.txt").write_text("subscribe to continue\n")
    feeds_md = tmp_path / "FEEDS.md"
    feeds_md.write_text(
        "### Test\n\n| Source | Feed URL | Status | Notes |\n|---|---|---|---|\n"
        "| Full | `https://full.test/feed` | active | |\n"
        "| Wall | `https://wall.test/feed` | active | |\n"
        "| Err | `https://err.test/feed` | active | |\n"
    )
    db_path = tmp_path / "mb.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE articles (id INTEGER)")
    con.execute("INSERT INTO articles VALUES (1)")
    con.commit()
    con.close()
    signals = tmp_path / "signals"
    signals.mkdir()
    (signals / "s.json").write_text("{}")
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    real_client = httpx.AsyncClient

    def mock_client(*a, **kw):
        kw["transport"] = httpx.MockTransport(_handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    monkeypatch.setattr("src.cli.load_config", _cfg(feeds_md, db_path))

    result = CliRunner().invoke(cli, ["audit-sources", "--days", "7"])
    assert result.exit_code == 0, result.output
    for name, verdict in [("Full", "ok"), ("Wall", "paywalled"), ("Err", "failing")]:
        line = next(ln for ln in result.output.splitlines() if ln.startswith(name))
        assert line.endswith(verdict)

    reports = list((tmp_path / "data").glob("source-audit-*.json"))
    assert len(reports) == 1
    assert len(json.loads(reports[0].read_text())["feeds"]) == 3

    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    con.close()
    assert [p.name for p in signals.iterdir()] == ["s.json"]


def _cfg(feeds_md, db_path):
    from src.config import load_config

    def load():
        cfg = load_config()
        cfg.feeds_path = feeds_md
        cfg.database_path = db_path
        return cfg

    return load
