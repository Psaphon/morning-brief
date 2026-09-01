"""Signal artifact emitter — maps scored articles onto atrade's ticker universe.

Produces a versioned, point-in-time JSON artifact that atrade consumes for
backtesting and live trading.  See docs/SIGNAL-SCHEMA.md for the full contract.

The single property everything here protects:

    A backtest replaying these artifacts must see exactly what a live run
    would have seen at that moment — no more.

Key correctness rules (do not relax without a contract version bump):
  - score is the MEAN of contributing article scores (§4), not the sum.
  - knowable_at = max(published_at or fetched_at) across articles (§5).
  - No free text in any signal record (§3).
  - Artifact is written atomically: .tmp → fsync → rename (§2).
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"


class SignalEmitError(Exception):
    """Raised when the ticker map is malformed or signal emission fails."""


# ---------------------------------------------------------------------------
# Ticker map
# ---------------------------------------------------------------------------


class TickerEntry:
    """One row from ticker_map.toml, with compiled match patterns."""

    __slots__ = ("symbol", "asset_class", "aliases", "_patterns")

    def __init__(self, symbol: str, asset_class: str, aliases: list[str]) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        self.aliases = aliases
        # Word-boundary, case-insensitive patterns — never raw substring search.
        self._patterns: list[re.Pattern[str]] = [
            re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE) for alias in aliases
        ]

    def matches(self, text: str) -> bool:
        """Return True if any alias matches text on a word boundary."""
        return any(p.search(text) for p in self._patterns)


class TickerMap:
    """Parsed and validated ticker_map.toml."""

    __slots__ = ("schema_version", "universe_ref", "tickers")

    def __init__(
        self,
        schema_version: str,
        universe_ref: str,
        tickers: dict[str, TickerEntry],
    ) -> None:
        self.schema_version = schema_version
        self.universe_ref = universe_ref
        self.tickers = tickers


def load_ticker_map(path: Path) -> TickerMap:
    """Parse and validate config/ticker_map.toml.

    Raises SignalEmitError on any malformed or missing required field.
    Uses stdlib tomllib (Python 3.11+); no extra dependency.
    """
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SignalEmitError(f"Cannot read ticker map {path}: {exc}") from exc

    schema_version = raw.get("schema_version")
    universe_ref = raw.get("universe_ref")
    if not schema_version or not universe_ref:
        raise SignalEmitError(
            f"ticker_map.toml missing required field(s): "
            f"schema_version={schema_version!r}, universe_ref={universe_ref!r}"
        )

    raw_tickers = raw.get("tickers", [])
    if not isinstance(raw_tickers, list):
        raise SignalEmitError("ticker_map.toml: 'tickers' must be a TOML array")

    tickers: dict[str, TickerEntry] = {}
    for entry in raw_tickers:
        sym = entry.get("symbol")
        if not sym or not isinstance(sym, str):
            raise SignalEmitError(
                f"ticker_map.toml: ticker entry missing or invalid 'symbol': {entry!r}"
            )
        asset_class = entry.get("asset_class", "")
        aliases = entry.get("aliases", [])
        if not aliases or not isinstance(aliases, list):
            raise SignalEmitError(
                f"ticker_map.toml: ticker {sym!r} has no aliases — "
                "every entry must have at least one alias to match against"
            )
        tickers[sym] = TickerEntry(sym, asset_class, [str(a) for a in aliases])

    return TickerMap(
        schema_version=schema_version,
        universe_ref=universe_ref,
        tickers=tickers,
    )


# ---------------------------------------------------------------------------
# Article → ticker matching
# ---------------------------------------------------------------------------


def match_articles_to_tickers(
    articles: list[dict[str, Any]],
    ticker_map: TickerMap,
) -> dict[str, list[dict[str, Any]]]:
    """Match articles to tickers via word-boundary, case-insensitive alias search.

    Searches each article's title and full_text (when present).
    One article may match multiple tickers.
    Articles that match nothing are silently ignored — not an error (§6).

    Returns: symbol → list of matching article dicts.
    """
    matched: dict[str, list[dict[str, Any]]] = {}

    for article in articles:
        title = article.get("title") or ""
        full_text = article.get("full_text") or ""
        corpus = title + " " + full_text

        for symbol, entry in ticker_map.tickers.items():
            if entry.matches(corpus):
                matched.setdefault(symbol, []).append(article)

    return matched


# ---------------------------------------------------------------------------
# Signal record construction
# ---------------------------------------------------------------------------


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime, or None if unusable.

    Two formats occur in the articles table and they are NOT comparable as
    strings:
      - fetched_at is always ISO-8601 (datetime.isoformat(), '+00:00' offset).
      - published_at comes verbatim from the feed and is usually RFC 822
        ('Tue, 14 Jul 2026 00:00:00 GMT'), because src/fetchers/rss.py stores
        entry['published'] unmodified.

    Lexicographic max() over the two picks the RFC 822 value every time ('T' >
    '2'), which silently backdates knowable_at. Always compare datetimes.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_z(dt: datetime) -> str:
    """Format an aware UTC datetime as the Z-suffixed ISO-8601 the contract requires (§3)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _effective_dt(article: dict[str, Any]) -> datetime:
    """Return the timestamp at which this article became knowable, as UTC (§5).

    published_at when it is present, parseable, and not later than fetched_at;
    otherwise fetched_at. Never now or emitted_at — both are look-ahead leaks.

    Distrusting a published_at that postdates fetched_at is what makes a
    future-dated feed entry harmless: an article cannot have been fetched
    before it was published, so such a value is wrong, and honouring it would
    push knowable_at past emitted_at — a record the consumer must reject (§5).
    Clamping here makes that unrepresentable at the source.
    """
    fetched = _parse_ts(article.get("fetched_at"))
    if fetched is None:
        raise SignalEmitError(
            f"Article {article.get('id')!r} has an unparseable fetched_at "
            f"{article.get('fetched_at')!r}; cannot derive knowable_at."
        )

    published = _parse_ts(article.get("published_at"))
    if published is None:
        if article.get("published_at"):
            logger.warning(
                "Article %s has unparseable published_at %r; falling back to fetched_at",
                article.get("id"),
                article.get("published_at"),
            )
        return fetched
    if published > fetched:
        logger.warning(
            "Article %s published_at %r postdates fetched_at; distrusting it",
            article.get("id"),
            article.get("published_at"),
        )
        return fetched
    return published


