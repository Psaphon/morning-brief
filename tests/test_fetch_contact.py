"""fetch-contact: one User-Agent with a contact on every request; footer; no leaks."""

import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import src.http as shttp
from src.config import load_config
from src.fetchers.health import check_all_endpoints
from src.fetchers.rss import RawArticle, fetch_all_feeds
from src.processors.extractor import extract_articles
from src.publishers.html import render_dashboard

# Built at runtime so this file never contains the literal value the leak scan looks for.
ADDR = "-".join(["alias", "7f3a"]) + "@" + "example.test"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
RSS = (
    b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
    b"<item><title>x</title><link>http://x.test/1</link><description>d</description></item>"
    b"</channel></rss>"
)


@pytest.fixture
def stub():
    seen: list[tuple[str, str | None]] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("User-Agent")))
            body = RSS if self.path.startswith("/feed") else b"<html><body>hi</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/xml" if body is RSS else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    shttp._warned = False
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("CONTACT_URL", raising=False)


def _feeds_md(tmp_path, base):
    p = tmp_path / "FEEDS.md"
    p.write_text(
        f"## RSS Feeds\n\n### World News\n\n| Source | Feed URL | Status | Notes |\n"
        f"|---|---|---|---|\n| Stub | `{base}/feed` | active | test |\n"
    )
    return p


async def _all_request_types(tmp_path, base):
    await fetch_all_feeds(_feeds_md(tmp_path, base))
    await extract_articles(
        [RawArticle(title="x", url=f"{base}/article", source="S", category="World News")]
    )
    await check_all_endpoints([{"url": f"{base}/health", "name": "stub"}])


async def test_every_request_type_sends_one_contact_user_agent(stub, tmp_path, monkeypatch):
    base, seen = stub
    monkeypatch.setenv("CONTACT_EMAIL", ADDR)
    await _all_request_types(tmp_path, base)
    paths = {p for p, _ in seen}
    assert {"/feed", "/article", "/health"} <= paths
    agents = {ua for _, ua in seen}
    assert len(agents) == 1
    ua = agents.pop()
    assert ua.startswith(f"{shttp.TOOL_NAME}/") and ADDR in ua


async def test_url_used_when_email_unset(stub, tmp_path, monkeypatch):
    base, seen = stub
    monkeypatch.setenv("CONTACT_URL", "https://example.test/about")
    await fetch_all_feeds(_feeds_md(tmp_path, base))
    assert "https://example.test/about" in seen[0][1]


async def test_unset_contact_still_fetches_and_warns_once(stub, tmp_path, caplog):
    base, seen = stub
    with caplog.at_level(logging.WARNING):
        articles = await fetch_all_feeds(_feeds_md(tmp_path, base))
        await fetch_all_feeds(_feeds_md(tmp_path, base))
    assert articles and seen
    assert all("@" not in ua and "contact" not in ua for _, ua in seen)
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "CONTACT" in r.message
    ]
    assert len(warnings) == 1


def _page(**kw):
    return render_dashboard([], [], [], template_dir=TEMPLATES, **kw)


def test_footer_with_and_without_contact():
    assert f"Contact: {ADDR}" in _page(contact_email=ADDR)
    assert "Contact:" not in _page()
    assert "Contact:" not in _page(contact_email="  ")


def test_config_reads_contact(monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", ADDR)
    monkeypatch.setenv("CONTACT_URL", "https://example.test")
    cfg = load_config()
    assert (cfg.contact_email, cfg.contact_url) == (ADDR, "https://example.test")


async def test_address_never_in_tracked_files_or_logs(stub, tmp_path, monkeypatch, caplog):
    base, _ = stub
    monkeypatch.setenv("CONTACT_EMAIL", ADDR)
    with caplog.at_level(logging.DEBUG):
        await _all_request_types(tmp_path, base)
        _page(contact_email=ADDR)
    assert ADDR not in caplog.text
    root = Path(__file__).resolve().parent.parent
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.split(b"\0")
    for name in filter(None, files):
        f = root / name.decode()
        if f.is_file():
            assert ADDR.encode() not in f.read_bytes(), f"{name!r} contains the address"


def test_compose_passes_the_host_contact_file_optionally():
    """The pipeline container only sees CONTACT_EMAIL through this optional env_file."""
    from pathlib import Path

    import yaml

    compose_file = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_file.read_text())
    env_files = compose["services"]["morning-brief"]["env_file"]
    entry = next(e for e in env_files if isinstance(e, dict) and "contact.env" in e["path"])
    assert entry["path"] == "${HOME}/.config/morning-brief/contact.env"
    assert entry["required"] is False
