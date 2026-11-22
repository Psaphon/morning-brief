"""Grounding check: every generated sentence is verified against its source text.

Three checks, run in this order; the first failure is the drop reason:

* ``number`` — every digit group (commas stripped) must appear in the source.
* ``name``   — every capitalized, non-sentence-initial token must match the source.
* ``banned`` — filler phrases from ``config/banned_phrases.txt`` (plus an overlay).

A failing sentence is dropped, never rewritten: a small model's rewrite would
introduce new errors. The validator is a backstop, not a fact checker — a name that
appears in the source in a different role still passes.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..summarizers.schema import _SENTENCE_SPLIT_RE

logger = logging.getLogger(__name__)

# Anchored to the repo root (as src/processors/lanes.py does) so the lookup does not
# depend on the working directory, which differs in the container.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
DEFAULT_BANNED_PATH = _CONFIG_DIR / "banned_phrases.txt"
DEFAULT_ALLOWLIST_PATH = _CONFIG_DIR / "name_allowlist.txt"
OVERLAY_ENV = "MORNING_BRIEF_BANNED_OVERLAY"

MODES = ("enforce", "warn", "off")
REASONS = ("number", "name", "banned")

MIN_NAME_LEN = 3
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")
_PART_SPLIT_RE = re.compile(r"[-‐-―/]")
_EDGE_PUNCT = " \t.,;:!?\"'()[]{}‘’“”—–*_"


def split_sentences(text: str) -> list[str]:
    """Split *text* into sentences; "U.S. officials" stays in one sentence."""
    text = " ".join(text.split())
    return [s for s in _SENTENCE_SPLIT_RE.split(text) if s]


def _digit_groups(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in _NUMBER_RE.findall(text)}


def check_numbers(sentence: str, source: str) -> bool:
    """True if every digit group in *sentence* also appears in *source*.

    Scale words and suffixes need no special handling: "$44.6 million" and "$44.6M"
    share the digit group ``44.6``, while "$45.6 million" does not.
    """
    wanted = _digit_groups(sentence)
    return not wanted or wanted <= _digit_groups(source)


def _norm_word(word: str) -> str:
    return word.replace("’", "'").strip(_EDGE_PUNCT).lower()


def _name_tokens(sentence: str) -> list[str]:
    """Capitalized, non-sentence-initial name tokens (possessive and punctuation stripped)."""
    tokens: list[str] = []
    for index, raw in enumerate(sentence.split()):
        if index == 0:
            continue
        word = raw.replace("’", "'").strip(_EDGE_PUNCT)
        if word.lower().endswith("'s"):
            word = word[:-2]
        for part in _PART_SPLIT_RE.split(word):
            part = part.strip(_EDGE_PUNCT)
            if len(part) >= MIN_NAME_LEN and part[0].isupper() and part.replace("'", "").isalpha():
                tokens.append(part)
    return tokens


def check_names(sentence: str, source: str, allowlist: Collection[str] = ()) -> bool:
    """True if every checked name token in *sentence* matches the *source*.

    A token is checked when it is capitalized, not sentence-initial, at least 3
    characters and not in *allowlist*. It matches when the source has a word starting
    with the token's first ``max(4, len - 2)`` characters (case-insensitive), so
    "Democrats" matches "Democratic".
    """
    allowed = {_norm_word(a) for a in allowlist}
    haystack = source.replace("’", "'").lower()
    for token in _name_tokens(sentence):
        lowered = token.lower()
        if lowered in allowed or _norm_word(token) in allowed:
            continue
        prefix = lowered[: max(4, len(lowered) - 2)]
        if not re.search(rf"(?<!\w){re.escape(prefix)}", haystack):
            return False
    return True


def check_banned(sentence: str, phrases: Collection[str]) -> bool:
    """True if *sentence* contains none of *phrases* (case-insensitive substring)."""
    lowered = sentence.replace("’", "'").lower()
    return not any(p and p.replace("’", "'").lower() in lowered for p in phrases)


def _first_failure(
    sentence: str, source: str, allowlist: Collection[str], phrases: Collection[str]
) -> str | None:
    if not check_numbers(sentence, source):
        return "number"
    if not check_names(sentence, source, allowlist):
        return "name"
    if not check_banned(sentence, phrases):
        return "banned"
    return None


@dataclass(frozen=True)
class DroppedSentence:
    sentence: str
    reason: str  # one of REASONS


@dataclass
class ValidationResult:
    """Outcome of validating one text: kept sentences, dropped ones and counts."""

    kept: list[str] = field(default_factory=list)
    dropped: list[DroppedSentence] = field(default_factory=list)
    text: str = ""  # kept sentences re-joined; paragraph breaks are preserved

    @property
    def checked(self) -> int:
        return len(self.kept) + len(self.dropped)

    @property
    def counts(self) -> dict[str, int]:
        return {r: sum(1 for d in self.dropped if d.reason == r) for r in REASONS}


def validate_text(
    text: str,
    source: str,
    *,
    allowlist: Collection[str] = (),
    phrases: Collection[str] = (),
) -> ValidationResult:
    """Check each sentence of *text* against *source*; drop those that fail."""
    result = ValidationResult()
    paragraphs: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        kept_here: list[str] = []
        for sentence in split_sentences(paragraph):
            reason = _first_failure(sentence, source, allowlist, phrases)
            if reason is None:
                kept_here.append(sentence)
                result.kept.append(sentence)
            else:
                result.dropped.append(DroppedSentence(sentence, reason))
        if kept_here:
            paragraphs.append(" ".join(kept_here))
    result.text = "\n\n".join(paragraphs)
    return result


def check_chunk(
    text: str,
    sources: Sequence[str],
    *,
    allowlist: Collection[str] = (),
    phrases: Collection[str] = (),
) -> str | None:
    """The first failure reason (one of REASONS) of *text* against several *sources*, or None.

    The sources are joined, so a chunk that combines two articles may take a number from
    one and a name from the other. Every sentence of *text* must pass.
    """
    source = "\n".join(sources)
    for sentence in split_sentences(text):
        reason = _first_failure(sentence, source, allowlist, phrases)
        if reason is not None:
            return reason
    return None


def _read_list(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def load_banned_phrases(path: Path = DEFAULT_BANNED_PATH, overlay: Path | None = None) -> list[str]:
    """Committed banned phrases plus the overlay, if that file exists.

    *overlay* defaults to ``$MORNING_BRIEF_BANNED_OVERLAY``. A missing overlay is not
    an error (DEBUG log only).
    """
    phrases = _read_list(path)
    if overlay is None and (env := os.environ.get(OVERLAY_ENV)):
        overlay = Path(env)
    if overlay is not None:
        if overlay.is_file():
            phrases.extend(_read_list(overlay))
        else:
            logger.debug("Banned-phrase overlay not found, skipping: %s", overlay)
    return phrases


def load_name_allowlist(path: Path = DEFAULT_ALLOWLIST_PATH) -> frozenset[str]:
    """Common capitalized words that need no match in the source (lowercased)."""
    return frozenset(_norm_word(w) for w in _read_list(path))


@dataclass
class ValidatorRun:
    """One pipeline run's validator: mode, word lists and running counts."""

    mode: str = "enforce"
    allowlist: Collection[str] = ()
    phrases: Collection[str] = ()
    checked: int = 0
    dropped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(REASONS, 0))

    @classmethod
    def from_config(cls, config) -> ValidatorRun:
        mode = config.validator_mode
        if mode == "off":
            return cls(mode=mode)
        return cls(
            mode=mode,
            allowlist=load_name_allowlist(),
            phrases=load_banned_phrases(overlay=config.banned_overlay),
        )

    def apply(self, text: str, source: str, label: str) -> str:
        """The text to use: failing sentences removed (enforce), or unchanged (warn/off).

        ``""`` means every sentence was dropped. In warn mode failing sentences are
        counted and logged but kept.
        """
        if self.mode == "off":
            return text
        result = validate_text(text, source, allowlist=self.allowlist, phrases=self.phrases)
        self.checked += result.checked
        for item in result.dropped:
            self.dropped[item.reason] += 1
            if self.mode == "warn":
                logger.warning(
                    "Ungrounded sentence (%s) kept in %s: %s", item.reason, label, item.sentence
                )
            else:
                logger.info("Dropped sentence (%s) from %s: %s", item.reason, label, item.sentence)
        return text if self.mode == "warn" else result.text

    def metadata(self) -> dict:
        return {"mode": self.mode, "checked": self.checked, "dropped": dict(self.dropped)}
