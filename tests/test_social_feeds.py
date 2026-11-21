"""Tests for social-feeds: feed kinds, post grouping and extraction, newsletters, audit."""

from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from src.fetchers.rss import RawArticle, fetch_feed, html_to_text
from src.processors.extractor import extract_articles, extract_single
from src.processors.lanes import load_feed_lanes
from src.processors.posts import group_posts, post_text
from src.processors.source_audit import audit_feeds
from src.summarizers.local import extract_post
from tests.stub_ollama import EXTRACTION_MARKER, StubOllama
from tests.test_signals_golden import _environment

FIXTURES = Path(__file__).parent / "fixtures" / "social"
BSKY = "https://bsky.example.test/profile.xml"
MASTO = "https://masto.example.test/@reporter.rss"
NEWS = "https://news.example.test/feed"
FEED_FILES = {BSKY: "bluesky_profile.xml", MASTO: "mastodon_profile.xml", NEWS: "newsletter.xml"}
TEASER_URL = "https://news.example.test/p/teaser-only"
FULL_URL = "https://news.example.test/p/full-issue"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _feed_transport(extra=None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in FEED_FILES:
            return httpx.Response(200, text=(FIXTURES / FEED_FILES[url]).read_text())
        if extra:
            return extra(request)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _fetch(url: str, source: str, kind: str) -> list[RawArticle]:
    async with httpx.AsyncClient(transport=_feed_transport()) as client:
        return await fetch_feed(
            client, {"source": source, "url": url, "category": "Tech", "kind": kind}
        )


# --- kind in the lanes config ------------------------------------------------


def test_kind_defaults_to_article_and_is_exposed(tmp_path):
    f = _write(
        tmp_path / "l.toml",
        '[feeds."A"]\nlanes = ["reader"]\n[feeds."N"]\nlanes = ["reader"]\nkind = "newsletter"\n',
    )
    lanes = load_feed_lanes(f)
    assert lanes.kind_for("A") == "article"
    assert lanes.kind_for("N") == "newsletter"
    assert lanes.kind_for("Unlisted") == "article"


def test_post_feed_without_lanes_is_reader_only(tmp_path):
    f = _write(tmp_path / "l.toml", '[feeds."P"]\nkind = "post"\n')
    lanes = load_feed_lanes(f)
    assert lanes.lanes_for("P") == {"reader"}
    assert lanes.lanes_for("Other") == {"reader", "signals"}


def test_overlay_post_feed_is_added_reader_only(tmp_path):
    base = _write(tmp_path / "l.toml", "")
    overlay = _write(
        tmp_path / "o.toml",
        '[feeds."Reporter"]\nurl = "https://x.example/rss"\ncategory = "Tech"\nkind = "post"\n',
    )
    lanes = load_feed_lanes(base, overlay=overlay)
    assert lanes.lanes_for("Reporter") == {"reader"}
    assert lanes.kind_for("Reporter") == "post"
    assert lanes.extra_feeds[0]["source"] == "Reporter"


def test_explicit_lanes_win_over_post_default(tmp_path):
    f = _write(tmp_path / "l.toml", '[feeds."P"]\nkind = "post"\nlanes = ["reader", "rnd"]\n')
    assert load_feed_lanes(f).lanes_for("P") == {"reader", "rnd"}


@pytest.mark.parametrize("where", ["committed", "overlay"])
def test_unknown_kind_raises(tmp_path, where):
    bad = '[feeds."X"]\nlanes = ["reader"]\nkind = "tweet"\n'
    if where == "committed":
        with pytest.raises(ValueError, match="tweet"):
            load_feed_lanes(_write(tmp_path / "l.toml", bad))
    else:
        base = _write(tmp_path / "l.toml", "")
        with pytest.raises(ValueError, match="tweet"):
            load_feed_lanes(base, overlay=_write(tmp_path / "o.toml", bad))


# --- fetch -------------------------------------------------------------------


def test_html_to_text_strips_tags_and_entities():
    assert html_to_text("<p>A &amp; B</p><p>C<br/>D</p>") == "A & B\nC\nD"
    assert html_to_text(None) == ""


async def test_mastodon_html_is_stripped_and_kind_carried():
    articles = await _fetch(MASTO, "Masto", "post")
    assert {a.kind for a in articles} == {"post"}
    text = post_text(next(a for a in articles if a.url.endswith("1001")))
    assert "<" not in text
    assert "Shipping volumes rose & held steady." in text


async def test_newsletter_content_is_read_from_content_encoded():
    articles = {a.url: a for a in await _fetch(NEWS, "News", "newsletter")}
    assert len(articles[FULL_URL].content.split()) > 150
    assert articles[TEASER_URL].content is None


# --- grouping ----------------------------------------------------------------


async def test_bluesky_thread_groups_to_two_entries():
    raw = await _fetch(BSKY, "Reporter", "post")
    assert len(raw) == 4
    grouped = group_posts(raw, 12)
    assert len(grouped) == 2
    thread = next(a for a in grouped if "council" in a.title)
    assert thread.url.endswith("/post/a1")
    assert thread.published == "Mon, 05 Jan 2026 09:00:00 GMT"
    assert thread.content.index("1/3") < thread.content.index("2/3") < thread.content.index("3/3")
    assert len(thread.title) <= 100
    assert len(raw) == 4  # input untouched


async def test_group_window_is_configurable_and_other_kinds_pass_through():
    raw = await _fetch(BSKY, "Reporter", "post")
    # Gaps are 40 and 50 minutes: a 45-minute window keeps the first two together only.
    assert len(group_posts(raw, 0.75)) == 3
    assert len(group_posts(raw, 30)) == 1
    art = RawArticle(
        url="u", title="t", source="Reporter", category="c", published=raw[0].published
    )
    assert art in group_posts([art, *raw], 12)


def test_posts_without_author_group_by_feed_and_authors_stay_apart():
    def post(n, author, hour):
        return RawArticle(
            url=f"u{n}",
            title=f"t{n}",
            source="Feed",
            category="c",
            author=author,
            published=f"Mon, 05 Jan 2026 {hour:02d}:00:00 GMT",
            summary=f"text {n}",
            kind="post",
        )

    assert len(group_posts([post(1, None, 1), post(2, None, 2)], 12)) == 1
    assert len(group_posts([post(1, "a", 1), post(2, "b", 2)], 12)) == 2


def test_long_title_is_cut_to_100_chars():
    long = "word " * 60
    mk = lambda n: RawArticle(  # noqa: E731
        url=f"u{n}",
        title="x",
        source="F",
        category="c",
        published=f"Mon, 05 Jan 2026 0{n}:00:00 GMT",
        summary=long,
        kind="post",
    )
    (merged,) = group_posts([mk(1), mk(2)], 12)
    assert len(merged.title) == 100


# --- extraction --------------------------------------------------------------


def _no_page_fetch(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected page fetch: {request.url}")


async def test_post_extract_makes_no_page_fetch():
    (post,) = (a for a in await _fetch(MASTO, "Masto", "post") if a.url.endswith("1001"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_no_page_fetch)) as client:
        row = await extract_single(client, post)
    assert row["full_text"].startswith("Port traffic is up")


async def test_full_content_newsletter_skips_page_fetch_and_teaser_fetches():
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if str(request.url) == FULL_URL:
            raise AssertionError("page fetched despite full feed content")
        return httpx.Response(200, text="<html><body><p>nothing useful</p></body></html>")

    articles = {a.url: a for a in await _fetch(NEWS, "News", "newsletter")}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        full = await extract_single(client, articles[FULL_URL])
        teaser = await extract_single(client, articles[TEASER_URL])
    assert full["full_text"] == articles[FULL_URL].content
    assert fetched == [TEASER_URL]
    assert teaser["full_text"]  # fell back to the feed summary


async def test_newsletter_threshold_follows_audit_min_words(monkeypatch):
    monkeypatch.setenv("AUDIT_MIN_WORDS", "100000")
    articles = {a.url: a for a in await _fetch(NEWS, "News", "newsletter")}
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await extract_single(client, articles[FULL_URL])
    assert fetched == [FULL_URL]


async def test_extract_articles_never_needs_network_for_posts(monkeypatch):
    raw = group_posts(await _fetch(BSKY, "Reporter", "post"), 12)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_no_page_fetch))
    )
    assert len(await extract_articles(raw)) == 2


