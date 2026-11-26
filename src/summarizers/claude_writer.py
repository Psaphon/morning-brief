"""Claude section writer: one cited, long summary per reader section.

The request carries extractions only (id, title, source, ``what_happened``, ``details``):
no full text and no URLs. The reply is split into chunks at ``[[id]]`` citation markers
and every chunk is checked in code against the articles it cites; failing chunks are
dropped. ``writer.py`` decides what happens when a section loses too much.

The API key is read from a systemd credential file and is never logged: errors are
reported by type and status only, never with ``exc_info``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import WriterConfig
from ..validators.grounding import ValidatorRun, check_chunk
from .local import _briefing_text

logger = logging.getLogger(__name__)

CREDENTIAL_NAME = "anthropic_api_key"
ANTHROPIC_VERSION = "2023-06-01"
THINKING_HEADROOM_TOKENS = 4000
DROP_REASONS = ("uncited", "unknown_id", "number", "name", "banned")

_MARKER_RE = re.compile(r"\[\[([^\[\]]*)\]\]")

SYSTEM_PROMPT = """\
You write one section of a daily news briefing from the numbered articles you are given.

Rules:
- Use only facts stated in the given articles. No outside knowledge, no evaluation, \
no speculation, no filler.
- Every sentence or group of sentences drawn from sources must end with a citation \
marker: [[id]] for one article, or [[id1,id2]] when it combines several. Use only the \
ids given. Put the marker after the final punctuation.
- Copy names, numbers and dates exactly as the articles state them.
- Write {min_words} to {max_words} words of prose paragraphs. No headings, no lists, \
no preamble."""


class WriterError(Exception):
    """A Claude call failed. The message is safe to log (it never holds the key).

    ``tokens_in`` / ``tokens_out`` are set when the API billed the call anyway (a reply
    that arrived but can't be used), so the spend guard still counts it.
    """

    def __init__(self, message: str, *, tokens_in: int = 0, tokens_out: int = 0) -> None:
        super().__init__(message)
        self.tokens_in, self.tokens_out = tokens_in, tokens_out


@dataclass
class WriterRun:
    """One run's writer counts, reported in ``last_run.json`` under ``writer``."""

    sections: int = 0
    claude: int = 0
    fallback: int = 0
    dropped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(DROP_REASONS, 0))
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    alerts: list[str] = field(default_factory=list)

    def metadata(self) -> dict:
        return {
            "sections": self.sections,
            "claude": self.claude,
            "fallback": self.fallback,
            "dropped": dict(self.dropped),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass(frozen=True)
class ClaudeReply:
    text: str
    tokens_in: int
    tokens_out: int


@dataclass
class SectionDraft:
    """A validated section: prose without markers, prose with markers, cited ids."""

    text: str
    cited_text: str
    source_article_ids: list[int]


def load_api_key(environ: dict[str, str] | None = None) -> str | None:
    """The API key from ``$CREDENTIALS_DIRECTORY/anthropic_api_key``, or None.

    Whitespace is stripped. A missing variable, file or content logs one line that names
    the credential (never its value) and returns None. The read error is not chained or
    logged with a traceback.
    """
    env = os.environ if environ is None else environ
    directory = env.get("CREDENTIALS_DIRECTORY", "").strip()
    if not directory:
        logger.warning(
            "SUMMARY_WRITER=claude but CREDENTIALS_DIRECTORY is not set; "
            "credential %s unavailable, using the local writer",
            CREDENTIAL_NAME,
        )
        return None
    path = Path(directory) / CREDENTIAL_NAME
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "SUMMARY_WRITER=claude but credential %s is missing or unreadable; "
            "using the local writer",
            CREDENTIAL_NAME,
        )
        return None
    if not key:
        logger.warning(
            "SUMMARY_WRITER=claude but credential %s is empty; using the local writer",
            CREDENTIAL_NAME,
        )
        return None
    return key


def cost_usd(config: WriterConfig, tokens_in: int, tokens_out: int) -> float:
    """Estimated cost of one call; prices must be set (``prices_configured``)."""
    assert config.price_in_per_mtok is not None and config.price_out_per_mtok is not None
    return (
        tokens_in * config.price_in_per_mtok + tokens_out * config.price_out_per_mtok
    ) / 1_000_000


def prices_configured(config: WriterConfig) -> bool:
    return config.price_in_per_mtok is not None and config.price_out_per_mtok is not None


def build_request(config: WriterConfig, section: str, articles: list[dict]) -> dict:
    """The Messages API body for one section: extractions only, nothing else."""
    lines = [f"Section: {section}", ""]
    for article in articles:
        lines.append(f"[{article['id']}] {article.get('title', '')}")
        lines.append(f"Source: {article.get('source', '')}")
        lines.append(f"What happened: {article['summary']}")
        details = article.get("details") or []
        if details:
            lines.append("Details:")
            lines.extend(f"- {d}" for d in details)
        lines.append("")
    return {
        "model": config.model,
        # Thinking tokens count toward max_tokens, so leave room beyond the answer.
        "max_tokens": config.max_words * 3 + 300 + THINKING_HEADROOM_TOKENS,
        # A grounded rewrite of given extractions needs little reasoning.
        "output_config": {"effort": "low"},
        "system": SYSTEM_PROMPT.format(min_words=config.min_words, max_words=config.max_words),
        "messages": [{"role": "user", "content": "\n".join(lines).rstrip()}],
    }


