"""Summary bake-off: score a local model's section writing against the stored Claude sections.

The local model gets exactly what Claude gets (``build_request``'s system and user text),
and its reply goes through the same ``split_cited_chunks`` / ``check_section`` validator.
Nothing is published or saved to the database, and the Claude API is never called: the
Claude side is read from the day's stored briefing segments.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from ..config import Config, OllamaConfig
from ..processors.lanes import filter_lane, load_lanes_for_run
from ..validators.grounding import ValidatorRun
from .claude_writer import (
    WriterRun,
    _chunk_failure,
    build_request,
    check_section,
    split_cited_chunks,
)
from .local import BRIEFING_TIMEOUT, _briefing_text, with_details
from .writer import _group_by_section

logger = logging.getLogger(__name__)


class BakeoffError(Exception):
    """The bake-off cannot run; the message is meant for the user."""


@dataclass
class Score:
    """One writer's result for one section. ``None`` means not available (n/a)."""

    pass_rate: float | None = None
    coverage: float | None = None
    words: int | None = None
    fallback: bool | None = None


def _words(text: str) -> int:
    return len(text.split())


def load_day(db, date: str, config: Config) -> dict[str, list[dict]]:
    """The briefing input of UTC *date*, by section: what the live writer would have seen.

    Reader-lane articles fetched that day that have a stored extraction, minus roundups and
    failed extractions (``_group_by_section`` applies those rules), with their ``details``.
    """
    articles = db.get_articles_for_date(date)
    lanes = load_lanes_for_run(config.feeds_overlay)
    articles = filter_lane(articles, "reader", lanes)
    extracted = db.get_extraction_details([a["id"] for a in articles])
    articles = [a for a in articles if a["id"] in extracted]
    if not articles:
        raise BakeoffError(f"No stored extractions for {date}; nothing to score.")
    sections = _group_by_section(with_details(db, articles))
    if not sections:
        raise BakeoffError(f"No usable extractions for {date} (all roundups or failed).")
    return sections


async def local_section_reply(
    ollama: OllamaConfig,
    model: str,
    section: str,
    articles: list[dict],
    config: Config,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """One plain-text Ollama reply with the same system and user text Claude gets.

    Returns None when the call fails or the reply is empty.
    """
    request = build_request(config.writer, section, articles)
    body = {
        "model": model,
        "system": request["system"],
        "prompt": request["messages"][0]["content"],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.5,
            "top_p": 0.9,
            "num_predict": request["max_tokens"],
            "num_ctx": ollama.briefing_num_ctx,
        },
    }
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        resp = await client.post(f"{ollama.host}/api/generate", json=body, timeout=BRIEFING_TIMEOUT)
        resp.raise_for_status()
        text = resp.json().get("response", "")
    except (httpx.HTTPError, ValueError, AttributeError) as e:
        logger.warning("Section %r: local model call failed (%s)", section, type(e).__name__)
        return None
    finally:
        if own:
            await client.aclose()
    return text.strip() or None


def score_local(
    reply: str | None, articles: list[dict], config: Config, validator: ValidatorRun | None
) -> Score:
    """Score a reply with the claude writer's own chunking and validation."""
    if reply is None:
        return Score(pass_rate=None, coverage=0.0, words=0, fallback=True)
    chunks = split_cited_chunks(reply)
    if not chunks:
        return Score(pass_rate=None, coverage=0.0, words=0, fallback=True)
    run = WriterRun()
    draft = check_section(reply, articles, config.writer, validator, run)
    dropped = sum(run.dropped.values())
    pass_rate = (len(chunks) - dropped) / len(chunks)
    if draft is not None:
        text, ids = draft.text, set(draft.source_article_ids)
    else:  # the section would fall back; still score what survived
        sources = {a["id"]: f"{a.get('title', '')}\n" + _briefing_text(a) for a in articles}
        kept = [(b, i) for b, i in chunks if _chunk_failure(b, i, sources, validator) is None]
        text = " ".join(b for b, _ in kept)
        ids = {i for _, cited in kept for i in cited if isinstance(i, int)}
    return Score(
        pass_rate=pass_rate,
        coverage=len(ids & {a["id"] for a in articles}) / len(articles),
        words=_words(text),
        fallback=draft is None,
    )


