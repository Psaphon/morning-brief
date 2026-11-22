# AI Development Prompt — filing-facts

**Branch:** `feature/filing-facts`
**Base:** `develop`

Read `CLAUDE.md`, `docs/DEVPLAN.md` (`## Feature: filing-facts`, the acceptance criteria) and
`docs/SIGNAL-SCHEMA.md` §12 (what `components`, `direction` and `direction_n` mean).
Do NOT push — the host workflow handles push and PR.

The `filings` table and `src/fetchers/edgar.py` already exist (edgar-fetcher, #96). This
feature reads stored earnings filings, extracts numbers with the local model, **verifies
every number against the source text in code**, and computes components and direction in
code. It does not write `signals-filings.json` (that is `filings-signals`).

## Two rules that shape everything

1. **The model only copies.** It returns numbers and labels exactly as written. It is never
   asked whether results are good or bad, and nothing it returns can set `direction`,
   components or status. Output keys outside the schema are ignored.
2. **Code decides.** Every extracted value is checked against the source text after
   normalization; an unverified value becomes null and is counted. Components and
   `direction` are pure functions of verified values plus config.

## What real releases look like (the PM sampled 9 real EX-99.1s on hub, 2026-11-22)

Use these patterns for synthetic fixtures. Don't copy real releases into the repo.

- Length: 11k–57k characters of plain text. The default `num_ctx` (8192 tokens) can't hold
  one, so trimming is required (see below).
- EPS wording varies: "Diluted earnings per share was $2.10", "$1.89 per diluted share,
  compared with … $1.95 per diluted share", "Diluted net income per common share $2.87 $2.12"
  (table row: current then prior), "GAAP and non-GAAP earnings per diluted share were $2.70
  and $2.68", "earnings per share assuming dilution" (one issuer never says "diluted").
- **GAAP, not adjusted.** Many releases give both. Extract the GAAP figure; never the
  "adjusted", "non-GAAP" or "constant currency" one.
- Revenue labels vary: "revenue", "net revenue", "net sales", "total revenues and other
  income". Values usually come with a scale word ("$12.1 billion") or a table header
  ("in millions").
- The prior-year value is usually in the same sentence or the next table column.

## What to build

### `src/processors/filing_facts.py`

- `FILING_FACTS_SCHEMA` (Ollama `format`, `additionalProperties: false`): for each of
  `eps_diluted_current`, `eps_diluted_prior`, `revenue_current`, `revenue_prior`: an object
  `{as_written: string|null, scale: "units"|"thousands"|"millions"|"billions"|null}`; plus
  `period_label` (string|null, e.g. "third quarter fiscal 2026"), and `guidance`: null or
  `{metric: "revenue"|"eps", period_label: string, low: {as_written, scale}, high: {as_written, scale}}`.
- **Trim** (`trim_release(text, max_chars)`): keep the opening highlights (first ~3,000
  characters), then whole paragraphs and table rows containing EPS, revenue or guidance
  terms (`per share`, `diluted`, `assuming dilution`, `revenue`, `net sales`, `outlook`,
  `guidance`, `expects`), in original order, up to the budget. Budget:
  `FILINGS_NUM_CTX` (default 16384) minus `FILINGS_NUM_PREDICT` (default 768) minus a
  prompt reserve, at ~3 characters per token. Test that a 57k-character synthetic release
  trims under budget and still contains its EPS sentence.
- One Ollama call per filing, with `format`, `num_ctx`, `num_predict`, temperature 0 and
  `FILINGS_SEED` (default 42) in `options`. Follow `summarize_single` in
  `src/summarizers/local.py` for the HTTP call and retry style. Invalid JSON → retry once
  → store status `failed` with the reason.
- The prompt names the fields and the GAAP rule and says to copy values exactly as written
  or use null. It contains no words about good, bad, positive, negative, beat, miss,
  sentiment or stock price (a test asserts this word list is absent).
- **Verify** (`verify_value(as_written, scale, source_text)`): normalize both sides
  (strip `$`, commas, spaces; parentheses mean negative; "billion"/"million"/"thousand"
  and the scale field set the multiplier), then require the number **as written** to occur
  in the source text (normalized the same way). Return the value in base units, or None.
  Fixture tests: a release whose numbers all appear verifies fully; an altered number
  (`2.11` where the text says `2.10`) is nulled and counted; `(0.45)` parses as -0.45.

### `src/processors/filing_direction.py`

- `eps_yoy`, `rev_yoy`: `100 * (current - prior) / abs(prior)`; null if either input is
  null or `prior == 0`. Scale cancels because both values come from the same release.
- `rev_accel`: this filing's `rev_yoy` minus the same ticker's previous stored earnings
  filing's `rev_yoy` (by `accepted_at`), only if that previous filing is 50–120 days
  earlier; else null.
- `guidance_change`: compare this release's guidance midpoint with the most recent earlier
  release for the same ticker, same metric and same `period_label` (case/space-insensitive).
  +1 if higher by more than `GUIDANCE_TOLERANCE` (default 0.01, relative), -1 if lower,
  0 within tolerance, null if no comparable earlier guidance.
- `direction = clip(Σ wᵢ·tanh(componentᵢ / kᵢ), -1, 1)` over non-null components only, with
  weights renormalized to sum to 1. `direction_n` = number used; with 0, `direction` is
  null. Config defaults: weights `FILINGS_W_EPS_YOY=0.3`, `FILINGS_W_REV_YOY=0.3`,
  `FILINGS_W_REV_ACCEL=0.2`, `FILINGS_W_GUIDANCE=0.2`; scales `FILINGS_K_EPS_YOY=25`,
  `FILINGS_K_REV_YOY=10`, `FILINGS_K_REV_ACCEL=5`, `FILINGS_K_GUIDANCE=1`. Pure functions,
  table-driven tests including the renormalization case.

### Storage and wiring

- New `filing_facts` table in `src/db.py` (same migration style): `accession` (UNIQUE,
  references `filings`), `status` (`ok` | `partial` | `failed`), the four verified values
  (REAL or NULL), the guidance fields, `period_label`, `unverified_count`, the four
  components, `direction`, `direction_n`, `model`, `extracted_at`, `error`. Don't store the
  model's raw output text beyond a short error reason.
- Only filings whose items include `2.02` are extracted (the others are for `filing-events`).
- CLI: `morning-brief filings extract` (all stored 2.02 filings without facts, oldest first
  so `rev_accel` and guidance can find earlier rows; `--redo` re-extracts all) printing one
  summary line: extracted / partial / failed / unverified values.
- Daily run: after the fetch step, when `FILINGS_ENABLED`, extract new filings inside the
  same non-fatal try/except pattern, timed as its own `filings_extract` lap.
- Config and `env.example`: every `FILINGS_*` setting above.

### Tests (`tests/test_filing_facts.py`, fixtures under `tests/fixtures/filing_facts/`)

Use a stub Ollama (see `tests/stub_ollama.py`), not mocks of internal functions:
- The criteria in DEVPLAN, including: the stub's request has `num_ctx`, temperature 0
  and the seed; the prompt has none of the banned words; a stub reply that includes
  `"direction": 1` or `"status": "ok"` changes nothing; invalid JSON twice → `failed`.
- Verification: full, altered (nulled + counted), negative in parentheses, "billion" scale,
  table-row form (current and prior on one row).
- Direction: renormalization with two null components; all null → `direction` null and
  `direction_n` 0; output always in [-1, 1].
- `rev_accel` and `guidance_change` against stored earlier rows, including "too far apart"
  → null.

## Rules

- `tests/test_signals_golden.py` passes **unchanged**; don't regenerate golden files.
- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit.
- Do NOT push. Do NOT change the scorer, signals output, feeds, `docs/SIGNAL-SCHEMA.md` or
  `src/fetchers/edgar.py` (beyond importing from it).
- Don't put fake executables in `/tmp` (hub's `/tmp` is noexec).
- No real company text, names or contact addresses in fixtures.
- Commit message:

```
feat: filing facts extraction and code-computed direction

Extracts GAAP diluted EPS, revenue and guidance from stored earnings 8-Ks
with the local model, verifies every number against the release text,
and computes eps_yoy, rev_yoy, rev_accel, guidance_change and a bounded
direction in code. The model never judges and cannot set direction.
```
