"""Tests for interest-filter: loading, scoring, gating, ordering and labels."""

import logging
from pathlib import Path

from src.processors.interest import (
    ENV_VAR,
    interest_labels,
    interest_score,
    load_interests,
)
from src.publishers.html import render_dashboard
from src.summarizers.local import select_balanced_articles

TEMPLATES = Path(__file__).parent.parent / "templates"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _art(i, title, category="Tech", source="Feed", text="", score=0.0):
    return {
        "id": i,
        "title": title,
        "category": category,
        "source": source,
        "full_text": text,
        "score": score,
        "url": f"https://example.com/{i}",
    }


def test_default_path_used_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert not load_interests()
    cfg = tmp_path / ".config" / "morning-brief"
    cfg.mkdir(parents=True)
    _write(cfg / "interests.toml", "[keywords]\nrust = 2\n")
    assert load_interests().keywords == {"rust": 2.0}


def test_score_sums_weights_case_insensitive_word_boundary(tmp_path):
    f = _write(tmp_path / "i.toml", '[keywords]\nrust = 3\n"data pipeline" = 2\n')
    interests = load_interests(str(f))
    art = _art(1, "RUST release", text="A Data Pipeline guide")
    assert interest_score(art, interests) == 5
    assert interest_score(_art(2, "Trusted rusty tools"), interests) == 0


def test_score_only_first_2000_chars(tmp_path):
    interests = load_interests(str(_write(tmp_path / "i.toml", "[keywords]\nrust = 3\n")))
    assert interest_score(_art(1, "x", text="a" * 1990 + " rust"), interests) == 3
    assert interest_score(_art(2, "x", text="a" * 2001 + " rust"), interests) == 0


def test_multiple_files_merge_highest_weight_wins(tmp_path):
    a = _write(tmp_path / "a.toml", "[keywords]\nrust = 2\nzig = 1\n")
    b = _write(tmp_path / "b.toml", "[keywords]\nrust = 4\n")
    interests = load_interests(f"{a}:{b}")
    assert interests.keywords == {"rust": 4.0, "zig": 1.0}
    assert interest_score(_art(1, "Rust"), interests) == 4


def test_missing_files_skipped_with_one_info_line_each(tmp_path, caplog):
    real = _write(tmp_path / "real.toml", "[keywords]\nrust = 1\n")
    m1, m2 = tmp_path / "nope1.toml", tmp_path / "nope2.toml"
    with caplog.at_level(logging.INFO, logger="src.processors.interest"):
        interests = load_interests(f"{m1}:{real}:{m2}")
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 2
    assert str(m1) in infos[0].getMessage() and str(m2) in infos[1].getMessage()
    assert interests.keywords == {"rust": 1.0}


def test_bad_toml_does_not_crash(tmp_path):
    f = _write(tmp_path / "bad.toml", "[keywords\n")
    assert not load_interests(str(f))


def test_feed_min_interest_excludes_and_includes(tmp_path):
    f = _write(
        tmp_path / "i.toml",
        '[keywords]\nrust = 3\n\n[feeds."Preprints"]\nmin_interest = 2\n',
    )
    interests = load_interests(str(f))
    articles = [
        _art(1, "Unrelated paper", source="Preprints"),
        _art(2, "Rust compilers", source="Preprints"),
        _art(3, "Unrelated news", source="Other Feed"),
    ]
    ids = {a["id"] for a in select_balanced_articles(articles, interests=interests)}
    assert ids == {2, 3}


def test_ordering_by_interest_then_existing_order(tmp_path):
    interests = load_interests(str(_write(tmp_path / "i.toml", "[keywords]\nrust = 3\nzig = 1\n")))
    articles = [
        _art(1, "plain a"),
        _art(2, "zig news"),
        _art(3, "plain b"),
        _art(4, "rust news"),
    ]
    out = select_balanced_articles(articles, interests=interests)
    assert [a["id"] for a in out] == [4, 2, 1, 3]
    # interest decides who survives the per-category cap
    out = select_balanced_articles(articles, per_category=2, interests=interests)
    assert [a["id"] for a in out] == [4, 2]


def test_no_interests_selection_identical_to_before(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "absent.toml"))
    articles = [
        _art(i, f"Title {i}", category=f"Cat{i % 3}", source="Preprints", score=float(i % 5))
        for i in range(40)
    ]
    interests = load_interests()
    assert not interests
    baseline = select_balanced_articles([dict(a) for a in articles], per_category=4, total=9)
    with_empty = select_balanced_articles(
        [dict(a) for a in articles], per_category=4, total=9, interests=interests
    )
    assert with_empty == baseline
    assert [a["id"] for a in with_empty] == [a["id"] for a in baseline]


def test_labels_only_tag_matching_bullets_in_page_json(tmp_path):
    f = _write(tmp_path / "i.toml", 'label = "On radar"\n[keywords]\nrust = 1\nzig = 1\n')
    g = _write(tmp_path / "g.toml", 'label = "On radar"\n[keywords]\nrust = 1\n')
    h = _write(tmp_path / "h.toml", "[keywords]\ngo = 1\n")  # unlabelled
    interests = load_interests(f"{f}:{g}:{h}")
    arts = [
        _art(1, "Rust and zig"),
        _art(2, "Nothing here"),
        _art(3, "go go go"),
    ]
    assert interest_labels(arts[0], interests) == ["On radar"]  # de-duplicated
    html = render_dashboard(arts, [], [], template_dir=TEMPLATES, interests=interests)
    import json
    import re

    data = json.loads(re.search(r'id="articles-data">(.*?)</script>', html, re.S).group(1))
    assert data["1"]["labels"] == ["On radar"]
    assert "labels" not in data["2"] and "labels" not in data["3"]
    assert html.count('<span class="interest-label">On radar</span>') == 1


def test_render_without_interests_has_no_labels():
    html = render_dashboard([_art(1, "Rust")], [], [], template_dir=TEMPLATES)
    assert '<span class="interest-label">' not in html
