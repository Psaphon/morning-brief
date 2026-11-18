# Plan: brief-quality

**Plan type:** features
**Target repo:** morning-brief
**Part:** 1 of 1
**Created:** 2026-11-18

## PM Kickoff

- **First action:** Add features 1–8 below to morning-brief's `docs/DEVPLAN.md` in order, after anything already queued. The two **companion features at the end go elsewhere**: `signal-direction-reader` to atrade's DEVPLAN (it must merge before morning-brief's `signal-direction` ships), and `radar-brief-interests` to hub's DEVPLAN. Paths marked *(confirm)* are guesses: map them to the real files before queuing.
- **Tech radar status check (do this first):** `PLAN-tech-radar.md` (2026-10-04, `Plan type: features`, target hub) defined `radar-doc-and-cli`, `radar-phone-capture` and `radar-inbox-triage`. The operator believes they were never picked up. Report in hub's DEVPLAN PR whether each one is built, queued, or missing. If any are missing, ping the operator so the plan can be re-sent. `radar-brief-interests` depends on `radar-doc-and-cli`.
- **Run mode:** mixed.
  - Overnight: `signals-golden-test`, `output-validator`, `briefing-dedupe`, `interest-filter`, `dates-ahead`.
  - Supervised: `article-extraction` and `category-briefing`, because prompt quality has to be checked against live Ollama on hub. Also `signal-direction` and the atrade companion, because they are a cross-repo contract change.
- **Operator setup:** Write the interests file (see `interest-filter`) and add any personal banned phrases to the overlay file. Both happen after merge and neither one blocks the build.
- **Supersedes / overlaps:** Replaces the free-text prompt in `summarize_single` and the single-call `_build_briefing_prompt` (both in `src/summarizers/local.py`). Extends the signal-contract work in `docs/SIGNAL-SCHEMA.md`. Gives hub's tech radar its first automated reader: radar entries become briefing interests (`interest-filter` plus hub's `radar-brief-interests`).

## Open Questions

- {default: the seed list in `output-validator` Notes; the operator extends it later via the overlay file} Which phrases should be banned outright?
- {default: on (`LEDE_ENABLED=true`)} Should each section get a one-sentence lede above its bullets, or bullets only?
- {default: include, built last} Add the optional per-ticker `direction` field for atrade's shadow arm (`signal-direction` plus the atrade companion)? Answering "defer" moves both features to FEATURE-REQUESTS.
- {default: `enforce`} Should the validator drop failing sentences (`enforce`), or only log them (`warn`) for a first week of tuning?
- {default: keep the configured model} Evaluating a model upgrade stays a backlog item and is not part of this plan.

## Constraints

- **atrade boundary.** This plan does not change the scorer (`src/processors/scorer.py`) or its weights, fetching, retention, the ticker alias map, or the signals file's location, format or timing. The one exception is `signal-direction`. Selection, dedupe and filtering here only change which articles get **summarized and shown in the briefing**. Every article is still stored and scored exactly as today.
- **The signals golden test (feature 1) must pass unchanged in every PR** except `signal-direction`, which updates the golden file in the same PR as the contract bump.
- **No free text reaches atrade**, and `knowable_at` is derived only from article times, never from run time or `emitted_at`.
- **Context budget.** Every Ollama call sends `num_ctx` explicitly from config. Before sending, code estimates the prompt tokens (a conservative chars-per-token ratio from config). If prompt plus reserved output would exceed `num_ctx`, it drops the lowest-priority articles and logs a warning. It never sends an over-length prompt, because Ollama silently truncates the *start* of the prompt.
- **Determinism.** Extraction uses the temperature and seed from config (defaults: 0 and a fixed seed).
- **Public-repo hygiene.** Personal interests and personal banned-phrase additions live in uncommitted files referenced by env vars, and the committed examples are generic. Test fixtures are synthetic articles, never real article text.
- **Impermanent values live in config.** Model name, context size, word limits and day windows go in `src/config.py`, never as constants in prompts or code.
- **GPU window unchanged** (08:15 UTC run). Generate calls per run stay at or below `articles + categories + 1`.

## Features

## Feature: signals-golden-test

**Branch:** `feature/signals-golden-test`
**Depends on:** none
**Status:** Not Started
**Requires:** ai

