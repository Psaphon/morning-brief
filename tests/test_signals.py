"""Tests for src/publishers/signals.py.

Covers contract correctness (§3–§7 of docs/SIGNAL-SCHEMA.md):
  - Aggregation is the MEAN, not the sum.
  - knowable_at is the MAX timestamp across contributing articles.
  - published_at=null falls back to fetched_at; null is retained in provenance.
  - No look-ahead: knowable_at is never derived from now or emitted_at.
  - Atomic write: no .tmp survives, output parses as valid JSON.
  - Word-boundary matching: "Visage" does not match V; "Visa" does.
  - Ticker-map coverage: exactly the 27 symbols in atrade's fixed universe.
  - Empty result (no matches) is valid, not an error.
  - Unmatched articles raise nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.publishers.signals import (
    TickerEntry,
    TickerMap,
    build_signals,
    emit_signals,
    load_ticker_map,
    match_articles_to_tickers,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TICKER_MAP_PATH = Path(__file__).parent.parent / "config" / "ticker_map.toml"

# Authoritative 27-symbol universe — edit this set only when atrade's
# config/universe.toml changes, and update the ticker_map in the same review.
EXPECTED_UNIVERSE: frozenset[str] = frozenset(
    {
        # ETFs (12)
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "VTI",
        "XLF",
        "XLK",
        "XLE",
        "XLV",
        "XLI",
        "XLY",
        "XLP",
        # Stocks (15)
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "GOOGL",
        "META",
        "TSLA",
        "JPM",
        "JNJ",
        "V",
        "PG",
        "UNH",
        "HD",
        "MA",
        "XOM",
    }
)


def _make_ticker_map(entries: list[tuple[str, str, list[str]]]) -> TickerMap:
    """Build a minimal TickerMap from (symbol, asset_class, aliases) triples."""
    tickers = {sym: TickerEntry(sym, ac, aliases) for sym, ac, aliases in entries}
    return TickerMap("1.0.0", "test-universe-ref", tickers)


def _article(
    *,
    id: int = 1,
    title: str = "Test article",
    source: str = "Reuters",
    url_hash: str = "abc123",
    published_at: str | None = "2026-09-01T06:00:00Z",
    fetched_at: str = "2026-09-01T07:00:00Z",
    full_text: str | None = None,
    score: float | None = 0.5,
) -> dict:
    return {
        "id": id,
        "title": title,
        "source": source,
        "url_hash": url_hash,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "full_text": full_text,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Aggregation: mean, not sum
# ---------------------------------------------------------------------------


def test_aggregation_is_mean_not_sum():
    """score must be the mean of contributing article scores (contract §4).

    Three articles with scores 0.8, 0.9, 0.7 → mean 0.8000.
    Their sum (2.4) would exceed [0,1], proving mean is used.
    """
    tm = _make_ticker_map([("MSFT", "stock", ["Microsoft"])])
    articles = [
        _article(id=1, title="Microsoft earnings beat", score=0.8),
        _article(id=2, title="Microsoft Azure growth", score=0.9),
        _article(id=3, title="Microsoft Teams update", score=0.7),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    assert len(signals) == 1
    sig = signals[0]
    assert sig["ticker"] == "MSFT"
    assert sig["score"] == 0.8  # mean(0.8, 0.9, 0.7) = 0.8000
    assert sig["score"] <= 1.0  # bound preserved; sum would be 2.4


def test_aggregation_rounds_to_4_decimals():
    """score is rounded to exactly 4 decimal places (§4)."""
    tm = _make_ticker_map([("AAPL", "stock", ["Apple Inc"])])
    articles = [
        _article(id=1, title="Apple Inc sells iPhones", score=0.1),
        _article(id=2, title="Apple Inc Q3 results", score=0.2),
        _article(id=3, title="Apple Inc stock dips", score=0.3),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    # mean(0.1, 0.2, 0.3) = 0.2 exactly; still check it is a proper float
    assert signals[0]["score"] == round(0.2, 4)


# ---------------------------------------------------------------------------
# knowable_at: max, not min
# ---------------------------------------------------------------------------


def test_knowable_at_is_max_not_min():
    """knowable_at must be max(published_at) across contributing articles (§5).

    Using min() would backdate the signal to before all information existed.
    The test constructs articles where min != max so a min() implementation fails.
    """
    tm = _make_ticker_map([("NVDA", "stock", ["Nvidia"])])
    articles = [
        _article(
            id=1,
            title="Nvidia GPU sales",
            published_at="2026-09-01T05:00:00Z",
            fetched_at="2026-09-01T06:00:00Z",
            score=0.6,
        ),
        _article(
            id=2,
            title="Nvidia AI chips",
            published_at="2026-09-01T09:00:00Z",
            fetched_at="2026-09-01T10:00:00Z",
            score=0.7,
        ),
        _article(
            id=3,
            title="Nvidia Jensen Huang",
            published_at="2026-09-01T07:00:00Z",
            fetched_at="2026-09-01T08:00:00Z",
            score=0.5,
        ),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    assert len(signals) == 1
    sig = signals[0]
    # max published_at is 09:00; min would be 05:00 → a min() impl would fail
    assert sig["knowable_at"] == "2026-09-01T09:00:00Z"
    assert sig["knowable_at"] != "2026-09-01T05:00:00Z"


# ---------------------------------------------------------------------------
# published_at=null → fetched_at fallback; null retained in provenance
# ---------------------------------------------------------------------------


def test_null_published_at_falls_back_to_fetched_at():
    """When published_at is null, knowable_at uses fetched_at for that article (§5).

    The null must also be retained in provenance so an auditor can identify
    which articles used the fetched_at fallback (§7).
    """
    tm = _make_ticker_map([("TSLA", "stock", ["Tesla"])])
    articles = [
        _article(
            id=10,
            title="Tesla Model Y sales",
            published_at=None,  # source omitted publish date
            fetched_at="2026-09-01T08:30:00Z",
            score=0.6,
        ),
        _article(
            id=11,
            title="Tesla earnings beat",
            published_at="2026-09-01T06:00:00Z",
            fetched_at="2026-09-01T07:00:00Z",
            score=0.8,
        ),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    assert len(signals) == 1
    sig = signals[0]

    # Article 10: published_at=null → falls back to fetched_at "08:30"
    # Article 11: published_at="06:00"
    # max("08:30", "06:00") = "08:30"
    assert sig["knowable_at"] == "2026-09-01T08:30:00Z"

    # Provenance must retain published_at=null (not replace with fetched_at)
    prov_by_id = {p["article_id"]: p for p in sig["provenance"]}
    assert prov_by_id[10]["published_at"] is None
    assert prov_by_id[11]["published_at"] == "2026-09-01T06:00:00Z"


# ---------------------------------------------------------------------------
# No look-ahead: knowable_at < emitted_at
# ---------------------------------------------------------------------------


def test_no_look_ahead_knowable_at_before_emitted_at(tmp_path: Path):
    """knowable_at must never equal or exceed emitted_at (§5, §3).

    Build articles with timestamps firmly in the past and assert that
    knowable_at < emitted_at in the written artifact.
    """
    tm = _make_ticker_map([("AMZN", "stock", ["Amazon"])])
    articles = [
        _article(
            id=1,
            title="Amazon AWS growth",
            published_at="2026-09-01T04:00:00Z",
            fetched_at="2026-09-01T05:00:00Z",
            score=0.7,
        ),
        _article(
            id=2,
            title="Amazon Prime expansion",
            published_at="2026-09-01T03:00:00Z",
            fetched_at="2026-09-01T04:00:00Z",
            score=0.5,
        ),
    ]

    out = emit_signals(articles, tm, tmp_path / "signals", "test-ref")
    assert out is not None

    artifact = json.loads(out.read_text())
    sig = artifact["signals"][0]
    assert sig["knowable_at"] < artifact["emitted_at"]
    # emitted_at must not appear as knowable_at
    assert sig["knowable_at"] != artifact["emitted_at"]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_no_tmp_file_remains(tmp_path: Path):
    """After emit_signals, the .tmp file must not exist (§2)."""
    tm = _make_ticker_map([("GOOGL", "stock", ["Google"])])
    articles = [_article(id=1, title="Google search update", score=0.6)]

    out = emit_signals(articles, tm, tmp_path / "signals", "test-ref")

    assert out is not None
    assert out.exists()
    assert not out.with_suffix(".json.tmp").exists()

    # Must parse as valid JSON with the expected envelope shape.
    artifact = json.loads(out.read_text())
    assert artifact["schema_version"] == "1.0.0"
    assert "emitted_at" in artifact
    assert "universe_ref" in artifact
    assert isinstance(artifact["signals"], list)


# ---------------------------------------------------------------------------
# Word-boundary matching
# ---------------------------------------------------------------------------


def test_word_boundary_visage_does_not_match_visa():
    """'Visage' must NOT match V's 'Visa' alias — no raw substring matching (§6)."""
    tm = _make_ticker_map([("V", "stock", ["Visa Inc", "Visa card", "Visa"])])
    articles = [_article(id=1, title="Visage cosmetics launches new line", score=0.5)]

    matched = match_articles_to_tickers(articles, tm)
    assert "V" not in matched


