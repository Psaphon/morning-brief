"""Tests for same-story dedupe."""

import json
from pathlib import Path

from src.db import Database
from src.processors.dedupe import (
    capitalized_words,
    distinctive_numbers,
    find_duplicates,
    titles_match,
)
from src.publishers.html import _collapse_duplicates
from src.summarizers.local import select_balanced_articles

FIXTURE = Path(__file__).parent / "fixtures" / "brief_fixture" / "articles.json"


def _fixture() -> list[dict]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        {
            "id": i,
            "title": a["title"],
            "category": a["category"],
            "published_at": a["published_at"],
            "full_text": a["text"],
            "url": f"https://example.com/{a['id']}",
        }
        for i, a in enumerate(raw if isinstance(raw, list) else raw["articles"], start=1)
    ]


def test_number_tokens():
    assert distinctive_numbers("Raises $3,847,000,000 in bonds") == {"3847000000"}
    assert distinctive_numbers("Stock up 4.5% on $12M deal") == {"4.5"}
    assert distinctive_numbers("Three of 12 firms, 99 percent") == set()
    assert distinctive_numbers("Fund tops 250M") == {"250"}


def test_capitalized_words_skip_allowlist_and_possessive():
    assert capitalized_words("The Brightwater's new bond") == {"brightwater"}
    assert capitalized_words("Monday: Investors react") == {"investors"}


def test_titles_need_both_number_and_name():
    assert titles_match("Acme prices $500M notes", "Acme's $500M notes draw demand")
    assert titles_match("Acme prices $500M notes", "Zeta sells $500M notes") is False
    assert not titles_match("Acme raises $500 million", "Acme raises $600 million")
    assert not titles_match("the firm raises $500M", "the bank raises $500M")


def test_real_same_story_pairs_match():
    # Real title pairs from the 2026-11-19 PM review (14 days of live articles).
    pairs = [
        (
            "$85.8M Kandinsky, $82.3M Klimt Propel Mixed $626.6M Christie\u2019s Sale",
            "Kandinsky Painting Sells for $85.8M at Christie\u2019s, a New Auction Record",
        ),
        (
            "Florida pastor found guilty over 2021 assassination of Haitian president",
            "Miami court convicts US-Haitian pastor over 2021 assassination of president",
        ),
        (
            "Germany's 2027 budget: A record \u20ac561 billion and a mountain of debt",
            "Germany news: Parliament agrees on debt-laden 2027 budget",
        ),
        (
            "Bitcoin Holds Near $64,800 After 20.5% Four-Week Drop",
            "Bitcoin Holds Near $64,800 as ETF Outflow Streak Ends",
        ),
    ]
    for a, b in pairs:
        assert titles_match(a, b), (a, b)


def test_shared_version_number_or_year_alone_is_not_a_duplicate():
    # Shapes of the false positives seen in review: different stories about one
    # product version, or that share only a year/code and a name.
    pairs = [
        (
            "Acme Nova 5.2 vs. Zeta 3: Same scores at 4x the cost",
            "Acme Nova 5.2 vs. Nova 5.1: Fewer refusals",
        ),
        (
            "Acme Widens Nova for Law Access With Nova-5.2 Update",
            "Why early testers love Nova 5.2 from Acme",
        ),
        (
            "Proceedings of the 2026 XCSP3 Competition",
            "The 7th Neural Network Verification Competition (2026)",
        ),
        (
            "Acme Cobalt 200 chips go GA for AI agents",
            "200 OK is dead as an AI agent health signal",
        ),
    ]
    for a, b in pairs:
        assert not titles_match(a, b), (a, b)


def test_fixture_same_story_pair_detected_and_longer_kept():
    articles = _fixture()
    dupes = find_duplicates(articles)
    by_id = {a["id"]: a for a in articles}
    assert len(dupes) == 1
    ((dup, kept),) = dupes.items()
    assert "Brightwater" in by_id[dup]["title"]
    assert len(by_id[kept]["full_text"]) > len(by_id[dup]["full_text"])


def test_unrelated_same_category_not_duplicates():
    articles = [a for a in _fixture() if "Brightwater" not in a["title"]]
    assert find_duplicates(articles) == {}


def test_different_category_or_outside_window_not_duplicates():
    a = {"id": 1, "category": "World", "title": "Acme prices $500M notes", "full_text": "xx"}
    b = {"id": 2, "category": "Tech", "title": "Acme prices $500M notes", "full_text": "x"}
    assert find_duplicates([a, b]) == {}
    b["category"] = "World"
    a["published_at"] = "2026-01-10T00:00:00Z"
    b["published_at"] = "2026-01-12T01:00:00Z"
    assert find_duplicates([a, b], window_hours=48) == {}
    assert find_duplicates([a, b], window_hours=49) == {2: 1}


def test_freed_slot_is_refilled():
    articles = [
        {
            "id": i,
            "category": "World",
            "title": f"Story{i} unique",
            "full_text": "x",
            "score": 100 - i,
        }
        for i in range(1, 10)
    ]
    articles[0].update(title="Acme prices $500M notes", full_text="long text here")
    articles[1].update(title="Acme's $500M notes snapped up")
    dupes = find_duplicates(articles)
    assert dupes == {2: 1}
    remaining = [a for a in articles if a["id"] not in dupes]
    selected = select_balanced_articles(remaining, per_category=8)
    assert len(selected) == 8
    assert 9 in {a["id"] for a in selected}


def test_mark_duplicates_keeps_rows_and_excludes_from_unsummarized(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    db.connect()
    try:
        for n, text in ((1, "short"), (2, "a much longer body")):
            db.insert_article(
                url=f"https://example.com/{n}",
                url_hash=f"h{n}",
                title=f"Acme prices $500M notes {n}",
                source="s",
                category="World",
                full_text=text,
            )
        count = lambda: db.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]  # noqa: E731
        before = count()
        candidates = db.get_unsummarized_articles()
        dupes = find_duplicates(candidates)
        db.mark_duplicates(dupes)
        assert count() == before
        row = db.conn.execute("SELECT id, duplicate_of FROM articles ORDER BY id").fetchall()
        assert row[0]["duplicate_of"] == row[1]["id"]
        assert row[1]["duplicate_of"] is None
        assert [a["id"] for a in db.get_unsummarized_articles()] == [row[1]["id"]]
    finally:
        db.close()


def test_collapse_duplicates_lists_also_covered_by():
    articles = [
        {"id": 1, "title": "k", "url": "https://k", "duplicate_of": None},
        {"id": 2, "title": "d", "url": "https://d", "duplicate_of": 1},
    ]
    visible = _collapse_duplicates(articles)
    assert [a["id"] for a in visible] == [1]
    assert visible[0]["also_covered_by"] == ["https://d"]
