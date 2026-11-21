"""Tests for feed-lanes: loading, overlay, selection gating and the FEEDS.md parser."""

import logging
import tomllib
from pathlib import Path

import pytest

from src.config import load_config
from src.db import Database
from src.fetchers.rss import parse_feeds_md
from src.processors.lanes import (
    DEFAULT_LANES_PATH,
    FeedLanes,
    ReaderLaneDB,
    filter_lane,
    load_feed_lanes,
)
from tests.test_signals_golden import load_fixture_articles, run_pipeline_on

REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _art(i, source, title=None):
    return {"id": i, "source": source, "title": title or f"Story {i}", "category": "Tech"}


# --- loading ---------------------------------------------------------------


def test_unknown_source_gets_default_reader_and_signals(tmp_path):
    f = _write(tmp_path / "l.toml", 'default = ["reader", "signals"]\n')
    assert load_feed_lanes(f).lanes_for("Never Heard Of It") == {"reader", "signals"}
    # Even when the file has no `default` key at all.
    assert load_feed_lanes(_write(tmp_path / "e.toml", "")).lanes_for("X") == {"reader", "signals"}


@pytest.mark.parametrize(
    "body",
    [
        'lanes = ["reader", "weather"]',  # unknown lane
        "lanes = []",  # empty list
        'lanes = "reader"',  # not a list
    ],
)
def test_bad_lanes_in_committed_file_raise_value_error(tmp_path, body):
    f = _write(tmp_path / "l.toml", f'[feeds."Fixture Feed"]\n{body}\n')
    with pytest.raises(ValueError, match="Fixture Feed"):
        load_feed_lanes(f)


def test_bad_default_raises(tmp_path):
    with pytest.raises(ValueError):
        load_feed_lanes(_write(tmp_path / "l.toml", 'default = ["nope"]\n'))


def test_committed_file_arxiv_is_rnd_only():
    lanes = load_feed_lanes(DEFAULT_LANES_PATH)
    assert lanes.lanes_for("ArXiv CS.AI") == {"rnd"}
    assert lanes.lanes_for("ProPublica") == {"reader", "signals"}


def test_every_feeds_md_feed_has_lanes_entry():
    with DEFAULT_LANES_PATH.open("rb") as fh:
        listed = set(tomllib.load(fh).get("feeds", {}))
    missing = [f["source"] for f in parse_feeds_md(REPO_ROOT / "docs" / "FEEDS.md")]
    missing = [s for s in missing if s not in listed]
    assert not missing, f"feeds without lanes in config/feed_lanes.toml: {missing}"


# --- overlay ---------------------------------------------------------------


@pytest.fixture
def base(tmp_path):
    return _write(
        tmp_path / "base.toml",
        'default = ["reader", "signals"]\n[feeds."Fixture Preprints"]\nlanes = ["rnd"]\n',
    )


def test_overlay_overrides_lanes_by_feed_id(tmp_path, base):
    overlay = _write(
        tmp_path / "o.toml",
        '[feeds."Fixture Preprints"]\nlanes = ["signals"]\n'
        '[feeds."Other Feed"]\nlanes = ["reader"]\n',
    )
    lanes = load_feed_lanes(base, overlay)
    assert lanes.lanes_for("Fixture Preprints") == {"signals"}
    assert lanes.lanes_for("Other Feed") == {"reader"}
    assert lanes.extra_feeds == []


def test_overlay_adds_new_feed_with_url(tmp_path, base):
    overlay = _write(
        tmp_path / "o.toml",
        '[feeds."Fixture Local Feed"]\nurl = "https://example.test/f.xml"\n'
        'category = "Local"\nlanes = ["reader"]\n',
    )
    lanes = load_feed_lanes(base, overlay)
    assert lanes.extra_feeds == [
        {"source": "Fixture Local Feed", "url": "https://example.test/f.xml", "category": "Local"}
    ]
    assert lanes.lanes_for("Fixture Local Feed") == {"reader"}
    assert lanes.lanes_for("Fixture Preprints") == {"rnd"}  # untouched


def test_overlay_new_feed_needs_url_and_category(tmp_path, base):
    overlay = _write(tmp_path / "o.toml", '[feeds."F"]\nurl = "https://example.test/f"\n')
    with pytest.raises(ValueError, match="url and category"):
        load_feed_lanes(base, overlay)


def test_missing_overlay_logs_one_info_line_and_changes_nothing(tmp_path, base, caplog):
    with caplog.at_level(logging.INFO, logger="src.processors.lanes"):
        lanes = load_feed_lanes(base, tmp_path / "absent.toml")
    records = [r for r in caplog.records if r.name == "src.processors.lanes"]
    assert len(records) == 1 and records[0].levelno == logging.INFO
    assert lanes == load_feed_lanes(base)


@pytest.mark.parametrize("body", ['lanes = ["bogus"]', "lanes = []", "lanes = 3", ""])
def test_malformed_overlay_raises_value_error(tmp_path, base, body):
    overlay = _write(tmp_path / "o.toml", f'[feeds."Fixture Preprints"]\n{body}\n')
    with pytest.raises(ValueError):
        load_feed_lanes(base, overlay)


def test_overlay_invalid_toml_raises_value_error(tmp_path, base):
    with pytest.raises(ValueError):
        load_feed_lanes(base, _write(tmp_path / "o.toml", "[feeds\n"))


def test_config_reads_overlay_env(monkeypatch):
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", "/tmp/x.toml")
    assert load_config().feeds_overlay == Path("/tmp/x.toml")
    monkeypatch.delenv("MORNING_BRIEF_FEEDS_OVERLAY")
    assert load_config().feeds_overlay is None


