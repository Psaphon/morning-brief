"""Tests verifying that render_dashboard emits valid HMAC tokens and article signatures.

These tests confirm that the pipeline-side signing logic in src/publishers/html.py
produces output that is verifiable against the same algorithms used in the Worker's
auth.js helpers.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from src.publishers.html import compute_article_sig, compute_brief_token, render_dashboard

# ---------------------------------------------------------------------------
# Helpers (mirrors of the Worker verification logic)
# ---------------------------------------------------------------------------

TOKEN_MAX_AGE_S = 48 * 60 * 60


def _verify_brief_token(token: str, hmac_key: str) -> dict:
    if not hmac_key:
        return {"valid": True}
    if not token:
        return {"valid": False, "reason": "Missing token"}
    try:
        colon_idx = token.index(":")
    except ValueError:
        return {"valid": False, "reason": "Malformed token"}

    timestamp_str = token[:colon_idx]
    provided_hmac = token[colon_idx + 1 :]
    try:
        timestamp_s = int(timestamp_str)
    except ValueError:
        return {"valid": False, "reason": "Bad timestamp"}

    age_s = time.time() - timestamp_s
    if age_s > TOKEN_MAX_AGE_S:
        return {"valid": False, "reason": "Expired"}

    brief_id = datetime.fromtimestamp(timestamp_s, tz=timezone.utc).strftime("%Y-%m-%d")
    message = f"{brief_id}:{timestamp_str}"
    expected = _hmac.new(hmac_key.encode(), message.encode(), hashlib.sha256).hexdigest()
    if provided_hmac != expected:
        return {"valid": False, "reason": "Bad signature"}
    return {"valid": True}


def _verify_article_sig(article: dict, hmac_key: str) -> dict:
    if not hmac_key:
        return {"valid": True}
    sig = article.get("sig")
    if not sig:
        return {"valid": False, "reason": "Missing sig"}
    canonical = json.dumps(
        {
            "id": article["id"],
            "title": article.get("title", ""),
            "source": article.get("source", ""),
            "summary": article.get("summary", ""),
        },
        separators=(",", ":"),
    )
    expected = _hmac.new(hmac_key.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if sig != expected:
        return {"valid": False, "reason": "Mismatch"}
    return {"valid": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HMAC_KEY = "a1b2c3d4" * 8  # 64-hex-char test key

SAMPLE_ARTICLES = [
    {
        "id": 1,
        "title": "Pipeline test article",
        "url": "https://example.com/1",
        "source": "Test Source",
        "summary": "A summary of the test article.",
        "category": "World News",
        "score": 0.9,
    },
    {
        "id": 2,
        "title": "Second article",
        "url": "https://example.com/2",
        "source": "Another Source",
        "summary": "Another summary.",
        "category": "World News",
        "score": 0.7,
    },
]


def _render(hmac_key: str = "", brief_id: str = "2026-05-17") -> str:
    return render_dashboard(
        articles=SAMPLE_ARTICLES,
        market_data=[],
        health_checks=[],
        artworks=[],
        briefing=None,
        briefing_segments=None,
        template_dir=Path("templates"),
        hmac_key=hmac_key,
        brief_id=brief_id,
    )


# ---------------------------------------------------------------------------
# compute_brief_token unit tests
# ---------------------------------------------------------------------------


def test_compute_brief_token_format():
    token = compute_brief_token(HMAC_KEY, "2026-05-17")
    parts = token.split(":")
    assert len(parts) == 2
    assert parts[0].isdigit()
    assert len(parts[1]) == 64  # SHA-256 hex digest


def test_compute_brief_token_is_verifiable():
    brief_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    token = compute_brief_token(HMAC_KEY, brief_id)
    result = _verify_brief_token(token, HMAC_KEY)
    assert result["valid"] is True


def test_compute_brief_token_wrong_key_fails():
    brief_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    token = compute_brief_token(HMAC_KEY, brief_id)
    result = _verify_brief_token(token, "wrongkey" * 8)
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# compute_article_sig unit tests
# ---------------------------------------------------------------------------


def test_compute_article_sig_format():
    sig = compute_article_sig(HMAC_KEY, 1, "Title", "Source", "Summary")
    assert len(sig) == 64  # SHA-256 hex digest
    assert all(c in "0123456789abcdef" for c in sig)


def test_compute_article_sig_is_verifiable():
    article = {"id": 1, "title": "Title", "source": "Reuters", "summary": "A summary."}
    sig = compute_article_sig(
        HMAC_KEY, article["id"], article["title"], article["source"], article["summary"]
    )
    article["sig"] = sig
    result = _verify_article_sig(article, HMAC_KEY)
    assert result["valid"] is True


def test_compute_article_sig_tampered_summary_fails():
    article = {"id": 1, "title": "Title", "source": "Reuters", "summary": "Real summary."}
    sig = compute_article_sig(
        HMAC_KEY, article["id"], article["title"], article["source"], article["summary"]
    )
    article["sig"] = sig
    article["summary"] = "Tampered"
    result = _verify_article_sig(article, HMAC_KEY)
    assert result["valid"] is False


def test_compute_article_sig_empty_fields():
    # Empty optional fields should not raise
    sig = compute_article_sig(HMAC_KEY, 99, "", "", "")
    assert len(sig) == 64


# ---------------------------------------------------------------------------
# render_dashboard integration: token in HTML meta tag
# ---------------------------------------------------------------------------


def test_render_dashboard_emits_token_meta_tag():
    html = _render(hmac_key=HMAC_KEY, brief_id="2026-05-17")
    assert 'name="dashboard-token"' in html


def test_render_dashboard_token_is_valid():
    brief_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html = _render(hmac_key=HMAC_KEY, brief_id=brief_id)
    match = re.search(r'name="dashboard-token"\s+content="([^"]+)"', html)
    assert match, "dashboard-token meta tag not found"
    token = match.group(1)
    result = _verify_brief_token(token, HMAC_KEY)
    assert result["valid"] is True, result.get("reason")


def test_render_dashboard_no_key_empty_token():
    html = _render(hmac_key="", brief_id="2026-05-17")
    match = re.search(r'name="dashboard-token"\s+content="([^"]*)"', html)
    assert match
    assert match.group(1) == ""


# ---------------------------------------------------------------------------
# render_dashboard integration: article sigs in articles-data JSON
# ---------------------------------------------------------------------------


def _extract_articles_by_id(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="articles-data">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "articles-data script block not found"
    return json.loads(match.group(1))


def test_render_dashboard_articles_have_sigs():
    html = _render(hmac_key=HMAC_KEY, brief_id="2026-05-17")
    articles_by_id = _extract_articles_by_id(html)
    assert len(articles_by_id) > 0
    for art in articles_by_id.values():
        assert "sig" in art, f"Article {art.get('id')} missing sig"


def test_render_dashboard_article_sigs_are_valid():
    html = _render(hmac_key=HMAC_KEY, brief_id="2026-05-17")
    articles_by_id = _extract_articles_by_id(html)
    for art in articles_by_id.values():
        result = _verify_article_sig(art, HMAC_KEY)
        assert result["valid"] is True, f"Article {art.get('id')}: {result.get('reason')}"


def test_render_dashboard_no_key_no_sigs():
    html = _render(hmac_key="", brief_id="2026-05-17")
    articles_by_id = _extract_articles_by_id(html)
    for art in articles_by_id.values():
        assert "sig" not in art


def test_render_dashboard_articles_have_id_field():
    html = _render(hmac_key=HMAC_KEY, brief_id="2026-05-17")
    articles_by_id = _extract_articles_by_id(html)
    for key, art in articles_by_id.items():
        assert "id" in art
        assert str(art["id"]) == key
