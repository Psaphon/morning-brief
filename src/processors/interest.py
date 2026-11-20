"""Operator interests: keyword scoring, feed gating and labels.

Interests live in TOML files named by ``$MORNING_BRIEF_INTERESTS`` (paths separated
by ``:``; default ``~/.config/morning-brief/interests.toml``). Each file may hold::

    label = "On radar"            # optional; tags bullets matching this file

    [keywords]
    term = 3                      # weight 1-5

    [feeds."Source Name"]
    min_interest = 2              # drop that feed's articles scoring below this

Keywords merge across files (highest weight wins). With no files present the
interests are empty and callers must behave exactly as before.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "MORNING_BRIEF_INTERESTS"
DEFAULT_PATH = "~/.config/morning-brief/interests.toml"
TEXT_CHARS = 2_000


@dataclass
class Interests:
    """Merged interests from every loaded file."""

    keywords: dict[str, float] = field(default_factory=dict)
    # source -> min_interest (highest wins when several files set one)
    feed_minimums: dict[str, float] = field(default_factory=dict)
    # (label, lowercase keyword set) per labelled file
    labelled: list[tuple[str, frozenset[str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.keywords or self.feed_minimums)


def _weight(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def load_interests(spec: str | None = None) -> Interests:
    """Load and merge the files named by ``spec`` (default: the environment)."""
    if spec is None:
        spec = os.environ.get(ENV_VAR) or DEFAULT_PATH
    interests = Interests()
    for raw in spec.split(":"):
        if not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        if not path.is_file():
            logger.info("Interests file not found, skipping: %s", path)
            continue
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("Could not read interests file %s: %s", path, exc)
            continue

        file_terms: set[str] = set()
        for term, raw_weight in (data.get("keywords") or {}).items():
            weight = _weight(raw_weight)
            key = str(term).strip().lower()
            if weight is None or not key:
                logger.warning("Ignoring invalid keyword %r in %s", term, path)
                continue
            file_terms.add(key)
            interests.keywords[key] = max(weight, interests.keywords.get(key, weight))

        for source, cfg in (data.get("feeds") or {}).items():
            minimum = _weight(cfg.get("min_interest")) if isinstance(cfg, dict) else None
            if minimum is None:
                logger.warning("Ignoring invalid feed entry %r in %s", source, path)
                continue
            interests.feed_minimums[source] = max(
                minimum, interests.feed_minimums.get(source, minimum)
            )

        label = data.get("label")
        if isinstance(label, str) and label.strip() and file_terms:
            interests.labelled.append((label.strip(), frozenset(file_terms)))
    return interests


def _matches(term: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def _haystack(article: dict[str, Any]) -> str:
    return f"{article.get('title') or ''}\n{(article.get('full_text') or '')[:TEXT_CHARS]}"


def interest_score(article: dict[str, Any], interests: Interests) -> float:
    """Sum of weights of keywords found in the title plus first 2,000 chars of text."""
    text = _haystack(article)
    return sum(w for term, w in interests.keywords.items() if _matches(term, text))


def interest_labels(article: dict[str, Any], interests: Interests) -> list[str]:
    """De-duplicated labels of the labelled files whose keywords match the article."""
    text = _haystack(article)
    labels: list[str] = []
    for label, terms in interests.labelled:
        if label not in labels and any(_matches(t, text) for t in terms):
            labels.append(label)
    return labels


def passes_feed_minimum(article: dict[str, Any], interests: Interests) -> bool:
    """False when the article's feed sets ``min_interest`` and it scores below it."""
    minimum = interests.feed_minimums.get(article.get("source") or "")
    return minimum is None or interest_score(article, interests) >= minimum


def filter_by_feed_minimum(
    articles: list[dict[str, Any]], interests: Interests
) -> list[dict[str, Any]]:
    """Drop articles from gated feeds that do not reach ``min_interest``."""
    if not interests.feed_minimums:
        return articles
    return [a for a in articles if passes_feed_minimum(a, interests)]