def test_word_boundary_visa_matches_v():
    """'Visa reported earnings' must match V (word-boundary match on 'Visa')."""
    tm = _make_ticker_map([("V", "stock", ["Visa Inc", "Visa card", "Visa"])])
    articles = [_article(id=1, title="Visa reported record earnings", score=0.7)]

    matched = match_articles_to_tickers(articles, tm)
    assert "V" in matched


def test_word_boundary_case_insensitive():
    """Matching is case-insensitive (e.g. 'MASTERCARD' matches 'Mastercard')."""
    tm = _make_ticker_map([("MA", "stock", ["Mastercard"])])
    articles = [_article(id=1, title="MASTERCARD payment volumes surge", score=0.6)]

    matched = match_articles_to_tickers(articles, tm)
    assert "MA" in matched


def test_full_text_also_searched():
    """Aliases in full_text (not just title) should trigger a match."""
    tm = _make_ticker_map([("HD", "stock", ["Home Depot"])])
    articles = [
        _article(
            id=1,
            title="Retail sector roundup",
            full_text="Home Depot reported strong lumber sales this quarter.",
            score=0.5,
        )
    ]
    matched = match_articles_to_tickers(articles, tm)
    assert "HD" in matched


# ---------------------------------------------------------------------------
# Ticker-map coverage — two layers
# ---------------------------------------------------------------------------


