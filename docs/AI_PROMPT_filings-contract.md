# AI Development Prompt — filings-contract

**Branch:** `feature/filings-contract`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` (`## Feature: filings-contract`)
for the acceptance criteria. The full lane design, including the Constraints section this
contract must encode, is in the atrade plan; the parts you need are restated below.
Do NOT push — the host workflow handles push and PR.

This feature is **documentation, a JSON schema, config and tests only**. No producer code,
no fetcher, no change to `src/`. Nothing emits the new files yet.

## What to build

### 1. `docs/SIGNAL-SCHEMA.md` → version `1.1.0`

- Bump the header `**Schema version:**` to `1.1.0`. Per §8 this is a **minor** bump: the
  existing `signals-<date>.json` artifact and every existing field are unchanged.
- Do not edit §1–§11 except: the version line, and one sentence in §8 noting that 1.1.0
  added the filings-lane files (a new artifact, not a change to the existing one).
- Add **`## 12. Filings lane`** at the end, with these subsections:

**12.1 Why a separate file.** atrade's current reader ignores unknown fields, so filings
records in the existing file would silently mix into the news shadow arm. A separate file
makes that impossible. A consumer that only knows §2 cannot read these by accident.

**12.2 Artifacts.**

| File | Path (relative to the data root) | Write mode |
|---|---|---|
| Daily | `data/signals/signals-filings.json` (next to the existing signals file) | Atomic, same rule as §2 |
| History | `data/filings-history.jsonl` | One event record per line; rewritten atomically by the backfill |

**12.3 Daily file envelope:** `schema_version` (string, semver), `emitted_at` (UTC
timestamp), `universe_ref` (same meaning as §3), `lane` (const `"filings"`), `events`
(array of event records). The history file has no envelope: each line is one event record
plus `schema_version`.

**12.4 Event record.** Every field below is required; nullable ones say so.

| Field | Type | Rule |
|---|---|---|
| `ticker` | string | A symbol from the universe with `asset_class = "stock"` |
| `cik` | string | `^[0-9]{10}$`, the filer CIK that produced this event (may be a `former_ciks` entry for old events) |
| `event` | enum | `earnings`, `guidance`, `acquisition`, `agreement`, `exec_change` |
| `origin` | enum | `edgar`, `wire`, `both` |
| `accession` | string or null | EDGAR accession `^[0-9]{10}-[0-9]{2}-[0-9]{6}$`; null only when `origin = wire` |
| `wire_id` | string or null | `^[0-9a-f]{64}$` (sha256 of the canonical release URL); null only when `origin = edgar` |
| `knowable_at` | string | UTC timestamp with explicit `Z`, see 12.5 |
| `period_end` | string or null | `YYYY-MM-DD` fiscal period end the numbers describe; null for non-earnings events |
| `direction` | number or null | In [-1, 1]; null when `direction_n = 0` or the event type has no direction |
| `direction_n` | integer | 0–4, count of verified components used |
| `components` | object | Exactly the keys `eps_yoy`, `rev_yoy`, `rev_accel`, `guidance_change`, each number or null. `eps_yoy`/`rev_yoy`/`rev_accel` are percent-change values; `guidance_change` is one of -1, 0, 1 |
| `in_model_window` | boolean | `knowable_at` earlier than the configured local model's training-data cutoff (runtime config, not in the file) |

State in the section that `direction` is computed by code from verified components, never
by a model, and that acquisitions, agreements and exec changes carry `direction = null`.
State that this `direction` is defined independently of any future news-lane direction.

**12.5 `knowable_at` for filings.** EDGAR filing: the acceptance time. Wire item: its own
publish time. `origin = both`: the earlier of the two. Never fetch time, run time or
`emitted_at`, and never a date without a time. A timestamp without an explicit zone, or
later than the fetch time, is rejected at the producer and logged. Consumers apply §5's
rule: no record is usable before its `knowable_at`.