def test_extract_post_shape_and_word_cap():
    text = "word " * 400
    result = extract_post({"id": 7, "full_text": text.strip() + "."})
    ex = result.extraction
    assert result.success and result.model == "post"
    assert len(ex["what_happened"].split()) <= 120
    assert (ex["details"], ex["entities"], ex["numbers"], ex["event_date"]) == ([], [], [], None)
    assert (ex["status"], ex["is_roundup"]) == ("happened", False)
    assert extract_post({"id": 8, "full_text": "  "}).extraction_failed


# --- pipeline: no generate call for posts ------------------------------------


def _serve(files: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = next((v for k, v in files.items() if self.path.endswith(k)), None)
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


def test_pipeline_posts_make_no_generate_call(tmp_path, monkeypatch):
    from src.cli import cli

    work = tmp_path / "run"
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "FEEDS.md").write_text("# Feeds\n\n## RSS Feeds\n", encoding="utf-8")
    (work / "templates").symlink_to(
        Path(__file__).resolve().parent.parent / "templates", target_is_directory=True
    )
    overlay = _write(
        tmp_path / "overlay.toml",
        '[feeds."Reporter (Bluesky)"]\nurl = "http://social.example.test/bluesky.xml"\n'
        'category = "Tech"\nkind = "post"\n',
    )
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", str(overlay))
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    server = _serve({"bluesky.xml": (FIXTURES / "bluesky_profile.xml").read_text()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with StubOllama() as stub:
            with _environment(work, stub.url, f"http://127.0.0.1:{server.server_port}"):
                monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", str(overlay))
                result = CliRunner().invoke(cli, ["run"])
            assert result.exit_code == 0, result.output
            extraction_calls = [
                c for c in stub.generate_calls if EXTRACTION_MARKER in c["body"]["prompt"]
            ]
    finally:
        server.shutdown()
        server.server_close()

    assert extraction_calls == []
    conn = sqlite3.connect(work / "data" / "morning_brief.db")
    rows = conn.execute("SELECT summary, summary_model FROM articles").fetchall()
    conn.close()
    assert len(rows) == 2  # four posts, one thread
    assert {m for _, m in rows} == {"post"}
    assert all(len(s.split()) <= 120 for s, _ in rows)


# --- source audit ------------------------------------------------------------


async def test_audit_gives_ok_post_for_short_post_feed():
    feeds = [
        {"source": "Reporter", "url": BSKY, "category": "c", "kind": "post"},
        {"source": "Plain", "url": BSKY, "category": "c"},
    ]
    client = httpx.AsyncClient(transport=_feed_transport(lambda r: httpx.Response(500)))
    async with client:
        audits = {a.source: a for a in await audit_feeds(feeds, days=36500, client=client)}
    assert audits["Reporter"].verdict == "ok-post"
    assert audits["Reporter"].fetch_errors == 0
    assert audits["Plain"].verdict == "failing"  # same short items, but page fetches fail


async def test_audit_post_feed_that_fails_is_still_failing():
    feeds = [
        {"source": "Gone", "url": "https://gone.example.test/rss", "category": "c", "kind": "post"}
    ]
    client = httpx.AsyncClient(transport=_feed_transport())
    async with client:
        (audit,) = await audit_feeds(feeds, days=7, client=client)
    assert audit.verdict == "failing"


def test_audit_cli_passes_feed_kinds(tmp_path, monkeypatch):
    """audit-sources must stamp each feed's kind, or post feeds never get ok-post."""
    from click.testing import CliRunner

    import src.cli as cli_mod
    import src.processors.source_audit as audit_mod

    overlay = tmp_path / "feeds-overlay.toml"
    overlay.write_text(
        '[feeds."Fixture Reporter Posts"]\n'
        'url = "https://posts.example/rss"\ncategory = "World News"\nkind = "post"\n'
    )
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", str(overlay))
    seen = {}

    async def fake_audit(feeds, days, min_words, markers):
        seen.update({f["source"]: f.get("kind") for f in feeds})
        return []

    monkeypatch.setattr(audit_mod, "audit_feeds", fake_audit)
    monkeypatch.setattr(audit_mod, "write_report", lambda *a, **k: tmp_path / "report.json")
    result = CliRunner().invoke(cli_mod.cli, ["audit-sources", "--days", "1"])
    assert result.exit_code == 0, result.output
    assert seen.get("Fixture Reporter Posts") == "post"
    assert seen.get("ArXiv CS.AI") == "article"


def test_untitled_post_entries_get_a_title_from_their_text():
    """Real Bluesky/Mastodon profile RSS items have no <title>; they must not be skipped."""
    from src.fetchers.rss import _title_from_text

    title = _title_from_text("<p>Port traffic is up.</p><p>More.</p>")
    assert title.startswith("Port traffic is up.")
    long = "<p>" + "word " * 60 + "</p>"
    assert len(_title_from_text(long)) <= 100
    assert _title_from_text(None) == ""
