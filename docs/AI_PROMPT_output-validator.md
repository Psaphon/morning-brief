# AI Development Prompt — output-validator

**Branch:** `feature/output-validator`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: output-validator` for the full acceptance criteria, key decisions and the seed banned-phrase list (Notes).
Do NOT push — the host workflow handles push and PR.

## What to build

A code-only grounding check that runs before anything is rendered. Every generated sentence is checked against its source text, and sentences that fail are dropped (never rewritten).

### Facts about the current code (verified by the PM)

- **Per-article extraction** (article-extraction, #77):
  - `summarize_single()` in `src/summarizers/local.py` returns a result carrying the parsed `extraction` dict and `summary` = trimmed `what_happened`.
  - `run_pipeline()` in `src/main.py` then calls `db.save_extraction(article_id, extraction, model)` (writes the `extractions` row and sets `articles.summary`) or `db.mark_extraction_failed(article_id)`.
  - Article text is capped for the prompt by `extraction_max_chars(config)`; `MAX_INPUT_CHARS = 12_000` is the older fixed cut.
- **Briefing:** `generate_daily_briefing()` builds a structured briefing:
  - `BRIEFING_SCHEMA`: `{"segments": [{"topic", "text", "source_article_ids"}]}`.
  - It is parsed by `_parse_briefing_response()` and saved with `db.save_briefing(today, rendered, model, segment_map_json)`.
  - `run_pipeline()` reads `segment_map` back for the page.
  - The briefing input skips articles with `is_roundup` or `extraction_failed` (helper `_briefing_summaries`).
- **No "section lede" exists yet.** That comes from the later `category-briefing` feature. Its current equivalent is each briefing **segment's `text`**. The source for a segment is the `what_happened` texts (plus titles) of its `source_article_ids`.
- **Run metadata** is `_write_status()` in `src/main.py`, which writes `data/last_run.json` with `**extra` fields.
- Feed lanes: the briefing reads the DB through `ReaderLaneDB`. Keep that working.

### Implement

1. **`src/validators/__init__.py`** and **`src/validators/grounding.py`.** Follow the DEVPLAN criteria exactly:
   - `split_sentences(text)`: reuse or mirror `trim_words`' splitting in `src/summarizers/schema.py` so "U.S. officials" stays one sentence.
   - `check_numbers(sentence, source)`:
     - Each digit group (commas stripped) in the sentence must appear in the source.
     - Normalize money/scale so "$44.6 million" matches a source containing "$44.6M" (digit group `44.6` present). "$45.6 million" fails.
     - Years and plain integers count as digit groups too.
   - `check_names(sentence, source, allowlist)`: each capitalized token that is not sentence-initial, has 3+ characters and is not in the allowlist must match the source case-insensitively on its first `max(4, len-2)` characters. "Democrats" vs "Democratic" passes. "Christie's" vs a source without it fails. Strip possessive `'s` and punctuation before matching.
   - `check_banned(sentence, phrases)`: a case-insensitive substring match.
   - `validate_text(text, source, *, allowlist, phrases) -> ValidationResult`: kept sentences, dropped sentences with a reason (`number` | `name` | `banned`), and counts. The first failing check, in that order, is the reason.
   - **Loaders:** `load_banned_phrases()` reads `config/banned_phrases.txt` plus `$MORNING_BRIEF_BANNED_OVERLAY` if that file exists (missing → no error, no log noise beyond DEBUG). `load_name_allowlist()` reads `config/name_allowlist.txt`. Anchor default paths to the repo root, as `src/processors/lanes.py` does (`Path(__file__).resolve().parents[2]`), so they work in the container.

2. **`config/banned_phrases.txt`:** the seed list from the DEVPLAN Notes, one per line. **`config/name_allowlist.txt`:** months, weekdays, common sentence words ("The", "This", "A", "In", "On", "After", "But", "And"), and common acronyms ("US", "U.S.", "UK", "EU", "UN", "AI", "CEO", "GDP"). Lines starting with `#` are comments.

3. **`src/config.py`:** `VALIDATOR_MODE` (`enforce` | `warn` | `off`, default `enforce`, unknown → `enforce` with a WARNING) and `MORNING_BRIEF_BANNED_OVERLAY`. Add the overlay env line to `docker-compose.yml`: `MORNING_BRIEF_BANNED_OVERLAY=/app/interests/banned-phrases.txt`. The directory is already mounted.

4. **Wire it into `run_pipeline()`:**
   - **After extraction, before `save_extraction`:** validate `what_happened` against title + article text (the same cut as extraction).
     - `enforce`: save the kept sentences as `what_happened`/`summary`. If **every** sentence is dropped, don't save a summary; mark the article so it is left out of the briefing. Reuse `mark_extraction_failed` or add a `validation_failed` flag, and say which in the commit body.
     - `warn`: keep everything and log a WARNING per failing sentence.
     - `off`: skip.
   - **After the briefing is parsed, before it is saved/rendered:** validate each segment's `text` against its cited articles' titles + `what_happened`. Implement this inside `generate_daily_briefing` (and the Claude path in `src/summarizers/cloud.py` if it produces segments) or as a post-step in `run_pipeline`, whichever keeps `segment_map` and the rendered text consistent.
     - A segment whose text is fully dropped is removed.
     - A segment citing no known article id is checked against all of that run's briefing input.
   - **Run metadata:** add to `_write_status(..., validator={"mode": ..., "checked": N, "dropped": {"number": a, "name": b, "banned": c}})` for the run. Log one INFO summary line.

5. **Signals are untouched.** Validation never changes `emit_signals` input.

### Tests (`tests/test_validator.py`)

- Every unit case named in the DEVPLAN criteria (numbers, names, banned phrase, missing overlay).
- **Fixture run with the stub:** make one extraction's `what_happened` contain an ungrounded number (extend `tests/stub_ollama.py`'s canned extraction controls, e.g. a per-title override).
  - `enforce`: the stored summary drops that sentence and `last_run.json` reports `dropped: {"number": 1, ...}`.
  - `warn`: it is kept and a warning is logged (`caplog`).
- An all-dropped extraction is absent from the briefing prompt.
- A briefing segment with an ungrounded sentence is removed from both the rendered text and `segment_map` in `enforce` mode.
- `VALIDATOR_MODE=off` changes nothing.
- `tests/test_signals_golden.py` must pass **unchanged**. Don't edit it or its fixtures.

No network. Follow `tests/test_extraction.py` style.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/validators/`, `config/banned_phrases.txt`, `config/name_allowlist.txt`
  - `src/config.py`, `src/main.py`, `src/summarizers/local.py`, `src/summarizers/cloud.py`, `src/db.py` (only if adding a flag)
  - `docker-compose.yml`
  - `tests/stub_ollama.py`, `tests/test_validator.py`
  - Existing tests only where the new validation legitimately changes an assertion; name them in the commit body
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- Do not change `src/publishers/signals.py`, `docs/SIGNAL-SCHEMA.md` or `config/ticker_map.toml`.
- No personal or location details in code, config lists, fixtures or the commit message. Fixtures are synthetic.
- Commit message:
  ```
  feat: output-validator — drop ungrounded or filler sentences before render

  Every generated sentence (per-article what_happened, briefing segment text) is
  checked in code against its source: digit groups must appear, capitalized names
  must match, banned filler phrases are rejected. VALIDATOR_MODE enforce drops the
  sentence, warn logs it; counts go to last_run.json.
  ```
