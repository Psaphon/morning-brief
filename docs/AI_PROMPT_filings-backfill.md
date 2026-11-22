# AI Development Prompt — filings-backfill

**Branch:** `feature/filings-backfill`
**Base:** `develop`

Read `CLAUDE.md`, `docs/DEVPLAN.md` (`## Feature: filings-backfill`, the acceptance criteria) and
`docs/SIGNAL-SCHEMA.md` §12 (the history file is `filings-history.jsonl`, one event record
per line plus `schema_version`). Do NOT push.

Everything this needs already exists. **Reuse it; don't copy it:**
- `src/fetchers/edgar.py`: the client, rate limiter, backoff, contact rule, exhibit lookup,
  `parse_acceptance_time`, `load_stock_ciks` and `_process_cik`.
- `src/processors/filing_facts.py`: `extract_filings`.
- `src/publishers/signals_filings.py`: `build_events`, `parse_model_cutoff`. History records
  must be built by the same code as the daily file.

## Facts the PM verified against live EDGAR (2026-11-22)

- `submissions/CIK##########.json` → `filings.recent` holds roughly the last 1,000 filings.
  Older ones are in `filings.files`: a list of `{name, filingCount, filingFrom, filingTo}`,
  e.g. `CIK0000034088-submissions-001.json` (2005-10-04 → 2020-01-01). Fetch those at
  `https://data.sec.gov/submissions/<name>`.
- **A paged file has the parallel arrays at the TOP LEVEL** (`accessionNumber`, `form`,
  `items`, `acceptanceDateTime`, …), not under `filings.recent`. Handle both shapes with one
  normalizer and test both.
- Skip any paged file whose `filingTo` is before `--since`.
- Old 8-Ks still carry `items` (checked: 48 8-Ks from 2016–2019 for one issuer, none empty)
  and UTC `acceptanceDateTime`.
- **Predecessor CIKs:** `config/ticker_map.toml` gives XOM `former_ciks = ["0000034088"]`.
  Its 2016 → mid-2026 history is filed only under that CIK. The backfill polls
  `cik` **and** every `former_ciks` entry for each stock, and stores the filing's real filer
  CIK in the row. (The daily fetch keeps ignoring `former_ciks`.)

## What to build

### `src/processors/filings_backfill.py` + CLI `morning-brief filings backfill --since YYYY-MM-DD`

- `--since` defaults to `FILINGS_BACKFILL_SINCE` (default `2016-01-01`).
- For every universe stock and each of its CIKs: list 8-Ks (not `8-K/A`) with item `2.02` and
  acceptance on or after `--since`, across `recent` and the needed paged files. Skip
  accessions already stored **before** fetching documents. Fetch and store the rest exactly
  like the daily fetch (same table, same exhibit rules, `origin = 'edgar'`).
- Then run `extract_filings` over the stored filings, **oldest first**, so `rev_accel` and
  `guidance_change` find their earlier rows.
- Then write `data/filings-history.jsonl` (path: `FILINGS_HISTORY_PATH`, default under the
  data root next to `signals/`) atomically: one line per earnings filing that has a facts row,
  built by `build_events` with no emit-day window, plus `schema_version`, sorted by
  `knowable_at`. Validate each line against `docs/schemas/signals-filings.schema.json`
  before writing. If any line fails, write nothing and log an ERROR.
- `in_model_window` uses `FILINGS_MODEL_CUTOFF`, the same as the daily file. The PM sets
  `2025-02-01` on hub: the Gemma 4 model card states a January 2025 training cutoff.
- **Time window:** `BACKFILL_WINDOW_UTC` (default `05:00-07:45`). Outside the window the
  command logs one INFO and exits 0 without work, unless `--ignore-window` is passed (for a
  supervised run). Inside it, check the clock **before each filing and before each
  extraction**, and stop cleanly at the next item once the window ends. Use an injectable
  clock.
- **Resumable:** a second run skips stored accessions and existing facts rows. An
  interrupted run loses at most the item in flight.
- One summary line at the end, for example: `backfill: 15 stocks, 18 CIKs, 643 earnings
  8-Ks (612 new, 31 already stored), 640 extracted (602 ok, 31 partial, 7 failed), history
  640 lines, stopped: window end | done`.
- Add a `--counts` flag that prints the [HUMAN] review numbers from the DB without fetching:
  - events per ticker;
  - the verification rate: share of the four values verified;
  - the `direction_n` distribution;
  - in-window vs out-of-window counts.

### Tests (`tests/test_filings_backfill.py`)

Use a stub HTTP server and stub Ollama, the same patterns as `tests/test_edgar_fetcher.py`
and `tests/test_filing_facts.py`. Cover:
- `recent` plus one paged file, with the top-level shape: both are read, and a paged file
  before `--since` is never requested.
- A `former_ciks` stock: filings under both CIKs are stored with their own filer CIK.
- Resumable: a second run requests no documents and creates no facts rows.
- Window: a stub clock past the window end stops at the next item with
  `stopped: window end`; outside the window without `--ignore-window` does nothing; one
  request through the shared client shows the User-Agent and rate limiter apply.
- History file: lines validate against the schema, are sorted, match `build_events` output
  for the same rows, and use `in_model_window` from the cutoff; nothing is written when one
  line is invalid.
- `--counts` output on a seeded DB.

## Rules

- `tests/test_signals_golden.py`, its golden files and
  `tests/fixtures/brief_fixture/signals-filings.golden.json` pass **unchanged**.
- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit.
- Do NOT push. Don't change `filing_facts.py` or `signals_filings.py` behaviour (importing is
  fine; a tiny refactor to expose a helper is fine if both existing test files still pass
  unchanged). Don't touch `docs/SIGNAL-SCHEMA.md` or the JSON schema.
- Don't put fake executables in `/tmp`.
- Commit message:

```
feat: resumable EDGAR earnings backfill and filings-history.jsonl

Pages through EDGAR's submissions history (recent + older paged files,
including predecessor CIKs), stores earnings 8-Ks, extracts facts oldest
first with the daily code, and writes a schema-validated history file.
Runs only inside BACKFILL_WINDOW_UTC unless --ignore-window.
```