### Goal

Pin today's signals output with an end-to-end test. Every later feature then has to prove it leaves atrade's input unchanged.

### Acceptance Criteria

- [ ] `tests/fixtures/brief_fixture/articles.json` holds at least 12 synthetic articles across at least 3 categories with fixed published times (UTC). At least 4 of them match ticker aliases from the real alias map. Two of them are a same-story pair (the same sale, the same distinctive figure in both titles), for `briefing-dedupe` to use later.
- [ ] `tests/test_signals_golden.py` runs the real CLI run command end-to-end against the fixture, using a temp data dir and DB. Ollama is replaced by the stub in `tests/stub_ollama.py`, which serves canned responses over HTTP and records every request. The test asserts that `data/signals-<date>.json` equals `signals.golden.json`, ignoring only `emitted_at`.
- [ ] A second test changes one fixture article's text so it no longer matches its ticker alias, and asserts the output now **differs** from the golden file. This proves the test is live.
- [ ] A separate test, `test_generate_call_count`, asserts the stub received exactly `articles + 1` generate calls (current behaviour). Later features update only this test.
- [ ] `python scripts/regen_golden.py` regenerates the golden file and exits 0. A failing run exits non-zero.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `tests/fixtures/brief_fixture/articles.json` | Create | Synthetic articles |
| `tests/fixtures/brief_fixture/signals.golden.json` | Create | Pinned signals output |
| `tests/stub_ollama.py` | Create | HTTP-level Ollama stub that records requests |
| `tests/test_signals_golden.py` | Create | Golden, liveness and call-count tests |
| `scripts/regen_golden.py` | Create | Regenerates the golden file explicitly |

### Key Decisions

- Stub at the HTTP layer so the real summarizer code runs: mocking functions would skip the boundary.
- The golden file is regenerated only by the script, and only in PRs that change the signal contract.

## Feature: article-extraction

**Branch:** `feature/article-extraction`
**Depends on:** signals-golden-test
**Status:** Not Started
**Requires:** both

### Goal

Change the per-article step from free prose to validated JSON facts. Use the article's own title as the headline, and flag roundup articles.

### Acceptance Criteria

- [ ] `summarize_single` calls Ollama with a structured-output JSON schema (`format`). Each request the stub records includes the schema, `num_ctx`, temperature and seed from config, and the article's published date.
- [ ] Schema fields:
  - `what_happened`: at most 2 sentences and `EXTRACT_MAX_WORDS` words (default 50).
  - `entities`: names copied exactly from the article.
  - `numbers`: figures copied exactly.
  - `event_date`: `YYYY-MM-DD` or null, resolved against the published date.
  - `status`: `happened`, `scheduled` or `ongoing`.
  - `is_roundup`: bool.
- [ ] With the stub returning valid JSON, the fixture run stores one row per article in a new `extractions` table (`article_id` PK, `json`, `model`, `created_at`). The existing `summary` column holds `what_happened`, so the page keeps working.
- [ ] With the stub returning malformed JSON twice for one article, that article is marked `extraction_failed` and left out of the briefing. The run exits 0 and logs the article id.
- [ ] A `what_happened` over the word limit is cut at the last sentence boundary within the limit (unit test).
- [ ] Articles with `is_roundup: true` are left out of the briefing input but still appear in the signals output.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] On hub, the PM runs extraction live over one day's articles, reports the JSON-valid rate (target 95% or higher), and pastes 5 sample extractions into the PR.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | New extraction prompt and call |
| `src/summarizers/schema.py` | Create | JSON schema, parsing, word-limit trim |
| `src/config.py` | Modify | `OLLAMA_NUM_CTX`, `EXTRACT_TEMPERATURE`, `EXTRACT_SEED`, `EXTRACT_MAX_WORDS`, `EXTRACT_NUM_PREDICT`, `CHARS_PER_TOKEN` |
| `src/db.py` *(confirm)* | Modify | `extractions` table and `extraction_failed` flag |
| `tests/stub_ollama.py` | Modify | Canned JSON responses |
| `tests/test_extraction.py` | Create | The criteria above |

### Key Decisions

