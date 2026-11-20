# Plan: brief-reader-v2

**Plan type:** features
**Target repo:** morning-brief
**Part:** 1 of 1
**Created:** 2026-11-20

## PM Kickoff

- **First action:** Confirm that every brief-quality feature is merged and deployed; this plan builds on them. Then add features 1–7 below to morning-brief's `docs/DEVPLAN.md` in order. Also refresh the Planner Context artifact: as of 2026-11-20 it still showed the 2026-11-18 build with brief-quality unstarted. Paths marked *(confirm)* are guesses; map them before queuing.
- **Radar entry (PM task, not a feature):** Add a `watch` entry with `radar.py add`. Title: "Local model writes the reader summaries". Applies to: morning-brief. Trigger to revisit: `summary-bakeoff` shows a local model matching the Claude writer on validator pass rate and citation coverage. Next action: run the bakeoff at each quarterly model audit. The operator wants the stack to go back to local-only when a local model can do the job.
- **Run mode:** mixed.
  - Overnight: `source-audit`, `reader-coverage`, `social-feeds`, `summary-bakeoff`.
  - Supervised: `feed-lanes`, because it sits on atrade's input; `claude-section-writer`, because it holds an API credential and sends content off-box; `cited-summary-page`, because it's the first live render of the new format.
- **Operator setup:** The Claude API key for the app workspace goes on the SECRETS USB before `claude-section-writer`. The operator reviews the source-audit results and approves the reader feed changes (see Open Questions).
- **Supersedes / overlaps:**
  - Replaces `category-briefing`'s lede and bullet output with long cited section summaries. Ranking and the token guard stay.
  - Generalizes `stack-feeds`' `signals` flag into lanes.
  - Moves arXiv out of the reader.

## Open Questions

