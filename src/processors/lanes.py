"""Feed lanes: which parts of the pipeline may use each feed.

* ``reader``  — the briefing and the dashboard.
* ``signals`` — the per-ticker signals.
* ``rnd``     — stored in the DB only; never shown in the briefing or on the page.

Lanes are per feed (keyed by the feed's ``source`` name), declared in
``config/feed_lanes.toml``. A per-host overlay file (``$MORNING_BRIEF_FEEDS_OVERLAY``)
uses the same shape and merges over it by feed id; it may also add new feeds.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_LANES = frozenset({"reader", "signals", "rnd"})
BUILTIN_DEFAULT = frozenset({"reader", "signals"})
# Anchored to the repo root so the lookup does not depend on the working directory.
DEFAULT_LANES_PATH = Path(__file__).resolve().parents[2] / "config" / "feed_lanes.toml"


@dataclass(frozen=True)
class FeedLanes:
    """Lane assignments by feed id, plus feeds added by the overlay."""

    default: frozenset[str] = BUILTIN_DEFAULT
    feeds: dict[str, frozenset[str]] = field(default_factory=dict)
    extra_feeds: list[dict[str, str]] = field(default_factory=list)

    def lanes_for(self, source: str) -> frozenset[str]:
        return self.feeds.get(source, self.default)


def _parse_lanes(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: lanes must be a non-empty list, got {value!r}")
    unknown = [v for v in value if v not in VALID_LANES]
    if unknown:
        raise ValueError(
            f"{where}: unknown lane(s) {unknown!r}; valid lanes are {sorted(VALID_LANES)}"
        )
    return frozenset(value)


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: invalid TOML: {e}") from e


def _entries(data: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    feeds = data.get("feeds", {})
    if not isinstance(feeds, dict) or not all(isinstance(v, dict) for v in feeds.values()):
        raise ValueError(f'{path}: [feeds."<name>"] must be tables')
    return feeds


def load_feed_lanes(path: Path = DEFAULT_LANES_PATH, overlay: Path | None = None) -> FeedLanes:
    """Load the committed lanes file and merge the optional overlay over it."""
    data = _read(path)
    default = (
        _parse_lanes(data["default"], f"{path}: default") if "default" in data else BUILTIN_DEFAULT
    )
    feeds = {
        name: _parse_lanes(entry.get("lanes"), f"{path}: feed {name!r}")
        for name, entry in _entries(data, path).items()
    }
    extra: list[dict[str, str]] = []

    if overlay is not None:
        if not overlay.is_file():
            logger.info("Feed lanes overlay not found, skipping: %s", overlay)
        else:
            odata = _read(overlay)
            if "default" in odata:
                default = _parse_lanes(odata["default"], f"{overlay}: default")
            for name, entry in _entries(odata, overlay).items():
                where = f"{overlay}: feed {name!r}"
                url, category = entry.get("url"), entry.get("category")
                if url is not None or category is not None:
                    if not (
                        isinstance(url, str) and url and isinstance(category, str) and category
                    ):
                        raise ValueError(f"{where}: a new feed needs both url and category")
                    extra.append({"source": name, "url": url, "category": category})
                    # A new feed without lanes gets the default.
                    feeds[name] = (
                        _parse_lanes(entry["lanes"], where) if "lanes" in entry else default
                    )
                else:
                    feeds[name] = _parse_lanes(entry.get("lanes"), where)

    return FeedLanes(default=default, feeds=feeds, extra_feeds=extra)


def load_lanes_for_run(overlay: Path | None, path: Path = DEFAULT_LANES_PATH) -> FeedLanes:
    """Lanes for a pipeline run: a broken per-host overlay must not cost the day's brief.

    An invalid overlay is logged as an error and ignored, falling back to the
    committed file (which CI validates). An invalid committed file still raises.
    """
    try:
        return load_feed_lanes(path, overlay=overlay)
    except ValueError:
        logger.exception("Feed lanes overlay is invalid; using the committed lanes only")
        return load_feed_lanes(path)


def filter_lane(
    articles: list[dict[str, Any]], lane: str, feed_lanes: FeedLanes
) -> list[dict[str, Any]]:
    """Keep the articles whose source is in *lane*."""
    if lane not in VALID_LANES:
        raise ValueError(f"unknown lane {lane!r}")
    return [a for a in articles if lane in feed_lanes.lanes_for(a.get("source", ""))]


class ReaderLaneDB:
    """Database view whose ``get_todays_articles`` returns only ``reader`` articles.

    The briefing generators read summaries straight from the DB, so wrapping the
    DB keeps a previously summarized ``rnd`` article out of the briefing without
    changing their signatures. Every other attribute is delegated unchanged.
    """

    def __init__(self, db: Any, feed_lanes: FeedLanes) -> None:
        self._db = db
        self._lanes = feed_lanes

    def get_todays_articles(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return filter_lane(self._db.get_todays_articles(*args, **kwargs), "reader", self._lanes)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)
