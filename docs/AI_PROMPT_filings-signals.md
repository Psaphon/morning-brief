# AI Development Prompt — filings-signals

**Branch:** `feature/filings-signals`
**Base:** `develop`

Read `CLAUDE.md`, `docs/DEVPLAN.md` (`## Feature: filings-signals`, the acceptance criteria) and
`docs/SIGNAL-SCHEMA.md` **§12** (the contract you are producing). The JSON schema is
`docs/schemas/signals-filings.schema.json`. Do NOT push.

atrade's reader for this file is already merged and validates every rule in §12, so emit
exactly the contract. **EDGAR only:** `wire-feeds` is held, so every record has
`origin = "edgar"`, an `accession` and `wire_id = null`.

## What to build

### `src/publishers/signals_filings.py`

`emit_filings_signals(db, signals_dir, ticker_map, config, now=None) -> Path | None`:
- One record per stored filing that has a `filing_facts` row (any status), item `2.02`,
  and `accepted_at` within `FILINGS_EMIT_DAYS` (default 30) of `now`. Filings without a
  facts row are skipped until extracted.
- Record fields per §12.4, from `filings` + `filing_facts`:
  - `ticker`, and `cik` from the filing row (the filer CIK);
  - `event = "earnings"`, `origin = "edgar"`, `accession`, `wire_id = null`;
  - `knowable_at` is the filing's `accepted_at`, **never** fetch time, run time or `emitted_at`;
  - `period_end = null`: not extracted yet; the schema allows null. Add a short code comment
    saying so;
  - `direction`, `direction_n`, and `components` with exactly the four keys;
  - `in_model_window = knowable_at < FILINGS_MODEL_CUTOFF`.
- `failed` or `partial` facts rows emit with null components and the stored
  `direction`/`direction_n`, which is null and 0 when nothing verified.
- `FILINGS_MODEL_CUTOFF` is a new runtime config (ISO date, no default value in code).
  When it's unset, log one WARNING and emit `in_model_window = true` for every record.
  That's the conservative reading: the model may have seen the outcome. Document it in
  `env.example`.
- Envelope: `schema_version "1.1.0"`, `emitted_at` (UTC, `Z`), `universe_ref` from the
  ticker map, `lane "filings"`, `events` sorted by `knowable_at` then `ticker`.
- Validate the whole document against the committed JSON schema before writing (use
  `jsonschema`, already in requirements). On failure, log an ERROR and write nothing.
- Write `signals_dir/signals-filings.json` atomically (`.tmp`, fsync, `os.replace`), the
  same way `src/publishers/signals.py` does.
- Never fail the brief. The caller wraps the call in the same non-fatal pattern as the
  other filings stages.

### Wiring

In `run_pipeline`, after the filings extract step and only when `FILINGS_ENABLED`, call
the writer with `config.signals_dir`, timed as a `filings_emit` lap. With the lane
disabled, no file is written and the existing signals output is byte-identical (test it).
Add `morning-brief filings emit` for a manual run.

### Tests (`tests/test_filings_signals.py`)

Seed a temp DB with synthetic `filings` + `filing_facts` rows, using made-up CIKs and
accessions that match the patterns. Cover:
- The output validates against the schema. A string walk allows only enum values, universe
  tickers, `Z` timestamps, dates and pattern-checked accession/cik values.
- Golden file `tests/fixtures/brief_fixture/signals-filings.golden.json`: a fixed `now` and
  fixed rows produce exactly this file. `emitted_at` comes from the injected `now`.
- `knowable_at` stays the acceptance time when the fetch was two days after acceptance.
- The emit window: a filing 31 days old is excluded.
- A filing with no facts row is skipped. A failed facts row emits with null direction and
  `direction_n` 0.
- `FILINGS_MODEL_CUTOFF` unset gives all `true` plus one WARNING. Set, it splits correctly
  either side.
- Lane disabled: no file, and the existing `signals-<date>.json` is unchanged.
- Cross-check: load the produced file with the same field rules atrade enforces (§12.4:
  no extra keys, `direction` null when `direction_n == 0`, bools are real bools).

## Rules

- `tests/test_signals_golden.py` and its golden files pass **unchanged**.
- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit.
- Do NOT push. Do NOT touch the scorer, `src/publishers/signals.py`, `docs/SIGNAL-SCHEMA.md`,
  the JSON schema, `edgar.py` or `filing_facts.py`.
- Don't put fake executables in `/tmp`.
- Commit message:

```
feat: emit signals-filings.json for atrade

Writes one contract-validated record per recent earnings 8-K with extracted
facts (SIGNAL-SCHEMA 1.1.0 §12), atomically, next to the existing signals
file. knowable_at is EDGAR acceptance time; in_model_window uses
FILINGS_MODEL_CUTOFF and is conservative when unset. Off unless
FILINGS_ENABLED.
```