- **The headline is the article's title, not generated text.** Titles are accurate and cost nothing.
- **No "significance" or "key actors" fields.** Those sections were the main source of filler, and their label text was leaking into the output.
- **Retry once, then exclude.** Never fall back to raw article text.
- Prompt rules: facts only (who did what, figures, when); copy names, tokens and companies exactly as written; no evaluation or impact language.

### Notes

The briefing step will only ever see these extractions, never the full text, which matches the current design. One article per extraction rules out mixing up facts across articles, the failure behind "Modigliani at Christie's".

## Feature: output-validator

**Branch:** `feature/output-validator`
**Depends on:** article-extraction
**Status:** Not Started
**Requires:** ai

### Goal

Before anything is rendered, check every generated sentence in code and drop the ones that aren't grounded in their source or that contain filler.

### Acceptance Criteria

- [ ] **Number check:** each digit group in a sentence (commas stripped) must appear in its source text. Unit cases: "$44.6 million" against a source containing "$44.6M" passes; "$45.6 million" fails.
- [ ] **Name check:** each capitalized token that isn't sentence-initial, is at least 3 characters, and isn't in `config/name_allowlist.txt` must match the source case-insensitively on its first `max(4, len-2)` characters. Unit cases: "Democrats" against "Democratic" passes; "Christie's" against a source without it fails.
- [ ] **Banned-phrase check:** case-insensitive match against `config/banned_phrases.txt`, plus the overlay at `$MORNING_BRIEF_BANNED_OVERLAY` if that file exists. Unit case: "This highlights the volatility" fails. With the overlay file missing, there is no error.
- [ ] The source for `what_happened` is the article's title plus its text (the same 12,000-character cut). The source for a section lede is that section's bullets.
- [ ] With `VALIDATOR_MODE=enforce`, the fixture run with one ungrounded sentence from the stub renders without it, and the run metadata reports `dropped: {number: 1}`. With `warn`, the sentence is kept and a warning is logged.
- [ ] If every sentence of an extraction is dropped, the article is left out of the briefing.
- [ ] Each run writes sentences checked and sentences dropped by reason to the run metadata.
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/validators/__init__.py` | Create | Package |
| `src/validators/grounding.py` | Create | Number, name and banned-phrase checks |
| `config/banned_phrases.txt` | Create | Seed list (see Notes) |
| `config/name_allowlist.txt` | Create | Common capitalized words: months, weekdays, "The", acronyms |
| `src/config.py` | Modify | `VALIDATOR_MODE`, overlay path |
| `src/pipeline.py` *(confirm)* | Modify | Run the validator after extraction and after the briefing |
| `tests/test_validator.py` | Create | The criteria above |

### Key Decisions

- Drop the whole sentence rather than rewrite it. A 7B rewrite would introduce new errors.
- The validator is a backstop. It can't catch a name that appears in the source in a different role. The main defence is structural: one article per bullet.

### Notes

Seed `banned_phrases.txt` (one per line): `highlights`, `underscores`, `key actors`, `key players`, `landscape`, `it is worth noting`, `plays a crucial role`, `significant shifts`, `these developments`, `will be closely monitored`, `shape the future`, `pave the way`, `ever-evolving`, `navigating`, `the importance of`, `a testament to`, `sheds light`, `delve`, `in today's`, `robust`.

## Feature: briefing-dedupe

**Branch:** `feature/briefing-dedupe`
**Depends on:** signals-golden-test
**Status:** Not Started
**Requires:** ai

### Goal

When several articles cover the same story, show one of them in the briefing and list the others as "also covered by".

### Acceptance Criteria