def _provenance_published_at(article: dict[str, Any]) -> str | None:
    """Normalised published_at for provenance, or None when there is no usable one (§7).

    None is meaningful to an auditor: it marks the articles whose knowable_at
    came from the fetched_at fallback.
    """
    dt = _parse_ts(article.get("published_at"))
    return _to_z(dt) if dt is not None else None


def build_signals(
    matched: dict[str, list[dict[str, Any]]],
    universe_ref: str,
) -> list[dict[str, Any]]:
    """Build one signal record per ticker that has at least one scored article.

    Contract rules applied here:
      §4 — score is the MEAN of contributing article scores, rounded to 4 dp.
      §5 — knowable_at = max(published_at or fetched_at) across contributors, as UTC.
      §3 — no free text: articles reach the artifact only as url_hash + score.
      §7 — provenance carries article_id, source (lowercased), url_hash,
           published_at (null retained so auditors can see which used fetched_at).
    """
    signals: list[dict[str, Any]] = []

    for symbol, articles in matched.items():
        # Only articles that have been scored can contribute.
        scored = [(a, a.get("score")) for a in articles if a.get("score") is not None]
        if not scored:
            logger.debug(
                "Ticker %s: %d matched article(s) all lack a score; skipping",
                symbol,
                len(articles),
            )
            continue

        mean_score = round(sum(s for _, s in scored) / len(scored), 4)
        knowable_at = _to_z(max(_effective_dt(a) for a, _ in scored))

        provenance = [
            {
                "article_id": a["id"],
                "source": (a.get("source") or "").lower(),
                "url_hash": a.get("url_hash") or "",
                # Retain null so auditors can see which articles used fetched_at (§7).
                "published_at": _provenance_published_at(a),
            }
            for a, _ in scored
        ]

        signals.append(
            {
                "ticker": symbol,
                "score": mean_score,
                "knowable_at": knowable_at,
                "article_count": len(scored),
                "provenance": provenance,
            }
        )

    return signals


# ---------------------------------------------------------------------------
# Artifact emission
# ---------------------------------------------------------------------------


def emit_signals(
    articles: list[dict[str, Any]],
    ticker_map: TickerMap,
    signals_dir: Path,
    universe_ref: str | None = None,
) -> Path | None:
    """Match articles to tickers and write a versioned signal artifact atomically.

    Output path: signals_dir/signals-<UTC-date>.json
    Write mode:  write .tmp in the same directory, fsync, then os.replace (§2).
    An empty signals array is a valid, meaningful result (§3).

    Returns the written Path on success.  Raises SignalEmitError on failure.
    """
    try:
        signals_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SignalEmitError(f"Cannot create signals directory {signals_dir}: {exc}") from exc

    ref = universe_ref if universe_ref is not None else ticker_map.universe_ref
    matched = match_articles_to_tickers(articles, ticker_map)
    signals = build_signals(matched, ref)

    now = datetime.now(timezone.utc)
    emitted_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_str = now.strftime("%Y-%m-%d")

    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": emitted_at,
        "universe_ref": ref,
        "signals": signals,
    }

    target = signals_dir / f"signals-{date_str}.json"
    tmp = signals_dir / f"signals-{date_str}.json.tmp"

    payload = json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"
    try:
        tmp.write_text(payload, encoding="utf-8")
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        raise SignalEmitError(f"Failed to write signal artifact to {target}: {exc}") from exc

    logger.info(
        "Emitted %d signal(s) to %s (universe_ref=%s)",
        len(signals),
        target,
        ref,
    )
    return target
