# Development Plan: Morning Brief

**Status:** In Progress
**Created:** 2026-03-28
**Updated:** 2026-04-07

## Overview

Morning Brief is an automated daily news and intelligence dashboard. It fetches RSS feeds, financial/crypto data, and artwork, summarizes articles with a local LLM (Qwen via Ollama), renders an interactive HTML dashboard, and deploys to Cloudflare Pages. This plan covers the remaining work to reach a production-ready v1.0 with progressive-disclosure content presentation.

## Constraints

- Python 3.11+, async HTTP via httpx
- SQLite only (single-user, once-daily batch)
- Ollama for summarization (no paid API for bulk work)
- No secrets in code — all config via environment variables
- One broken feed must never crash the pipeline
- Docker deployment with cap_drop ALL, no-new-privileges
- Dashboard must be hostable for free (Cloudflare Pages static + Workers for API)

---

## Feature: docker-deployment

**Branch:** `feature/docker-deployment`
**Depends on:** none
**Status:** Complete
**Requires:** ai

### Goal

Finalize Dockerfile and docker-compose.yml so the full pipeline runs unattended in a container with persistent data.

### Acceptance Criteria

- [x] Multi-stage Dockerfile: build stage installs deps, runtime stage is slim
- [x] docker-compose.yml mounts `data/` volume for SQLite persistence and `.env` for config
- [x] `docker compose up` runs the full pipeline end-to-end
- [x] Container uses cap_drop ALL and no-new-privileges
- [x] Container can reach Ollama on the host network (for summarization)
- [x] Output dashboard.html is accessible from the host via volume mount
- [x] All tests pass inside the container
- [x] Lint clean

---

## Feature: systemd-scheduling

**Branch:** `feature/systemd-scheduling`
**Depends on:** docker-deployment
**Status:** Complete
**Requires:** both

### Goal

Create a systemd timer that triggers the pipeline at 4:15 AM ET daily.

### Acceptance Criteria

- [x] `morning-brief.service` systemd user unit runs the pipeline
- [x] `morning-brief.timer` triggers at 4:15 AM ET daily
- [x] Health check validates dashboard output
- [x] On failure: log with timestamp and exit code
- [x] [HUMAN] Enable timer: `systemctl --user enable --now morning-brief.timer`
- [x] Tests cover health check logic
- [x] Lint clean

---

## Feature: daily-briefing

**Branch:** `feature/daily-briefing-batch`
**Depends on:** none
**Status:** Complete
**Requires:** ai

### Goal

Combine article summaries into a ~500-1000 word daily briefing via Ollama.

### Acceptance Criteria

- [x] `generate_daily_briefing()` in `src/summarizers/local.py`
- [x] `daily_briefings` table in SQLite
- [x] Progressive-disclosure prompt: lead → themed sections → forward look
- [x] Appears in dashboard and terminal
- [x] Skips gracefully if Ollama unreachable
- [x] Only generates once per day
- [x] Tests pass, lint clean

---

---

## Feature: systemd-bugfix

**Branch:** `fix/systemd-units`
**Depends on:** none
**Status:** Merged
**Requires:** ai

### Goal

Fix two bugs in the systemd unit files that prevent them from working as user-level units.

### Acceptance Criteria

