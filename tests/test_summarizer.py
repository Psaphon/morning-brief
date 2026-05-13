"""Tests for the local LLM summarizer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import OllamaConfig
from src.summarizers.local import (
    SUMMARY_PROMPT,
    BatchMetrics,
    SummaryResult,
    check_ollama_available,
    select_balanced_articles,
    summarize_articles,
    summarize_single,
    truncate_text,
)

# --- select_balanced_articles ---


def _make_candidate(article_id: int, category: str, score: float = 0.5) -> dict:
    return {
        "id": article_id,
        "title": f"Article {article_id}",
        "source": "Test Source",
        "category": category,
        "full_text": "A" * 200,
        "score": score,
    }


def test_select_balanced_empty():
    """Empty input returns empty output."""
    assert select_balanced_articles([]) == []


def test_select_balanced_single_category():
    """Respects per_category cap within a single category."""
    articles = [_make_candidate(i, "Politics", score=float(10 - i)) for i in range(10)]
    result = select_balanced_articles(articles, per_category=3, total=50)
    assert len(result) == 3
    # Highest scores retained
    assert result[0]["id"] == 0
    assert result[1]["id"] == 1
    assert result[2]["id"] == 2


def test_select_balanced_multiple_categories():
    """Each category gets up to per_category slots."""
    articles = (
        [_make_candidate(i, "Politics", score=float(10 - i)) for i in range(5)]
        + [_make_candidate(i + 10, "Crypto", score=float(10 - i)) for i in range(5)]
        + [_make_candidate(i + 20, "Tech", score=float(10 - i)) for i in range(5)]
    )
    result = select_balanced_articles(articles, per_category=3, total=50)
    assert len(result) == 9  # 3 per category × 3 categories

    categories = [a["category"] for a in result]
    assert categories.count("Politics") == 3
    assert categories.count("Crypto") == 3
    assert categories.count("Tech") == 3


def test_select_balanced_total_cap():
    """Total cap is enforced even when per_category would allow more."""
    articles = (
        [_make_candidate(i, "Politics", score=float(10 - i)) for i in range(5)]
        + [_make_candidate(i + 10, "Crypto", score=float(10 - i)) for i in range(5)]
        + [_make_candidate(i + 20, "Tech", score=float(10 - i)) for i in range(5)]
    )
    result = select_balanced_articles(articles, per_category=5, total=7)
    assert len(result) == 7


def test_select_balanced_sorted_by_score():
    """Result is sorted by score descending."""
    articles = [
        _make_candidate(1, "Politics", score=0.3),
        _make_candidate(2, "Crypto", score=0.9),
        _make_candidate(3, "Tech", score=0.6),
    ]
    result = select_balanced_articles(articles, per_category=1, total=10)
    scores = [a["score"] for a in result]
    assert scores == sorted(scores, reverse=True)


def test_select_balanced_none_score_ranked_last():
    """Articles with None score are treated as 0 and ranked last."""
    articles = [
        _make_candidate(1, "Politics", score=0.8),
        {**_make_candidate(2, "Crypto"), "score": None},
        _make_candidate(3, "Tech", score=0.4),
    ]
    result = select_balanced_articles(articles, per_category=1, total=10)
    assert result[0]["id"] == 1  # score 0.8
    assert result[-1]["id"] == 2  # score None → 0


def test_select_balanced_default_params():
    """Default params yield at most 50 articles, 8 per category."""
    articles = [_make_candidate(i, f"Cat{i % 10}", score=float(i)) for i in range(200)]
    result = select_balanced_articles(articles)
    assert len(result) <= 50
    # No category should have more than 8
    from collections import Counter

    counts = Counter(a["category"] for a in result)
    assert all(v <= 8 for v in counts.values())


# --- truncate_text ---


def test_truncate_short_text():
    """Short text passes through unchanged."""
    text = "This is a short article."
    assert truncate_text(text) == text


def test_truncate_long_text_at_sentence():
    """Long text truncates at sentence boundary."""
    sentences = "Hello world. " * 2000  # ~26K chars
    result = truncate_text(sentences, max_chars=100)
    assert len(result) <= 110  # Some slack for boundary
    assert ". " in result or result.endswith(".")


def test_truncate_no_sentence_boundary():
    """Text with no sentence boundary gets hard-cut with ellipsis."""
    text = "x" * 20000
    result = truncate_text(text, max_chars=100)
    assert len(result) == 103  # 100 + "..."
    assert result.endswith("...")


def test_truncate_exact_limit():
    """Text at exactly the limit is not truncated."""
    text = "a" * 12000
    assert truncate_text(text, max_chars=12000) == text


# --- SUMMARY_PROMPT ---


def test_prompt_template_has_placeholders():
    """Prompt template has all required placeholders."""
    assert "{title}" in SUMMARY_PROMPT
    assert "{source}" in SUMMARY_PROMPT
    assert "{category}" in SUMMARY_PROMPT
    assert "{text}" in SUMMARY_PROMPT


def test_prompt_template_formats():
    """Prompt template formats without error."""
    result = SUMMARY_PROMPT.format(
        title="Test Article",
        source="Test Source",
        category="Testing",
        text="Some article text here.",
    )
    assert "Test Article" in result
    assert "Test Source" in result


# --- check_ollama_available ---


@pytest.fixture
def ollama_config():
    return OllamaConfig(host="http://localhost:11434", model="qwen2.5:7b-instruct-q4_K_M")


@pytest.mark.asyncio
async def test_check_ollama_model_available(ollama_config):
    """Returns True when model is available."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "models": [
            {"name": "qwen2.5:7b-instruct-q4_K_M"},
            {"name": "llama3:latest"},
        ]
    }
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    result = await check_ollama_available(mock_client, ollama_config)
    assert result is True