- [ ] Two articles in the same category, published within `DEDUPE_WINDOW_HOURS` (default 48) of each other, count as duplicates if their titles share a distinctive number token (one with a decimal point, or 3 or more digits, ignoring `$`, `%` and the M/B suffixes) **and** at least one capitalized non-allowlisted word. The fixture's same-story pair is detected; two unrelated same-category articles are not.
- [ ] The longer article is kept. The other gets `duplicate_of` set, and its URL shows as "also covered by" under the kept bullet.
- [ ] The freed slot is refilled from the category's next article, so each category still gets up to 8.
- [ ] The DB row count is the same before and after the run (dedupe never deletes rows).
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/dedupe.py` | Create | Duplicate detection |
| `src/processors/selection.py` *(confirm: where `select_balanced_articles` lives)* | Modify | Dedupe before selecting, then refill |
| `src/db.py` *(confirm)* | Modify | `duplicate_of` column |
| `tests/test_dedupe.py` | Create | The criteria above |

### Key Decisions

- Match on titles only, before summarizing, so duplicates never use up an LLM call.
- Plain word overlap (Jaccard) missed the real case: two Christie's titles scored about 0.2. The shared-figure rule catches it.

## Feature: interest-filter

**Branch:** `feature/interest-filter`
**Depends on:** signals-golden-test
**Status:** Not Started
**Requires:** both

### Goal

Rank briefing articles by the operator's interests, and stop high-volume feeds such as arXiv from filling sections with off-topic papers.

### Acceptance Criteria

- [ ] The interests TOML is read from `$MORNING_BRIEF_INTERESTS` (default `~/.config/morning-brief/interests.toml`, uncommitted). It has a `[keywords]` table (`term = weight`, 1–5) and `[feeds."<feed id>"] min_interest = N`. `config/interests.example.toml` is committed with generic placeholders.
- [ ] An article's interest score is the sum of the weights of keywords found in its title plus first 2,000 characters (case-insensitive, matched on word boundaries).
- [ ] Within a category, articles are ordered by interest score, then by current order. Articles from a feed with `min_interest` that score below it are not summarized. Fixture: a feed item with no matching keyword is excluded, and one with a match is included.
- [ ] `$MORNING_BRIEF_INTERESTS` accepts several paths separated by `:`. Keywords are merged across files, and when the same keyword appears in more than one file, the highest weight wins. Fixture: two files sharing a keyword at weights 2 and 4 score it as 4.
- [ ] A file may set a top-level `label = "<text>"`. Bullets whose article matches any keyword from that file show the label (several labels are de-duplicated). Fixture: a file labelled `On radar` tags only the matching bullets in the rendered page JSON.
- [ ] Listed files that are missing are skipped with one INFO line each. With no files present at all, selection is identical to before (a test compares the outputs).
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] The operator writes the interests file on hub.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/interest.py` | Create | Load the file and score articles |
| `config/interests.example.toml` | Create | Generic example |
| `src/processors/selection.py` *(confirm)* | Modify | Order and filter |
| `src/config.py` | Modify | Interests path |
| `tests/test_interest.py` | Create | The criteria above |

### Key Decisions

- Keyword weights, not an LLM relevance call: free, predictable, and easy to tune.
- **PM: confirm that `signals.py` reads all stored articles, not only summarized ones.** If it reads only summarized ones, this filter changes atrade's input, and the golden test will catch it.

## Feature: category-briefing

**Branch:** `feature/category-briefing`
**Depends on:** article-extraction, output-validator
**Status:** Not Started
**Requires:** both

### Goal

Replace the single briefing call with one small call per category. The model only ranks the validated bullets and writes an optional lede. Every item shown carries its own source.

### Acceptance Criteria

