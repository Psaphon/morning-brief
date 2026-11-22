"""Components and direction for earnings filings (SIGNAL-SCHEMA section 12).

Everything here is a pure function of verified numbers and config. No model output
reaches ``direction``: the model only copies figures, and those are checked first.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime

COMPONENT_KEYS = ("eps_yoy", "rev_yoy", "rev_accel", "guidance_change")

# rev_accel is only meaningful against the previous quarter's release.
ACCEL_MIN_DAYS = 50
ACCEL_MAX_DAYS = 120


def pct_change(current: float | None, prior: float | None) -> float | None:
    """100 * (current - prior) / abs(prior); None if an input is missing or prior is 0."""
    if current is None or prior is None or prior == 0:
        return None
    return 100.0 * (current - prior) / abs(prior)


def days_between(earlier: str, later: str) -> float:
    """Days from one ``...Z`` UTC timestamp to another."""
    a = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    b = datetime.fromisoformat(later.replace("Z", "+00:00"))
    return (b - a).total_seconds() / 86400.0


def rev_accel(
    rev_yoy: float | None,
    previous_rev_yoy: float | None,
    previous_accepted_at: str | None,
    accepted_at: str,
) -> float | None:
    """This filing's rev_yoy minus the previous one's, if that one is 50-120 days earlier."""
    if rev_yoy is None or previous_rev_yoy is None or previous_accepted_at is None:
        return None
    gap = days_between(previous_accepted_at, accepted_at)
    if not ACCEL_MIN_DAYS <= gap <= ACCEL_MAX_DAYS:
        return None
    return rev_yoy - previous_rev_yoy


def midpoint(low: float | None, high: float | None) -> float | None:
    if low is None or high is None:
        return None
    return (low + high) / 2.0


def guidance_change(
    current_mid: float | None, previous_mid: float | None, tolerance: float
) -> int | None:
    """+1 / -1 if the midpoint moved by more than ``tolerance`` (relative), else 0.

    None when either midpoint is missing or the earlier one is 0 (no relative change).
    """
    if current_mid is None or previous_mid is None or previous_mid == 0:
        return None
    change = (current_mid - previous_mid) / abs(previous_mid)
    if change > tolerance:
        return 1
    if change < -tolerance:
        return -1
    return 0


def normalise_period(label: str | None) -> str:
    """Case- and whitespace-insensitive form of a period label, for matching."""
    return " ".join((label or "").lower().split())


def compute_direction(
    components: Mapping[str, float | None],
    weights: Mapping[str, float],
    scales: Mapping[str, float],
) -> tuple[float | None, int]:
    """``clip(sum(w_i * tanh(c_i / k_i)), -1, 1)`` over non-null components.

    Weights are renormalised over the components actually used. Returns
    ``(direction, direction_n)``; with no usable component, ``(None, 0)``.
    """
    used = [
        key
        for key in COMPONENT_KEYS
        if components.get(key) is not None
        and math.isfinite(components[key])  # type: ignore[arg-type]
        and weights.get(key, 0.0) > 0
        and scales.get(key, 0.0) > 0
    ]
    total = sum(weights[key] for key in used)
    if not used or total <= 0:
        return None, 0
    value = sum(weights[key] / total * math.tanh(components[key] / scales[key]) for key in used)  # type: ignore[operator]
    return max(-1.0, min(1.0, value)), len(used)