def test_ticker_map_covers_exactly_27_symbols():
    """The ticker map must cover EXACTLY the 27 symbols in atrade's universe.

    Layer 1: assert against the explicit frozenset declared in this file.
    Editing the map without also editing this set will fail the test.
    """
    tm = load_ticker_map(TICKER_MAP_PATH)
    assert set(tm.tickers.keys()) == EXPECTED_UNIVERSE


def test_ticker_map_symbols_are_uppercase_and_unique():
    """Every symbol must be uppercase and appear exactly once."""
    tm = load_ticker_map(TICKER_MAP_PATH)
    symbols = list(tm.tickers.keys())
    assert all(sym == sym.upper() for sym in symbols)
    assert len(symbols) == len(set(symbols))  # no duplicates


def test_stock_entries_do_not_list_bare_ticker_as_alias():
    """No stock entry may include its own bare ticker symbol as an alias (§6 guard).

    Common-word tickers (V, MA, HD, PG) would generate constant false positives
    if matched as raw strings.  This test enforces the rule rather than leaving
    it to code review.
    """
    tm = load_ticker_map(TICKER_MAP_PATH)
    violations = []
    for sym, entry in tm.tickers.items():
        if entry.asset_class == "stock" and sym in entry.aliases:
            violations.append(sym)
    assert violations == [], (
        f"Stock entries with bare ticker as alias (must be removed): {violations}"
    )


