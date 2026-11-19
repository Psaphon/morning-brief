"""Same-story detection for Morning Brief.

Matches on titles only, before summarizing, so a duplicate never costs an LLM
call. Two articles in the same category, published within a time window of each
other, are duplicates when their titles share:

* a distinctive number token (has a decimal point, or 3+ digits; ``$``, ``%``
  and M/B suffixes are ignored), **and**
* at least one capitalized word that is not on the allowlist, **and**
* enough significant words (4+ letters, not stopwords): 2 when a shared number is
  a money or percent figure (``$85.8M``, ``20.5%``), otherwise 3.

Plain word overlap (Jaccard) scored ~0.2 on a real pair of Christie's titles;
the shared-figure rule catches it. The word-overlap floor was added in PM review
(2026-11-19): on 14 days of real titles the first two rules alone merged distinct
stories that only shared a version number ("Nova 5.2 ...") or a year plus one
name, which would have dropped them from the briefing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 48

# Capitalized words too common to tie two stories together.
CAPITALIZED_ALLOWLIST = frozenset(
    w.lower()
    for w in (
        "The A An And But For Nor Or Yet So As At By In Of On To Up With From Into After Before "
        "Over Under About Why How What When Where Who Which This That These Those It Its "
        "New Newly Says Say Said Report Reports Update Updates Breaking Live Watch "
        "Monday Tuesday Wednesday Thursday Friday Saturday Sunday "
        "January February March April May June July August September October November December "
        "Inc Corp Co Ltd Llc Plc Group Company"
    ).split()
)

# A number, optionally with thousands separators / decimals, then an optional M/B suffix.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:[MmBb]\b)?")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
# A figure marked as money or a share: currency before it, or %/M/B after it.
_FIGURE_RE = re.compile(r"[$€£]\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:%|[MmBb]\b)")
_SIG_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]+")
_STOPWORDS = frozenset(
    "about after again also been before being between both could does doing down during each "
    "from further have having here into just more most much only other over said says same "
    "should some such than that their them then there these they this those through under "
    "until very were what when where which while will with would your".split()
)
# Significant shared words needed: fewer when the shared number is a money/percent figure.
SIG_WORDS_WITH_FIGURE = 2
SIG_WORDS_OTHERWISE = 3


def distinctive_numbers(title: str) -> set[str]:
    """Return normalized number tokens that are distinctive enough to match on."""
    found: set[str] = set()
    for raw in _NUMBER_RE.findall(title):
        token = raw.rstrip("MmBb").replace(",", "").rstrip(".")
        if not token:
            continue
        if "." in token or len(token.replace(".", "")) >= 3:
            found.add(token)
    return found


def capitalized_words(title: str) -> set[str]:
    """Return lowercase capitalized words, minus possessives and allowlisted words."""
    words: set[str] = set()
    for raw in _WORD_RE.findall(title):
        word = re.sub(r"['’]s$", "", raw)
        word = word.strip("'")
        if len(word) < 2 or not word[0].isupper():
            continue
        if word.lower() in CAPITALIZED_ALLOWLIST:
            continue
        words.add(word.lower())
    return words


def figures(title: str) -> set[str]:
    """Return normalized money/percent figures (``$85.8M`` -> ``85.8``)."""
    found: set[str] = set()
    for before, after in _FIGURE_RE.findall(title):
        token = (before or after).replace(",", "").rstrip(".")
        if token:
            found.add(token)
    return found


def significant_words(title: str) -> set[str]:
    """Return lowercase words of 4+ letters, minus possessives and stopwords."""
    words: set[str] = set()
    for raw in _SIG_WORD_RE.findall(title):
        word = re.sub(r"['’]s$", "", raw).strip("'’").lower()
        if len(word) >= 4 and word not in _STOPWORDS:
            words.add(word)
    return words


def titles_match(title_a: str, title_b: str) -> bool:
    """True if two titles share a distinctive number, a name and enough words."""
    shared_numbers = distinctive_numbers(title_a) & distinctive_numbers(title_b)
    if not shared_numbers:
        return False
    if not capitalized_words(title_a) & capitalized_words(title_b):
        return False
    shared_figure = shared_numbers & figures(title_a) & figures(title_b)
    needed = SIG_WORDS_WITH_FIGURE if shared_figure else SIG_WORDS_OTHERWISE
    return len(significant_words(title_a) & significant_words(title_b)) >= needed


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _within_window(a: dict, b: dict, window: timedelta) -> bool:
    ts_a, ts_b = _parse_ts(a.get("published_at")), _parse_ts(b.get("published_at"))
    if ts_a is None or ts_b is None:
        return True  # unknown publish time: both were fetched the same day
    return abs(ts_a - ts_b) <= window


def find_duplicates(
    articles: list[dict[str, Any]],
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> dict[int, int]:
    """Map duplicate article id -> id of the kept (longest) article.

    Pure function: no rows are touched. Among a group of same-story articles the
    one with the longest ``full_text`` is kept; ties go to the earlier entry in
    ``articles`` (callers pass them score-ordered).
    """
    window = timedelta(hours=window_hours)
    by_category: dict[str, list[dict]] = {}
    for article in articles:
        by_category.setdefault(article.get("category", ""), []).append(article)

    duplicates: dict[int, int] = {}
    for group in by_category.values():
        # sorted() is stable, so equal lengths keep the caller's (score) order.
        ranked = sorted(group, key=lambda a: len(a.get("full_text") or ""), reverse=True)
        kept: list[dict] = []
        for article in ranked:
            title = article.get("title") or ""
            match = next(
                (
                    k
                    for k in kept
                    if titles_match(title, k.get("title") or "")
                    and _within_window(article, k, window)
                ),
                None,
            )
            if match is None:
                kept.append(article)
            else:
                duplicates[article["id"]] = match["id"]

    if duplicates:
        logger.info("Dedupe: %d duplicate article(s) found", len(duplicates))
    return duplicates