def score_claude(segments: list[dict] | None, sections: dict[str, list[dict]]) -> dict[str, Score]:
    """The stored Claude side per section; sections with no stored segment are absent.

    A ``claude`` segment gives pass rate (from ``stats``, else n/a), coverage and words.
    A ``local fallback`` segment is a Claude fallback with every other column n/a.
    """
    scores: dict[str, Score] = {}
    for seg in segments or []:
        topic = seg.get("topic")
        if topic not in sections:
            continue
        writer = seg.get("writer")
        if writer == "claude":
            stats = seg.get("stats") or {}
            chunks = stats.get("chunks")
            dropped = sum((stats.get("dropped") or {}).values())
            rate = (chunks - dropped) / chunks if chunks else None
            known = {a["id"] for a in sections[topic]}
            cited = {i for i in seg.get("source_article_ids", []) if i in known}
            scores[topic] = Score(
                pass_rate=rate,
                coverage=len(cited) / len(known),
                words=_words(seg.get("text", "")),
                fallback=False,
            )
        elif writer == "local fallback":
            scores[topic] = Score(fallback=True)
    return scores


def _mean(values: list[float | int]) -> float | None:
    return sum(values) / len(values) if values else None


def totals(scores: list[Score]) -> dict:
    """Mean pass rate, coverage and words over the sections that have them, and fallbacks.

    ``fallbacks`` is None when there are no sections to count (no stored Claude sections).
    """
    flags = [s.fallback for s in scores if s.fallback is not None]
    return {
        "pass_rate": _mean([s.pass_rate for s in scores if s.pass_rate is not None]),
        "coverage": _mean([s.coverage for s in scores if s.coverage is not None]),
        "words": _mean([s.words for s in scores if s.words is not None]),
        "fallbacks": sum(flags) if flags else None,
    }


async def run_bakeoff(db, config: Config, model: str, date: str) -> dict:
    """Score *model* on *date*'s sections against the stored Claude sections."""
    sections = load_day(db, date, config)
    validator = ValidatorRun.from_config(config)
    stored = db.get_briefing(date)
    claude = score_claude(stored["segment_map"] if stored else None, sections)

    local: dict[str, Score] = {}
    async with httpx.AsyncClient() as client:
        for section, articles in sections.items():
            reply = await local_section_reply(
                config.ollama, model, section, articles, config, client
            )
            local[section] = score_local(reply, articles, config, validator)

    claude_totals = totals(list(claude.values()))
    return {
        "date": date,
        "model": model,
        "sections": {
            name: {
                "articles": len(sections[name]),
                "local": asdict(local[name]),
                "claude": asdict(claude[name]) if name in claude else None,
            }
            for name in sections
        },
        "totals": {"local": totals(list(local.values())), "claude": claude_totals},
    }


def report_path(directory: Path, date: str, model: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    return directory / f"bakeoff-{date}-{safe}.json"


def write_report(report: dict, directory: Path) -> Path:
    """Write the report to ``<directory>/bakeoff-<date>-<model>.json``; returns the path."""
    path = report_path(directory, report["date"], report["model"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: float | int | None) -> str:
    return "n/a" if value is None else f"{value:.0f}"


def _fb(value: int | bool | None) -> str:
    return "n/a" if value is None else str(int(value))


def _cells(pass_rate, coverage, words, fallback) -> list[str]:
    return [_pct(pass_rate), _pct(coverage), _num(words), _fb(fallback)]


def format_table(report: dict) -> str:
    """Side-by-side table: section, local pass% cov% words fb, claude pass% cov% words fb."""
    head = ["pass%", "cov%", "words", "fb"]
    rows: list[list[str]] = []
    for name, entry in report["sections"].items():
        local, claude = entry["local"], entry["claude"] or {}
        rows.append(
            [name]
            + _cells(*(local[k] for k in ("pass_rate", "coverage", "words", "fallback")))
            + _cells(*(claude.get(k) for k in ("pass_rate", "coverage", "words", "fallback")))
        )
    local_t, claude_t = report["totals"]["local"], report["totals"]["claude"]
    keys = ("pass_rate", "coverage", "words", "fallbacks")
    rows.append(
        ["TOTAL"] + _cells(*(local_t[k] for k in keys)) + _cells(*(claude_t[k] for k in keys))
    )
    header = ["section"] + head + head
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    title = (
        " " * widths[0] + "  " + f"{'local: ' + report['model']:<{sum(widths[1:5]) + 6}}  claude"
    )

    def line(cells: list[str]) -> str:
        return "  ".join(
            c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells)
        )

    return "\n".join([title, line(header), *map(line, rows)])