@pytest.mark.asyncio
async def test_check_ollama_model_missing(ollama_config):
    """Returns False when model is not in the list."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"models": [{"name": "llama3:latest"}]}
    mock_resp.raise_for_status = lambda: None
    mock_client.get = AsyncMock(return_value=mock_resp)

    result = await check_ollama_available(mock_client, ollama_config)
    assert result is False


@pytest.mark.asyncio
async def test_check_ollama_unreachable(ollama_config):
    """Returns False when Ollama server is down."""
    import httpx

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    result = await check_ollama_available(mock_client, ollama_config)
    assert result is False


# --- summarize_single ---


def _make_article(article_id=1, full_text="A" * 200):
    return {
        "id": article_id,
        "title": "Test Article",
        "source": "Test Source",
        "category": "Testing",
        "full_text": full_text,
    }


@pytest.mark.asyncio
async def test_summarize_single_success(ollama_config):
    """Successful summarization returns a SummaryResult."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": "This is a concise summary of the article."}
    mock_resp.raise_for_status = lambda: None
    mock_client.post = AsyncMock(return_value=mock_resp)

    result = await summarize_single(mock_client, ollama_config, _make_article())
    assert result.success is True
    assert result.summary == "This is a concise summary of the article."
    assert result.model == ollama_config.model
    assert result.elapsed_seconds >= 0


@pytest.mark.asyncio
async def test_summarize_single_short_text(ollama_config):
    """Articles with too little text are skipped."""
    mock_client = AsyncMock()
    article = _make_article(full_text="Short.")

    result = await summarize_single(mock_client, ollama_config, article)
    assert result.success is False
    assert "too short" in result.error
    # Should not have made any HTTP calls
    mock_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_single_empty_response(ollama_config):
    """Empty model response is treated as failure."""
    mock_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"response": ""}
    mock_resp.raise_for_status = lambda: None
    mock_client.post = AsyncMock(return_value=mock_resp)

    result = await summarize_single(mock_client, ollama_config, _make_article())
    assert result.success is False
    assert "Empty response" in result.error


@pytest.mark.asyncio
async def test_summarize_single_timeout_retries(ollama_config):
    """Timeouts are retried up to MAX_RETRIES times."""
    import httpx

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

    with patch("src.summarizers.local._async_sleep", new_callable=AsyncMock):
        result = await summarize_single(mock_client, ollama_config, _make_article())

    assert result.success is False
    assert "Timeout" in result.error
    # Initial call + MAX_RETRIES retries
    assert mock_client.post.call_count == 3


@pytest.mark.asyncio
async def test_summarize_single_http_error_retries(ollama_config):
    """HTTP errors are retried."""
    import httpx

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "http://localhost"),
            response=httpx.Response(500),
        )
    )

    with patch("src.summarizers.local._async_sleep", new_callable=AsyncMock):
        result = await summarize_single(mock_client, ollama_config, _make_article())

    assert result.success is False
    assert mock_client.post.call_count == 3


# --- summarize_articles ---


@pytest.mark.asyncio
async def test_summarize_articles_empty_list(ollama_config):
    """Empty article list returns empty results and zero metrics."""
    results, metrics = await summarize_articles(ollama_config, [])
    assert results == []
    assert isinstance(metrics, BatchMetrics)
    assert metrics.total_articles == 0
    assert metrics.succeeded == 0
    assert metrics.failed == 0
    assert metrics.total_seconds == 0.0
    assert metrics.articles_per_minute == 0.0