- [ ] Each category call receives at most 8 lines of the form `[id] title — what_happened` and returns JSON: `{lede: string (≤ LEDE_MAX_WORDS, default 25, or ""), ranked_ids: [3–5 ids from the input]}`.
- [ ] The stub records exactly one generate call per non-empty category. `test_generate_call_count` is updated to `articles + categories`.
- [ ] With `num_ctx` set very small, the lowest-interest articles are dropped from that call, a warning is logged, and no recorded request exceeds the budget.
- [ ] Ids not in the input are ignored. If no valid ids come back, the section falls back to the top 3 by interest score with no lede.
- [ ] Section titles come from configured category display names, with no LLM involved. Sections are ordered by their highest interest score.
- [ ] The lede goes through the validator with the section's bullets as its source.
- [ ] A test walks the rendered page JSON. Every bullet has a source URL belonging to its own article, every source appears exactly once, and no text field exists without a source id (the lede is the only exception).
- [ ] `_build_briefing_prompt` is removed.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] The PM runs one live day on hub, and the operator reviews the page on the phone.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/briefing.py` | Create | Per-category ranking and lede call, token guard |
| `src/summarizers/local.py` | Modify | Remove the old briefing prompt |
| `src/config.py` | Modify | `LEDE_ENABLED`, `LEDE_MAX_WORDS`, category display names |
| `templates/` *(confirm)* | Modify | Section: title, lede, bullets (linked title, `what_happened`, source, also-covered) |
| page publisher *(confirm)* | Modify | New page JSON shape |
| `tests/test_briefing.py` | Create | The criteria above |

### Key Decisions

- **Bullets are the extractions themselves, not rewritten.** The model never paraphrases across articles, so it can't merge two stories into one.
- **Titles come from config.** Model-written titles were the source of the false "Upcoming Events and Trends" framing.
- **Per-category calls fix the silent truncation risk.** The worst case was about 12k tokens in a single call.

## Feature: dates-ahead

**Branch:** `feature/dates-ahead`
**Depends on:** category-briefing
**Status:** Not Started
**Requires:** ai

### Goal

Build a "Dates ahead" list in code from extracted scheduled events, replacing the model-written "upcoming" section.

### Acceptance Criteria

- [ ] Includes extractions with `status: scheduled` and an `event_date` from the run date (UTC) through `DATES_AHEAD_DAYS` (default 30) days later. Each entry renders as `Mon DD — title`, linked to its source, sorted by date.
- [ ] Fixture: an event 5 days ahead is included. Past dates, dates beyond the window, and malformed dates are excluded (a malformed date logs a warning).
- [ ] With no qualifying items, the section is hidden.
- [ ] This feature makes no generate calls (the stub's call count is unchanged).
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/dates_ahead.py` | Create | Filter and sort |
| `src/config.py` | Modify | `DATES_AHEAD_DAYS` |
| `templates/` *(confirm)* | Modify | Dates-ahead block |
| `tests/test_dates_ahead.py` | Create | The criteria above |

## Feature: signal-direction

**Branch:** `feature/signal-direction`
**Depends on:** article-extraction; atrade `signal-direction-reader` merged
**Status:** Not Started
**Requires:** ai

### Goal

Add an optional per-ticker news `direction` to the signals file for atrade's shadow arm. `score` is not changed.

### Acceptance Criteria