@pytest.mark.skipif(
    not Path("/home/comp/Projects/atrade/config/universe.toml").exists(),
    reason="atrade universe.toml not available on this machine",
)
def test_ticker_map_matches_atrade_universe():
    """Layer 2: cross-repo check against the live atrade universe.toml.

    Skipped in CI (file absent); runs on the dev box where atrade is checked out,
    providing a real sync check at the source of truth.
    """
    import tomllib

    atrade_path = Path("/home/comp/Projects/atrade/config/universe.toml")
    atrade_raw = tomllib.loads(atrade_path.read_text(encoding="utf-8"))
    atrade_symbols = frozenset(t["symbol"] for t in atrade_raw.get("tickers", []))

    mb_tm = load_ticker_map(TICKER_MAP_PATH)
    mb_symbols = frozenset(mb_tm.tickers.keys())

    assert mb_symbols == atrade_symbols, (
        f"Symbol mismatch between ticker_map.toml and atrade universe.toml.\n"
        f"  Only in ticker_map: {mb_symbols - atrade_symbols}\n"
        f"  Only in atrade:     {atrade_symbols - mb_symbols}"
    )


# ---------------------------------------------------------------------------
# Empty result
# ---------------------------------------------------------------------------


def test_empty_signals_on_no_matches(tmp_path: Path):
    """When no article matches any ticker, the artifact has signals=[] — valid (§3)."""
    tm = _make_ticker_map([("AAPL", "stock", ["Apple Inc"])])
    articles = [_article(id=1, title="Totally unrelated news about cabbage", score=0.4)]

    out = emit_signals(articles, tm, tmp_path / "signals", "test-ref")
    assert out is not None

    artifact = json.loads(out.read_text())
    assert artifact["signals"] == []


def test_empty_signals_on_empty_article_list(tmp_path: Path):
    """An empty article list produces a valid artifact with signals=[]."""
    tm = load_ticker_map(TICKER_MAP_PATH)
    out = emit_signals([], tm, tmp_path / "signals")
    assert out is not None

    artifact = json.loads(out.read_text())
    assert artifact["signals"] == []


# ---------------------------------------------------------------------------
# Unmatched articles — not an error
# ---------------------------------------------------------------------------


def test_unmatched_articles_do_not_raise():
    """Articles matching no ticker contribute to nothing and raise nothing (§6)."""
    tm = _make_ticker_map([("XOM", "stock", ["ExxonMobil"])])
    articles = [
        _article(id=1, title="Unrelated story about weather", score=0.3),
        _article(id=2, title="Another unrelated story", score=0.4),
    ]
    # Should not raise; matched dict simply has no XOM entry
    matched = match_articles_to_tickers(articles, tm)
    assert matched == {}
    signals = build_signals(matched, "test-ref")
    assert signals == []


