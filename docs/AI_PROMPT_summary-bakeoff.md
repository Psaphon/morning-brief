# AI Development Prompt — summary-bakeoff

**Branch:** `feature/summary-bakeoff`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: summary-bakeoff` for the acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

`morning-brief bakeoff --model NAME --date YYYY-MM-DD` scores a **local** Ollama model's section writing against that day's stored Claude sections. It uses the same input, the same citation format and the same chunk validator, so the "switch back to local" decision is a measurement.

### Facts about the current code (verified by the PM)

- **`src/summarizers/claude_writer.py`:**
  - `build_request(config, section, articles)` builds the Messages API body (system prompt with the citation rules, and user content with each article's id, title, source, `what_happened` and `details`).
  - `split_cited_chunks(text)` and `check_section(...)` split a reply at `[[id]]` markers and validate every chunk. They count drops as `uncited`, `unknown_id`, `number`, `name` and `banned`.
- **`src/summarizers/writer.py`:**
  - `write_sections(db, config, validator)` groups the briefing input by section (`_group_by_section`).
  - It saves segments `{topic, text, cited_text, source_article_ids, writer}` through `db.save_briefing(date, rendered, model, segment_map_json)`.
  - It falls back to local per section when the drop share exceeds `WRITER_MAX_DROP_SHARE`.
- **The briefing input** comes from `src/summarizers/local.py`: `_briefing_summaries` / `with_details`. It is reader-lane only, and roundups and failed extractions are excluded. Extractions live in the `extractions` table.
- **The local model call pattern** is in `generate_section_local` (`/api/generate`, `think: False`, `num_ctx: config.briefing_num_ctx`).

### Implement

1. **Writer stats, so Claude's side is measurable.** In `writer.py`, add `stats: {"chunks": N, "dropped": {...reasons...}}` to each segment written by Claude, taken from `check_section` before dropped chunks are removed. This is a small additive change: old segments without `stats` must still load and render.

2. **`src/summarizers/bakeoff.py`:**
   - **Input:** `load_day(db, date)` rebuilds that date's briefing input, using the same reader-lane, roundup and failed-extraction rules as the live writer, from the stored articles and `extractions` (articles fetched on that UTC date). Add a read helper to `src/db.py` if needed. If there are no stored extractions for that date, raise a clear error, and the CLI exits 1 with the message.
   - **Generation:** for each section, `local_section_reply(ollama_config, model, section, articles)` sends the **same** system and user text as `build_request` to Ollama `/api/generate` as plain text (no JSON `format`), with the model overridden by `--model`.
   - **Scoring, per section:**
     - **Pass rate:** run the reply through `split_cited_chunks` + `check_section`, then kept chunks / total chunks.
     - **Citation coverage:** distinct valid cited ids / articles in the section.
     - **Words:** words of the kept prose.
     - **Fallback:** true if the drop share > `WRITER_MAX_DROP_SHARE`, or the call failed.
   - **Claude side:** the day's stored segments with `writer == "claude"`.
     - Pass rate comes from `stats` (`n/a` if absent).
     - Coverage comes from `source_article_ids` / section articles.
     - Words come from `text`.
     - Sections stored as `"local fallback"` count as Claude fallbacks.
     - If the day has no Claude sections, print `n/a` for that column and still score the local model.
   - **Totals:** mean pass rate, mean coverage, mean words and the fallback count, for each side.
   - **Report:** `write_report(...)` writes `data/bakeoff-<date>-<model>.json` (model name sanitized for the filename) with per-section and total scores for both sides.

3. **`src/cli.py`:** a `bakeoff` command with `--model` (required) and `--date` (default: today UTC). It prints a side-by-side table (section | local pass% cov% words fb | claude pass% cov% words fb, then a totals row), writes the JSON and exits 0. It never publishes, never writes `daily_briefings`, and **never calls the Claude API** (it doesn't load the credential at all).

### Tests (`tests/test_bakeoff.py`)

- Seed a temp DB with a day of articles and extractions in 2 sections, plus a stored briefing with one `claude` segment (with `stats`) and one `local fallback` segment.
- Use the Ollama stub (`tests/stub_ollama.py`, extended for plain-text bakeoff replies keyed by section) with **known** replies:
  - one fully cited and grounded;
  - one with an uncited chunk and a wrong number.
- **Assert:**
  - the exact pass rates, coverage, word counts and fallback flags;
  - the JSON file is written with those values, and the CLI exit code is 0;
  - the table has both columns.
- A date with no extractions exits 1 with a clear message (`CliRunner`).
- **No API call:** with the Claude stub server (`tests/stub_claude.py`) running and `WRITER_API_URL` pointed at it, it records **zero** requests during a bakeoff.
- A segment without `stats` gives `n/a` for Claude's pass rate.
- `tests/test_signals_golden.py` passes unchanged.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/summarizers/bakeoff.py`, `src/summarizers/writer.py` (stats only), `src/cli.py`, `src/db.py` (read helper only)
  - `tests/test_bakeoff.py`, `tests/stub_ollama.py` (bakeoff replies)
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- Do not change the validator rules, signals, lanes or the page.
- No personal or location details. Fixtures are synthetic.
- Commit message:
  ```
  feat: summary-bakeoff — score a local model's section writing against Claude

  `morning-brief bakeoff --model NAME --date D` reruns the day's sections on a
  local model with the same input, citation format and chunk validator as the
  Claude writer, and prints pass rate, citation coverage, words and fallbacks
  side by side with the stored Claude sections. No publish, no API calls.
  The writer now stores per-section chunk stats so Claude's side is measurable.
  ```
