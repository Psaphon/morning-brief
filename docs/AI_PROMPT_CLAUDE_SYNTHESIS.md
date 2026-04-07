# AI Development Prompt — Claude Synthesis Feature

**Branch:** `feature/claude-synthesis`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` Feature: claude-synthesis for full acceptance criteria. Make one commit for this feature. Do NOT push — the host workflow handles push and PR.

---

## Feature: claude-synthesis

Optional upgrade: when `ANTHROPIC_API_KEY` is set, replace the Ollama daily briefing with a Claude-generated cross-topic narrative with higher reasoning quality.

### What to build

1. **`src/summarizers/cloud.py`** — Implement `generate_claude_briefing(db, config)`:
   - Use httpx to call Claude API directly (POST to `https://api.anthropic.com/v1/messages`)
   - Headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`
   - Send all today's summaries grouped by category as a user message
   - Prompt for a 3-5 paragraph narrative connecting themes across categories
   - Use the same progressive-disclosure style as the Ollama briefing (see `_build_briefing_prompt` in `src/summarizers/local.py` for the tone and structure to match)
   - Gated behind `ANTHROPIC_API_KEY` env var — return None if not set
   - Max ~4000 tokens input to keep costs low
   - Store result in the same `daily_briefings` table (reuse `db.save_briefing()`)

2. **`src/config.py`** — Add `ANTHROPIC_API_KEY` as an optional config field (default None)

3. **`src/main.py`** — Try Claude synthesis first, fall back to Ollama briefing if key not set or call fails

4. **`tests/test_cloud.py`** — Test prompt construction, graceful skip when no key, mock API response

**Commit as:** `feat: add optional Claude API synthesis for higher-quality briefings`

---

## Rules

- Run `ruff check . && ruff format --check .` before committing. Fix all issues.
- Run `python -m pytest tests/ -v` before committing. All tests must pass.
- Do NOT push — the host workflow handles push and PR creation.
- Do NOT modify files outside the scope of this feature.
- Use `logging` not `print`. Use `httpx` for HTTP. Use `pathlib.Path` for paths.
