"""Heuristic article relevance scoring for Morning Brief.

Scores articles by five weighted factors (no LLM required):
1. Source reputation    — weight 0.25
2. Recency              — weight 0.20
3. Cross-source coverage — weight 0.25
4. Category priority    — weight 0.20
5. Title keyword signals — weight 0.10
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .dedup import _normalize_title

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default mappings (easy to extend)
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_REPUTATION: dict[str, float] = {
    # Major outlets = 1.0
    "bbc": 1.0,
    "bbc news": 1.0,
    "npr": 1.0,
    "propublica": 1.0,
    "pbs": 1.0,
    "pbs newshour": 1.0,
    # Mid-tier = 0.8
    "coindesk": 0.8,
    "the guardian": 0.8,
    "al jazeera": 0.8,
    "reuters": 0.8,
    "associated press": 0.8,
    "ap news": 0.8,
}

DEFAULT_CATEGORY_PRIORITY: dict[str, float] = {
    "politics": 1.0,
    "florida": 1.0,
    "world": 0.8,
    "crypto": 0.7,
    "tech": 0.6,
    "art": 0.4,
}

BOOST_KEYWORDS: frozenset[str] = frozenset({"breaking", "exclusive", "investigation", "urgent"})

# ---------------------------------------------------------------------------
# Config & result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ScoringConfig:
    """Weights and mappings for the relevance scorer. All overridable."""

    # Factor weights (should sum to 1.0)
    weight_source_reputation: float = 0.25
    weight_recency: float = 0.20
    weight_cross_source: float = 0.25
    weight_category_priority: float = 0.20
    weight_title_keywords: float = 0.10

    # Recency thresholds (hours)
    recency_full_score_hours: float = 6.0
    recency_zero_score_hours: float = 48.0

    # Per-source and per-category scores
    source_reputation: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SOURCE_REPUTATION)
    )
    category_priority: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_PRIORITY)
    )

    # Default scores for unknowns
    default_source_score: float = 0.5
    default_category_score: float = 0.5

    # Cross-source score values
    cross_source_multi: float = 1.0  # 2+ sources cover same story
    cross_source_single: float = 0.3  # Only 1 source

    # Boost keywords
    boost_keywords: frozenset[str] = field(default_factory=lambda: frozenset(BOOST_KEYWORDS))


@dataclass
class ScoredArticle:
    """An article with its relevance score and per-factor breakdown."""

    article: dict
    score: float
    factors: dict[str, float]  # breakdown of contributing factors


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _score_source_reputation(source: str, config: ScoringConfig) -> float:
    key = source.lower().strip()
    return config.source_reputation.get(key, config.default_source_score)


def _score_recency(article: dict, config: ScoringConfig, now: datetime) -> float:
    """Linear decay: 1.0 within recency_full_score_hours, 0.0 at recency_zero_score_hours."""
    ts_str = article.get("published_at") or article.get("fetched_at")
    if not ts_str:
        return 0.0
    try:
        # Handle both offset-aware and naive ISO strings
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_hours = (now - ts).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0

    if age_hours <= config.recency_full_score_hours:
        return 1.0
    if age_hours >= config.recency_zero_score_hours:
        return 0.0
    span = config.recency_zero_score_hours - config.recency_full_score_hours
    return 1.0 - (age_hours - config.recency_full_score_hours) / span


def _build_title_coverage(articles: list[dict]) -> dict[str, int]:
    """Return a mapping of normalized_title -> count of articles sharing that title."""
    counts: dict[str, int] = defaultdict(int)
    for a in articles:
        norm = _normalize_title(a.get("title", ""))
        if norm:
            counts[norm] += 1
    return dict(counts)


def _score_cross_source(article: dict, coverage: dict[str, int], config: ScoringConfig) -> float:
    norm = _normalize_title(article.get("title", ""))
    if not norm:
        return config.cross_source_single
    count = coverage.get(norm, 1)
    return config.cross_source_multi if count >= 2 else config.cross_source_single


def _score_category_priority(category: str, config: ScoringConfig) -> float:
    key = category.lower().strip()
    return config.category_priority.get(key, config.default_category_score)


def _score_title_keywords(title: str, config: ScoringConfig) -> float:
    lower = title.lower()
    return 1.0 if any(kw in lower for kw in config.boost_keywords) else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_articles(
    articles: list[dict],
    config: ScoringConfig | None = None,
) -> list[ScoredArticle]:
    """Score and rank articles by relevance. Returns sorted list, highest score first."""
    if config is None:
        config = ScoringConfig()

    now = datetime.now(timezone.utc)
    coverage = _build_title_coverage(articles)

    scored: list[ScoredArticle] = []
    for article in articles:
        f_source = _score_source_reputation(article.get("source", ""), config)
        f_recency = _score_recency(article, config, now)
        f_cross = _score_cross_source(article, coverage, config)
        f_category = _score_category_priority(article.get("category", ""), config)
        f_keywords = _score_title_keywords(article.get("title", ""), config)

        total = (
            config.weight_source_reputation * f_source
            + config.weight_recency * f_recency
            + config.weight_cross_source * f_cross
            + config.weight_category_priority * f_category
            + config.weight_title_keywords * f_keywords
        )

        scored.append(
            ScoredArticle(
                article=article,
                score=round(total, 4),
                factors={
                    "source_reputation": f_source,
                    "recency": f_recency,
                    "cross_source": f_cross,
                    "category_priority": f_category,
                    "title_keywords": f_keywords,
                },
            )
        )

    scored.sort(key=lambda sa: sa.score, reverse=True)
    logger.debug("Scored %d articles", len(scored))
    return scored


def select_top_articles(
    scored: list[ScoredArticle],
    per_category: int = 5,
    total: int = 25,
) -> list[ScoredArticle]:
    """Select top articles ensuring each category gets at least `per_category` slots.

    1. Collect the top `per_category` articles per category (by score).
    2. Fill remaining slots (up to `total`) from the global top-scored list,
       skipping articles already selected.
    """
    # Group by category, preserving score order (scored is already sorted)
    by_category: dict[str, list[ScoredArticle]] = defaultdict(list)
    for sa in scored:
        cat = sa.article.get("category", "").lower()
        by_category[cat].append(sa)

    selected: list[ScoredArticle] = []
    selected_ids: set[int] = set()

    # Phase 1: guaranteed per-category slots
    for cat_articles in by_category.values():
        for sa in cat_articles[:per_category]:
            article_id = sa.article.get("id")
            if article_id not in selected_ids:
                selected.append(sa)
                selected_ids.add(article_id)

    # Phase 2: fill remaining from global ranking
    for sa in scored:
        if len(selected) >= total:
            break
        article_id = sa.article.get("id")
        if article_id not in selected_ids:
            selected.append(sa)
            selected_ids.add(article_id)

    # Return capped at total, sorted by score
    selected = selected[:total]
    selected.sort(key=lambda sa: sa.score, reverse=True)
    return selected
