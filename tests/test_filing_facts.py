"""filing-facts: extraction, verification against the release text, and code-computed direction.

Uses the HTTP-level Ollama stub; nothing internal is mocked.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

import src.processors.filing_facts as ff
from src.cli import cli
from src.config import Config, FilingFactsConfig, OllamaConfig, load_config
from src.db import Database
from src.processors import filing_direction as fd
from src.processors.filing_facts import (
    FILING_FACTS_SCHEMA,
    extract_filings,
    release_budget_chars,
    trim_release,
    verify_value,
)
from tests.stub_ollama import StubOllama

FIXTURES = Path(__file__).parent / "fixtures" / "filing_facts"
BASIC = (FIXTURES / "release_basic.txt").read_text()
TABLE = (FIXTURES / "release_table.txt").read_text()
BANNED = ["good", "bad", "positive", "negative", "beat", "miss", "sentiment", "stock price"]


def val(written: str | None, scale: str | None = "units") -> dict:
    return {"as_written": written, "scale": scale if written else None}


def basic_reply(**overrides) -> dict:
    reply = {
        "eps_diluted_current": val("$2.10"),
        "eps_diluted_prior": val("$1.95"),
        "revenue_current": val("$12.1 billion", "billions"),
        "revenue_prior": val("$11.0 billion", "billions"),
        "period_label": "third quarter fiscal 2026",
        "guidance": {
            "metric": "revenue",
            "period_label": "fourth quarter fiscal 2026",
            "low": val("$12.4 billion", "billions"),
            "high": val("$12.8 billion", "billions"),
        },
    }
    reply.update(overrides)
    return reply


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.connect()
    yield d
    d.close()


@pytest.fixture
def stub():
    with StubOllama() as s:
        yield s


def make_config(stub: StubOllama, **facts) -> Config:
    return Config(
        ollama=OllamaConfig(host=stub.url, model="gemma4:e4b"),
        filing_facts=FilingFactsConfig(**facts),
    )


_counter = iter(range(1, 10_000))


def add_filing(db, text=BASIC, *, ticker="AAA", accepted="2026-11-04T21:05:10Z", items=("2.02",)):
    accession = f"0000000001-26-{next(_counter):06d}"
    db.insert_filing(
        accession=accession,
        cik="0000000001",
        ticker=ticker,
        form="8-K",
        items=list(items),
        accepted_at=accepted,
        fetched_at="2026-11-22T12:00:00Z",
        primary_doc_url="http://x.test/p.htm",
        primary_html=b"<p>x</p>",
        exhibits=[],
        primary_text="See exhibit.",
        exhibit_text=text,
    )
    return accession


def facts_row(db, accession):
    return db.conn.execute(
        "SELECT * FROM filing_facts WHERE accession = ?", (accession,)
    ).fetchone()


# --- acceptance: extraction ---------------------------------------------------------


async def test_full_release_verifies_and_computes(db, stub):
    stub.set_filing_reply(basic_reply())
    acc = add_filing(db)
    summary = await extract_filings(make_config(stub), db)

    assert (summary.ok, summary.partial, summary.failed, summary.unverified) == (1, 0, 0, 0)
    row = facts_row(db, acc)
    assert row["status"] == "ok"
    assert row["eps_diluted_current"] == 2.10 and row["eps_diluted_prior"] == 1.95
    assert row["revenue_current"] == 12.1e9 and row["revenue_prior"] == 11.0e9
    assert (row["guidance_metric"], row["guidance_low"], row["guidance_high"]) == (
        "revenue",
        12.4e9,
        12.8e9,
    )
    assert row["period_label"] == "third quarter fiscal 2026"
    assert row["unverified_count"] == 0
    assert row["eps_yoy"] == pytest.approx(100 * 0.15 / 1.95)
    assert row["rev_yoy"] == pytest.approx(10.0)
    assert row["rev_accel"] is None and row["guidance_change"] is None  # no earlier rows
    assert row["direction_n"] == 2 and -1 <= row["direction"] <= 1
    assert row["model"] == "gemma4:e4b" and row["error"] is None


async def test_request_carries_context_temperature_seed_and_schema(db, stub):
    stub.set_filing_reply(basic_reply())
    add_filing(db)
    await extract_filings(make_config(stub, num_ctx=12000, num_predict=500, seed=7), db)

    (call,) = stub.generate_calls
    body = call["body"]
    assert body["options"] == {"num_ctx": 12000, "num_predict": 500, "temperature": 0, "seed": 7}
    assert body["format"] == FILING_FACTS_SCHEMA
    assert FILING_FACTS_SCHEMA["additionalProperties"] is False
    assert body["think"] is False and body["stream"] is False


async def test_defaults_from_environment(monkeypatch):
    for name in (
        "FILINGS_NUM_CTX",
        "FILINGS_NUM_PREDICT",
        "FILINGS_SEED",
        "GUIDANCE_TOLERANCE",
        "FILINGS_W_EPS_YOY",
        "FILINGS_K_REV_ACCEL",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config().filing_facts
    assert (cfg.num_ctx, cfg.num_predict, cfg.seed) == (16384, 768, 42)
    assert cfg.guidance_tolerance == 0.01
    assert cfg.weights() == {
        "eps_yoy": 0.3,
        "rev_yoy": 0.3,
        "rev_accel": 0.2,
        "guidance_change": 0.2,
    }
    assert cfg.scales() == {"eps_yoy": 25, "rev_yoy": 10, "rev_accel": 5, "guidance_change": 1}
    monkeypatch.setenv("FILINGS_SEED", "9")
    monkeypatch.setenv("FILINGS_K_REV_ACCEL", "2.5")
    cfg = load_config().filing_facts
    assert cfg.seed == 9 and cfg.k_rev_accel == 2.5


async def test_prompt_has_no_judgement_words_and_names_gaap_rule(db, stub):
    stub.set_filing_reply(basic_reply())
    add_filing(db)
    await extract_filings(make_config(stub), db)

    prompt = stub.generate_calls[0]["body"]["prompt"]
    # The release text is data; check the instructions around it.
    instructions = prompt.split("Release (")[0].lower()
    for word in BANNED:
        assert not re.search(rf"\b{re.escape(word)}\b", instructions), word
    assert "gaap" in instructions and "exactly as written" in instructions
    assert "null" in instructions


async def test_reply_cannot_set_direction_status_or_components(db, stub):
    # Altered EPS (2.11, not in the text) plus injected keys: none of them may take effect.
    reply = basic_reply(eps_diluted_current=val("$2.11"))
    reply.update(direction=1, status="ok", eps_yoy=99.0, direction_n=4, unverified_count=0)
    stub.set_filing_reply(reply)
    acc = add_filing(db)
    await extract_filings(make_config(stub), db)

    row = facts_row(db, acc)
    assert row["status"] == "partial"
    assert row["unverified_count"] == 1
    assert row["eps_yoy"] is None
    assert row["direction_n"] == 1  # rev_yoy only
    assert row["direction"] == pytest.approx(math.tanh(10.0 / 10.0))


async def test_altered_number_is_nulled_and_counted(db, stub):
    stub.set_filing_reply(basic_reply(eps_diluted_current=val("$2.11")))
    acc = add_filing(db)
    summary = await extract_filings(make_config(stub), db)

    row = facts_row(db, acc)
    assert row["eps_diluted_current"] is None and row["eps_diluted_prior"] == 1.95
    assert row["unverified_count"] == 1 and summary.unverified == 1
    assert "1 unverified" in summary.line()


async def test_unverified_guidance_bound_drops_the_range(db, stub):
    guidance = basic_reply()["guidance"]
    guidance["high"] = val("$13.9 billion", "billions")
    stub.set_filing_reply(basic_reply(guidance=guidance))
    acc = add_filing(db)
    await extract_filings(make_config(stub), db)

    row = facts_row(db, acc)
    assert row["guidance_metric"] is None and row["guidance_low"] is None
    assert row["unverified_count"] == 1 and row["status"] == "partial"


async def test_table_row_current_and_prior_on_one_row(db, stub):
    stub.set_filing_reply(
        {
            "eps_diluted_current": val("$ 2.87"),
            "eps_diluted_prior": val("$ 2.12"),
            "revenue_current": val("5,432", "millions"),
            "revenue_prior": val("4,987", "millions"),
            "period_label": "second quarter 2026",
            "guidance": {
                "metric": "revenue",
                "period_label": "full year 2026",
                "low": val("$20.0 billion", "billions"),
                "high": val("$21.0 billion", "billions"),
            },
        }
    )
    acc = add_filing(db, TABLE)
    await extract_filings(make_config(stub), db)

    row = facts_row(db, acc)
    assert row["status"] == "ok"
    assert row["eps_diluted_current"] == 2.87 and row["eps_diluted_prior"] == 2.12
    assert row["revenue_current"] == 5432e6 and row["revenue_prior"] == 4987e6
    assert row["rev_yoy"] == pytest.approx(100 * (5432 - 4987) / 4987)


async def test_invalid_json_twice_is_failed_with_reason(db, stub):
    stub.set_filing_reply('{"eps_diluted_current": {"as_wri')
    acc = add_filing(db)
    summary = await extract_filings(make_config(stub), db)

    assert len(stub.generate_calls) == 2  # retried once
    assert (summary.ok, summary.partial, summary.failed) == (0, 0, 1)
    row = facts_row(db, acc)
    assert row["status"] == "failed" and "invalid JSON" in row["error"]
    assert row["direction"] is None and row["direction_n"] == 0
    assert row["eps_diluted_current"] is None
    assert '{"eps' not in row["error"]  # the raw reply is not stored


async def test_invalid_once_then_valid_succeeds(db, stub):
    replies = iter(["not json", basic_reply()])
    stub.set_filing_reply(lambda prompt: next(replies))
    acc = add_filing(db)
    await extract_filings(make_config(stub), db)
    assert len(stub.generate_calls) == 2
    assert facts_row(db, acc)["status"] == "ok"


async def test_wrongly_typed_reply_counts_as_invalid(db, stub):
    stub.set_filing_reply(basic_reply(eps_diluted_current={"as_written": 2.1, "scale": "units"}))
    acc = add_filing(db)
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["status"] == "failed"


async def test_only_2_02_filings_are_extracted_and_only_once(db, stub):
    stub.set_filing_reply(basic_reply())
    wanted = add_filing(db, items=("2.02", "9.01"))
    other = add_filing(db, items=("7.01",), accepted="2026-11-05T12:00:00Z")
    config = make_config(stub)

    first = await extract_filings(config, db)
    assert first.ok == 1 and facts_row(db, other) is None
    again = await extract_filings(config, db)
    assert (again.ok, again.partial, again.failed) == (0, 0, 0)
    assert len(stub.generate_calls) == 1

    redo = await extract_filings(config, db, redo=True)
    assert redo.ok == 1 and len(stub.generate_calls) == 2
    assert facts_row(db, wanted)["status"] == "ok"
    assert db.conn.execute("SELECT COUNT(*) FROM filing_facts").fetchone()[0] == 1


async def test_unreachable_model_stores_nothing_so_it_is_retried(db, monkeypatch):
    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(ff, "_sleep", no_sleep)
    acc = add_filing(db)
    config = Config(ollama=OllamaConfig(host="http://127.0.0.1:9"))
    summary = await extract_filings(config, db)
    assert summary.failed == 1
    assert facts_row(db, acc) is None


async def test_release_without_text_is_failed(db, stub):
    acc = add_filing(db, "")
    db.conn.execute("UPDATE filings SET primary_text = '' WHERE accession = ?", (acc,))
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["status"] == "failed"
    assert stub.generate_calls == []


# --- trimming -----------------------------------------------------------------------


def long_release(chars: int = 57_000) -> str:
    filler = "\n".join(
        f"Paragraph {i}: management discussed facilities and community programs at length."
        for i in range(2000)
    )
    middle = filler[: chars - len(BASIC) - len(TABLE) - 200]
    return BASIC.split("Outlook:")[0] + middle + "\n" + TABLE


def test_57k_release_trims_under_budget_and_keeps_eps_sentence():
    text = long_release()
    assert len(text) > 56_000
    budget = release_budget_chars(16384, 768)
    assert budget < len(text)
    trimmed = trim_release(text, budget)
    assert len(trimmed) <= budget
    assert "Diluted earnings per share was $2.10" in trimmed
    assert re.search(r"Diluted net income per common share \$ 2\.87\s+\$ 2\.12", trimmed)
    assert "in millions" in trimmed
    assert "Paragraph 1500" not in trimmed  # irrelevant paragraphs are dropped


def test_trim_keeps_late_matching_lines_in_order_and_respects_small_budget():
    filler = "\n".join(f"Filler line {i} with nothing of interest." for i in range(800))
    text = "Headline\n" + filler + "\nDiluted EPS $3.33 for the quarter.\n" + filler
    trimmed = trim_release(text, 4000)
    assert trimmed.index("Headline") < trimmed.index("$3.33")
    assert len(trimmed) <= 4000
    assert len(trim_release(text, 500)) <= 500
    assert trim_release("short", 500) == "short"


async def test_long_release_prompt_fits_the_context_budget(db, stub):
    stub.set_filing_reply(basic_reply())
    add_filing(db, long_release())
    config = make_config(stub)
    await extract_filings(config, db)
    prompt = stub.generate_calls[0]["body"]["prompt"]
    assert len(prompt) / ff.CHARS_PER_TOKEN < config.filing_facts.num_ctx - 768


async def test_verification_uses_the_whole_release_not_the_trimmed_text(db, stub):
    # The figure sits in a line the trimmer drops, but it is in the release.
    text = (
        BASIC
        + "\n".join(f"Filler {i}." for i in range(8000))
        + "\nTotal assets were $77.7 billion."
    )
    reply = basic_reply(revenue_current=val("$77.7 billion", "billions"))
    stub.set_filing_reply(reply)
    acc = add_filing(db, text)
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["revenue_current"] == 77.7e9


# --- verify_value -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "scale", "expected"),
    [
        ("$2.10", "units", 2.10),
        ("2.10", None, 2.10),
        ("(0.45)", "units", -0.45),
        ("$(0.45)", "units", -0.45),
        ("-0.45", "units", -0.45),  # a minus sign and parentheses are both negative
        ("0.45", "units", None),  # sign dropped
        ("$12.1 billion", "billions", 12.1e9),
        ("12.1 billion", "units", 12.1e9),  # the attached word wins over the scale field
        ("12.1", "billions", 12.1e9),  # the scale field agrees with the printed word
        ("12,100", "millions", 12_100e6),
        ("12,100", "units", 12_100.0),
        ("12,100", "billions", None),  # the table says "in millions", not billions
        ("$2.11", "units", None),
        ("2.1", "units", None),  # prefix of 2.10 is not 2.10
        ("12", "millions", None),  # a fragment of 12,100 / 12.1
        ("about two", "units", None),
        ("", "units", None),
        (None, "units", None),
        ("2.10", "dozens", None),
    ],
)
def test_verify_value_table(written, scale, expected):
    source = (
        "Diluted earnings per share was $2.10. Net loss from other operations $ (0.45) .\n"
        "Revenue was $12.1 billion. (in millions) Net revenue 12,100 11,000"
    )
    result = verify_value(written, scale, source)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_verify_value_negative_forms_and_spacing():
    assert verify_value("(0.45)", "units", "per share $ ( 0.45 ) loss") == -0.45
    assert verify_value("(0.45)", "units", "(see note (0.45 of a cent) here") is None
    assert verify_value("(0.45)", "units", "Net loss $ (0.45) $ 0.30") == -0.45
    assert verify_value("-0.45", "units", "EPS of -0.45 versus 0.30") == -0.45
    assert verify_value("−0.45", "units", "EPS of −0.45") == -0.45
    assert verify_value("0.30", "units", "Net loss $ (0.45) $ 0.30") == 0.30
    assert verify_value("$0.45", "units", "an EPS of $.45 a share") == 0.45
    assert verify_value("1,234.5", "millions", "(in millions) 1,234.5") == 1234.5e6


def test_verify_value_rejects_dropped_or_wrong_scale_word():
    source = "Revenue was $12.1 billion in the quarter."
    assert verify_value("$12.1", "units", source) is None
    assert verify_value("$12.1", "millions", source) is None
    assert verify_value("$12.1 million", "millions", source) is None
    assert verify_value("$12.1 billion", "billions", source) == 12.1e9
    assert verify_value("12.1 BILLION", "units", source) == 12.1e9


# --- direction ----------------------------------------------------------------------

W = {"eps_yoy": 0.3, "rev_yoy": 0.3, "rev_accel": 0.2, "guidance_change": 0.2}
K = {"eps_yoy": 25, "rev_yoy": 10, "rev_accel": 5, "guidance_change": 1}


def test_direction_all_components():
    comps = {"eps_yoy": 10.0, "rev_yoy": 5.0, "rev_accel": -2.0, "guidance_change": 1}
    direction, n = fd.compute_direction(comps, W, K)
    expected = (
        0.3 * math.tanh(10 / 25)
        + 0.3 * math.tanh(5 / 10)
        + 0.2 * math.tanh(-2 / 5)
        + 0.2 * math.tanh(1)
    )
    assert n == 4 and direction == pytest.approx(expected)


def test_direction_renormalises_over_non_null_components():
    comps = {"eps_yoy": None, "rev_yoy": 5.0, "rev_accel": None, "guidance_change": 1}
    direction, n = fd.compute_direction(comps, W, K)
    expected = (0.3 * math.tanh(0.5) + 0.2 * math.tanh(1)) / 0.5
    assert n == 2 and direction == pytest.approx(expected)


def test_direction_with_one_component_is_its_own_tanh():
    direction, n = fd.compute_direction({"rev_yoy": 10.0}, W, K)
    assert n == 1 and direction == pytest.approx(math.tanh(1))


def test_direction_all_null_is_null_with_zero_n():
    comps = dict.fromkeys(fd.COMPONENT_KEYS)
    assert fd.compute_direction(comps, W, K) == (None, 0)
    assert fd.compute_direction({}, W, K) == (None, 0)


@pytest.mark.parametrize("sign", [-1, 1])
def test_direction_always_within_bounds(sign):
    comps = {
        "eps_yoy": sign * 1e9,
        "rev_yoy": sign * 1e9,
        "rev_accel": sign * 1e9,
        "guidance_change": sign,
    }
    direction, n = fd.compute_direction(comps, W, K)
    assert n == 4 and -1.0 <= direction <= 1.0
    assert direction == pytest.approx(sign * (0.8 + 0.2 * math.tanh(1)), abs=1e-9)


def test_direction_ignores_zero_weights_and_non_finite_values():
    comps = {"eps_yoy": float("inf"), "rev_yoy": 10.0, "rev_accel": 3.0, "guidance_change": None}
    weights = {**W, "rev_accel": 0.0}
    direction, n = fd.compute_direction(comps, weights, K)
    assert n == 1 and direction == pytest.approx(math.tanh(1))


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [
        (110.0, 100.0, 10.0),
        (90.0, 100.0, -10.0),
        (-0.30, -0.60, 50.0),  # loss halved: improvement
        (0.5, -0.5, 200.0),
        (1.0, 0.0, None),
        (None, 5.0, None),
        (5.0, None, None),
    ],
)
def test_pct_change_table(current, prior, expected):
    result = fd.pct_change(current, prior)
    assert result == pytest.approx(expected) if expected is not None else result is None


@pytest.mark.parametrize(
    ("current", "previous", "expected"),
    [
        (105.0, 100.0, 1),
        (95.0, 100.0, -1),
        (100.5, 100.0, 0),
        (99.2, 100.0, 0),
        (101.0, 100.0, 0),  # exactly at tolerance is not "more than"
        (101.1, 100.0, 1),
        (-9.0, -10.0, 1),  # a smaller loss is higher
        (1.0, 0.0, None),
        (None, 100.0, None),
        (100.0, None, None),
    ],
)
def test_guidance_change_table(current, previous, expected):
    assert fd.guidance_change(current, previous, 0.01) == expected


@pytest.mark.parametrize(
    ("previous_at", "expected"),
    [
        ("2026-08-05T21:00:00Z", 4.0),  # 91 days
        ("2026-09-15T21:00:00Z", 4.0),  # 50 days
        ("2026-07-07T21:00:00Z", 4.0),  # 120 days
        ("2026-09-16T21:00:00Z", None),  # 49 days
        ("2026-07-06T21:00:00Z", None),  # 121 days
        ("2025-11-04T21:00:00Z", None),  # a year
        (None, None),
    ],
)
def test_rev_accel_requires_previous_quarter_window(previous_at, expected):
    assert fd.rev_accel(14.0, 10.0, previous_at, "2026-11-04T21:00:00Z") == expected


# --- stored history -----------------------------------------------------------------


def by_filing_date(releases: dict[str, dict]):
    """Stub reply chosen by the 'filed <date>' line of the prompt."""

    def reply(prompt: str):
        match = re.search(r"filed (\d{4}-\d{2}-\d{2})\)", prompt)
        return releases[match.group(1)]

    return reply


def release(revenue: str, prior: str, label: str, low: str, high: str, metric="revenue") -> dict:
    return basic_reply(
        eps_diluted_current=val(None),
        eps_diluted_prior=val(None),
        revenue_current=val(revenue, "billions"),
        revenue_prior=val(prior, "billions"),
        guidance={
            "metric": metric,
            "period_label": label,
            "low": val(low, "billions"),
            "high": val(high, "billions"),
        },
    )


def text_for(*numbers: str) -> str:
    return "Results. " + " ".join(f"${n} billion." for n in numbers)


async def test_rev_accel_and_guidance_change_use_earlier_stored_rows(db, stub):
    # Added newest-first on purpose: extraction must still go oldest first.
    newest = add_filing(
        db, text_for("13.2", "11.0", "13.6", "14.0"), accepted="2026-11-04T21:00:00Z"
    )
    older = add_filing(
        db, text_for("12.1", "11.0", "12.4", "12.8"), accepted="2026-08-05T21:00:00Z"
    )
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-08-05": release(
                    "$12.1", "$11.0", " Fourth  Quarter FY2026", "$12.4", "$12.8"
                ),
                "2026-11-04": release("$13.2", "$11.0", "fourth quarter fy2026", "$13.6", "$14.0"),
            }
        )
    )
    await extract_filings(make_config(stub), db)

    first, second = facts_row(db, older), facts_row(db, newest)
    assert first["rev_yoy"] == pytest.approx(100 * 1.1 / 11.0)
    assert first["rev_accel"] is None and first["guidance_change"] is None
    assert second["rev_yoy"] == pytest.approx(20.0)
    assert second["rev_accel"] == pytest.approx(10.0)  # 20% minus 10%, 91 days apart
    assert second["guidance_change"] == 1  # midpoint 13.8 vs 12.6, label matches loosely


async def test_guidance_change_down_flat_and_label_mismatch(db, stub):
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-02-04": release("$10.0", "$9.0", "full year 2026", "$40.0", "$42.0"),
                "2026-05-05": release("$10.0", "$9.0", "full year 2026", "$40.0", "$42.2"),
                "2026-08-05": release("$10.0", "$9.0", "full year 2026", "$38.0", "$40.0"),
                "2026-11-04": release("$10.0", "$9.0", "full year 2027", "$30.0", "$31.0"),
            }
        )
    )
    dates = ["2026-02-04", "2026-05-05", "2026-08-05", "2026-11-04"]
    texts = [
        text_for("10.0", "9.0", "40.0", "42.0"),
        text_for("10.0", "9.0", "40.0", "42.2"),
        text_for("10.0", "9.0", "38.0", "40.0"),
        text_for("10.0", "9.0", "30.0", "31.0"),
    ]
    accs = [add_filing(db, t, accepted=f"{d}T21:00:00Z") for d, t in zip(dates, texts)]
    await extract_filings(make_config(stub), db)

    changes = [facts_row(db, a)["guidance_change"] for a in accs]
    # Q1: none earlier. Q2: 41.1 vs 41.0 is within 1%. Q3: 39 vs 41.1 is a cut.
    # Q4: a different period label has nothing comparable.
    assert changes == [None, 0, -1, None]


async def test_guidance_must_match_metric(db, stub):
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-08-05": release("$10.0", "$9.0", "full year 2026", "$40.0", "$42.0"),
                "2026-11-04": release(
                    "$10.0", "$9.0", "full year 2026", "$40.0", "$42.0", metric="eps"
                ),
            }
        )
    )
    add_filing(db, text_for("10.0", "9.0", "40.0", "42.0"), accepted="2026-08-05T21:00:00Z")
    last = add_filing(db, text_for("10.0", "9.0", "40.0", "42.0"), accepted="2026-11-04T21:00:00Z")
    # EPS guidance is per share: the values 40.0 / 42.0 must appear without a scale word.
    db.conn.execute(
        "UPDATE filings SET exhibit_text = ? WHERE accession = ?",
        ("Results. $10.0 billion. $9.0 billion. EPS $40.0 to $42.0.", last),
    )
    reply = release("$10.0", "$9.0", "full year 2026", "$40.0", "$42.0", metric="eps")
    reply["guidance"]["low"] = val("$40.0")
    reply["guidance"]["high"] = val("$42.0")
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-08-05": release("$10.0", "$9.0", "full year 2026", "$40.0", "$42.0"),
                "2026-11-04": reply,
            }
        )
    )
    await extract_filings(make_config(stub), db)
    row = facts_row(db, last)
    assert row["guidance_metric"] == "eps" and row["guidance_change"] is None


async def test_rev_accel_null_when_previous_quarter_is_too_far_back(db, stub):
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-03-04": release("$12.1", "$11.0", "x", "$1.0", "$2.0"),
                "2026-11-04": release("$13.2", "$11.0", "y", "$1.0", "$2.0"),
            }
        )
    )
    add_filing(db, text_for("12.1", "11.0", "1.0", "2.0"), accepted="2026-03-04T21:00:00Z")
    late = add_filing(db, text_for("13.2", "11.0", "1.0", "2.0"), accepted="2026-11-04T21:00:00Z")
    await extract_filings(make_config(stub), db)
    assert facts_row(db, late)["rev_accel"] is None  # 245 days apart


async def test_rev_accel_is_per_ticker(db, stub):
    stub.set_filing_reply(
        by_filing_date(
            {
                "2026-08-05": release("$12.1", "$11.0", "x", "$1.0", "$2.0"),
                "2026-11-04": release("$13.2", "$11.0", "y", "$1.0", "$2.0"),
            }
        )
    )
    add_filing(
        db, text_for("12.1", "11.0", "1.0", "2.0"), ticker="BBB", accepted="2026-08-05T21:00:00Z"
    )
    mine = add_filing(
        db, text_for("13.2", "11.0", "1.0", "2.0"), ticker="AAA", accepted="2026-11-04T21:00:00Z"
    )
    await extract_filings(make_config(stub), db)
    assert facts_row(db, mine)["rev_accel"] is None


# --- CLI ----------------------------------------------------------------------------


def test_cli_extract_prints_one_summary_line(tmp_path, stub, monkeypatch):
    db_path = tmp_path / "cli.db"
    d = Database(db_path)
    d.connect()
    add_filing(d)
    d.close()
    stub.set_filing_reply(basic_reply(eps_diluted_current=val("$2.11")))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("OLLAMA_HOST", stub.url)

    result = CliRunner().invoke(cli, ["filings", "extract"])
    assert result.exit_code == 0, result.output
    assert (
        result.output.strip()
        == "filings extract: 0 extracted, 1 partial, 0 failed, 1 unverified values"
    )

    again = CliRunner().invoke(cli, ["filings", "extract"])
    assert "0 extracted, 0 partial, 0 failed, 0 unverified" in again.output
    redo = CliRunner().invoke(cli, ["filings", "extract", "--redo"])
    assert "1 partial" in redo.output
    assert len(stub.generate_calls) == 2


def test_stub_transport_sanity(stub):
    # The stub answers /api/tags; guards against a port clash masking real failures.
    assert httpx.get(f"{stub.url}/api/tags").status_code == 200


# --- guidance stated as growth from a base ------------------------------------------

GROWTH_RANGE = (FIXTURES / "release_growth_range.txt").read_text()
GROWTH_SINGLE = (FIXTURES / "release_growth_single.txt").read_text()
ABSOLUTE_RANGE = (FIXTURES / "release_absolute_range.txt").read_text()
BASE = 14.23


def eps_guidance(low, high, *, basis="growth_from_base", base="$14.23") -> dict:
    return {
        "metric": "eps",
        "period_label": "fiscal 2026",
        "basis": basis,
        "base": val(base) if base else val(None),
        "low": low,
        "high": high,
    }


def growth_reply(low: str, high: str, **kw) -> dict:
    return basic_reply(
        eps_diluted_current=val(None),
        eps_diluted_prior=val(None),
        revenue_current=val("$8.0 billion", "billions"),
        revenue_prior=val("$7.5 billion", "billions"),
        guidance=eps_guidance(low, high, **kw),
    )


async def run_pair(db, stub, first_text, first_reply, second_text, second_reply):
    stub.set_filing_reply(by_filing_date({"2026-02-03": first_reply, "2026-05-05": second_reply}))
    a = add_filing(db, first_text, accepted="2026-02-03T21:00:00Z")
    b = add_filing(db, second_text, accepted="2026-05-05T21:00:00Z")
    await extract_filings(make_config(stub), db)
    return facts_row(db, a), facts_row(db, b)


def test_schema_guidance_has_basis_and_base():
    guidance = FILING_FACTS_SCHEMA["properties"]["guidance"]["anyOf"][1]
    assert guidance["properties"]["basis"]["enum"] == ["absolute", "growth_from_base"]
    assert "base" in guidance["required"] and "basis" in guidance["required"]


async def test_prompt_tells_model_to_put_the_prior_figure_in_base(db, stub):
    stub.set_filing_reply(basic_reply())
    add_filing(db)
    await extract_filings(make_config(stub), db)
    instructions = stub.generate_calls[0]["body"]["prompt"].split("Release (")[0]
    assert "growth_from_base" in instructions and "never in low or high" in instructions


async def test_growth_guidance_is_verified_and_stored_as_implied_range(db, stub):
    stub.set_filing_reply(growth_reply("flat", "4.0%"))
    acc = add_filing(db, GROWTH_RANGE)
    summary = await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert summary.unverified == 0
    assert row["guidance_basis"] == "growth_from_base"
    assert row["guidance_base"] == BASE
    assert (row["guidance_low_pct"], row["guidance_high_pct"]) == (0.0, 4.0)
    assert row["guidance_low"] == pytest.approx(BASE)
    assert row["guidance_high"] == pytest.approx(BASE * 1.04)
    assert fd.midpoint(row["guidance_low"], row["guidance_high"]) == pytest.approx(BASE * 1.02)


async def test_growth_guidance_within_tolerance_is_unchanged(db, stub):
    first, second = await run_pair(
        db,
        stub,
        GROWTH_RANGE,
        growth_reply("flat", "4.0%"),
        GROWTH_SINGLE,
        growth_reply("1.5%", "1.5%"),
    )
    assert second["guidance_low"] == pytest.approx(BASE * 1.015)
    assert first["guidance_change"] is None
    assert second["guidance_change"] == 0  # 1.015 vs 1.02 is a 0.49% move, inside 1%


async def test_growth_guidance_cut_beyond_tolerance_is_minus_one(db, stub):
    eight = GROWTH_RANGE.replace("flat to 4.0%", "8.0%")
    two = GROWTH_SINGLE.replace("1.5%", "2.0%")
    _, second = await run_pair(
        db, stub, eight, growth_reply("8.0%", "8.0%"), two, growth_reply("2.0%", "2.0%")
    )
    assert second["guidance_low"] == pytest.approx(BASE * 1.02)
    assert second["guidance_change"] == -1


async def test_growth_percent_not_in_source_drops_the_whole_guidance(db, stub):
    stub.set_filing_reply(growth_reply("flat", "5.0%"))
    acc = add_filing(db, GROWTH_RANGE)
    summary = await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert row["guidance_metric"] is None and row["guidance_low"] is None
    assert row["guidance_basis"] is None and row["status"] == "partial"
    assert summary.unverified == 1


async def test_percent_needs_a_percent_sign_in_the_source(db, stub):
    # "14" is in the text, but never followed by % or "percent".
    stub.set_filing_reply(growth_reply("14%", "14%"))
    acc = add_filing(db, GROWTH_SINGLE)
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["guidance_metric"] is None


async def test_percent_word_form_verifies(db, stub):
    text = GROWTH_SINGLE.replace("1.5%", "1.5 percent")
    stub.set_filing_reply(growth_reply("1.5%", "1.5%"))
    acc = add_filing(db, text)
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["guidance_low_pct"] == 1.5


async def test_flat_must_be_in_the_same_paragraph_as_the_base(db, stub):
    text = GROWTH_SINGLE + "\nThe stock price was flat on the news.\n"
    text = text.replace("Outlook:", "Separately, results were flat.\n\nOutlook:")
    stub.set_filing_reply(growth_reply("flat", "1.5%"))
    acc = add_filing(db, text)
    await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["guidance_metric"] is None


async def test_growth_base_not_in_source_drops_the_guidance(db, stub):
    stub.set_filing_reply(growth_reply("1.5%", "1.5%", base="$14.99"))
    acc = add_filing(db, GROWTH_SINGLE)
    summary = await extract_filings(make_config(stub), db)
    assert facts_row(db, acc)["guidance_metric"] is None
    assert summary.unverified == 1


async def test_absolute_range_still_works_unchanged(db, stub):
    reply = growth_reply("$14.50", "$14.80")
    reply["guidance"] = eps_guidance(val("$14.50"), val("$14.80"), basis="absolute", base=None)
    stub.set_filing_reply(reply)
    acc = add_filing(db, ABSOLUTE_RANGE)
    summary = await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert summary.unverified == 0
    assert (row["guidance_basis"], row["guidance_base"]) == ("absolute", None)
    assert (row["guidance_low"], row["guidance_high"]) == (14.50, 14.80)


async def test_guard_drops_absolute_guidance_that_is_really_a_from_base(db, stub):
    # The model copied the base into low and high and called it absolute. 14.23 is in the text.
    reply = growth_reply("x", "x")
    reply["guidance"] = eps_guidance(val("$14.23"), val("$14.23"), basis="absolute", base=None)
    stub.set_filing_reply(reply)
    acc = add_filing(db, GROWTH_SINGLE)
    summary = await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert row["guidance_metric"] is None and row["guidance_low"] is None
    assert summary.unverified == 1 and row["unverified_count"] == 1
    assert row["status"] == "partial"


async def test_guard_does_not_hide_a_cut_that_the_old_code_missed(db, stub):
    bad = growth_reply("x", "x")
    bad["guidance"] = eps_guidance(val("$14.23"), val("$14.23"), basis="absolute", base=None)
    _, second = await run_pair(db, stub, GROWTH_RANGE, bad, GROWTH_SINGLE, bad)
    assert second["guidance_change"] is None  # no guidance stored, rather than a false 0


def test_preceded_by_from_window():
    assert ff.preceded_by_from("$14.23", "grow 1.5% from $14.23 in fiscal 2025")
    assert ff.preceded_by_from("14.23", "grow 1.5% from about $14.23")
    assert not ff.preceded_by_from("$14.50", "earnings of $14.50 to $14.80, from last year")
    assert not ff.preceded_by_from("$14.80", "earnings of $14.50 to $14.80")


async def test_migration_adds_columns_to_an_existing_table(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE filing_facts (id INTEGER PRIMARY KEY, accession TEXT)")
    conn.commit()
    conn.close()
    d = Database(path)
    d.connect()
    cols = {r["name"] for r in d.conn.execute("PRAGMA table_info(filing_facts)")}
    d.close()
    assert {"guidance_basis", "guidance_base", "guidance_low_pct", "guidance_high_pct"} <= cols


async def test_growth_bounds_given_as_value_objects_are_accepted(db, stub):
    # Live gemma replies (PM review, 2026-11-22) wrap growth percents in value objects,
    # because the schema allows either shape. Those replies must parse, not fail.
    reply = growth_reply(val("1.5%", None), val("1.5%", None))
    stub.set_filing_reply(reply)
    acc = add_filing(db, GROWTH_SINGLE)
    await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert row["status"] != "failed"
    assert row["guidance_basis"] == "growth_from_base"


async def test_malformed_guidance_keeps_verified_eps_and_revenue(db, stub):
    bad = basic_reply(
        guidance={
            "metric": "eps",
            "period_label": "fiscal 2026",
            "basis": "growth_from_base",
            "base": val("$14.23"),
            "low": 1.5,
            "high": 1.5,
        }
    )
    stub.set_filing_reply(bad)
    acc = add_filing(db)
    await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert row["status"] != "failed"
    assert row["eps_diluted_current"] == pytest.approx(2.10)
    assert row["guidance_metric"] is None


DECLINE_TEXT = (
    "Fiscal 2025 outlook\n\nDiluted earnings-per-share to decline approximately 6.0% "
    "from $14.91 in fiscal 2024\n\nRevenue was $8.0 billion compared with $7.5 billion."
)


def test_growth_sign_reads_decline_words_in_code():
    assert ff.growth_sign("$14.91", DECLINE_TEXT) == -1
    grow = "Diluted earnings-per-share to grow approximately 1.5% from $14.23 in fiscal 2025"
    assert ff.growth_sign("$14.23", grow) == 1
    flat = "Diluted earnings-per-share to grow approximately flat to 4.0% from $14.23"
    assert ff.growth_sign("$14.23", flat) == 1


async def test_decline_guidance_is_stored_as_negative_growth(db, stub):
    # Live review 2026-11-22: "decline approximately 6.0% from $14.91" was stored as +6%.
    reply = growth_reply("6.0%", "6.0%", base="$14.91")
    stub.set_filing_reply(reply)
    acc = add_filing(db, DECLINE_TEXT)
    await extract_filings(make_config(stub), db)
    row = facts_row(db, acc)
    assert row["guidance_low_pct"] == -6.0 and row["guidance_high_pct"] == -6.0
    assert row["guidance_low"] == pytest.approx(14.91 * 0.94)
