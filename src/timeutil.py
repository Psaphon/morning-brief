"""Timestamp parsing and normalisation.

The articles table receives timestamps from two very different places, and they
are NOT comparable as strings:

  - ``fetched_at`` is generated locally by ``datetime.isoformat()``
    (``2026-09-01T22:34:25.031038+00:00``).
  - ``published_at`` arrives from RSS feeds, which overwhelmingly use RFC 822
    (``Tue, 14 Jul 2026 00:00:00 GMT``).

Sorting those lexicographically puts every RFC 822 value above every ISO one,
because ``'T'`` sorts after ``'2'``. Anything that compares timestamps must
parse them first. This module is the single place that knows how.
"""

from __future__ import annotations

import email.utils
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The one format this project stores and emits: ISO-8601, UTC, ``Z``-suffixed.
UTC_Z_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 or RFC 822 timestamp into an aware UTC datetime.

    Returns None when *value* is empty or cannot be parsed as either format.
    A naive datetime is assumed to be UTC.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(str(value))
            except (TypeError, ValueError):
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_utc_z(dt: datetime) -> str:
    """Format an aware datetime as ISO-8601 UTC with a ``Z`` suffix."""
    return dt.astimezone(timezone.utc).strftime(UTC_Z_FORMAT)


def normalise_timestamp(value: str | None) -> str | None:
    """Normalise a timestamp string to ISO-8601 UTC ``Z``, or None if unusable.

    None is a meaningful value in this schema: ``articles.published_at`` is
    nullable and null means "the source did not give us one". An unparseable
    string carries no more information than null while breaking every consumer
    that tries to order by it, so it normalises to None.
    """
    dt = parse_timestamp(value)
    return to_utc_z(dt) if dt is not None else None
