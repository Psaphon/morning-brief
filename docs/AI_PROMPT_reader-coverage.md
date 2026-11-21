# AI Development Prompt — reader-coverage

**Branch:** `feature/reader-coverage`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: reader-coverage` for the acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Summarize more articles per category, with richer facts per article, and time every run.

### Facts about the current code (verified by the PM)

- **`select_balanced_articles(articles, per_category=8, total=50, interests=None)`** in `src/summarizers/local.py`:
  - It is called from `run_pipeline()` with defaults.
  - It orders each category by interest score when interests are set.
  - **The `total=50` cap would hide any per-category increase, so it must become configurable too.**
- **Extraction** (`src/summarizers/schema.py`, `summarize_single` in `local.py`):
  - `EXTRACTION_SCHEMA` has `what_happened`, `entities`, `numbers` (both `maxItems: MAX_LIST_ITEMS` = 10), `event_date`, `status`, `is_roundup`.
  - `parse_extraction(raw, max_words)` validates and trims `what_happened` with `trim_words` (≤ 2 sentences, ≤ `max_words`).
  - Config lives on `OllamaConfig` in `src/config.py`: `extract_max_words` (env `EXTRACT_MAX_WORDS`, default 50) and `extract_num_predict` (env `EXTRACT_NUM_PREDICT`, default 600).
  - Article text is capped by `extraction_max_chars(config)`, which depends on `num_ctx` and `num_predict`.
- **The validator** (`src/validators/grounding.py`, `ValidatorRun`) checks `what_happened` against title + text in `run_pipeline()` before `db.save_extraction(...)`. Its counts go to `last_run.json` via `_write_status(..., validator=...)`.
- **`run_pipeline()`** (`src/main.py`) logs `Stage 1:` … `Stage 4:` and records `start = datetime.now(timezone.utc)` at the top.
- On hub the run starts at 10:15 UTC, and atrade reads its signals at 11:30 UTC.

### Implement

1. **Config** (`src/config.py`):

   | Setting | Default | Notes |
   |---|---|---|
   | `READER_MAX_PER_CATEGORY` | 15 | The plan's Open Questions default |
   | `READER_MAX_TOTAL` | 120 | Replaces the hard 50 |
   | `RUN_DEADLINE_MINUTES` | 60 | |
   | `EXTRACT_MAX_WORDS` | **120** | Was 50 |
   | `EXTRACT_NUM_PREDICT` | **900** | Was 600: room for the longer `what_happened` plus `details`. Keep `extraction_max_chars` consistent. |

   Pass the reader caps into `select_balanced_articles` from `run_pipeline()`.

2. **Schema:** add `details`, an array of strings with `maxItems: 4`. These are short factual sentences beyond `what_happened`.
   - Each item is ≤ 1 sentence and ≤ 30 words. Use `trim_words(item, 30)` and drop items that end up empty.
   - The field is required in the schema. `parse_extraction` validates it like the other lists.
   - Prompt rule: `details`, up to 4 more facts from the article (figures, who, when, what's next), each one sentence, no repeats of `what_happened`, facts only.
   - Store `details` in the `extractions.json` (already the full dict). `articles.summary` stays `what_happened`.

3. **The validator covers `details` too.** Each detail sentence is checked against the same title + text source in `run_pipeline()`. Failing details are dropped (enforce), and their counts go into the same `validator` metadata. An article is "fully dropped" only when `what_happened` is fully dropped (details alone don't save it).

4. **Briefing input** (`_briefing_summaries` / `generate_daily_briefing`): give the briefing model `what_happened` plus the surviving `details` for each article, so section summaries can be longer. Keep the `[id]` references and the existing `BRIEFING_SCHEMA`. Read the details from the `extractions` table, and add a DB helper if needed.

5. **Stage timing** in `run_pipeline()`:
   - Measure each stage with `time.monotonic()`: fetch, process, score, signals, summarize, briefing, publish.
   - At the end, log one INFO line with the total and per-stage seconds.
   - Add `timing: {"total_s": ..., "stages": {...}}` to `last_run.json`.
   - If the total exceeds `RUN_DEADLINE_MINUTES`, log a WARNING naming the deadline and the slowest stage.
   - Make the clock injectable (e.g. a module-level `_clock = time.monotonic` that tests monkeypatch, or a small `StageTimer` class taking a clock), so the deadline test uses a stub clock.

### Tests (`tests/test_reader_coverage.py`)

- **Selection:**
  - 20 eligible articles in one category with `READER_MAX_PER_CATEGORY=12` select exactly 12, highest interest first (use an interests fixture as `tests/test_interest_filter.py` does).
  - `READER_MAX_TOTAL` caps across categories.
- **Extraction:**
  - Stub JSON with 5 details keeps 4.
  - An over-long detail is trimmed to 30 words.
  - A detail with an ungrounded number is dropped in `enforce` mode and counted in `last_run.json`.
- **Briefing:** the recorded briefing prompt contains an article's surviving detail text.
- **Timing:**
  - `last_run.json` has `timing.total_s` and all stage keys.
  - With a stub clock that jumps past `RUN_DEADLINE_MINUTES`, a WARNING is logged (`caplog`).
- **Config:** the defaults are as in the table above.
- **Golden test:** `tests/test_signals_golden.py` must pass **unchanged**.

Update existing extraction tests only where the new defaults (120 words, 900 predict, the `details` field) legitimately change an assertion. Name them in the commit body. Extend `tests/stub_ollama.py`'s canned extraction with `details`.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/config.py`, `src/main.py`, `src/summarizers/local.py`, `src/summarizers/schema.py`, `src/summarizers/cloud.py` (briefing input only), `src/db.py` (read helper only)
  - `src/validators/grounding.py` (only if a helper is needed)
  - `tests/stub_ollama.py`, `tests/test_reader_coverage.py`, plus the minimal existing-test updates
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- Do not change signals, lanes behaviour, `docs/SIGNAL-SCHEMA.md` or `config/ticker_map.toml`.
- No personal or location details anywhere. Fixtures are synthetic.
- Commit message:
  ```
  feat: reader-coverage — more articles, richer facts, timed runs

  READER_MAX_PER_CATEGORY (15) and READER_MAX_TOTAL (120) replace the fixed
  8/50 caps. Extraction gains up to 4 validated `details` and a 120-word
  what_happened, fed to the briefing. Each run logs and records per-stage
  timing and warns past RUN_DEADLINE_MINUTES.
  ```