**12.6 No free text.** Both files hold only enum values, numbers, booleans, timestamps,
dates, tickers and pattern-checked IDs. There is no title, summary, URL or company name.
Any other string is a contract violation; consumers reject the record and keep the rest of
the file.

**12.7 Original values only.** Numbers come from the filing or release as first published,
never from later-restated sources (for example XBRL company-facts).

**12.8 Universe binding.** `config/ticker_map.toml` carries `cik` for every stock (see
below). Note the cross-repo rule from §6 still holds.

### 2. `docs/schemas/signals-filings.schema.json`

One JSON Schema (draft 2020-12) file with `$defs` for the event record, the daily envelope,
and a history line. Use `additionalProperties: false` everywhere and the patterns above.
Encode `accession`/`wire_id` nullability against `origin` with `if`/`then`.

### 3. `config/ticker_map.toml`

Add `cik = "<10 digits>"` to every `asset_class = "stock"` entry, and nothing to ETFs.
Values (the PM pulled these from SEC's `company_tickers.json` on 2026-11-22; use exactly):

```
AAPL 0000320193   MSFT 0000789019   NVDA 0001045810   AMZN 0001018724
GOOGL 0001652044  META 0001326801   TSLA 0001318605   JPM 0000019617
JNJ 0000200406    V 0001403161      PG 0000080424     UNH 0000731766
HD 0000354950     MA 0001141391     XOM 0002115436
```

XOM reorganized under a new holding-company registrant in 2026; its pre-reorganization
filings (back to 2016) are under the old CIK. Add `former_ciks = ["0000034088"]` to XOM
only. `former_ciks` is optional (a list of 10-digit strings) and exists so the backfill
can reach history filed by a predecessor registrant. Document it in a comment block at
the top of the stocks section and in 12.8.

Do NOT bump the file's `schema_version` or `universe_ref` (the symbol set is unchanged).
Make sure whatever loads this file (`src/` loader) tolerates the new keys without code
changes; if it does not, stop and report rather than editing `src/`.

### 4. Fixtures: `tests/fixtures/filings/`

- `signals-filings.valid.json`: envelope with at least one record per event type and per
  origin, one with `direction = null`, one with `in_model_window = true`. Synthetic values.
- `filings-history.valid.jsonl`: 3+ lines.
- `invalid/`: one small file per rejection case: unknown `event`, `direction` 1.5,
  zone-less `knowable_at`, an extra free-text string field (`"title"`), bad accession
  pattern, `origin = edgar` with null `accession`.

### 5. `tests/test_filings_contract.py`

- Valid fixtures validate against the schema; each invalid fixture fails.
- Walks every string in the valid fixtures and asserts each is an enum value, a ticker
  from `ticker_map.toml`, a timestamp/date, or matches the accession/wire_id/cik pattern.
- Loads the REAL `config/ticker_map.toml`: every stock has a 10-digit `cik`, every ETF
  has none, `former_ciks` (when present) is a list of 10-digit strings, and stock CIKs are
  unique.
- The doc header version and the schema's documented version agree (`1.1.0`).
- Add `jsonschema` to `requirements.txt` next to the other test deps (pin `>=4.23`), and
  check CI installs from `requirements.txt` (it should; don't hand-list deps in CI).

## Rules

- `tests/test_signals_golden.py` must pass **unchanged**. Do not regenerate any golden file.
- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit.
- Do NOT push.
- Do NOT modify anything under `src/`, the scorer, existing signals, feeds or workflows.
- No real names, locations or contact addresses anywhere.
- Commit message:

```
feat: filings-lane contract (SIGNAL-SCHEMA 1.1.0) and ticker CIKs

Adds §12 for signals-filings.json and filings-history.jsonl, a JSON schema,
synthetic fixtures and contract tests, and a CIK for every stock in the
ticker map (XOM also lists its predecessor CIK). Existing signals unchanged.
```
