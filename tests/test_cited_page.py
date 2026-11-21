"""Cited summary page: sections with superscript citations and sources."""

import json
import re

import html5lib

from src.publishers.html import _script_json, build_cited_sections, render_dashboard

HOSTILE = "</script><img src=x onerror=alert(1)>"


def _articles():
    return [
        {
            "id": i,
            "title": f"Article {i}",
            "source": f"Source {i}",
            "url": f"https://example.com/a{i}",
            "summary": f"What happened {i}.",
            "category": "World News",
        }
        for i in range(1, 6)
    ]


def _by_id(articles):
    return {
        str(a["id"]): {k: a[k] for k in ("id", "title", "source", "url", "summary")}
        for a in articles
    }


def _segments():
    return [
        {
            "topic": "Lead",
            "text": "Plain one. Plain two.",
            "cited_text": "Plain one.[[3]] Plain two.[[1,3]]\n\nNext para.[[2,99]] Ghost.[[98]]",
            "source_article_ids": [3, 1, 2],
            "writer": "claude",
        },
        {
            "topic": "Second",
            "text": "Other.",
            "cited_text": "Other.[[5,4]]",
            "source_article_ids": [5, 4],
            "writer": "local fallback",
        },
    ]


def _render(articles=None, segments=None):
    return render_dashboard(
        articles=articles or _articles(),
        market_data=[],
        health_checks=[],
        briefing="plain",
        briefing_segments=segments if segments is not None else _segments(),
    )


def test_cites_map_to_sources_in_first_cited_order():
    sections = build_cited_sections(_segments(), _by_id(_articles()))
    assert len(sections) == 2
    first = sections[0]
    assert [s["id"] for s in first["sources"]] == [3, 1, 2]
    assert [s["n"] for s in first["sources"]] == [1, 2, 3]
    by_n = {s["n"]: s for s in first["sources"]}
    cited = [n for para in first["paragraphs"] for p in para for n in p.get("cite", [])]
    assert cited == [1, 2, 1, 3]
    assert set(cited) == set(by_n)
    assert by_n[1]["url"] == "https://example.com/a3"
    assert by_n[1]["what_happened"] == "What happened 3."
    assert [s["id"] for s in sections[1]["sources"]] == [5, 4]
    assert sections[1]["writer"] == "local fallback"


def test_unknown_id_yields_no_cite():
    first = build_cited_sections(_segments(), _by_id(_articles()))[0]
    para2 = first["paragraphs"][1]
    assert para2[1] == {"cite": [3]}
    assert all(p != {"cite": []} for para in first["paragraphs"] for p in para)
    assert not any("99" in str(s["id"]) or "98" in str(s["id"]) for s in first["sources"])


def test_rendered_html():
    html = _render()
    # No citation marker survives (nested JSON arrays like "[[{" are not markers).
    assert not re.search(r"\[\[[\w, ]*\]\]", html)
    assert "[[3" not in html
    assert "local fallback" in html  # in the JSON blob
    cited = json.loads(re.search(r'id="cited-data">(.*?)</script>', html, re.DOTALL).group(1))
    assert [c["writer"] for c in cited].count("local fallback") == 1
    assert html.count("Sources (") == 1  # JS template literal; DOM built client-side
    assert "createElement('details')" in html


def test_old_shape_segments_unchanged():
    old = [{"topic": "T", "text": "Body.", "source_article_ids": [1]}]
    html = _render(segments=old)
    seg = json.loads(re.search(r'id="segment-data">(.*?)</script>', html, re.DOTALL).group(1))
    assert seg == old
    cited = json.loads(re.search(r'id="cited-data">(.*?)</script>', html, re.DOTALL).group(1))
    assert cited == []


def test_hostile_title_cannot_close_script():
    arts = _articles()
    arts[0]["title"] = HOSTILE
    html = _render(articles=arts)
    assert "</script><img" not in html
    assert "<img src=x" not in html
    m = re.search(r'id="articles-data">(.*?)</script>', html, re.DOTALL)
    assert json.loads(m.group(1))["1"]["title"] == HOSTILE
    m = re.search(r'id="cited-data">(.*?)</script>', html, re.DOTALL)
    assert HOSTILE in [s["title"] for sec in json.loads(m.group(1)) for s in sec["sources"]]
    assert json.loads(_script_json({"a": "<&>"})) == {"a": "<&>"}


def test_page_is_valid_html5():
    html = _render()
    parser = html5lib.HTMLParser(strict=True)
    parser.parse(html)
    assert not parser.errors