- [ ] For articles that match ticker aliases (using the existing alias code, run before the LLM call), the extraction request includes those tickers. The response may contain `ticker_sentiment: {TICKER: -1|0|1}`. Tickers that weren't requested, and values that aren't one of those three, are discarded. Articles with no matches get no ticker list (checked with the stub).
- [ ] Each signal record gains `direction` (a float in [-1, 1], the mean over contributing articles, rounded to 2 places, or null) and `direction_n` (an int). Fixture: a ticker with sentiments +1 and −1 gets `0.0` and `2`. With the stub returning `"bullish"`, the result is `null` and `0`.
- [ ] `score`, `knowable_at`, `article_count` and provenance are byte-identical to the previous golden file. A test checks this field by field. The golden file is regenerated in this PR, with only the two new fields added.
- [ ] `docs/SIGNAL-SCHEMA.md` gets a version bump and documents the fields, following its own versioning rules. The same bump lands in atrade the same day. The output validates against the schema's JSON schema, or a test-local schema if there isn't one.
- [ ] The signal file contains no string values outside enumerated fields (a test walks the file).
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/schema.py` | Modify | `ticker_sentiment` field |
| `src/summarizers/local.py` | Modify | Pass matched tickers into the extraction call |
| `src/publishers/signals.py` | Modify | `direction`, `direction_n` |
| `docs/SIGNAL-SCHEMA.md` | Modify | Version bump and field docs |
| `tests/fixtures/brief_fixture/signals.golden.json` | Modify | New fields only |
| `tests/test_signal_direction.py` | Create | The criteria above |

### Key Decisions

- **No extra LLM call.** Sentiment rides on the existing extraction call.
- **Per ticker, not per article.** An article about one company often mentions others in passing.
- **Ship order:** contract bump in both repos, then atrade's reader merged, then this feature emits.

### Notes

Sentiment comes from a model reading untrusted article text, so a crafted article can push it. It is bounded, numeric and paper-only, and the shadow arm is there to measure whether it helps at all. Scores bunching between 0.44 and 0.61 is a scorer issue and is out of scope; it goes to the backlog.

---

## Companion feature for atrade's DEVPLAN (not morning-brief's)

## Feature: signal-direction-reader

**Branch:** `feature/signal-direction-reader`
**Depends on:** none (merge before morning-brief `signal-direction`)
**Status:** Not Started
**Requires:** ai

### Goal

atrade accepts the new contract version and uses `direction` in the shadow arm only.

### Acceptance Criteria

- [ ] A signals file at the new version with `direction` and `direction_n` loads and validates. A file at the previous version without those fields still loads, with direction treated as absent.
- [ ] A record whose `direction` is outside [-1, 1] or non-numeric is rejected and logged. The rest of the file still loads.
- [ ] The shadow arm's tilt uses `direction` only when `direction_n` is at least the configured minimum (default 2). A test shows the equal-weight paper account is untouched.
- [ ] The existing point-in-time test still passes: no signal is usable before its `knowable_at`.
- [ ] `docs/SIGNAL-SCHEMA.md` matches morning-brief's copy (same version, same text).
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| atrade signal loader *(PM maps)* | Modify | New version, optional fields |
| atrade shadow-arm module *(PM maps)* | Modify | Direction tilt |
| `docs/SIGNAL-SCHEMA.md` | Modify | Lockstep bump |
| tests *(PM maps)* | Create | The criteria above |

---

## Companion feature for hub's DEVPLAN (not morning-brief's)

## Feature: radar-brief-interests

**Branch:** `feature/radar-brief-interests`
**Depends on:** radar-doc-and-cli (hub); morning-brief `interest-filter` merged before the [HUMAN] check
**Status:** Not Started
**Requires:** both

### Goal

Make the tech radar feed morning-brief. Every radar entry with status `adopt`, `trial` or `watch` becomes a weighted briefing keyword, so news about tracked tech shows up labelled "On radar". Today nothing reads the radar automatically.

### Acceptance Criteria

- [ ] Radar entries accept an optional `Keywords:` line (comma-separated). `radar.py check` validates it, and entries without it still pass.
- [ ] `radar.py export-interests --out PATH` writes a TOML file that morning-brief's interest-filter accepts. It contains `label = "On radar"` and one keyword per entry keyword, falling back to the entry title when there's no `Keywords:` line. Weights come from status: `trial` 4, `watch` 3, `adopt` 2. `park` entries are excluded. The test parses the output with `tomllib`.
- [ ] Every successful `radar.py add` and `inbox promote` regenerates the export at the configured path (default `/data/radar/brief-interests.toml`). A failed validation leaves the old export untouched (written via temp file plus rename).
- [ ] The export contains only titles, keywords and weights: no notes, URLs or inbox text (a test walks the file).
- [ ] [HUMAN] The PM adds the export path to morning-brief's `MORNING_BRIEF_INTERESTS` on hub. The next run shows at least one "On radar" label when a matching story exists, or logs that the file was loaded.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `scripts/radar.py` | Modify | `Keywords:` field, `export-interests`, regenerate on write |
| `tests/test_radar_export.py` | Create | The criteria above |
| `docs/tech-radar.md` | Modify | Explain the brief feed and how to add keywords |
| morning-brief service env *(PM maps: the hub role that owns its unit)* | Modify | Add the export path |

### Key Decisions

- **A file handoff, not code coupling.** The radar stays private on hub, and morning-brief (public) only reads a runtime file outside git, the same way it reads the operator's interests file.
- This closes the loop: radar entry → news in the brief tagged "On radar" → share to the radar inbox (`radar-phone-capture`) → PM triage.

---

## For morning-brief's FEATURE-REQUESTS.md (backlog, not features)

- **Roundup splitting:** break newsletter-style roundups into separate items instead of leaving them out.
- **Story threads:** link an item to earlier coverage of the same entities ("previously: …"), so ongoing stories keep their context.
- **Model eval command:** run extraction over the synthetic fixtures with any local model and report the JSON-valid rate and validator drop rate, for the planned model audit.
- **Scorer spread:** scores bunch between 0.44 and 0.61. Fixing this is a contract change, to be planned jointly with atrade.
- **Feed review:** replace or narrow the arXiv cs.AI firehose.
