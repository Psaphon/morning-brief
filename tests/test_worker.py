"""Tests for Cloudflare Worker request validation and response formatting.

These tests exercise the validation logic described in worker/src/index.js
without running a live Worker. They validate the contract between the dashboard
and the Worker — ensuring the request format, field constraints, and error
conditions are handled correctly.
"""

from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Helpers mirroring worker validation logic in Python so we can unit-test the
# contract without a running JS runtime.
# ---------------------------------------------------------------------------

VALID_ACTIONS = {"elaborate", "research", "sources"}


def validate_body(body: object) -> dict:
    """Python mirror of the Worker's validateBody() function."""
    if not isinstance(body, dict):
        return {"valid": False, "error": "Request body must be a JSON object"}

    action = body.get("action")
    article_ids = body.get("article_ids")
    question = body.get("question")

    if not action or action not in VALID_ACTIONS:
        return {
            "valid": False,
            "error": f'"action" must be one of: {", ".join(sorted(VALID_ACTIONS))}',
        }

    if not isinstance(article_ids, list) or len(article_ids) == 0:
        return {"valid": False, "error": '"article_ids" must be a non-empty array'}

    if len(article_ids) > 20:
        return {"valid": False, "error": '"article_ids" must contain at most 20 items'}

    if action == "research" and question is not None and not isinstance(question, str):
        return {"valid": False, "error": '"question" must be a string'}

    return {"valid": True}


def resolve_articles(article_ids: list, articles_by_id: dict) -> list:
    """Python mirror of the Worker's resolveArticles() function."""
    return [articles_by_id[str(i)] for i in article_ids if str(i) in articles_by_id]


