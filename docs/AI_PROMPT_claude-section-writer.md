# AI Development Prompt — claude-section-writer

**Branch:** `feature/claude-section-writer`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: claude-section-writer` for the acceptance criteria and key decisions. The plan is in `docs/plans/2026-11-20-brief-reader-v2.md`; see its Constraints: credential and content sent off-box.
Do NOT push — the host workflow handles push and PR.

## What to build

An optional writer that asks the Claude Messages API for one long, cited summary per reader section, built **only** from validated extractions. It checks every chunk in code and falls back to the local model per section. A spend guard caps it.

**The default stays `SUMMARY_WRITER=local`. With the default, the run must be byte-for-byte the same path as today.**

### Facts about the current code (verified by the PM)

- **The briefing today:** `generate_daily_briefing(db, config, validator)` in `src/summarizers/local.py` makes one Ollama call over all reader articles.
  - The input comes from `_briefing_summaries` / `with_details`: `what_happened` + `details` per article, `[id]` references. Roundups and failed extractions are excluded.
  - The output is `BRIEFING_SCHEMA` segments `{topic, text, source_article_ids}`.
  - The output is validated by `validate_briefing(...)` and saved with `db.save_briefing(today, rendered, model, segment_map_json)`.
  - `run_pipeline()` reads `segment_map` back for the page.
- **`src/summarizers/cloud.py`** has an older whole-brief Claude path, `generate_daily_briefing_claude`. It is keyed by **`ANTHROPIC_API_KEY` in the environment** (`config.api_keys.anthropic`; `run_pipeline()` tries it first). That breaks the plan's credential rule (never in env files). **Remove that env path:**
  - delete `api_keys.anthropic`;
  - delete the `ANTHROPIC_API_KEY` branch in `run_pipeline()`;
  - delete `generate_daily_briefing_claude` (or reduce `cloud.py` to shared helpers);
  - update or remove its tests, and say which in the commit body.
- **The validator:** `src/validators/grounding.py` has `validate_text(text, source, *, allowlist, phrases)` and `ValidatorRun` (mode, lists, counts). `last_run.json` is written by `_write_status(..., validator=..., timing=...)` in `src/main.py`.
- **The container:**
  - `scripts/run-pipeline.sh` runs `docker compose up --build ...` under rootless Docker.
  - `docker-compose.yml` mounts `${HOME}/.config/morning-brief` read-only at `/app/interests`.
  - Alerts come from the host side: `scripts/notify-failure.sh` uses `/usr/local/sbin/hub-alert`, and `scripts/health-check.sh` is an `ExecStartPost` of `morning-brief.service`.
- **Reader sections** are the article `category` values in the briefing input.

### Implement

1. **Config** (`src/config.py`, a new `WriterConfig`):

   | Setting | Default | Notes |
   |---|---|---|
   | `SUMMARY_WRITER` | `local` | `local` or `claude`; unknown → `local` with a WARNING |
   | `WRITER_MODEL` | `claude-sonnet-5-5` | |
   | `WRITER_API_URL` | `https://api.anthropic.com/v1/messages` | Tests point it at the stub |
   | `WRITER_MIN_WORDS` / `WRITER_MAX_WORDS` | 250 / 450 | Per section |
   | `WRITER_MAX_DROP_SHARE` | 0.3 | |
   | `WRITER_TIMEOUT_S` | 120 | |
   | `WRITER_MONTHLY_CAP_USD` | 10 | |
   | `WRITER_PRICE_IN_PER_MTOK` / `WRITER_PRICE_OUT_PER_MTOK` | **none** | Prices are host config, not repo facts. With either unset, `claude` refuses to run: one WARNING, then local. |

2. **Credential:**
   - Read the key from `$CREDENTIALS_DIRECTORY/anthropic_api_key`, stripping whitespace.
   - When the variable, the file or the content is missing, fall back to local with **one** clear INFO/WARNING line that names the credential, not its value.
   - Never log the key or put it in an exception message. Wrap the read so a traceback can't include it.
   - **Container passthrough:**
     - Add `docker-compose.credentials.yml`, an override that bind-mounts `${CREDENTIALS_DIRECTORY}` read-only at `/run/credentials` and sets `CREDENTIALS_DIRECTORY=/run/credentials` in the container.
     - `scripts/run-pipeline.sh` adds `-f docker-compose.yml -f docker-compose.credentials.yml` **only when** `"$CREDENTIALS_DIRECTORY/anthropic_api_key"` exists on the host. Otherwise it runs exactly as today.
   - The host-side `LoadCredential=` drop-in is **not** in this repo. The PM adds it in the hub repo when the key exists. Document that in the README.

