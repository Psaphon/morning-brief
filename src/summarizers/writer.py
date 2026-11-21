"""Chooses the briefing writer: the local model (default) or Claude per section.

``SUMMARY_WRITER=local`` is exactly the pre-existing path (``generate_daily_briefing``).
With ``claude`` each reader section gets one API call; a section that fails, times out or
loses too many chunks falls back to the local model for that section only, and a monthly
spend cap switches every remaining section to local.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from ..config import Config
from ..validators.grounding import ValidatorRun
from .claude_writer import (
    SectionDraft,
    WriterError,
    WriterRun,
    check_section,
    cost_usd,
    load_api_key,
    prices_configured,
    request_section,
)
from .local import (
    _briefing_articles,
    generate_daily_briefing,
    generate_section_local,
    with_details,
)

logger = logging.getLogger(__name__)


def _group_by_section(articles: list[dict]) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {}
    for article in _briefing_articles(articles):
        sections.setdefault(article.get("category", "General"), []).append(article)
    return sections


def _marked(ids: list[int]) -> str:
    return f"[[{','.join(map(str, ids))}]]"


async def _local_section(
    config: Config, section: str, articles: list[dict], validator: ValidatorRun | None
) -> dict:
    """A section written locally; if Ollama gives nothing, a plain list of the articles."""
    known = {a["id"] for a in articles}
    segments = await generate_section_local(config.ollama, section, articles, validator)
    if segments:
        text = "\n\n".join(s["text"] for s in segments)
        cited = "\n\n".join(
            f"{s['text']} {_marked(ids)}" if (ids := _known_ids(s, known)) else s["text"]
            for s in segments
        )
        ids = sorted({i for s in segments for i in _known_ids(s, known)})
    else:
        logger.warning("Section %r: local writer gave nothing; using a plain article list", section)
        text = "\n\n".join(a["summary"] for a in articles)
        cited = "\n\n".join(f"{a['summary']} [[{a['id']}]]" for a in articles)
        ids = sorted(known)
    return _segment(section, text, cited, ids, "local fallback")


def _known_ids(segment: dict, known: set[int]) -> list[int]:
    return sorted({i for i in segment.get("source_article_ids", []) if i in known})


def _segment(section: str, text: str, cited: str, ids: list[int], writer: str) -> dict:
    return {
        "topic": section,
        "text": text,
        "cited_text": cited,
        "source_article_ids": ids,
        "writer": writer,
    }


def _claude_segment(section: str, draft: SectionDraft) -> dict:
    return _segment(section, draft.text, draft.cited_text, draft.source_article_ids, "claude")


async def write_sections(
    db,
    config: Config,
    validator: ValidatorRun | None = None,
    run: WriterRun | None = None,
) -> str | None:
    """Write today's briefing and save it; returns the rendered text, or None.

    *run* collects the writer's counts and alerts for ``last_run.json``.
    """
    writer = config.writer
    if writer.mode != "claude":
        return await generate_daily_briefing(db, config.ollama, validator)

    run = run if run is not None else WriterRun()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = db.get_briefing(today)
    if existing:
        logger.info("Daily briefing already exists for %s, reusing", today)
        return existing["content"]

    # Refuse to run without prices or a key: the whole brief goes to the local writer.
    if not prices_configured(writer):
        logger.warning(
            "SUMMARY_WRITER=claude needs WRITER_PRICE_IN_PER_MTOK and "
            "WRITER_PRICE_OUT_PER_MTOK; using the local writer"
        )
        return await generate_daily_briefing(db, config.ollama, validator)
    api_key = load_api_key()
    if api_key is None:
        return await generate_daily_briefing(db, config.ollama, validator)

    articles = with_details(db, db.get_todays_articles())
    sections = _group_by_section(articles)
    if not sections:
        logger.info("No summaries available for daily briefing")
        return None

    segments: list[dict] = []
    cap_reached = False
    async with httpx.AsyncClient() as client:
        for section, section_articles in sections.items():
            run.sections += 1
            draft: SectionDraft | None = None
            if not cap_reached:
                cap_reached = _cap_reached(db, config, run)
            if not cap_reached:
                draft = await _claude_section(
                    client, db, config, api_key, section, section_articles, validator, run
                )
            if draft is not None:
                run.claude += 1
                segments.append(_claude_segment(section, draft))
            else:
                run.fallback += 1
                segments.append(await _local_section(config, section, section_articles, validator))

    rendered = "\n\n".join(s["text"] for s in segments)
    model = writer.model if run.claude else config.ollama.model
    db.save_briefing(today, rendered, model, json.dumps(segments))
    logger.info(
        "Daily briefing generated (%d chars): %d sections by Claude, %d local fallback",
        len(rendered),
        run.claude,
        run.fallback,
    )
    return rendered


def _cap_reached(db, config: Config, run: WriterRun) -> bool:
    """True once the month-to-date spend is at the cap; warns and alerts the first time."""
    cap = config.writer.monthly_cap_usd
    spent = db.month_to_date_cost()
    if spent < cap:
        return False
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    logger.warning(
        "Writer spend cap reached for %s ($%.2f of $%.2f); using the local writer",
        month,
        spent,
        cap,
    )
    if db.claim_marker(f"writer_cap_alert_{month}"):
        run.alerts.append(f"writer spend cap reached {month}")
    return True


async def _claude_section(
    client: httpx.AsyncClient,
    db,
    config: Config,
    api_key: str,
    section: str,
    articles: list[dict],
    validator: ValidatorRun | None,
    run: WriterRun,
) -> SectionDraft | None:
    writer = config.writer
    try:
        reply = await request_section(client, writer, api_key, section, articles)
    except WriterError as e:
        logger.warning("Section %r: Claude call failed (%s); falling back to local", section, e)
        return None
    cost = cost_usd(writer, reply.tokens_in, reply.tokens_out)
    db.record_api_usage(writer.model, reply.tokens_in, reply.tokens_out, cost)
    run.tokens_in += reply.tokens_in
    run.tokens_out += reply.tokens_out
    run.cost_usd += cost
    return check_section(reply.text, articles, writer, validator, run)