# --- filtering and the DB --------------------------------------------------


def test_filter_lane_keeps_only_matching_sources():
    lanes = FeedLanes(feeds={"Sig": frozenset({"signals"}), "Rdr": frozenset({"reader"})})
    arts = [_art(1, "Sig"), _art(2, "Rdr"), _art(3, "Unlisted")]
    assert [a["id"] for a in filter_lane(arts, "signals", lanes)] == [1, 3]
    assert [a["id"] for a in filter_lane(arts, "reader", lanes)] == [2, 3]
    with pytest.raises(ValueError):
        filter_lane(arts, "bogus", lanes)


def test_arxiv_dropped_from_reader_and_signals_but_kept_in_db(tmp_path):
    lanes = load_feed_lanes(DEFAULT_LANES_PATH)
    db = Database(tmp_path / "t.db")
    db.connect()
    try:
        for i, source in enumerate(["ArXiv CS.AI", "ProPublica"], start=1):
            db.insert_article(
                url=f"https://example.test/{i}",
                url_hash=f"h{i}",
                title=f"Fixture story {i}",
                source=source,
                category="Tech",
                full_text="body",
            )
        stored = db.get_todays_articles()
        assert {a["source"] for a in stored} == {"ArXiv CS.AI", "ProPublica"}
        for lane in ("reader", "signals"):
            assert {a["source"] for a in filter_lane(stored, lane, lanes)} == {"ProPublica"}
        unsummarized = filter_lane(db.get_unsummarized_articles(), "reader", lanes)
        assert {a["source"] for a in unsummarized} == {"ProPublica"}

        # A summarized rnd article never reaches the briefing's input.
        for a in stored:
            db.update_summary(a["id"], "summary", "m")
        view = ReaderLaneDB(db, lanes)
        assert {a["source"] for a in view.get_todays_articles()} == {"ProPublica"}
        assert {a["source"] for a in db.get_todays_articles()} == {"ArXiv CS.AI", "ProPublica"}
    finally:
        db.close()


# --- end to end: real pipeline on fixture feeds ------------------------------


def _signal_article_ids(artifact):
    return {p["article_id"] for s in artifact["signals"] for p in s["provenance"]}


def test_pipeline_lanes_signals_only_reader_only_and_rnd(tmp_path, monkeypatch):
    articles = load_fixture_articles()
    # Fixture feeds are named "Fixture <category>": give each a different lane set.
    overlay = _write(
        tmp_path / "overlay.toml",
        '[feeds."Fixture Tech"]\nlanes = ["signals"]\n'
        '[feeds."Fixture Crypto"]\nlanes = ["reader"]\n'
        '[feeds."Fixture World"]\nlanes = ["rnd"]\n',
    )
    monkeypatch.setenv("MORNING_BRIEF_FEEDS_OVERLAY", str(overlay))
    monkeypatch.setenv("MORNING_BRIEF_INTERESTS", str(tmp_path / "none.toml"))
    artifact, stub = run_pipeline_on(articles, tmp_path / "run")

    page = (tmp_path / "run" / "data" / "output" / "dashboard.html").read_text(encoding="utf-8")
    by_cat = {c: [a for a in articles if a["category"] == c] for c in ("Tech", "Crypto", "World")}

    # Signals-only feed: in the signals input, not on the page.
    assert all(a["title"] not in page for a in by_cat["Tech"])
    # Reader-only feed: on the page.
    assert all(a["title"] in page for a in by_cat["Crypto"])
    # rnd feed: not on the page either.
    assert all(a["title"] not in page for a in by_cat["World"])

    provenance_sources = {p["source"] for s in artifact["signals"] for p in s["provenance"]}
    assert "fixture tech" in provenance_sources
    assert "fixture crypto" not in provenance_sources
    assert "fixture world" not in provenance_sources

    # All three lanes are stored in the DB.
    db = Database(tmp_path / "run" / "data" / "morning_brief.db")
    db.connect()
    try:
        stored = {a["source"] for a in db.get_todays_articles()}
    finally:
        db.close()
    assert stored == {"Fixture Tech", "Fixture Crypto", "Fixture World"}

    # Only reader-lane articles were sent to the summarizer.
    prompts = " ".join(str(r) for r in stub.generate_calls)
    assert by_cat["Crypto"][0]["title"] in prompts or by_cat["Crypto"][0]["text"] in prompts
    assert all(a["title"] not in prompts for a in by_cat["World"] + by_cat["Tech"])


# --- parser -----------------------------------------------------------------


def test_parse_feeds_md_ignores_api_and_mcp_sections(tmp_path):
    f = _write(
        tmp_path / "FEEDS.md",
        """# Registry

## RSS Feeds

### News

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| Fixture Reader Feed | `https://example.test/rss.xml` | active | ok |
| Fixture Dead Feed | `https://example.test/dead.xml` | broken | gone |

## APIs (Free Tier)

### Data

| Provider | URL | Status | Use |
|----------|-----|--------|-----|
| Fixture API | `https://api.example.test/v1` | active | data |

## MCP Servers

| Server | URL/Repo | Cost | Use Case |
|--------|----------|------|----------|
| Fixture MCP | `https://mcp.example.test/mcp` | Free | tools |
""",
    )
    feeds = parse_feeds_md(f)
    assert feeds == [
        {"source": "Fixture Reader Feed", "url": "https://example.test/rss.xml", "category": "News"}
    ]
