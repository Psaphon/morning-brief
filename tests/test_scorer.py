"""Tests for the heuristic relevance scorer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest  # noqa: F401

from src.processors.scorer import (
    ScoringConfig,
    score_articles,
    select_top_articles,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_article(
    id: int = 1,
    title: str = "Test Article",
    source: str = "Unknown Blog",
    category: str = "tech",
    published_at: str | None = None,
    fetched_at: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": id,
        "title": title,
        "source": source,
        "category": category,
        "published_at": published_at,
        "fetched_at": fetched_at or now.isoformat(),
    }


def _hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


# ---------------------------------------------------------------------------
# Source reputation
# ---------------------------------------------------------------------------


def test_known_source_scores_higher_than_unknown():
    known = _make_article(id=1, source="BBC")
    unknown = _make_article(id=2, source="random-blog")
    results = {sa.article["id"]: sa for sa in score_articles([known, unknown])}
    assert results[1].factors["source_reputation"] > results[2].factors["source_reputation"]


def test_unknown_source_uses_default():
    article = _make_article(source="completely-unknown-source-xyz")
    sa = score_articles([article])[0]
    assert sa.factors["source_reputation"] == ScoringConfig().default_source_score


def test_source_lookup_is_case_insensitive():
    bbc_lower = _make_article(id=1, source="bbc")
    bbc_upper = _make_article(id=2, source="BBC")
    results = score_articles([bbc_lower, bbc_upper])
    assert results[0].factors["source_reputation"] == results[1].factors["source_reputation"]


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------


def test_recent_article_scores_higher_than_old():
    recent = _make_article(id=1, published_at=_hours_ago(1))
    old = _make_article(id=2, published_at=_hours_ago(36))
    results = {sa.article["id"]: sa for sa in score_articles([recent, old])}
    assert results[1].factors["recency"] > results[2].factors["recency"]


def test_very_recent_article_gets_full_recency_score():
    article = _make_article(published_at=_hours_ago(0.5))
    sa = score_articles([article])[0]
    assert sa.factors["recency"] == 1.0


def test_very_old_article_gets_zero_recency():
    article = _make_article(published_at=_hours_ago(100))
    sa = score_articles([article])[0]
    assert sa.factors["recency"] == 0.0


def test_recency_falls_back_to_fetched_at():
    article = _make_article(published_at=None, fetched_at=_hours_ago(1))
    sa = score_articles([article])[0]
    assert sa.factors["recency"] == 1.0


# ---------------------------------------------------------------------------
# Cross-source coverage
# ---------------------------------------------------------------------------


def test_multi_source_story_scores_higher():
    config = ScoringConfig()
    # Two articles with the same normalized title → cross-source count = 2
    a1 = _make_article(id=1, title="Big news today", source="BBC")
    a2 = _make_article(id=2, title="Breaking: Big news today", source="NPR")
    a3 = _make_article(id=3, title="A completely different story", source="Niche Blog")

    results = {sa.article["id"]: sa for sa in score_articles([a1, a2, a3])}
    # a1 and a2 share a normalized title — both should get cross_source_multi
    assert results[1].factors["cross_source"] == config.cross_source_multi
    assert results[2].factors["cross_source"] == config.cross_source_multi
    # a3 is unique — single-source score
    assert results[3].factors["cross_source"] == config.cross_source_single


def test_single_source_story_gets_lower_cross_source():
    article = _make_article(id=1, title="Very niche obscure story no one else covers")
    sa = score_articles([article])[0]
    assert sa.factors["cross_source"] == ScoringConfig().cross_source_single


# ---------------------------------------------------------------------------
# Category priority
# ---------------------------------------------------------------------------


def test_politics_scores_higher_than_art():
    politics = _make_article(id=1, category="politics")
    art = _make_article(id=2, category="art")
    results = {sa.article["id"]: sa for sa in score_articles([politics, art])}
    assert results[1].factors["category_priority"] > results[2].factors["category_priority"]


def test_unknown_category_uses_default():
    article = _make_article(category="sports")
    sa = score_articles([article])[0]
    assert sa.factors["category_priority"] == ScoringConfig().default_category_score


# ---------------------------------------------------------------------------
# Title keyword signals
# ---------------------------------------------------------------------------


def test_breaking_in_title_boosts_score():
    boosted = _make_article(id=1, title="Breaking: Major event unfolds")
    plain = _make_article(id=2, title="A routine update on the situation")
    results = {sa.article["id"]: sa for sa in score_articles([boosted, plain])}
    assert results[1].factors["title_keywords"] == 1.0
    assert results[2].factors["title_keywords"] == 0.0


@pytest.mark.parametrize("keyword", ["breaking", "exclusive", "investigation", "urgent"])
def test_each_boost_keyword_triggers(keyword: str):
    article = _make_article(title=f"{keyword}: something important")
    sa = score_articles([article])[0]
    assert sa.factors["title_keywords"] == 1.0


# ---------------------------------------------------------------------------
# Score bounds
# ---------------------------------------------------------------------------


def test_all_scores_between_zero_and_one():
    articles = [
        _make_article(
            id=1,
            source="BBC",
            category="politics",
            published_at=_hours_ago(1),
            title="Breaking: Major story",
        ),
        _make_article(
            id=2,
            source="random-blog",
            category="art",
            published_at=_hours_ago(50),
            title="A quiet update",
        ),
    ]
    for sa in score_articles(articles):
        assert 0.0 <= sa.score <= 1.0, f"Score out of range: {sa.score}"


# ---------------------------------------------------------------------------
# select_top_articles — category balance
# ---------------------------------------------------------------------------


def test_select_top_articles_includes_all_categories():
    articles = []
    categories = ["politics", "tech", "crypto", "world", "art", "florida"]
    for i, cat in enumerate(categories):
        for j in range(3):
            articles.append(
                _make_article(id=i * 10 + j, category=cat, published_at=_hours_ago(j + 1))
            )

    scored = score_articles(articles)
    top = select_top_articles(scored, per_category=2, total=20)
    top_cats = {sa.article["category"] for sa in top}

    # Every category that was in the input should be represented
    assert top_cats == set(categories)


def test_select_top_articles_respects_total_limit():
    articles = [_make_article(id=i, category="tech") for i in range(50)]
    scored = score_articles(articles)
    top = select_top_articles(scored, per_category=5, total=10)
    assert len(top) <= 10


def test_select_top_articles_sorted_by_score():
    articles = [
        _make_article(id=1, source="BBC", category="politics", published_at=_hours_ago(1)),
        _make_article(id=2, source="random-blog", category="art", published_at=_hours_ago(40)),
    ]
    scored = score_articles(articles)
    top = select_top_articles(scored, per_category=1, total=5)
    scores = [sa.score for sa in top]
    assert scores == sorted(scores, reverse=True)