- {default: $10 per calendar month, out of the $40 app workspace cap} What's the monthly spend cap for the summary writer? When it's reached, the writer falls back to local for the rest of the month.
- {default: 15} What's the maximum number of articles summarized per reader category?
- {default: 250–450 words per section} How long should each section summary be?
- {default: the PM proposes replacements from the audit in one PR, and the operator approves by merging} Which reader feeds get replaced? The operator prefers free, full-text, direct-from-journalist sources (independent newsletters, nonprofit newsrooms, journalists' Bluesky and Mastodon accounts) over subscriptions. The total for any paid news source must stay at or below $10 per month. The operator may also send specific journalists at any time.
- {default: no} Should there be an opening "today in brief" paragraph above the sections? The page reads as one summary made of its sections, so it shouldn't need one.

## Constraints

- **atrade boundary.** The signals lane's feeds, the scorer, retention and the signals file are unchanged in this plan. Reader feed swaps never remove a feed from the signals lane. The signals golden test passes unchanged in every PR. Changing atrade's sources is a separate, contract-noted plan.
- **Location hygiene.** Feeds that reveal the operator's region (local and state outlets, local journalists) live in an uncommitted feeds overlay (`$MORNING_BRIEF_FEEDS_OVERLAY`). The committed feed config holds only generic, national sources.
- **Content sent off-box.** The Claude writer receives only titles, source names, URLs and validated extractions, never full article text. Its output goes only to the page, never to atrade or the radar, and passes the same validator as local output.
- **Credential.** The API key is loaded with systemd `LoadCredential=` from the SECRETS USB. It is never put in env files, logs or the repo.
- **GPU window.** The full run, including the larger coverage, finishes within `RUN_DEADLINE_MINUTES` (default 45) of its 08:15 UTC start. A run that's still going logs a warning, and if it hits the deadline it publishes whatever sections are done.
- **Impermanent values in config.** The writer model name, prices per token, caps and word targets are config, never constants.

## Features

## Feature: source-audit

**Branch:** `feature/source-audit`
**Depends on:** none
**Status:** Not Started
**Requires:** both

### Goal

Measure, per feed, how much real article text comes through, so paywalled and truncated sources can be identified and replaced.

### Acceptance Criteria

- [ ] `morning-brief audit-sources --days N` exits 0 and prints one row per feed with:
  - item count
  - median extracted text length in words
  - share of items under `AUDIT_MIN_WORDS` (default 150)
  - share of items containing paywall markers (from `config/paywall_markers.txt`: "subscribe to continue", "sign in to read", and similar)
  - fetch error count
  - a verdict of `ok`, `truncated`, `paywalled` or `failing`

  It also writes the same data to `data/source-audit-<date>.json`.
- [ ] Fixture: a synthetic feed whose items are 40-word teasers plus a marker gets `paywalled`. A full-text feed gets `ok`. A feed returning HTTP errors gets `failing`.
- [ ] Read-only: the DB row count and the signals output are unchanged by an audit run.
- [ ] [HUMAN] The PM runs it over 7 days on hub and posts the table in a PR comment with proposed replacements for every non-`ok` reader feed (free, full-text, journalist-direct first).
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/source_audit.py` | Create | Per-feed metrics and verdicts |
| `config/paywall_markers.txt` | Create | Marker phrases |
| `src/cli.py` *(confirm)* | Modify | `audit-sources` command |
| `tests/test_source_audit.py` | Create | The criteria above |

## Feature: feed-lanes

**Branch:** `feature/feed-lanes`
**Depends on:** source-audit
**Status:** Not Started
**Requires:** ai

### Goal

Give each feed explicit lanes (`reader`, `signals`, `rnd`), so the news the operator reads, atrade's inputs and R&D material can each change without affecting the others.

### Acceptance Criteria

- [ ] Feed config gains `lanes` (a list). `reader` feeds the briefing. `signals` feeds the scorer. `rnd` feeds only `radar-flags` and is never shown in the briefing.
- [ ] The existing `signals = false` flag is migrated: `false` becomes lanes without `signals`. Feeds with no `lanes` keep today's behaviour (`reader` + `signals`).
- [ ] arXiv feeds move to `rnd` only. The fixture run shows no arXiv item in the page JSON while its flag records still appear.
- [ ] A fixture feed in `signals` only appears in signals output but not on the page; a `reader`-only feed does the reverse.
- [ ] The signals golden test passes unchanged with today's feed config.
- [ ] Feeds from `$MORNING_BRIEF_FEEDS_OVERLAY` merge over the committed config, matched by feed id. A missing overlay file is skipped with one INFO line.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/config.py` | Modify | Lanes, overlay path |
| feed config *(confirm path)* | Modify | Lanes on every feed; arXiv to `rnd` |
| `src/processors/selection.py` *(confirm)* | Modify | Reader lane only |
| `src/processors/scorer.py` input selection *(confirm)* | Modify | Signals lane only, output unchanged |
| `tests/test_feed_lanes.py` | Create | The criteria above |

### Key Decisions

- Lanes are per feed, not per article: simple, auditable, and they match how sources are actually chosen.

## Feature: reader-coverage

**Branch:** `feature/reader-coverage`
**Depends on:** feed-lanes
**Status:** Not Started
**Requires:** both

### Goal

Cover more articles, with richer extracted facts, so section summaries can run 2–5× longer.

### Acceptance Criteria

- [ ] `READER_MAX_PER_CATEGORY` (default from Open Questions) replaces the fixed 8. A fixture with 20 eligible articles in one category summarizes exactly the configured number, chosen by interest score.
- [ ] `EXTRACT_MAX_WORDS` defaults to 120, and the extraction schema gains `details`: up to 4 short factual sentences beyond `what_happened`, under the same validator.
- [ ] Each run logs its total duration and per-stage durations. A run passing `RUN_DEADLINE_MINUTES` logs a warning (fixture with a stub clock).
- [ ] [HUMAN] The PM confirms on hub that one full run finishes within the deadline, and posts the timing.
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/config.py` | Modify | Caps, deadline |
| `src/summarizers/schema.py` | Modify | `details` field |
| `src/processors/selection.py` *(confirm)* | Modify | Configurable cap |
| `src/pipeline.py` *(confirm)* | Modify | Stage timing, deadline |
| `tests/test_reader_coverage.py` | Create | The criteria above |

## Feature: claude-section-writer

**Branch:** `feature/claude-section-writer`
**Depends on:** reader-coverage
**Status:** Not Started
**Requires:** both

### Goal

Have the Claude API write each reader section as a long, readable summary with research-paper-style citations, using only validated extractions.

### Acceptance Criteria

- [ ] With `SUMMARY_WRITER=claude`, each reader section gets one API call. Input: section name, plus for each article its id, title, source, `what_happened` and `details`. Output: prose paragraphs where each chunk drawn from a source ends with `[[id]]`, or `[[id1,id2]]` when it combines sources. Target length comes from config.
- [ ] Code splits the output into chunks at citation markers. Each chunk is validated against the union of its cited articles' text. Chunks that fail, cite unknown ids, or have no citation are dropped and counted in the run metadata.
- [ ] If a section loses more than `WRITER_MAX_DROP_SHARE` of its chunks (default 0.3), or the API errors or times out, that section falls back to the local writer, and the page marks it "local fallback".
- [ ] Spend guard: each call's token usage is recorded in SQLite, priced from config. When the month-to-date estimate reaches the cap, the writer switches to local until the month rolls over, logs once, and sends one `hub-alert`.
- [ ] The API key comes from `$CREDENTIALS_DIRECTORY` (systemd `LoadCredential=`). With the credential missing, the writer falls back to local and logs that clearly, with no traceback containing secrets.
- [ ] Tests use a stub HTTP server for the API. They cover valid output, an uncited chunk, an unknown id, a wrong number in a chunk, an API 500, and the cap being reached.
- [ ] A test walks the request bodies the stub recorded and finds no article full text.
- [ ] `SUMMARY_WRITER=local` uses the existing local path unchanged.
- [ ] [HUMAN] Operator: API key on the SECRETS USB. PM: one live run on hub, posting the drop counts and token spend.
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/claude_writer.py` | Create | API call, chunking, citation parsing |
| `src/summarizers/writer.py` | Create | Chooses `claude` or `local`, handles fallback |
| `src/validators/grounding.py` | Modify | Validate a chunk against several sources |
| `src/db.py` *(confirm)* | Modify | `api_usage` table |
| `src/config.py` | Modify | Writer, model, prices, cap, targets |
| systemd unit *(PM maps: the hub role that owns it)* | Modify | `LoadCredential=` |
| `tests/test_claude_writer.py` | Create | The criteria above |

### Key Decisions

- **Hybrid.** Local does the bulk extraction (one call per article). Claude does only the writing (one call per section). Quality goes up where it matters, while spend stays small and capped.
- **Extractions only.** Less data leaves the box, fewer tokens are used, and the validator has clean ground truth.
- **Fall back per section, not per page.** One bad section shouldn't cost the whole brief.

## Feature: cited-summary-page

**Branch:** `feature/cited-summary-page`
**Depends on:** claude-section-writer
**Status:** Not Started
**Requires:** both

### Goal

Render the brief as one long summary made of sections, with tappable citations, and per-article details tucked away.

### Acceptance Criteria

- [ ] Each section renders its title followed by its prose. Each `[[id]]` becomes a small superscript link to that article's URL (several ids become several links), with the source name in the link's title.
- [ ] Below each section, a collapsed "Sources (N)" list holds every cited article: title, source, and its `what_happened`.
- [ ] `dates-ahead` and the "On radar" and "For the stack" labels still render.
- [ ] A test walks the rendered page JSON and HTML: every citation link matches an article in that section, no `[[` markers remain in the HTML, and every cited article appears in the sources list.
- [ ] The page passes an HTML validator (`html5validator` or equivalent, pinned) in CI.
- [ ] [HUMAN] The operator reads one live brief on the phone and approves the layout.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `templates/` *(confirm)* | Modify | Section prose, citations, collapsed sources |
| page publisher *(confirm)* | Modify | New page JSON shape |
| `tests/test_cited_page.py` | Create | The criteria above |

## Feature: social-feeds

**Branch:** `feature/social-feeds`
**Depends on:** feed-lanes
**Status:** Not Started
**Requires:** ai

### Goal

Support journalist-direct sources: Bluesky and Mastodon profile feeds, plus Substack and Ghost newsletters.

### Acceptance Criteria

- [ ] Feed config accepts `kind`: `article` (default), `post` or `newsletter`.
  - `post` items (Bluesky and Mastodon RSS) skip page fetching and the extraction call. Their post text is used directly as `what_happened`, capped at 120 words, and validated against itself. The stub records no generate call for them.
  - `newsletter` behaves like `article`. If the item's own content field already holds at least `AUDIT_MIN_WORDS` words, that text is used instead of fetching the page.
- [ ] Posts from one author within `POST_GROUP_HOURS` (default 12) are grouped into one source entry, so a thread isn't counted as many sources.
- [ ] Fixtures (synthetic) cover a Bluesky-style RSS profile, a Mastodon-style RSS profile and a newsletter feed with full content.
- [ ] `source-audit` reports `post` feeds with verdict `ok-post` (short by design).
- [ ] The signals golden test passes unchanged (`post` feeds default to the `reader` lane only).
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/fetchers/` *(confirm)* | Modify | Feed `kind` handling |
| `src/processors/source_audit.py` | Modify | `ok-post` verdict |
| `tests/fixtures/social/` | Create | Synthetic feeds |
| `tests/test_social_feeds.py` | Create | The criteria above |

### Notes

The specific journalists and accounts go in the uncommitted feeds overlay, not the repo.

## Feature: summary-bakeoff

**Branch:** `feature/summary-bakeoff`
**Depends on:** claude-section-writer
**Status:** Not Started
**Requires:** ai

### Goal

Make the "go back to local-only" decision measurable: score any local model's section writing against the Claude writer on the same day's extractions.

### Acceptance Criteria

- [ ] `morning-brief bakeoff --model NAME --date YYYY-MM-DD` runs the local writer with that model on the stored extractions for that date, without publishing. It prints, side by side with that day's Claude output:
  - chunk validator pass rate
  - citation coverage (share of articles cited)
  - words per section
  - fallback count
- [ ] It writes `data/bakeoff-<date>-<model>.json` and exits 0. A date with no stored extractions exits non-zero with a clear message.
- [ ] Fixture: stub models with known outputs produce the expected scores.
- [ ] It makes no API calls (the stub records none).
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/bakeoff.py` | Create | Scoring |
| `src/cli.py` *(confirm)* | Modify | `bakeoff` command |
| `tests/test_bakeoff.py` | Create | The criteria above |

### Key Decisions

- The switch back to local is a config flip (`SUMMARY_WRITER=local`), made when a bakeoff shows parity. The radar entry carries the trigger.
