"""Per-article extraction schema, validation and word-limit trimming.

Ollama's structured output (``format``) constrains the model's reply to
``EXTRACTION_SCHEMA``; ``parse_extraction`` still validates it, because the
constraint does not guarantee a complete reply (a cut-off generation is valid
prefix text, not valid JSON) and the date format is not expressible in the schema.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

# Long articles otherwise list every name and figure, and the reply runs past
# num_predict mid-string (seen on hub: 7 of 100 replies cut off). Ten each keeps
# the useful ones; the prompt asks for the most important first.
MAX_LIST_ITEMS = 10

STATUSES = ("happened", "scheduled", "ongoing")

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "what_happened": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_LIST_ITEMS},
        "numbers": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_LIST_ITEMS},
        "event_date": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(STATUSES)},
        "is_roundup": {"type": "boolean"},
    },
    "required": [
        "what_happened",
        "entities",
        "numbers",
        "event_date",
        "status",
        "is_roundup",
    ],
}

MAX_SENTENCES = 2

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A sentence ends at . ! ? followed by whitespace and a capital, digit or opening
# quote/bracket. Requiring that start keeps "U.S. officials" in one sentence.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


class ExtractionError(ValueError):
    """The model's reply is not a valid extraction."""


def trim_words(text: str, max_words: int) -> str:
    """Trim *text* to at most two sentences and *max_words* words.

    Cuts at the last sentence boundary that fits. If even the first sentence is
    over the limit, hard-cuts it at *max_words* words.
    """
    text = " ".join(text.split())
    sentences = _SENTENCE_SPLIT_RE.split(text)[:MAX_SENTENCES]

    kept: list[str] = []
    words = 0
    for sentence in sentences:
        count = len(sentence.split())
        if words + count > max_words:
            break
        kept.append(sentence)
        words += count

    if kept:
        return " ".join(kept)
    return " ".join(sentences[0].split()[:max_words]) if sentences else ""


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def parse_extraction(raw: str, max_words: int) -> dict:
    """Parse and validate a model reply; ``what_happened`` is trimmed to *max_words*.

    Raises:
        ExtractionError: not valid JSON, not an object, a field is missing or of the
            wrong type, ``status`` is not an allowed value, or ``event_date`` is
            neither null nor a real ``YYYY-MM-DD`` date.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise ExtractionError(f"reply is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ExtractionError("reply is not a JSON object")

    for name in EXTRACTION_SCHEMA["required"]:
        if name not in data:
            raise ExtractionError(f"missing field {name!r}")

    what = data["what_happened"]
    if not isinstance(what, str) or not what.strip():
        raise ExtractionError("what_happened must be a non-empty string")
    for name in ("entities", "numbers"):
        if not _is_str_list(data[name]):
            raise ExtractionError(f"{name} must be a list of strings")
    if data["status"] not in STATUSES:
        raise ExtractionError(f"status must be one of {', '.join(STATUSES)}")
    if not isinstance(data["is_roundup"], bool):
        raise ExtractionError("is_roundup must be a boolean")

    event_date = data["event_date"]
    if event_date is not None:
        if not isinstance(event_date, str) or not _DATE_RE.match(event_date):
            raise ExtractionError("event_date must be YYYY-MM-DD or null")
        try:
            datetime.strptime(event_date, "%Y-%m-%d")
        except ValueError as e:
            raise ExtractionError(f"event_date is not a real date: {event_date}") from e

    return {
        "what_happened": trim_words(what, max_words),
        "entities": data["entities"],
        "numbers": data["numbers"],
        "event_date": event_date,
        "status": data["status"],
        "is_roundup": data["is_roundup"],
    }
