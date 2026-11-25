# AI Development Prompt — signal-direction

**Branch:** `feature/signal-direction`
**Base:** `develop`

Read `CLAUDE.md`, then `docs/DEVPLAN.md` → `## Feature: signal-direction` for the acceptance criteria, and `docs/SIGNAL-SCHEMA.md` (already at **1.2.0**: `direction` and `direction_n` are documented, and atrade's reader shipped in Psaphon/atrade#46).
Do NOT push — the host workflow handles push and PR.

## Context the DEVPLAN missed (operator decision 2026-11-25)

- Signals are emitted in `src/main.py` Stage 2.6 from **signals-lane** articles, **before** the per-article extraction (Stage 3, reader-lane articles chosen by `select_balanced_articles`). On 2026-11-25 only 24 of 73 signal articles got an extraction.
- The operator chose: **ride on the existing extraction call, no extra model call.** Move signal emission to **after** the extraction/save loop, and take `direction` only from signal articles that were extracted. Direction will be sparse at first; that is accepted.
- `SIGNAL-SCHEMA.md` is already 1.2.0. Do **not** edit it. Instead the DEVPLAN criterion "gets a version bump" is met by emitting `schema_version: "1.2.0"`; correct that criterion's text to say so.

## What to build

### 1. Extraction (`src/summarizers/schema.py`, `src/summarizers/local.py`)

- `summarize_single` takes an optional `tickers: list[str] | None`. When non-empty, the prompt adds one short instruction listing them and asking for `ticker_sentiment`: an object mapping each listed ticker to -1 (bad news for that company), 0 (neutral or only mentioned), or 1 (good news). Add `ticker_sentiment` to `EXTRACTION_SCHEMA` as an optional object of integers (keep the schema valid for the Ollama `format` parameter).
- `parse_extraction` gains `allowed_tickers`: keep only keys in that set whose values are exactly the ints -1, 0 or 1 (not bool, not strings like `"bullish"`, not 0.5). Anything else is dropped silently (log at DEBUG). The result always has `ticker_sentiment: {}` when nothing valid remains. A bad `ticker_sentiment` must **never** fail the extraction or trigger the retry.
- `summarize_articles` passes each article's matched tickers through. Articles with no matches get no ticker list and the prompt is unchanged byte-for-byte (test with the stub).
- Ticker matching: reuse `match_articles_to_tickers` / `load_ticker_map` from `src/publishers/signals.py` (no new alias logic). Build `{article_id: [symbols]}` in `main.py` once per run from the same ticker map Stage 2.6 uses, **only when `config.signals_enabled`**; otherwise pass nothing.
- `save_extraction` already stores the whole extraction JSON; `ticker_sentiment` rides along. Add `Database.get_ticker_sentiment(article_ids) -> dict[int, dict[str, int]]` reading it back (missing key or old rows → `{}`), next to `get_extraction_details`.

### 2. Signals (`src/publishers/signals.py`, `src/main.py`)

- Move the Stage 2.6 block so it runs **after** the extraction and save loop, keeping its `try/except` (non-fatal) and its `timer.lap("signals")`. It must still run when extraction is skipped, finds no articles, raises, or is cut short by the run deadline: put it where every path reaches it (a `finally`-style placement or an explicit fall-through), and test that a failing `summarize_articles` still writes `signals-<date>.json`.
- `emit_signals`/`build_signals` take an optional `sentiment: dict[int, dict[str, int]]`. For each ticker: collect `sentiment[article_id][symbol]` over its contributing articles where present → `direction` = mean rounded to 2 places, `direction_n` = count; none → `direction: null`, `direction_n: 0`.
- `schema_version` becomes `"1.2.0"`. `score`, `knowable_at`, `article_count`, `provenance` are computed exactly as now.
- Regenerate `tests/fixtures/brief_fixture/signals.golden.json` with only the version and the two new fields changed; a test compares every other field to the previous golden values field by field.

### 3. Tests (`tests/test_signal_direction.py`, plus updates where needed)

All DEVPLAN criteria with the stub, including: +1 and −1 → `0.0`, `2`; stub returning `"bullish"` → `null`, `0`; unrequested ticker dropped; no-match article gets the unchanged prompt; string-walk test (no free text outside enumerated fields); failing extraction still emits signals; with `SIGNALS_ENABLED` off nothing about extraction changes.

Hub's `/tmp` is noexec: pytest `tmp_path` for data files only, never fake executables.

### 4. Docs

README signals section: one paragraph on `direction` (sparse, from extracted articles only). Tick the DEVPLAN criteria you met; leave Status `In Progress`.

## Rules
- Run `ruff check . && ruff format --check .` and `pytest` before EVERY commit.
- Do NOT push. Do NOT modify files outside this scope. Do NOT edit `docs/SIGNAL-SCHEMA.md`.
- No real names, locations, time zones or hostnames anywhere.
- Commit message: `feat: signal-direction — per-ticker news direction from the extraction call (contract 1.2.0)`