def test_articles_without_score_do_not_contribute():
    """Articles with score=None are excluded — they have not been scored yet."""
    tm = _make_ticker_map([("JPM", "stock", ["JPMorgan"])])
    articles = [
        _article(id=1, title="JPMorgan quarterly results", score=None),
        _article(id=2, title="JPMorgan Chase profit rises", score=0.7),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    assert len(signals) == 1
    assert signals[0]["article_count"] == 1
    assert signals[0]["score"] == 0.7


# ---------------------------------------------------------------------------
# Provenance field rules
# ---------------------------------------------------------------------------


def test_provenance_source_is_lowercased():
    """source in provenance must be lowercased regardless of DB value (§7)."""
    tm = _make_ticker_map([("META", "stock", ["Facebook"])])
    articles = [_article(id=1, title="Facebook revenue up", source="Reuters", score=0.6)]

    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    assert signals[0]["provenance"][0]["source"] == "reuters"


def test_article_count_equals_provenance_length():
    """article_count must equal len(provenance) (§3)."""
    tm = _make_ticker_map([("UNH", "stock", ["UnitedHealth"])])
    articles = [
        _article(id=1, title="UnitedHealth revenue", score=0.5),
        _article(id=2, title="UnitedHealth Group expands", score=0.6),
    ]
    matched = match_articles_to_tickers(articles, tm)
    signals = build_signals(matched, "test-ref")

    sig = signals[0]
    assert sig["article_count"] == len(sig["provenance"])


class TestRealWorldTimestampFormats:
    """knowable_at must be derived from parsed datetimes, never string comparison.

    The articles table holds two mutually incomparable formats: fetched_at is
    ISO-8601 (datetime.isoformat()), while published_at is stored verbatim from
    the feed by src/fetchers/rss.py and is usually RFC 822. Sorting those as
    strings puts every RFC 822 value above every ISO one ('T' > '2'), which
    silently backdates the signal. 1919 of 2092 rows in the live database carry
    RFC 822 published_at, so this is the normal case, not an edge case.
    """

    def _article(self, aid, score, published_at, fetched_at):
        return {
            "id": aid,
            "source": "reuters",
            "url_hash": f"hash{aid}",
            "score": score,
            "published_at": published_at,
            "fetched_at": fetched_at,
        }

    def test_rfc822_published_at_does_not_backdate_the_signal(self):
        """The regression: an RFC 822 date must not win a lexicographic max()."""
        articles = [
            self._article(
                1, 0.9, "Tue, 14 Jul 2026 00:00:00 GMT", "2026-09-01T22:34:25.031038+00:00"
            ),
            self._article(2, 0.5, None, "2026-09-01T23:00:00.000000+00:00"),
        ]
        sig = build_signals({"AAPL": articles}, "ref")[0]
        # Article 2's fetched_at is the latest real moment; article 1 is 7 weeks older.
        assert sig["knowable_at"] == "2026-09-01T23:00:00Z"

    def test_knowable_at_is_always_z_suffixed_iso(self):
        """Contract §3: ISO-8601 UTC, Z-suffixed — never a raw feed string."""
        articles = [
            self._article(1, 0.7, "Tue, 14 Jul 2026 06:12:00 GMT", "2026-09-01T22:00:00+00:00"),
        ]
        sig = build_signals({"MSFT": articles}, "ref")[0]
        assert sig["knowable_at"] == "2026-07-14T06:12:00Z"
        datetime.strptime(sig["knowable_at"], "%Y-%m-%dT%H:%M:%SZ")

    def test_mixed_formats_pick_the_genuinely_latest(self):
        """Two RFC 822 dates plus an ISO one: the true maximum must win."""
        articles = [
            self._article(1, 0.5, "Mon, 03 Aug 2026 00:00:00 GMT", "2026-09-01T10:00:00+00:00"),
            self._article(2, 0.5, "Tue, 25 Aug 2026 00:00:00 GMT", "2026-09-01T10:00:00+00:00"),
            self._article(3, 0.5, "2026-08-10T00:00:00+00:00", "2026-09-01T10:00:00+00:00"),
        ]
        sig = build_signals({"NVDA": articles}, "ref")[0]
        assert sig["knowable_at"] == "2026-08-25T00:00:00Z"

    def test_provenance_published_at_is_normalised_or_null(self):
        """§7: provenance keeps a usable timestamp, or null to flag the fallback."""
        articles = [
            self._article(1, 0.5, "Tue, 14 Jul 2026 00:00:00 GMT", "2026-09-01T22:00:00+00:00"),
            self._article(2, 0.5, None, "2026-09-01T23:00:00+00:00"),
        ]
        sig = build_signals({"TSLA": articles}, "ref")[0]
        by_id = {p["article_id"]: p["published_at"] for p in sig["provenance"]}
        assert by_id[1] == "2026-07-14T00:00:00Z"
        assert by_id[2] is None

    def test_unparseable_published_at_falls_back_to_fetched_at(self):
        articles = [self._article(1, 0.5, "not a date at all", "2026-09-01T23:00:00+00:00")]
        sig = build_signals({"JPM": articles}, "ref")[0]
        assert sig["knowable_at"] == "2026-09-01T23:00:00Z"
        assert sig["provenance"][0]["published_at"] is None

    def test_future_dated_published_at_is_distrusted(self):
        """A feed claiming the future must not push knowable_at past emitted_at (§5).

        An article cannot be fetched before it is published, so a published_at
        later than fetched_at is wrong. Clamping to fetched_at makes the
        consumer's 'reject knowable_at after emitted_at' case unrepresentable.
        """
        articles = [
            self._article(1, 0.5, "Fri, 01 Jan 2100 00:00:00 GMT", "2026-09-01T23:00:00+00:00")
        ]
        sig = build_signals({"SPY": articles}, "ref")[0]
        assert sig["knowable_at"] == "2026-09-01T23:00:00Z"
