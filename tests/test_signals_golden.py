"""End-to-end golden test for the signals artifact.

Runs the real ``python -m src.cli run`` command against synthetic fixture
articles and pins the resulting ``signals-<date>.json``. Every later feature
must leave atrade's input unchanged, or regenerate the golden file explicitly
with ``python scripts/regen_golden.py`` in a PR that changes the signal contract.

Boundaries are faked at the HTTP layer only, so real code runs throughout:
  - RSS feeds and article pages are served by a tiny local server, reached
    through ``HTTP_PROXY`` so the fixture URLs (and hence url_hash) are stable.
  - Ollama is ``tests/stub_ollama.py``, which records every request.
  - Market, crypto and artwork fetchers are replaced with empty results
    (they are unrelated to signals and would hit the network).

Fixture published times are far enough in the past that the scorer's recency
factor is 0.0 (> 48h old), which keeps scores independent of the wall clock.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from tests.stub_ollama import StubOllama

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "brief_fixture"
ARTICLES_PATH = FIXTURE_DIR / "articles.json"
GOLDEN_PATH = FIXTURE_DIR / "signals.golden.json"

IGNORED_KEYS = ("emitted_at",)


def load_fixture_articles() -> list[dict]:
    return json.loads(ARTICLES_PATH.read_text(encoding="utf-8"))


def _article_url(article: dict) -> str:
    return f"http://news.example.test/a/{article['id']}"


def _feed_url(category: str) -> str:
    return f"http://feeds.example.test/{category.lower()}.xml"


def _rss(category: str, articles: list[dict]) -> str:
    items = []
    for art in articles:
        pub = datetime.fromisoformat(art["published_at"].replace("Z", "+00:00"))
        items.append(
            "<item>"
            f"<title>{escape(art['title'])}</title>"
            f"<link>{_article_url(art)}</link>"
            f"<pubDate>{format_datetime(pub, usegmt=True)}</pubDate>"
            f"<description>{escape(art['text'])}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        f"<title>{escape(category)}</title><link>http://feeds.example.test/</link>"
        f"<description>fixture</description>{''.join(items)}</channel></rss>"
    )


def _make_fixture_server(articles: list[dict]) -> ThreadingHTTPServer:
    """Proxy-style server: serves feeds; article pages 404 so the RSS summary is used."""
    feeds: dict[str, str] = {}
    for category in dict.fromkeys(a["category"] for a in articles):
        in_cat = [a for a in articles if a["category"] == category]
        feeds[urlparse(_feed_url(category)).path] = _rss(category, in_cat)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = feeds.get(urlparse(self.path).path)
            if body is None:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _feeds_md(articles: list[dict]) -> str:
    lines = ["# Fixture feeds", "", "## RSS Feeds", ""]
    for category in dict.fromkeys(a["category"] for a in articles):
        lines += [
            f"### {category}",
            "",
            "| Source | Feed URL | Status | Notes |",
            "|--------|----------|--------|-------|",
            f"| Fixture {category} | `{_feed_url(category)}` | active | fixture |",
            "",
        ]
    return "\n".join(lines)


async def _no_results(*args: object, **kwargs: object) -> list:
    return []


@contextlib.contextmanager
def _environment(workdir: Path, ollama_url: str, proxy_url: str) -> Iterator[None]:
    """cwd, env and fetcher patches for one CLI run. Restores everything on exit."""
    env = {
        "OLLAMA_HOST": ollama_url,
        "OLLAMA_MODEL": "gemma4:e4b",
        "DATABASE_PATH": str(workdir / "data" / "morning_brief.db"),
        "OUTPUT_DIR": str(workdir / "data" / "output"),
        "SIGNALS_ENABLED": "true",
        "SIGNALS_DIR": str(workdir / "data"),
        "HTTP_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "ANTHROPIC_API_KEY": "",
        "DEPLOY_ENABLED": "",
    }
    saved_env = {k: os.environ.get(k) for k in env}
    saved_cwd = Path.cwd()
    os.environ.update(env)
    os.chdir(workdir)
    try:
        with (
            mock.patch("src.main.fetch_all_financial", _no_results),
            mock.patch("src.main.fetch_all_crypto", _no_results),
            mock.patch("src.main.fetch_daily_artwork", _no_results),
            mock.patch("src.summarizers.local.BATCH_PAUSE_SECONDS", 0.0),
        ):
            yield
    finally:
        os.chdir(saved_cwd)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_pipeline_on(articles: list[dict], workdir: Path) -> tuple[dict, StubOllama]:
    """Run ``cli run`` end-to-end on *articles*; return (signals artifact, stub).

    Raises RuntimeError if the command fails or no signals artifact is written.
    """
    import src.main  # noqa: F401  (patch targets must be importable)
    from src.cli import cli

    (workdir / "docs").mkdir(parents=True)
    (workdir / "config").mkdir()
    (workdir / "docs" / "FEEDS.md").write_text(_feeds_md(articles), encoding="utf-8")
    # The real alias map, not a copy-edited one.
    shutil.copy(REPO_ROOT / "config" / "ticker_map.toml", workdir / "config" / "ticker_map.toml")

    # The dashboard renderer resolves templates relative to the cwd.
    (workdir / "templates").symlink_to(REPO_ROOT / "templates", target_is_directory=True)

    server = _make_fixture_server(articles)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with StubOllama() as stub:
            with _environment(workdir, stub.url, f"http://127.0.0.1:{server.server_port}"):
                result = CliRunner().invoke(cli, ["run"])
            if result.exit_code != 0:
                raise RuntimeError(
                    f"`cli run` exited {result.exit_code}: {result.exception!r}\n{result.output}"
                )
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            artifact_path = workdir / "data" / f"signals-{date}.json"
            if not artifact_path.exists():
                raise RuntimeError(f"No signals artifact at {artifact_path}\n{result.output}")
            return json.loads(artifact_path.read_text(encoding="utf-8")), stub
    finally:
        server.shutdown()
        server.server_close()


def run_in_temp_dir(articles: list[dict]) -> tuple[dict, StubOllama]:
    with tempfile.TemporaryDirectory(prefix="brief-golden-") as tmp:
        return run_pipeline_on(articles, Path(tmp) / "run")


def strip_ignored(artifact: dict) -> dict:
    return {k: v for k, v in artifact.items() if k not in IGNORED_KEYS}


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------


def test_fixture_shape() -> None:
    articles = load_fixture_articles()
    assert len(articles) >= 12
    assert len({a["category"] for a in articles}) >= 3
    assert len({a["id"] for a in articles}) == len(articles)
    for a in articles:
        assert a["published_at"].endswith("Z")
        datetime.fromisoformat(a["published_at"].replace("Z", "+00:00"))
    per_cat: dict[str, int] = {}
    for a in articles:
        per_cat[a["category"]] = per_cat.get(a["category"], 0) + 1
    # select_balanced_articles caps at 8 per category; stay under it so every
    # article is summarised and the call count is articles + 1.
    assert max(per_cat.values()) <= 8


def test_fixture_matches_real_alias_map() -> None:
    from src.publishers.signals import load_ticker_map, match_articles_to_tickers

    ticker_map = load_ticker_map(REPO_ROOT / "config" / "ticker_map.toml")
    articles = [
        {"id": a["id"], "title": a["title"], "full_text": a["text"]}
        for a in load_fixture_articles()
    ]
    matched = match_articles_to_tickers(articles, ticker_map)
    matching_articles = {a["id"] for arts in matched.values() for a in arts}
    assert len(matching_articles) >= 4


def test_fixture_has_same_story_pair() -> None:
    pair = [a for a in load_fixture_articles() if "$3,847,000,000" in a["title"]]
    assert len(pair) == 2
    assert pair[0]["title"] != pair[1]["title"]


# ---------------------------------------------------------------------------
# Pipeline tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline() -> tuple[dict, StubOllama]:
    return run_in_temp_dir(load_fixture_articles())


def test_signals_match_golden(baseline: tuple[dict, StubOllama]) -> None:
    artifact, _ = baseline
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert strip_ignored(artifact) == strip_ignored(golden)


def test_golden_is_live() -> None:
    """Break one ticker match; the output must differ from the golden file."""
    articles = copy.deepcopy(load_fixture_articles())
    target = next(a for a in articles if a["id"] == "tech-nvda")
    target["title"] = "A chipmaker unveils next-generation data center accelerator"
    target["text"] = target["text"].replace("Nvidia", "The company")
    artifact, _ = run_in_temp_dir(articles)

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert strip_ignored(artifact) != strip_ignored(golden)
    assert "NVDA" not in {s["ticker"] for s in artifact["signals"]}


def test_generate_call_count(baseline: tuple[dict, StubOllama]) -> None:
    """One summary call per non-duplicate article plus one briefing call.

    The fixture's same-story pair costs a single summary call (briefing-dedupe).
    """
    _, stub = baseline
    assert len(stub.generate_calls) == len(load_fixture_articles()) - 1 + 1
