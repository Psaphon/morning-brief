# AI Development Prompt — cited-summary-page

**Branch:** `feature/cited-summary-page`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: cited-summary-page` for the acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Render the brief as one long summary made of sections, with tappable superscript citations and a collapsed sources list per section.

### Facts about the current code (verified by the PM)

- **`render_dashboard(...)`** in `src/publishers/html.py` renders `templates/dashboard.html` (Jinja2).
  - It passes `briefing` (plain text), `briefing_segments_json = json.dumps(briefing_segments or [])` and `articles_by_id_json`.
  - The template embeds both JSON blobs in `<script type="application/json">` tags with `| safe`.
  - **Client-side JS builds the segments:** `segment-topic` header, chevron, `segment-text` paragraphs set with `textContent`, then `buildSourceArticles(seg.source_article_ids, articlesById)` and `buildActionButtons(...)`.
  - `#briefing-fallback` shows the plain text when there are no segments.
- **Segments now come from `write_sections`** (`src/summarizers/writer.py`, claude-section-writer, #83 or newer). Each is `{topic, text, cited_text, source_article_ids, writer}`:
  - `text` has no markers;
  - `cited_text` holds `[[id]]` / `[[id1,id2]]` markers;
  - `writer` is `"claude"` or `"local fallback"`.
  - **With the default `SUMMARY_WRITER=local`, segments come from the old `generate_daily_briefing`, with no `cited_text` and no `writer`.** That shape must keep rendering exactly as today.
- **Articles** carry `id`, `title`, `source`, `url`, `summary` (= `what_happened`). Interest labels already render on bullets ("On radar", via `interests`). `dates-ahead` and the "For the stack" label are not built yet: just don't break the hooks the template has.
- **Security bug to fix while here:** `json.dumps(...)` inside `<script>` with `| safe` doesn't escape `</`. An article title containing `</script>` would end the script element and inject markup.
  - Fix it for **both** JSON blobs: escape `<`, `>` and `&` as `<`, `>`, `&` (a small `_script_json(obj)` helper in `html.py`).
  - Add a test with a hostile title.

### Implement

1. **Segments with `cited_text`** (in the template JS):
   - Render the section title, then the prose paragraphs built from `cited_text`.
   - Each `[[...]]` marker becomes a small `<sup>` with one `<a>` per id: `href` = that article's `url`, `title` = its source name, text = its 1-based number within the section's sources list. Open in a new tab with `rel="noopener noreferrer"`.
   - Build with DOM APIs (`createElement`/`textContent`), never `innerHTML` with article text.
   - An id not found in `articlesById` renders no link, and no raw `[[` is left.
   - A `writer: "local fallback"` section shows a small muted "local fallback" note under its title.
2. **"Sources (N)":** below each such section, a collapsed `<details>` element with `<summary>Sources (N)</summary>` listing every cited article in first-cited order: title (linked), source, `summary`/`what_happened`.
3. **Old-shape segments** (no `cited_text`) render exactly as today.
4. **Server-side check data:** to make the criteria testable without a browser, add a pure Python function in `html.py`, `build_cited_sections(segments, articles_by_id) -> list[dict]`. It returns for each section `{topic, writer, paragraphs: [[{"text": ...} | {"cite": [n, ...]}]], sources: [{n, id, title, source, url, what_happened}]}`.
   - Pass its JSON to the template, using the escaped helper, and have the JS render from it.
   - Then the tests check this structure and the rendered HTML string.
5. **CI HTML check:**
   - Add `html5validator` pinned in a dev or test requirements file. It needs Java; check the CI image. If Java isn't available, use `html5lib` strict parsing (pinned) to the same effect, and say which in the commit body.
   - Add a test or CI step that renders a fixture page with cited sections and validates it.
   - The required CI job must run it, so it gates. Edit `.github/workflows/ci.yml` only for that step.

### Tests (`tests/test_cited_page.py`)

- For a fixture with 2 sections (one `claude`, one `local fallback`) and 5 articles:
  - every citation in `build_cited_sections` maps to an article cited in that section;
  - numbers are 1..N in first-cited order;
  - every cited article appears in that section's sources;
  - an unknown id yields no cite entry.
- **Rendered HTML:** no `[[` anywhere, a `<details>` with `Sources (` per cited section, and "local fallback" present once.
- **Old-shape segments:** the page renders as before (compare the JSON passed to the template with the current shape).
- **Hostile title** `</script><img src=x onerror=alert(1)>`: the rendered HTML contains no raw `</script><img` sequence, and the JSON still round-trips.
- The HTML validates (step 5).
- `tests/test_signals_golden.py` passes unchanged.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `templates/dashboard.html`, `src/publishers/html.py`
  - `tests/test_cited_page.py`, a test requirements pin
  - `.github/workflows/ci.yml` (only the validation step)
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- Do not change the writer, the validator, signals or the DB.
- No personal or location details. Fixtures are synthetic.
- Commit message:
  ```
  feat: cited-summary-page — sections with superscript citations and sources

  Sections from the section writer render as prose with [[id]] citations turned
  into numbered superscript links and a collapsed "Sources (N)" list; local
  fallback sections are marked. Old-shape segments render as before. Embedded
  page JSON now escapes <, > and & so article text can't close the script tag.
  ```