- [ ] `morning-brief.timer`: Fix `OnCalendar` timezone syntax — timezone goes at end, not beginning
- [ ] `morning-brief.service`: Remove `Requires=docker.service` (system-level unit, can't be referenced from user-level)
- [ ] `morning-brief.service`: Change `After=docker.service network-online.target` to `After=network-online.target`
- [ ] `morning-brief.service`: Change `WorkingDirectory=/opt/morning-brief` to `WorkingDirectory=%h/Projects/morning-brief`
- [ ] `morning-brief.service`: Add `Environment=DEPLOY_ENABLED=true`
- [ ] Verify with `systemd-analyze verify`
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `morning-brief.timer` | Modify | Fix OnCalendar syntax |
| `morning-brief.service` | Modify | Fix dependencies and paths |

---

## Feature: article-retention

**Branch:** `feature/article-retention`
**Depends on:** none
**Status:** Merged
**Requires:** ai

### Goal

Clear old articles from the database daily so the pipeline always works with fresh content. Prevents unbounded DB growth and keeps the dashboard focused on today's news.

### Acceptance Criteria

- [ ] At pipeline start, delete articles older than 2 days from the `articles` table
- [ ] Preserve the `daily_briefings` table (briefings are archived, not deleted)
- [ ] Deduplication still works — articles seen today that were also seen yesterday should not reappear as "new"
- [ ] Add a `last_seen_at` column to articles for smarter retention (update on re-fetch, delete when not seen for 2 days)
- [ ] Log how many articles were cleaned up
- [ ] Tests cover retention logic and dedup-across-days edge case
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/db.py` | Modify | Add retention cleanup method, `last_seen_at` column |
| `src/main.py` | Modify | Call cleanup at pipeline start |
| `tests/test_db.py` | Modify | Test retention and dedup edge cases |

### Key Decisions

- 2-day window (not 1) to handle articles that span midnight or late-publishing feeds
- `last_seen_at` tracks when an article was last fetched — prevents dedup gaps after cleanup
- Briefings table is never cleaned (used for archive/future trading bot)

---

## Feature: relevance-scoring

**Branch:** `feature/relevance-scoring`
**Depends on:** none
**Status:** Merged
**Requires:** ai

### Goal

Score and rank articles by relevance using cheap heuristics (no LLM). This determines which articles get summarized and which make it into the briefing. Runs in milliseconds, not minutes.

### Acceptance Criteria

- [ ] New `src/processors/scorer.py` module with `score_articles(articles) -> list[ScoredArticle]`
- [ ] Scoring factors (all configurable weights):
  - Source reputation weight (configurable per-source in FEEDS.md or config)
  - Recency (newer = higher)
  - Cross-source coverage (same story from multiple feeds = important)
  - Category priority weight (configurable — politics > art for "must-know")
  - Title keyword signals (e.g., "breaking", "exclusive", major entity names)
- [ ] Returns ranked list with numeric score and contributing factors
- [ ] Category-balanced output: top N articles per category, not just top N overall
- [ ] Scored articles stored in DB (score column on articles table)
- [ ] Tests cover each scoring factor independently
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/scorer.py` | Create | Scoring engine |
| `src/db.py` | Modify | Add score column to articles table |
| `src/main.py` | Modify | Wire scoring after dedup, before summarization |
| `tests/test_scorer.py` | Create | Test each scoring factor |

### Key Decisions

- No LLM involved — pure heuristics for speed
- Weights are configurable so the user can tune what "important" means
- Cross-source detection uses fuzzy title matching (same as existing dedup)
- Category balance ensures Florida politics always gets 1-2 slots even if score is low
- Source weights default: major outlets (BBC, NPR) = 1.0, niche blogs = 0.5, configurable

---

## Feature: docker-git-deploy

**Branch:** `fix/docker-git-deploy`
**Depends on:** none
**Status:** Not Started
**Requires:** ai

### Goal

Fix the gh-pages deploy step by adding git to the Docker runtime image. Currently `scripts/deploy-dashboard.sh` fails with `git: command not found` because the slim Python image doesn't include git.

### Acceptance Criteria

- [ ] `Dockerfile` runtime stage installs `git` via `apt-get`
- [ ] Keep the image slim — only add git, not the full build toolchain
- [ ] `scripts/deploy-dashboard.sh` runs successfully inside the container
- [ ] Dashboard deploys to gh-pages branch
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `Dockerfile` | Modify | Add `git` to runtime stage apt-get |

### Key Decisions

- Only add `git` to runtime, not build stage — deploy runs at the end of the pipeline
- Consider adding `openssh-client` too if git push uses SSH (check deploy script)

---

## Feature: balanced-summarization

**Branch:** `feature/balanced-summarization`
**Depends on:** relevance-scoring
**Status:** Not Started
**Requires:** ai

### Goal

Summarize only the top-ranked articles, balanced across categories. Replace the current "first 50 in DB order" approach with score-driven, category-aware selection.

### Acceptance Criteria

- [ ] `get_unsummarized_articles()` uses relevance score instead of insertion order
- [ ] Category round-robin: summarize top N per category (configurable, default 8 per category)
- [ ] Total summarization budget configurable (default ~50 articles)
- [ ] Dashboard only shows today's articles (not the entire DB history)
- [ ] Articles without summaries still appear in dashboard but below summarized ones
- [ ] Timing metrics still work (articles/minute, total duration)
- [ ] Tests cover category balancing and score-based selection
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/db.py` | Modify | Score-aware article selection |
| `src/summarizers/local.py` | Modify | Use scored selection |
| `src/publishers/html.py` | Modify | Filter to today's articles only |
| `tests/test_summarizer.py` | Modify | Test balanced selection |

### Key Decisions

- ~50 articles total (8 per category × 6 categories ≈ 48) fits within Ollama's throughput
- Summarized articles appear first in each category section
- Unsummarized articles appear as title-only links below

---

## Feature: structured-briefing

**Branch:** `feature/structured-briefing`
**Depends on:** balanced-summarization
**Status:** Not Started
**Requires:** ai

### Goal

Evolve the daily briefing to output structured data — the ~1000 word narrative plus a mapping of which segments reference which source articles. This enables the interactive expand/collapse UI.

### Acceptance Criteria

- [ ] Briefing prompt instructs Ollama to output JSON: `{"segments": [{"topic": "...", "text": "...", "source_article_ids": [1,2,3]}]}`
- [ ] Each segment is a thematic paragraph with IDs linking back to the article summaries that informed it
- [ ] `daily_briefings` table stores both the rendered text and the JSON segment map
- [ ] `db.get_briefing()` returns both text and segment data
- [ ] Fallback: if Ollama returns malformed JSON, store plain text briefing (no segments)
- [ ] HTML publisher passes segment data to template as JSON for JavaScript
- [ ] Tests cover JSON parsing, fallback behavior, segment-to-article mapping
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | Structured briefing prompt with JSON output |
| `src/db.py` | Modify | Store/retrieve segment map alongside briefing |
| `src/publishers/html.py` | Modify | Pass segment JSON to template |
| `tests/test_briefing.py` | Modify | Test structured output and fallback |

### Key Decisions

- JSON output from Ollama — Qwen 2.5 handles structured output reasonably well
- Graceful fallback: if JSON parsing fails, treat entire response as plain text (no expandable segments)
- Segment IDs reference article primary keys in the articles table
- Segment map stored as JSON text column in daily_briefings table

---

## Feature: interactive-dashboard

**Branch:** `feature/interactive-dashboard`
**Depends on:** structured-briefing
**Status:** Not Started
**Requires:** ai

### Goal

Transform the static HTML dashboard into a progressive-disclosure reading experience. The main ~1000 word briefing is always visible. Clicking a segment expands it to show source article summaries and action buttons.

### Acceptance Criteria

- [ ] Main briefing renders as readable paragraphs (no change to initial view)
- [ ] Each briefing segment is clickable — expands to show:
  - The individual article summaries that informed this segment
  - Article titles linked to original source URLs
  - Source attribution for each article
- [ ] Expanded view includes action buttons (placeholder — wired in research-worker feature):
  - "Elaborate" (Claude explains significance)
  - "Research" (Claude investigates a question)
  - "Sources & Further Reading" (extract citations from articles)
- [ ] Buttons are visible but disabled with "Coming soon" tooltip until research-worker is built
- [ ] Collapse/expand is smooth with CSS transitions
- [ ] Works on mobile (touch-friendly expand targets)
- [ ] Pure vanilla JavaScript — no framework dependencies
- [ ] Page still loads and reads fine with JavaScript disabled (progressive enhancement)
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `templates/dashboard.html` | Modify | Add expand/collapse UI, action buttons, JS |
| `src/publishers/html.py` | Modify | Embed segment JSON and article data in page |
| `tests/test_publishers.py` | Modify | Test that segment data is embedded correctly |

### Key Decisions

- Vanilla JS over React/Vue — keeps it a static page, no build step, Cloudflare Pages serves as-is
- Segment data embedded as `<script type="application/json">` in the page
- CSS transitions for expand/collapse (not JS animation)
- Action buttons render but are disabled until research-worker feature is complete

---

## Feature: research-worker

**Branch:** `feature/research-worker`
**Depends on:** interactive-dashboard
**Status:** Not Started
**Requires:** both

### Goal

Cloudflare Worker that proxies Claude API calls for on-demand research. Powers the action buttons on the interactive dashboard.

### Acceptance Criteria

- [ ] Cloudflare Worker at `/api/research` accepts POST with `{action, article_ids, question?}`
- [ ] Three actions:
  - `elaborate`: Claude explains significance/importance of the topic
  - `research`: Claude investigates a user-provided question about the topic
  - `sources`: Claude/Ollama extracts citations, sources, and further reading from articles
- [ ] Worker proxies to Claude API with `ANTHROPIC_API_KEY` stored as Cloudflare secret
- [ ] Response streams back to the dashboard and renders inline below the segment
- [ ] Rate limiting: max 10 requests per hour per user (prevent abuse)
- [ ] Error handling: graceful failure message if API is down or rate limited
- [ ] [HUMAN] Deploy Worker to Cloudflare (`wrangler deploy`)
- [ ] [HUMAN] Set `ANTHROPIC_API_KEY` as Cloudflare Worker secret
- [ ] Tests cover request validation and response formatting
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `worker/` | Create | Cloudflare Worker directory |
| `worker/src/index.js` | Create | Worker request handler |
| `worker/wrangler.toml` | Create | Worker configuration |
| `templates/dashboard.html` | Modify | Wire action buttons to Worker API |
| `tests/test_worker.py` | Create | Test request validation |

### Key Decisions

- Cloudflare Workers free tier: 100K requests/day — more than enough
- Claude API for elaborate/research, could use Ollama for sources extraction
- API key stored as Cloudflare secret, never exposed to client
- Streaming response for better UX on longer research queries
- Dashboard JS calls Worker via fetch(), renders response inline

---

## Feature: briefing-archive

**Branch:** `feature/briefing-archive`
**Depends on:** structured-briefing
**Status:** Not Started
**Requires:** ai

### Goal

Persist daily briefings in SQLite for historical reference and future use (e.g., trading bot, trend analysis). The daily_briefings table already exists — this feature ensures it's preserved across article cleanups and queryable.

### Acceptance Criteria

- [ ] `daily_briefings` table is never cleaned by article-retention
- [ ] Add `briefing_metadata` column: JSON with article count, categories covered, top sources
- [ ] Add `db.get_briefing_history(days=30)` method for querying past briefings
- [ ] Terminal command to view past briefings: `python -m src.cli history --days 7`
- [ ] Tests cover archive persistence across retention cycles
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/db.py` | Modify | Add metadata column, history query method |
| `src/cli.py` | Modify | Add `history` subcommand |
| `tests/test_db.py` | Modify | Test archive persistence |

### Key Decisions

- Simple SQLite storage for now — structured analytics (sentiment, entities) is future work
- Metadata captures enough context to understand the briefing without re-fetching articles
- No expiration on briefings — storage is trivial (few KB per day)

---

## Feature: claude-synthesis

**Branch:** `feature/claude-synthesis`
**Depends on:** structured-briefing
**Status:** Not Started
**Requires:** ai

### Goal

Optional upgrade: when `ANTHROPIC_API_KEY` is set, use Claude instead of Ollama for the daily briefing. Higher reasoning quality for connecting themes across categories.

### Acceptance Criteria

- [ ] `src/summarizers/cloud.py` calls Claude API with all summaries as context
- [ ] Outputs same structured JSON format as Ollama briefing (segments with source article IDs)
- [ ] Gated behind `ANTHROPIC_API_KEY` env var — falls back to Ollama if not set
- [ ] Rate-limited: one API call per pipeline run
- [ ] Tests cover prompt construction and graceful skip
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/cloud.py` | Modify | Implement Claude synthesis call |
| `src/config.py` | Modify | Add ANTHROPIC_API_KEY config |
| `src/main.py` | Modify | Try Claude first, fall back to Ollama |
| `tests/test_cloud.py` | Create | Test prompt construction and skip logic |

### Key Decisions

- Use Claude API directly (httpx), not anthropic SDK — keeps deps lighter
- Same structured output format as Ollama briefing for UI compatibility
- Max ~4000 tokens input to keep costs low

---

## Nice-to-Have

### Cloudflare Access

**Requires:** human
**When:** Once personal/local content is added (local news, portfolio data)

Add Cloudflare Access email-based authentication (free, 1 user) to restrict dashboard access. Not needed while content is public news only — URL is obscure and robots.txt blocks indexing.