def build_prompt(action: str, articles: list, question: str | None = None) -> str:
    """Python mirror of the Worker's buildPrompt() function."""
    article_context = "\n\n".join(
        "\n".join(
            filter(
                None,
                [
                    f"[{i + 1}] {a['title']}",
                    f"Source: {a['source']}" if a.get("source") else None,
                    f"Summary: {a['summary']}" if a.get("summary") else None,
                ],
            )
        )
        for i, a in enumerate(articles)
    )

    if action == "elaborate":
        return (
            "You are a knowledgeable analyst. Based on the following news articles, explain "
            "the broader significance and importance of this topic. Why does it matter? "
            "What are the key implications?\n\n"
            f"Articles:\n{article_context}\n\n"
            "Provide a concise, insightful analysis in 2-3 paragraphs."
        )
    if action == "research":
        q = question or "What are the key details and context?"
        return (
            "You are a research assistant. Based on the following news articles, investigate "
            f"this specific question: {q}\n\n"
            f"Articles:\n{article_context}\n\n"
            "Answer the question thoroughly based on the articles provided. "
            "Note any gaps or limitations in the available information."
        )
    if action == "sources":
        return (
            "You are a research librarian. Based on the following news articles, extract "
            "and list relevant citations, sources, and further reading suggestions. "
            "Include any organizations, reports, studies, or publications mentioned.\n\n"
            f"Articles:\n{article_context}\n\n"
            "Provide a structured list of:\n"
            "1. Sources cited in or referenced by these articles\n"
            "2. Key organizations or institutions mentioned\n"
            "3. Suggested further reading on this topic"
        )
    raise ValueError(f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# validate_body tests
# ---------------------------------------------------------------------------


def test_valid_elaborate_body():
    body = {"action": "elaborate", "article_ids": [1, 2, 3]}
    result = validate_body(body)
    assert result["valid"] is True


def test_valid_research_body_with_question():
    body = {"action": "research", "article_ids": [5], "question": "Why does this matter?"}
    result = validate_body(body)
    assert result["valid"] is True


def test_valid_research_body_without_question():
    body = {"action": "research", "article_ids": [5]}
    result = validate_body(body)
    assert result["valid"] is True


def test_valid_sources_body():
    body = {"action": "sources", "article_ids": [1]}
    result = validate_body(body)
    assert result["valid"] is True


def test_invalid_action_rejects():
    body = {"action": "summarize", "article_ids": [1]}
    result = validate_body(body)
    assert result["valid"] is False
    assert "action" in result["error"]


def test_missing_action_rejects():
    body = {"article_ids": [1]}
    result = validate_body(body)
    assert result["valid"] is False


def test_empty_article_ids_rejects():
    body = {"action": "elaborate", "article_ids": []}
    result = validate_body(body)
    assert result["valid"] is False
    assert "article_ids" in result["error"]


def test_non_list_article_ids_rejects():
    body = {"action": "elaborate", "article_ids": 42}
    result = validate_body(body)
    assert result["valid"] is False


def test_too_many_article_ids_rejects():
    body = {"action": "elaborate", "article_ids": list(range(21))}
    result = validate_body(body)
    assert result["valid"] is False
    assert "20" in result["error"]


def test_exactly_20_article_ids_passes():
    body = {"action": "elaborate", "article_ids": list(range(20))}
    result = validate_body(body)
    assert result["valid"] is True


def test_research_non_string_question_rejects():
    body = {"action": "research", "article_ids": [1], "question": 123}
    result = validate_body(body)
    assert result["valid"] is False
    assert "question" in result["error"]


def test_non_dict_body_rejects():
    for bad in [None, "string", 42, [1, 2]]:
        result = validate_body(bad)
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# resolve_articles tests
# ---------------------------------------------------------------------------

SAMPLE_ARTICLES_BY_ID = {
    "1": {"title": "Article One", "source": "BBC", "summary": "Summary one."},
    "3": {"title": "Article Three", "source": "AP", "summary": "Summary three."},
}


def test_resolve_articles_returns_matching():
    result = resolve_articles([1, 3], SAMPLE_ARTICLES_BY_ID)
    assert len(result) == 2
    assert result[0]["title"] == "Article One"
    assert result[1]["title"] == "Article Three"


def test_resolve_articles_skips_missing_ids():
    result = resolve_articles([1, 2, 3], SAMPLE_ARTICLES_BY_ID)
    # ID 2 is not in the map
    assert len(result) == 2
    titles = {a["title"] for a in result}
    assert "Article One" in titles
    assert "Article Three" in titles


def test_resolve_articles_all_missing_returns_empty():
    result = resolve_articles([99, 100], SAMPLE_ARTICLES_BY_ID)
    assert result == []


def test_resolve_articles_string_ids_match():
    # The worker casts IDs to strings for lookup
    result = resolve_articles(["1"], SAMPLE_ARTICLES_BY_ID)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# build_prompt tests
# ---------------------------------------------------------------------------

SAMPLE_ARTICLES = [
    {"title": "Test Article", "source": "Reuters", "summary": "A brief summary."},
]


def test_elaborate_prompt_contains_article_title():
    prompt = build_prompt("elaborate", SAMPLE_ARTICLES)
    assert "Test Article" in prompt
    assert "Reuters" in prompt
    assert "A brief summary." in prompt


def test_elaborate_prompt_includes_analyst_role():
    prompt = build_prompt("elaborate", SAMPLE_ARTICLES)
    assert "analyst" in prompt.lower()


def test_research_prompt_includes_question():
    prompt = build_prompt("research", SAMPLE_ARTICLES, question="What caused this?")
    assert "What caused this?" in prompt


def test_research_prompt_uses_default_question_when_none():
    prompt = build_prompt("research", SAMPLE_ARTICLES, question=None)
    assert "key details" in prompt.lower() or "context" in prompt.lower()


def test_sources_prompt_includes_librarian_role():
    prompt = build_prompt("sources", SAMPLE_ARTICLES)
    assert "librarian" in prompt.lower() or "citations" in prompt.lower()


def test_sources_prompt_lists_expected_categories():
    prompt = build_prompt("sources", SAMPLE_ARTICLES)
    assert "further reading" in prompt.lower()
    assert "organizations" in prompt.lower()


def test_build_prompt_unknown_action_raises():
    with pytest.raises(ValueError, match="Unknown action"):
        build_prompt("invalid_action", SAMPLE_ARTICLES)


def test_prompt_article_numbering():
    articles = [
        {"title": "First", "source": "S1", "summary": "Sum1"},
        {"title": "Second", "source": "S2", "summary": "Sum2"},
    ]
    prompt = build_prompt("elaborate", articles)
    assert "[1] First" in prompt
    assert "[2] Second" in prompt


def test_prompt_handles_missing_optional_fields():
    # Articles without summary or source should not crash
    articles = [{"title": "Bare Article"}]
    prompt = build_prompt("elaborate", articles)
    assert "Bare Article" in prompt


# ---------------------------------------------------------------------------
# SSE response format tests
# ---------------------------------------------------------------------------


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse a sequence of SSE data lines into event dicts."""
    events = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def test_sse_text_event_format():
    raw = 'data: {"type": "text", "text": "Hello world"}\n\n'
    events = _parse_sse_events(raw)
    assert len(events) == 1
    assert events[0]["type"] == "text"
    assert events[0]["text"] == "Hello world"


def test_sse_done_event_format():
    raw = 'data: {"type": "done"}\n\n'
    events = _parse_sse_events(raw)
    assert events[0]["type"] == "done"


def test_sse_error_event_format():
    raw = 'data: {"type": "error", "error": "API unavailable"}\n\n'
    events = _parse_sse_events(raw)
    assert events[0]["type"] == "error"
    assert "API unavailable" in events[0]["error"]


def test_sse_multiple_text_events_reconstruct():
    chunks = ["Hello", " ", "world", "!"]
    raw = "".join(f"data: {json.dumps({'type': 'text', 'text': c})}\n\n" for c in chunks)
    events = _parse_sse_events(raw)
    full_text = "".join(e["text"] for e in events if e["type"] == "text")
    assert full_text == "Hello world!"


# ---------------------------------------------------------------------------
# Rate limit logic tests
# ---------------------------------------------------------------------------


def test_rate_limit_allows_up_to_max():
    """Simulate the rate-limit counter allowing up to 10 requests."""

    class FakeStore:
        def __init__(self):
            self._data = {}

        def get(self, key):
            return self._data.get(key)

        def set(self, key, value):
            self._data[key] = value

    store = FakeStore()
    max_requests = 10
    window_ms = 3_600_000  # 1 hour

    def check(ip, now_ms):
        entry = store.get(ip)
        if not entry or now_ms - entry["windowStart"] > window_ms:
            store.set(ip, {"count": 1, "windowStart": now_ms})
            return {"allowed": True, "remaining": max_requests - 1}
        if entry["count"] >= max_requests:
            return {"allowed": False}
        entry["count"] += 1
        return {"allowed": True, "remaining": max_requests - entry["count"]}

    now = 1_000_000
    ip = "192.168.1.1"

    for i in range(max_requests):
        result = check(ip, now)
        assert result["allowed"] is True, f"Request {i + 1} should be allowed"

    result = check(ip, now)
    assert result["allowed"] is False


def test_rate_limit_resets_after_window():
    """Counter resets when the window has passed."""
    store = {}
    max_requests = 10
    window_ms = 3_600_000

    def check(ip, now_ms):
        entry = store.get(ip)
        if not entry or now_ms - entry["windowStart"] > window_ms:
            store[ip] = {"count": 1, "windowStart": now_ms}
            return {"allowed": True}
        if entry["count"] >= max_requests:
            return {"allowed": False}
        entry["count"] += 1
        return {"allowed": True}

    ip = "10.0.0.1"
    now = 0

    for _ in range(max_requests):
        check(ip, now)

    # Should be blocked
    assert check(ip, now)["allowed"] is False

    # After window passes, should be allowed again
    assert check(ip, now + window_ms + 1)["allowed"] is True


def test_rate_limit_different_ips_tracked_independently():
    store = {}
    max_requests = 10
    window_ms = 3_600_000

    def check(ip, now_ms):
        entry = store.get(ip)
        if not entry or now_ms - entry["windowStart"] > window_ms:
            store[ip] = {"count": 1, "windowStart": now_ms}
            return {"allowed": True}
        if entry["count"] >= max_requests:
            return {"allowed": False}
        entry["count"] += 1
        return {"allowed": True}

    now = 0
    ip_a = "1.2.3.4"
    ip_b = "5.6.7.8"

    for _ in range(max_requests):
        check(ip_a, now)

    assert check(ip_a, now)["allowed"] is False
    assert check(ip_b, now)["allowed"] is True