@pytest.mark.asyncio
async def test_summarize_articles_ollama_unavailable(ollama_config):
    """Gracefully skips if Ollama is not running."""

    target = "src.summarizers.local.check_ollama_available"
    with patch(target, new_callable=AsyncMock) as mock_check:
        mock_check.return_value = False
        results, metrics = await summarize_articles(ollama_config, [_make_article()])

    assert results == []
    assert metrics.total_articles == 1
    assert metrics.succeeded == 0


@pytest.mark.asyncio
async def test_summarize_articles_batch_processing(ollama_config):
    """Processes multiple articles and returns results for all."""
    articles = [_make_article(article_id=i) for i in range(7)]

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local.summarize_single", new_callable=AsyncMock) as mock_single,
        patch("src.summarizers.local._async_sleep", new_callable=AsyncMock),
    ):
        mock_check.return_value = True
        mock_single.return_value = SummaryResult(
            article_id=0,
            summary="A test summary.",
            model=ollama_config.model,
            elapsed_seconds=1.0,
            success=True,
        )

        results, metrics = await summarize_articles(ollama_config, articles)

    assert len(results) == 7
    assert all(r.success for r in results)
    # Should have been called once per article
    assert mock_single.call_count == 7


@pytest.mark.asyncio
async def test_summarize_articles_mixed_results(ollama_config):
    """Handles a mix of successes and failures."""
    articles = [_make_article(article_id=i) for i in range(3)]
    call_count = 0

    async def mock_summarize(client, config, article):
        nonlocal call_count
        call_count += 1
        if article["id"] == 1:
            return SummaryResult(
                article_id=1,
                summary="",
                model=config.model,
                elapsed_seconds=0.5,
                success=False,
                error="Timeout",
            )
        return SummaryResult(
            article_id=article["id"],
            summary="Good summary.",
            model=config.model,
            elapsed_seconds=1.0,
            success=True,
        )

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local.summarize_single", side_effect=mock_summarize),
        patch("src.summarizers.local._async_sleep", new_callable=AsyncMock),
    ):
        mock_check.return_value = True
        results, metrics = await summarize_articles(ollama_config, articles)

    assert len(results) == 3
    assert sum(1 for r in results if r.success) == 2
    assert sum(1 for r in results if not r.success) == 1


# --- BatchMetrics ---


@pytest.mark.asyncio
async def test_batch_metrics_fields(ollama_config):
    """BatchMetrics has correct counts and timing fields."""
    articles = [_make_article(article_id=i) for i in range(4)]

    async def mock_summarize(client, config, article):
        success = article["id"] % 2 == 0  # even IDs succeed
        return SummaryResult(
            article_id=article["id"],
            summary="Summary." if success else "",
            model=config.model,
            elapsed_seconds=1.0,
            success=success,
            error=None if success else "Failed",
        )

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local.summarize_single", side_effect=mock_summarize),
        patch("src.summarizers.local._async_sleep", new_callable=AsyncMock),
    ):
        mock_check.return_value = True
        _, metrics = await summarize_articles(ollama_config, articles)

    assert metrics.total_articles == 4
    assert metrics.succeeded == 2
    assert metrics.failed == 2
    assert metrics.total_seconds >= 0.0
    assert isinstance(metrics.articles_per_minute, float)


@pytest.mark.asyncio
async def test_batch_metrics_articles_per_minute(ollama_config):
    """articles_per_minute is computed from succeeded count and elapsed time."""
    articles = [_make_article(article_id=i) for i in range(3)]

    with (
        patch("src.summarizers.local.check_ollama_available", new_callable=AsyncMock) as mock_check,
        patch("src.summarizers.local.summarize_single", new_callable=AsyncMock) as mock_single,
        patch("src.summarizers.local._async_sleep", new_callable=AsyncMock),
        patch("src.summarizers.local.time") as mock_time,
    ):
        mock_check.return_value = True
        mock_single.return_value = SummaryResult(
            article_id=0,
            summary="A summary.",
            model=ollama_config.model,
            elapsed_seconds=1.0,
            success=True,
        )
        # Simulate 30s elapsed → 3 succeeded → 6 articles/min
        mock_time.monotonic.side_effect = [0.0, 30.0]

        _, metrics = await summarize_articles(ollama_config, articles)

    assert metrics.total_seconds == pytest.approx(30.0)
    assert metrics.articles_per_minute == pytest.approx(6.0)  # 3 / 30s * 60