async def request_section(
    client: httpx.AsyncClient,
    config: WriterConfig,
    api_key: str,
    section: str,
    articles: list[dict],
) -> ClaudeReply:
    """One Messages API call. Raises ``WriterError`` on an HTTP error, timeout or bad reply."""
    try:
        resp = await client.post(
            config.api_url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json=build_request(config, section, articles),
            timeout=config.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        raise WriterError(f"HTTP {e.response.status_code}") from None
    except httpx.TimeoutException:
        raise WriterError("timeout") from None
    except httpx.HTTPError as e:
        raise WriterError(f"request failed ({type(e).__name__})") from None
    except ValueError:
        raise WriterError("response is not valid JSON") from None
    try:
        # Current models think by default, so the reply can open with a ``thinking``
        # block (empty text by default); the answer is the ``text`` blocks, in order.
        blocks = data["content"]
        text = "".join(b["text"] for b in blocks if b.get("type") == "text")
        stop_reason = data.get("stop_reason")
        usage = data["usage"]
        tokens_in, tokens_out = int(usage["input_tokens"]), int(usage["output_tokens"])
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise WriterError("response has an unexpected shape") from None
    billed = {"tokens_in": tokens_in, "tokens_out": tokens_out}
    if stop_reason == "refusal":
        raise WriterError("model declined the request (refusal)", **billed)
    if stop_reason == "max_tokens":
        raise WriterError("response was cut off at max_tokens", **billed)
    if not text.strip():
        raise WriterError("response text is empty", **billed)
    return ClaudeReply(text, tokens_in, tokens_out)


def split_cited_chunks(text: str) -> list[tuple[str, list[int | str]]]:
    """Split *text* after each ``[[...]]`` marker into ``(chunk_text, ids)``.

    Trailing text with no marker is a chunk with ``[]``. A chunk that starts a new
    paragraph keeps a leading blank line. A marker with nothing before it (two markers in
    a row) adds its ids to the previous chunk. Ids that are not integers stay strings, so
    the caller can count them as unknown.
    """
    chunks: list[tuple[str, list[int | str]]] = []
    pos = 0

    def add(raw: str, ids: list[int | str]) -> None:
        body = raw.strip()
        if not body:
            if ids and chunks:
                chunks[-1][1].extend(i for i in ids if i not in chunks[-1][1])
            return
        if re.match(r"\s*\n\s*\n", raw):
            body = "\n\n" + body
        chunks.append((body, ids))

    for match in _MARKER_RE.finditer(text):
        tokens = [t.strip() for t in match.group(1).split(",") if t.strip()]
        ids: list[int | str] = [int(t) if t.isdigit() else t for t in tokens]
        add(text[pos : match.start()], ids)
        pos = match.end()
    add(text[pos:], [])
    return chunks


def check_section(
    text: str,
    articles: list[dict],
    config: WriterConfig,
    validator: ValidatorRun | None,
    run: WriterRun,
) -> SectionDraft | None:
    """Validate a reply chunk by chunk; None means the section should fall back.

    A chunk is dropped (and counted under its reason in *run*) when it has no citation,
    cites an id that is not in this section, or fails the grounding check against the
    union of its cited articles' title, ``what_happened`` and ``details``. The section
    falls back when dropped / total is above ``max_drop_share`` or nothing survives.
    """
    sources = {a["id"]: f"{a.get('title', '')}\n{_briefing_text(a)}" for a in articles}
    chunks = split_cited_chunks(text)
    kept: list[tuple[str, list[int]]] = []
    dropped = 0
    for body, ids in chunks:
        reason = _chunk_failure(body, ids, sources, validator)
        if reason is None:
            kept.append((body, [i for i in ids if isinstance(i, int)]))
        else:
            dropped += 1
            run.dropped[reason] += 1
            logger.info("Writer dropped a chunk (%s): %s", reason, body.strip()[:120])
    total = len(chunks)
    if not kept or dropped / total > config.max_drop_share:
        logger.warning(
            "Writer section lost %d of %d chunks (limit %.0f%%); falling back to local",
            dropped,
            total,
            config.max_drop_share * 100,
        )
        return None
    return SectionDraft(
        text=_join([body for body, _ in kept]),
        cited_text=_join([f"{body} [[{','.join(map(str, ids))}]]" for body, ids in kept]),
        source_article_ids=sorted({i for _, ids in kept for i in ids}),
    )


def _chunk_failure(
    body: str,
    ids: list[int | str],
    sources: dict[int, str],
    validator: ValidatorRun | None,
) -> str | None:
    if not ids:
        return "uncited"
    if any(not isinstance(i, int) or i not in sources for i in ids):
        return "unknown_id"
    if validator is None or validator.mode == "off":
        return None
    reason = check_chunk(
        body,
        [sources[i] for i in ids if isinstance(i, int)],
        allowlist=validator.allowlist,
        phrases=validator.phrases,
    )
    if reason is not None and validator.mode == "warn":
        logger.warning("Ungrounded chunk (%s) kept in writer output: %s", reason, body[:120])
        return None
    return reason


def _join(pieces: list[str]) -> str:
    """Join chunk texts with a space, or keep the blank line a chunk starts with."""
    out = ""
    for piece in pieces:
        out += piece if (not out or piece.startswith("\n\n")) else f" {piece}"
    return out
