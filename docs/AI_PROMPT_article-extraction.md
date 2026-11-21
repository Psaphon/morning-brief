# AI Development Prompt — article-extraction

**Branch:** `feature/article-extraction`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: article-extraction` for the full acceptance criteria, key decisions and notes.
Do NOT push — the host workflow handles push and PR.

## What to build

Replace the free-prose per-article summary with validated JSON facts from Ollama structured output.

### Facts about the current code (verified by the PM)

- **`summarize_single()`** (`src/summarizers/local.py`) posts to `/api/generate`:
  - with `SUMMARY_PROMPT`, `think: False` and options `temperature 0.3, top_p 0.9, num_predict 256`;
  - returning `SummaryResult(article_id, summary, model, elapsed_seconds, success, error)`;
  - retrying `MAX_RETRIES` times on timeout or HTTP error.
- **`summarize_articles()`** batches over it. `run_pipeline()` in `src/main.py` then calls `db.update_summary(article_id, summary, model)` for each success.
- **`generate_daily_briefing(db, config)`** builds its input from `db.get_todays_articles()`: every article with a non-empty `summary`, grouped by category. It already uses `"format": BRIEFING_SCHEMA` (see #69). Follow that pattern for the per-article schema.
- **`db.get_unsummarized_articles()`** selects `WHERE summary IS NULL`. Schema changes go through `Database._migrate()` (`ALTER TABLE ... ADD COLUMN`, see the existing entries) and the `CREATE TABLE IF NOT EXISTS` block.
- **`tests/stub_ollama.py`** returns `CANNED_SUMMARY` for any non-briefing generate call and records each request (`generate_calls`).
- **Signals** use all of today's articles via `emit_signals(filter_lane(todays, "signals", lanes))` and must not change.

### Implement

1. **`src/summarizers/schema.py`** (new):
   - `EXTRACTION_SCHEMA`: a JSON-schema object with all six fields required: `what_happened` (string), `entities` (array of string), `numbers` (array of string), `event_date` (string or null), `status` (enum `happened`/`scheduled`/`ongoing`), `is_roundup` (boolean).
   - `parse_extraction(raw: str, max_words: int) -> dict`. It raises `ExtractionError` when the reply is not valid JSON, is missing a field or has a wrong type, or when `event_date` is not `YYYY-MM-DD`/null.
   - `trim_words(text, max_words)`: cut at the last sentence boundary within the limit, and also keep at most 2 sentences. If the first sentence alone is over the limit, hard-cut at `max_words` words.

2. **`src/config.py`** adds these settings (env names exactly):

   | Setting | Default | Notes |
   |---|---|---|
   | `OLLAMA_NUM_CTX` | 8192 | |
   | `EXTRACT_TEMPERATURE` | 0.1 | |
   | `EXTRACT_SEED` | 42 | |
   | `EXTRACT_MAX_WORDS` | 50 | |
   | `EXTRACT_NUM_PREDICT` | 400 | |
   | `CHARS_PER_TOKEN` | 4 | Used to cap the article text so prompt + output fit `num_ctx`: `max_chars = (num_ctx - num_predict - 600) * chars_per_token`. Use that instead of the fixed `MAX_INPUT_CHARS` for extraction. |

   Put them on `OllamaConfig` or a new `ExtractConfig`, whichever fits the existing style. They are read from the environment like the others.

3. **`summarize_single`** becomes extraction:
   - **Prompt:** a new prompt with the title, source, category, the article's **published date** (`published_at`, else `fetched_at`; resolve relative dates against it) and the capped text.
   - **Prompt rules** (from Key Decisions): facts only (who did what, figures, when); copy names, tokens and companies exactly as written; no evaluation or impact language; `is_roundup` true for digest or roundup articles that cover several unrelated stories.
   - **Request:** `"format": EXTRACTION_SCHEMA`, `think: False`, and options `num_ctx`, `temperature`, `seed`, `num_predict` from config.
   - **Malformed or invalid JSON:** retry **once**. A second failure returns a result marked `extraction_failed`, and the article id is logged at WARNING.
   - **Never fall back to the raw article text.**
   - **Result:** extend `SummaryResult` (or add a field) so it carries the parsed extraction dict and an `extraction_failed` flag. `summary` = the trimmed `what_happened`.

4. **`src/db.py`:**
   - New table `extractions (article_id INTEGER PRIMARY KEY REFERENCES articles(id), json TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL)`.
   - New `articles` columns via `_migrate`: `extraction_failed INTEGER NOT NULL DEFAULT 0` and `is_roundup INTEGER NOT NULL DEFAULT 0`.
   - `save_extraction(article_id, extraction, model)`: upserts the row and sets `summary = what_happened`, `summary_model` and `is_roundup`.
   - `mark_extraction_failed(article_id)`.
   - `get_unsummarized_articles` must skip `extraction_failed = 1`, so a failed article isn't retried every run.
   - `get_todays_articles` must return `is_roundup` and `extraction_failed`.

5. **`src/main.py`:** in the summarize loop, call `save_extraction` on success and `mark_extraction_failed` on an extraction failure, in place of `update_summary`. The run still exits 0.

6. **Briefing input:** in `generate_daily_briefing` (and `generate_daily_briefing_claude` in `src/summarizers/cloud.py`, if it builds input the same way), skip articles with `is_roundup` or `extraction_failed`.
   - Signals are untouched: roundups still appear in signals output.
   - Keep the `ReaderLaneDB` wrapper from feed-lanes working. It delegates every attribute except `get_todays_articles`.

7. **`tests/stub_ollama.py`:**
   - Canned extraction JSON for article calls (detect them by a marker in the new prompt, as `BRIEFING_MARKER` does).
   - A way for a test to make the stub return malformed JSON for chosen calls, e.g. `stub.malformed_next(n)` or a per-title marker.
   - Keep existing tests passing.

### Tests (`tests/test_extraction.py`)

- The recorded generate request carries `format == EXTRACTION_SCHEMA`, and the `num_ctx`/`temperature`/`seed` from config (set them via env or config in the test), and the article's published date in the prompt.
- A fixture run with valid stub JSON gives one `extractions` row per article, and `articles.summary == what_happened`.
- Malformed JSON twice for one article gives `extraction_failed = 1`, no `extractions` row, absence from the briefing input, the article id in the log, and no exception. The run continues for the other articles.
- `trim_words` cases: under the limit is unchanged; a cut at the sentence boundary; a 3-sentence input keeps 2; a single over-long sentence is hard-cut.
- `is_roundup: true` is absent from the briefing prompt and still present in the signals output (use the existing signals fixture style).
- `parse_extraction` rejects missing fields, wrong types, a bad `event_date` and a bad `status`.
- `tests/test_signals_golden.py` must pass **unchanged**. Don't edit it or its fixtures.

No network in tests; use the stub. Follow `tests/test_briefing.py` and `tests/test_feed_lanes.py` style.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/summarizers/local.py`, `src/summarizers/schema.py`, `src/summarizers/cloud.py` (input filter only)
  - `src/config.py`, `src/db.py`, `src/main.py`
  - `tests/stub_ollama.py`, `tests/test_extraction.py`
  - Existing tests that assert the old prose-summary request shape: update only those assertions, and say which ones in the commit body
  - `docs/DEVPLAN.md` (status line only: `In Progress`)
- Do not change `docs/SIGNAL-SCHEMA.md`, `src/publishers/signals.py`, the lanes module's behaviour or `config/ticker_map.toml`.
- No personal or location details in code, prompts, fixtures or the commit message. Fixtures are synthetic.
- Commit message:
  ```
  feat: article-extraction — validated JSON facts per article

  summarize_single now asks Ollama for a schema-constrained extraction
  (what_happened, entities, numbers, event_date, status, is_roundup), stored in
  a new extractions table; summary holds what_happened so the page keeps working.
  Malformed replies retry once, then the article is marked extraction_failed and
  left out of the briefing. Roundups are kept out of the briefing input only.
  ```