3. **`src/summarizers/claude_writer.py`:**
   - **Request:** one Messages API call per section (`x-api-key`, `anthropic-version: 2023-06-01`, JSON).
     - The system prompt gives the citation rules: every sentence or chunk drawn from sources ends with `[[id]]` or `[[id1,id2]]`; only the given ids; facts only, no evaluation or filler; `WRITER_MIN_WORDS`–`WRITER_MAX_WORDS` words of prose paragraphs; no headings.
     - The user content is the section name plus, per article: id, title, source, `what_happened` and `details`. **Nothing else: no full text, no URLs needed.**
   - **Response handling:**
     - Parse `content[0].text` and `usage.input_tokens` / `usage.output_tokens`.
     - `split_cited_chunks(text) -> list[(chunk_text, [ids])]`: split after each `[[...]]` marker. Trailing text with no marker is a chunk with `[]`.
     - Validate each chunk against the union of its cited articles' title + `what_happened` + `details` (extend `grounding.py` with a helper that takes several sources). Drop chunks that have no citation, cite an unknown id, or fail validation, each counted under its own reason in run metadata (`writer: {sections, claude, fallback, dropped: {uncited, unknown_id, number, name, banned}, tokens_in, tokens_out, cost_usd}`).
     - If dropped / total > `WRITER_MAX_DROP_SHARE`, or on an HTTP error, timeout or bad JSON, return a fallback signal for that section.

4. **`src/summarizers/writer.py`:** `write_sections(db, config, validator) -> briefing`.
   - `local`: call the existing `generate_daily_briefing` unchanged.
   - `claude`: group the briefing input by section and call the Claude writer per section.
     - **A failed section falls back to local for that section only:** a local Ollama call over that section's articles using the existing `BRIEFING_SCHEMA` style and `validate_briefing`. If even that fails, use a plain list of the articles' `what_happened` with `[[id]]`.
     - Save the result through `db.save_briefing`. Each section becomes a segment `{topic: section, text: <prose without [[...]] markers>, cited_text: <prose with markers>, source_article_ids: [...], writer: "claude" | "local fallback"}`, so the current page keeps rendering. `cited-summary-page` will use `cited_text` later.
   - `run_pipeline()` calls `write_sections` in place of the current briefing block.

5. **Spend guard** (`src/db.py`):
   - Add an `api_usage (id, ts, model, tokens_in, tokens_out, cost_usd)` table.
   - Each call records usage priced from config.
   - Before each call, compute the month-to-date total (UTC month). If it is at or above the cap, the run uses local for all remaining sections until the month rolls over. Log one WARNING, and set `alerts: ["writer spend cap reached YYYY-MM"]` in `last_run.json` **only the first time that month**: store a marker row or a meta key.
   - **`scripts/health-check.sh`** (host side): when `last_run.json` has non-empty `alerts`, send each one once through `/usr/local/sbin/hub-alert` if it is executable, as `notify-failure.sh` does. Skip silently otherwise. Keep its existing checks unchanged.

### Tests (`tests/test_claude_writer.py`)

Use a stub HTTP server on `127.0.0.1` (like `tests/stub_ollama.py`) that records request bodies and returns canned Messages API JSON with `usage`. Cases:
- **Valid cited output:** the section is saved with `writer: "claude"`, the markers are removed from `text` and kept in `cited_text`, and usage is recorded.
- **An uncited chunk, an unknown id and a wrong number in a chunk:** each is dropped and counted under its reason.
- **Drop share above the limit:** that section falls back to local (with the Ollama stub), and the other sections stay `claude`.
- **API 500 and timeout:** per-section fallback.
- **Cap:** pre-seed `api_usage` to the cap. Every section is local, the WARNING is logged once, `alerts` is set once, and a second run in the same month does not set it again.
- **Missing credential, empty file and missing prices:** local, and the logs don't contain the fake key (set a recognizable fake key in the tests and assert it's absent from `caplog.text` and from any exception text).
- **Request bodies:** walk every recorded request and assert that no article `full_text` (fixture texts are long and distinctive) appears, and that no URL appears.
- **`SUMMARY_WRITER=local`:** the existing briefing tests pass unchanged, with no request to the API stub.
- **Container switch:** `scripts/run-pipeline.sh` adds the override only when the credential file exists. Test it with a fake `docker` on `PATH` in `tmp_path`.
  - **On hub, `/tmp` is noexec.** Make the fake an executable shell script, and skip the test if it can't execute there (see how existing script tests handle it, e.g. `tests/test_run_pipeline.py`).
- **Golden test:** `tests/test_signals_golden.py` passes unchanged.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/summarizers/claude_writer.py`, `src/summarizers/writer.py`, `src/summarizers/cloud.py`, `src/summarizers/local.py` (a per-section local helper only)
  - `src/validators/grounding.py`, `src/db.py`, `src/config.py`, `src/main.py`
  - `docker-compose.credentials.yml`, `scripts/run-pipeline.sh`, `scripts/health-check.sh`, README
  - `tests/test_claude_writer.py`, a stub helper under `tests/`, and the tests of the removed env path
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- **Never put a real key, price or account detail anywhere.** Fixtures use a fake key like `sk-test-NOT-A-REAL-KEY-123`.
- Do not change signals, lanes, `docs/SIGNAL-SCHEMA.md` or `config/ticker_map.toml`.
- No personal or location details.
- Commit message:
  ```
  feat: claude-section-writer — cited section summaries from validated extractions

  SUMMARY_WRITER=claude writes each reader section with one Messages API call
  over extractions only (no full text), splits the reply at [[id]] citations
  and validates every chunk; sections that lose too much, error or time out
  fall back to local. The key comes from $CREDENTIALS_DIRECTORY, usage is
  priced into api_usage, and a monthly cap switches to local and alerts once.
  Removes the old ANTHROPIC_API_KEY env path. Default stays local.
  ```
