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
**Status:** Merged
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
**Status:** Merged
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
**Status:** Merged
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
**Status:** Merged
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
**Status:** Merged
**Requires:** both

### Goal

Cloudflare Worker that proxies Claude API calls for on-demand research. Powers the action buttons on the interactive dashboard.

### Acceptance Criteria

- [x] Cloudflare Worker at `/api/research` accepts POST with `{action, article_ids, question?}`
- [x] Three actions:
  - `elaborate`: Claude explains significance/importance of the topic
  - `research`: Claude investigates a user-provided question about the topic
  - `sources`: Claude/Ollama extracts citations, sources, and further reading from articles
- [x] Worker proxies to Claude API with `ANTHROPIC_API_KEY` stored as Cloudflare secret
- [x] Response streams back to the dashboard and renders inline below the segment
- [x] Rate limiting: max 10 requests per hour per user (prevent abuse)
- [x] Error handling: graceful failure message if API is down or rate limited
- [x] [HUMAN] Deploy Worker to Cloudflare (`wrangler deploy`)
- [x] [HUMAN] Set `ANTHROPIC_API_KEY` as Cloudflare Worker secret
- [x] Tests cover request validation and response formatting
- [x] All tests pass
- [x] Lint clean

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

## Feature: worker-hardening

**Branch:** `feature/worker-hardening`
**Depends on:** research-worker
**Status:** Merged
**Requires:** both

### Goal

Close the three credit-drain attack vectors in the research Worker (open CORS, client-supplied article text, per-isolate rate-limit Map) and replace the hardcoded model constant with env configuration. Ships before `wrangler secret put ANTHROPIC_API_KEY` so the Worker is hardened before any spend can occur. Single atomic PR — partial hardening creates a false sense of security.

### Acceptance Criteria

**CORS lockdown**
- [ ] `corsHeaders()` reads allowed origin from `env.DASHBOARD_ORIGIN` (set via wrangler vars, default rejects all)
- [ ] Returns `Access-Control-Allow-Origin: <env.DASHBOARD_ORIGIN>` when request `Origin` header matches; otherwise omits the header (browsers will block)
- [ ] Preflight (OPTIONS) returns 204 only for matching origin

**Request authentication via HMAC**
- [ ] Pipeline computes `HMAC-SHA256(env.DASHBOARD_HMAC_KEY, brief_id)` at brief-generation time, embeds as `<meta name="dashboard-token">` in the rendered HTML
- [ ] Dashboard JS reads the token from the meta tag and sends it as `X-Dashboard-Token` on every Worker request
- [ ] Worker rejects (401) when `X-Dashboard-Token` is absent or fails HMAC verification
- [ ] Token includes a brief-generation timestamp; Worker rejects tokens older than 48h (covers a weekend-old brief but caps replay window)

**Per-article content signing (closes the proxy hole)**
- [ ] Pipeline computes `HMAC-SHA256(env.DASHBOARD_HMAC_KEY, JSON.stringify({id, title, source, summary}))` per article when generating the brief; emits as `article.sig`
- [ ] Worker, in `resolveArticles`, recomputes the HMAC over each provided article and rejects (400) on any mismatch
- [ ] An attacker who modifies `title`/`source`/`summary` in the request body to anything not generated by the pipeline causes the request to fail
- [ ] `validateBody` adds per-article size caps: `title <= 256`, `source <= 256`, `summary <= 4096` chars

**Rate-limit migration to Workers KV**
- [ ] In-memory `rateLimitStore = new Map()` removed
- [ ] `wrangler.toml` declares `[[kv_namespaces]]` binding named `RATE_LIMIT`
- [ ] `checkRateLimit(ip, env)` does `await env.RATE_LIMIT.get(ip, 'json')` / `.put(ip, json, {expirationTtl: 3600})`
- [ ] Eventual consistency documented in code comment (~60s global propagation, acceptable for hourly limit)

**Env-configurable model**
- [ ] Hardcoded `const ANTHROPIC_MODEL = 'claude-sonnet-4-6'` removed
- [ ] Read from `env.ANTHROPIC_MODEL` with fallback to `'claude-sonnet-4-6'`
- [ ] `wrangler.toml` declares `ANTHROPIC_MODEL` under `[vars]` for non-secret default

**Request size cap**
- [ ] Worker reads `Content-Length` header; rejects (413) if > 64 KB before parsing JSON

**Tests**
- [ ] `tests/test_worker.py` adds: off-origin request rejected (403/no CORS header); missing/expired/forged HMAC token rejected (401); article with tampered `summary` rejected (400); oversized payload rejected (413); KV rate-limit increment/window-reset behavior (mock KV); env-configurable model used when set
- [ ] All existing 32 tests still pass
- [ ] Lint clean

**[HUMAN] Cloudflare setup**
- [x] [HUMAN] Create Workers KV namespace `RATE_LIMIT` via `wrangler kv:namespace create RATE_LIMIT`, paste the binding ID into `wrangler.toml`
- [x] [HUMAN] Set `wrangler secret put DASHBOARD_HMAC_KEY` (generate with `openssl rand -hex 32`); copy the same value into pipeline env as `DASHBOARD_HMAC_KEY`
- [x] [HUMAN] Set `wrangler vars` for `DASHBOARD_ORIGIN` to the Pages production URL
- [x] [HUMAN] Set `wrangler secret put ANTHROPIC_API_KEY`
- [x] [HUMAN] `wrangler deploy`
- [x] [HUMAN] Smoke-test from the live dashboard; check `wrangler tail` for any 401/400 noise

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `worker/src/index.js` | Modify | All worker-side hardening |
| `worker/src/auth.js` | Create | HMAC token + per-article signature verification helpers (~40 LOC) |
| `worker/wrangler.toml` | Modify | KV namespace binding, `[vars]` for DASHBOARD_ORIGIN + ANTHROPIC_MODEL |
| `src/pipeline.py` | Modify | Compute per-brief token + per-article signatures during brief generation; inject into rendered HTML |
| `templates/dashboard.html` | Modify | Read token from meta tag; attach `X-Dashboard-Token` header on Worker fetch; drop `articles_by_id` upload pattern (articles carry their own signatures inline) |
| `tests/test_worker.py` | Modify | Add HMAC, off-origin, tampered-article, oversized-payload, KV rate-limit test cases |
| `tests/test_pipeline.py` | Modify | Verify pipeline emits valid tokens + signatures |
| `env.example` | Modify | Add `DASHBOARD_HMAC_KEY` |

### Key Decisions

- **Signed-content over server-sourced articles.** The brief is publicly served on Cloudflare Pages — the threat model isn't content confidentiality, it's preventing the Worker from being a generic Claude proxy. Per-article HMAC signatures close the proxy hole without introducing R2/D1/KV-for-articles. Same security outcome at a fraction of the implementation cost.
- **HMAC token validity: 48h.** A brief is generated nightly and consumed the next morning. 48h covers Friday-brief-read-Sunday-night without exposing a wide replay window. Tokens regenerate every cycle, so a leaked token is naturally rotated.
- **KV over Durable Object.** Single-user dashboard, $5–10/wk budget. KV's eventual consistency (~60s) lets a coordinated multi-region attacker exceed the per-hour limit by ~10× during the window — but Anthropic's account-level spend cap is the real ceiling. DO would be over-engineering.
- **Single PR, not split.** Partial hardening (CORS-only, HMAC-only) creates a deceptive checkmark while the proxy hole stays open. Ship all three protections atomically so the security state is binary: pre-hardening (vulnerable) → post-hardening (protected).
- **No request signature on Worker → Anthropic.** That call uses `x-api-key` directly. The hardening is for the dashboard → Worker boundary; the Worker → Anthropic call is already protected by the API key.

### Notes

- The existing user-facing error string `'Claude API authentication failed. Check ANTHROPIC_API_KEY.'` (line 180 of `worker/src/index.js`) is what triggered the 2026-05-14 stranded-worker bug under the old `_detect_auth_failure` substring scan. With the new `_classify_run` cascade, this string is no longer load-bearing for workflow safety, but consider whether the user-facing copy needs softening (it's currently developer-grade error text being shown to an end user).
- `wrangler kv:namespace create` produces a binding ID; the [HUMAN] step paste-into-wrangler.toml is the one place the autonomous workflow can't proceed without a human. Build a `wrangler.toml.template` if the user wants to make this cleaner in the future.
- After deploy, check `wrangler tail` for ~5 minutes during a real dashboard session — first-time HMAC mismatch bugs (clock skew, base64 padding, etc.) show up immediately and are cheap to fix before going live for real.
- This feature does NOT change the AI prompts in `buildPrompt` — `elaborate`/`research`/`sources` semantics are preserved. The hardening is purely at the request boundary.

---

## Feature: briefing-archive

**Branch:** `feature/briefing-archive`
**Depends on:** structured-briefing
**Status:** Merged
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
**Status:** Merged
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

## Feature: signal-emit

**Branch:** `feature/signal-emit`
**Depends on:** relevance-scoring, atrade `fixed-universe`
**Status:** Merged (#44)
**Requires:** both
**Contract:** `docs/SIGNAL-SCHEMA.md` — APPROVED 2026-09-01. Build to it exactly; do not redesign it.

### Goal

Emit a point-in-time, per-ticker signal artifact for the **atrade** project to consume. This is the *producer* side of a versioned contract shared with atrade (`/home/comp/Projects/atrade`). morning-brief already scores *articles* for relevance; this feature maps that news flow onto a fixed ticker universe, aggregates a per-ticker score, stamps each with the timestamp it was actually knowable, records provenance, and writes a stable, versioned artifact atrade reads.

This is a **shared contract** — the schema is defined once and versioned. It must match atrade's consumer (`docs/SIGNAL-SCHEMA.md` in atrade, feature `signal-schema-and-ingest`). Coordinate both sides before either merges.

**The contract is settled — read `docs/SIGNAL-SCHEMA.md` before writing any code here.** It was
signed off 2026-09-01 and is authoritative: artifact path and shape (§2), field rules (§3),
mean aggregation (§4), the `knowable_at` derivation including the nullable-`published_at`
fallback (§5), and provenance (§7). Do not re-derive any of it from these acceptance criteria.

**Prerequisite — satisfied 2026-09-01.** atrade's `fixed-universe` merged
(`Psaphon/atrade#11`), so `/home/comp/Projects/atrade/config/universe.toml` now exists and is
the authority for which symbols exist. **Read it; do not invent tickers.** It holds 27 symbols
(12 ETFs, 15 mega-cap stocks) in this shape:

```toml
schema_version = "1.0.0"

[[tickers]]
symbol = "SPY"
asset_class = "etf"
name = "SPDR S&P 500 ETF Trust"
```

`config/ticker_map.toml` must cover **exactly** those 27 symbols — no extras, none missing. A
symbol here that is absent from the universe produces signals atrade will reject outright
(contract §6); a universe symbol missing here silently produces no signals for that ticker,
with no error. Add a test asserting the two sets are equal, so the drift §10.4 accepts as a
`[HUMAN]` risk at least fails loudly on this side.

**Build order (contract §11):** atrade `fixed-universe` → morning-brief `signal-emit`
(producer) → atrade `signal-schema-and-ingest` (consumer).

### Acceptance Criteria

- [x] Maps scored articles to tickers via a committed ticker→keyword/entity map covering atrade's fixed universe (config-driven; unmatched articles contribute to no ticker)
- [x] Aggregates matched articles into a per-ticker numeric score (bounded, rank-able — never free text)
- [x] Each emitted signal record carries: `schema_version`, `ticker`, `score`, `knowable_at` (the article's publish/ingest time — the point-in-time control), and `provenance` (contributing article IDs + sources)
- [x] `knowable_at` is derived from real article timestamps, never "now at emit time" — no look-ahead leakage into the artifact
- [x] Writes a versioned artifact to a stable path morning-brief and atrade agree on (e.g. `data/signals/signals-<date>.json`, schema_version pinned); atomic write (temp + rename)
- [x] Emitting is optional/gated and never crashes the main pipeline if it fails (one broken stage must not break the brief)
- [x] [HUMAN] Coordinate the schema + output path with atrade's `signal-schema-and-ingest` before merge; version it; this feature must land before atrade's strategy layer depends on it
- [x] All tests pass
- [x] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/publishers/signals.py` | Create | Map articles→tickers, aggregate, write versioned artifact |
| `config/ticker_map.toml` | Create | Ticker → keyword/entity map over atrade's fixed universe |
| `src/main.py` | Modify | Call the signal emitter after scoring (gated, non-fatal) |
| `src/config.py` | Modify | Config for emit toggle + output path |
| `tests/test_signals.py` | Create | Aggregation, knowable_at correctness, no-look-ahead, atomic write |

### Key Decisions

- **Score is bounded and auditable**, never free text — mitigates prompt-injection from poisoned news sources (atrade treats it as non-dispositive anyway).
- **`knowable_at` comes from the article, not emit time** — the single most important correctness property; it is the look-ahead-bias control the whole atrade backtest rests on.
- **Versioned artifact, atomic write** — atrade may read while morning-brief writes; never expose a partial file.
- Ticker universe is owned by atrade (`config/universe.toml`); morning-brief's ticker_map references the same symbols. Keep them in sync.

### Cross-Repo Note

This is the morning-brief half of a two-repo contract flagged by the PM:
`signal-emit` (this, producer) ↔ atrade `signal-schema-and-ingest` (consumer).
Build/coordinate this **before** atrade's `strategy-and-risk-gate` (which depends
on `signal-schema-and-ingest`) so the strategy layer has a real, agreed signal feed.

---

## Nice-to-Have

### Cloudflare Access

**Requires:** human
**When:** Once personal/local content is added (Manatee County, portfolio data)

Add Cloudflare Access email-based authentication (free, 1 user) to restrict dashboard access. Not needed while content is public news only — URL is obscure and robots.txt blocks indexing.

---

## Feature: failure-alerting

**Branch:** `fix/failure-alerting`
**Depends on:** signal-emit; hub live with `ntfy-stack-startup` and `data-backup-runtime` deployed (hub #64, #66)
**Status:** Merged (#52)
**Requires:** both

### Goal

A failed morning-brief run must reach the operator's phone. Today it reaches nobody. Found 2026-09-14 while fixing the timer timezone (#47), re-scoped 2026-10-01 against the live hub install (#51):

- The unit that actually runs is the repo-root `morning-brief.service`, linked into hubop's user manager by `scripts/install-user-units.sh` from the pipeline clone `~/.local/share/morning-brief-pipeline`. It has no `OnFailure=` at all. (`deploy/` is the old root `/opt` install, unused on hub; out of scope.)
- `morning-brief-failure@.service` does not exist anywhere in the repo.
- hubop **cannot read its own journal** on hub (`journalctl --user` → "insufficient permissions"), and the run's output does not reach `/var/log/syslog`. A failed run today leaves the operator nothing to read. The run's output must also land in a file hubop can read.
- hubop's user manager PATH has no `/usr/local/sbin`, so a bare `hub-alert` in a user unit is "command not found". Call it by absolute path.
- A late or failed run matters downstream: atrade's cycle at 06:30 Eastern falls back to baseline-only when that day's signal artifact is missing, silently.

The alert path is hub's ntfy stack (`/usr/local/sbin/hub-alert`, which reads `/etc/hub/ntfy.env`; hub #64/#66/#156).

### Acceptance Criteria

- [x] `OnFailure=morning-brief-failure@%n.service` is in `[Unit]` of the repo-root `morning-brief.service`
- [x] The run's stdout+stderr go to `%S/morning-brief-last-run.log` (`append:`, emptied by the first `ExecStartPre`), so the last run is always readable by hubop without journal access. Not in a `StateDirectory=` subdirectory: systemd opens stdio before creating it (209/STDOUT on hub, 2026-10-01, fixed in a follow-up PR)
- [x] A `morning-brief-failure@.service` template exists at the repo root and `scripts/install-user-units.sh` links it next to the main unit
- [x] The failure unit runs `scripts/notify-failure.sh %i`, which calls hub's publisher by absolute path (`${HUB_ALERT:-/usr/local/sbin/hub-alert} "<title>" "<body>" high`) with a title naming the failed unit, and a body holding the last ~15 lines of `last-run.log` plus the command to read the whole file; it never hardcodes an ntfy host
- [x] If the publisher is missing or not executable, or `hub-alert` exits non-zero, `notify-failure.sh` writes an error to stderr and exits non-zero rather than succeeding silently
- [x] A test runs `systemd-analyze --user verify` on every `*.service`/`*.timer` at the repo root (template units via an instance name) and fails on any `Unknown key` / `Unknown section` line; it skips only when `systemd-analyze` is absent AND `CI` is unset, so CI can never skip it
- [x] A test runs `notify-failure.sh` with `HUB_ALERT` pointing at a recording stub and asserts the title, priority and body it receives; it fails if the stub was never called (a non-executable stub must not fall through to the real publisher)
- [x] Mutation-checked: moving `OnFailure=` into `[Service]`, deleting the failure unit, or dropping it from `install-user-units.sh` turns the suite red
- [x] [HUMAN] On the live hub, force a failing run and confirm the push reaches the phone with the log tail in it (2026-10-01: alert received; the real 08:15 failure also alerted, which surfaced the 209/STDOUT bug fixed in #54)
- [x] All tests pass
- [x] Lint clean

---

## Feature: signals-golden-test

**Branch:** `feature/signals-golden-test`
**Depends on:** none
**Status:** Merged (#67)
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
**Status:** Merged (#77)
**Requires:** both

### Goal

Change the per-article step from free prose to validated JSON facts. Use the article's own title as the headline, and flag roundup articles.

### Acceptance Criteria

- [x] `summarize_single` calls Ollama with a structured-output JSON schema (`format`). Each request the stub records includes the schema, `num_ctx`, temperature and seed from config, and the article's published date.
- [x] Schema fields:
  - `what_happened`: at most 2 sentences and `EXTRACT_MAX_WORDS` words (default 50).
  - `entities`: names copied exactly from the article.
  - `numbers`: figures copied exactly.
  - `event_date`: `YYYY-MM-DD` or null, resolved against the published date.
  - `status`: `happened`, `scheduled` or `ongoing`.
  - `is_roundup`: bool.
- [x] With the stub returning valid JSON, the fixture run stores one row per article in a new `extractions` table (`article_id` PK, `json`, `model`, `created_at`). The existing `summary` column holds `what_happened`, so the page keeps working.
- [x] With the stub returning malformed JSON twice for one article, that article is marked `extraction_failed` and left out of the briefing. The run exits 0 and logs the article id.
- [x] A `what_happened` over the word limit is cut at the last sentence boundary within the limit (unit test).
- [x] Articles with `is_roundup: true` are left out of the briefing input but still appear in the signals output.
- [x] The signals golden test passes unchanged.
- [x] [HUMAN] On hub, the PM runs extraction live over one day's articles, reports the JSON-valid rate (target 95% or higher), and pastes 5 sample extractions into the PR.
- [x] All tests pass
- [x] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | New extraction prompt and call |
| `src/summarizers/schema.py` | Create | JSON schema, parsing, word-limit trim |
| `src/config.py` | Modify | `OLLAMA_NUM_CTX`, `EXTRACT_TEMPERATURE`, `EXTRACT_SEED`, `EXTRACT_MAX_WORDS`, `EXTRACT_NUM_PREDICT`, `CHARS_PER_TOKEN` |
| `src/db.py` | Modify | `extractions` table and `extraction_failed` flag |
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
**Status:** Merged (#78)
**Requires:** ai

### Goal

Before anything is rendered, check every generated sentence in code and drop the ones that aren't grounded in their source or that contain filler.

### Acceptance Criteria

- [x] **Number check:** each digit group in a sentence (commas stripped) must appear in its source text. Unit cases: "$44.6 million" against a source containing "$44.6M" passes; "$45.6 million" fails.
- [x] **Name check:** each capitalized token that isn't sentence-initial, is at least 3 characters, and isn't in `config/name_allowlist.txt` must match the source case-insensitively on its first `max(4, len-2)` characters. Unit cases: "Democrats" against "Democratic" passes; "Christie's" against a source without it fails.
- [x] **Banned-phrase check:** case-insensitive match against `config/banned_phrases.txt`, plus the overlay at `$MORNING_BRIEF_BANNED_OVERLAY` if that file exists. Unit case: "This highlights the volatility" fails. With the overlay file missing, there is no error.
- [x] The source for `what_happened` is the article's title plus its text (the same 12,000-character cut). The source for a section lede is that section's bullets.
- [x] With `VALIDATOR_MODE=enforce`, the fixture run with one ungrounded sentence from the stub renders without it, and the run metadata reports `dropped: {number: 1}`. With `warn`, the sentence is kept and a warning is logged.
- [x] If every sentence of an extraction is dropped, the article is left out of the briefing.
- [x] Each run writes sentences checked and sentences dropped by reason to the run metadata.
- [x] The signals golden test passes unchanged.
- [x] All tests pass
- [x] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/validators/__init__.py` | Create | Package |
| `src/validators/grounding.py` | Create | Number, name and banned-phrase checks |
| `config/banned_phrases.txt` | Create | Seed list (see Notes) |
| `config/name_allowlist.txt` | Create | Common capitalized words: months, weekdays, "The", acronyms |
| `src/config.py` | Modify | `VALIDATOR_MODE`, overlay path |
| `src/main.py` (`run_pipeline`) | Modify | Run the validator after extraction and after the briefing |
| `tests/test_validator.py` | Create | The criteria above |

### Key Decisions

- Drop the whole sentence rather than rewrite it. A 7B rewrite would introduce new errors.
- The validator is a backstop. It can't catch a name that appears in the source in a different role. The main defence is structural: one article per bullet.

### Notes

Seed `banned_phrases.txt` (one per line): `highlights`, `underscores`, `key actors`, `key players`, `landscape`, `it is worth noting`, `plays a crucial role`, `significant shifts`, `these developments`, `will be closely monitored`, `shape the future`, `pave the way`, `ever-evolving`, `navigating`, `the importance of`, `a testament to`, `sheds light`, `delve`, `in today's`, `robust`.

## Feature: briefing-dedupe

**Branch:** `feature/briefing-dedupe`
**Depends on:** signals-golden-test
**Status:** Merged (#68)
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
| `src/db.py` | Modify | `duplicate_of` column |
| `tests/test_dedupe.py` | Create | The criteria above |

### Key Decisions

- Match on titles only, before summarizing, so duplicates never use up an LLM call.
- Plain word overlap (Jaccard) missed the real case: two Christie's titles scored about 0.2. The shared-figure rule catches it.

## Feature: interest-filter

**Branch:** `feature/interest-filter`
**Depends on:** signals-golden-test
**Status:** Merged (#70)
**Requires:** both

### Goal

Rank briefing articles by the operator's interests, and stop high-volume feeds such as arXiv from filling sections with off-topic papers.

### Acceptance Criteria

- [x] The interests TOML is read from `$MORNING_BRIEF_INTERESTS` (default `~/.config/morning-brief/interests.toml`, uncommitted). It has a `[keywords]` table (`term = weight`, 1–5) and `[feeds."<source>"] min_interest = N`, where `<source>` is the article's stored `source` value. `config/interests.example.toml` is committed with generic placeholders.
- [x] An article's interest score is the sum of the weights of keywords found in its title plus first 2,000 characters (case-insensitive, matched on word boundaries).
- [x] Within a category, articles are ordered by interest score, then by current order. Articles from a feed with `min_interest` that score below it are not summarized. Fixture: a feed item with no matching keyword is excluded, and one with a match is included.
- [x] `$MORNING_BRIEF_INTERESTS` accepts several paths separated by `:`. Keywords are merged across files, and when the same keyword appears in more than one file, the highest weight wins. Fixture: two files sharing a keyword at weights 2 and 4 score it as 4.
- [x] A file may set a top-level `label = "<text>"`. Bullets whose article matches any keyword from that file show the label (several labels are de-duplicated). Fixture: a file labelled `On radar` tags only the matching bullets in the rendered page JSON.
- [x] Listed files that are missing are skipped with one INFO line each. With no files present at all, selection is identical to before (a test compares the outputs).
- [x] The signals golden test passes unchanged.
- [ ] [HUMAN] The operator writes the interests file on hub.
- [x] All tests pass
- [x] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/interest.py` | Create | Load the file and score articles |
| `config/interests.example.toml` | Create | Generic example |
| `src/summarizers/local.py` | Modify | Order and filter in `select_balanced_articles` |
| `src/config.py` | Modify | Interests path |
| `tests/test_interest.py` | Create | The criteria above |

### Key Decisions

- Keyword weights, not an LLM relevance call: free, predictable, and easy to tune.
- Confirmed by the PM: signals are built from `db.get_todays_articles()` (every stored article) and emitted before summarization selection in `src/main.py`, so this filter cannot change atrade's input. The golden test still guards it.

## Feature: category-briefing

**Branch:** `feature/category-briefing`
**Depends on:** article-extraction, output-validator
**Status:** Ready (supervised)
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
| `templates/` | Modify | Section: title, lede, bullets (linked title, `what_happened`, source, also-covered) |
| `src/publishers/html.py` | Modify | New page JSON shape |
| `tests/test_briefing.py` | Create | The criteria above |

### Key Decisions

- **Bullets are the extractions themselves, not rewritten.** The model never paraphrases across articles, so it can't merge two stories into one.
- **Titles come from config.** Model-written titles were the source of the false "Upcoming Events and Trends" framing.
- **Per-category calls fix the silent truncation risk.** The worst case was about 12k tokens in a single call.

## Feature: dates-ahead

**Branch:** `feature/dates-ahead`
**Depends on:** category-briefing
**Status:** Held (deps)
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
| `templates/` | Modify | Dates-ahead block |
| `tests/test_dates_ahead.py` | Create | The criteria above |

## Feature: stack-feeds

**Branch:** `feature/stack-feeds`
**Depends on:** signals-golden-test
**Status:** Ready (supervised)
**Requires:** both

### Goal

Fetch sources relevant to the tools this fleet runs, and show them in the brief, without touching atrade's input.

### Acceptance Criteria

- [ ] `docs/FEEDS.md` tables gain an optional `Signals` column (`yes`/`no`, default `yes` when the column or cell is absent). `parse_feeds_md` returns it, and existing tables parse unchanged.
- [ ] Articles from `Signals = no` feeds are stored, summarized and shown, but excluded from the scorer's and `emit_signals`' input.
- [ ] Golden test: a `Signals = no` fixture article that matches a ticker alias leaves the signals output byte-identical. The same article on a `Signals = yes` feed changes it (liveness check).
- [ ] A new `FEEDS.md` section, "For the stack", becomes category `stack` with display name "For the stack". Its seed feeds all have `Signals = no`: GitHub release Atom feeds for ollama/ollama, anthropics/claude-code, tailscale/tailscale, binwiederhier/ntfy, astral-sh/ruff and comfyanonymous/ComfyUI; the Cloudflare blog; the Ollama blog; the Hacker News front page (with a high `min_interest` once `interest-filter` exists).
- [ ] The signals golden test passes unchanged, apart from the new liveness fixture.
- [ ] [HUMAN] The PM verifies each feed URL on hub and confirms the first run fetches them.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `docs/FEEDS.md` | Modify | `Signals` column, "For the stack" section |
| `src/fetchers/rss.py` | Modify | Parse `Signals` |
| `src/main.py` | Modify | Keep `Signals = no` articles out of scoring and signals |
| `tests/` | Modify | Parser, golden test, liveness fixture |

### Key Decisions

- atrade boundary exception: this adds feeds, but the signals output must stay byte-identical for them; the golden test proves it.

## Feature: radar-flags

**Branch:** `feature/radar-flags`
**Depends on:** interest-filter, output-validator
**Status:** Held (deps)
**Requires:** ai

### Goal

Write stack-relevant articles to a flag outbox that a downstream triage tool ingests. The local model only casts a cheap keyword net; judging happens downstream.

### Acceptance Criteria

- [ ] An interests file may set `action = "flag"` and `min_score` (default 3). Articles scoring at or above it on a flag file are appended to `$MORNING_BRIEF_FLAG_OUTBOX/YYYY-MM-DD.jsonl`. With the env var unset, the feature is off and nothing is written.
- [ ] Record fields only: `url`, `title`, `source`, `published` (UTC), `matched_keywords`, `score`, `what_happened` (only if it passed the validator, otherwise null). Never full text. Title capped at 200 characters, `what_happened` at 60 words, control characters stripped. A test walks the file.
- [ ] At most `FLAGS_PER_RUN` (config, default 5) per run, highest score first. URLs flagged in the last 30 days are skipped (state file). Writes are atomic (temp file plus rename).
- [ ] The signals golden test passes unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Key Decisions

- Flag records come from web articles and are read by an agent with privileges downstream, so they are treated as untrusted data: fixed fields, capped lengths, no full text.
- The interests file with `action = "flag"` lives outside the repo; the committed example stays generic.

## Feature: signal-direction

**Branch:** `feature/signal-direction`
**Depends on:** article-extraction; atrade `signal-direction-reader` merged
**Status:** Ready (supervised)
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

## Feature: source-audit

**Branch:** `feature/source-audit`
**Depends on:** none
**Status:** Merged (#74)
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
**Status:** Merged (#75)
**Requires:** ai

### Goal

Give each feed explicit lanes (`reader`, `signals`, `rnd`), so the news the operator reads, atrade's inputs and R&D material can each change without affecting the others.

### Acceptance Criteria

- [x] Feed config gains `lanes` (a list). `reader` feeds the briefing. `signals` feeds the scorer. `rnd` feeds only `radar-flags` and is never shown in the briefing.
- [x] The existing `signals = false` flag is migrated: `false` becomes lanes without `signals`. Feeds with no `lanes` keep today's behaviour (`reader` + `signals`).
- [x] arXiv feeds move to `rnd` only. The fixture run shows no arXiv item in the page JSON while its flag records still appear.
- [x] A fixture feed in `signals` only appears in signals output but not on the page; a `reader`-only feed does the reverse.
- [x] The signals golden test passes unchanged with today's feed config.
- [x] Feeds from `$MORNING_BRIEF_FEEDS_OVERLAY` merge over the committed config, matched by feed id. A missing overlay file is skipped with one INFO line.
- [x] All tests pass
- [x] Lint clean

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
**Depends on:** feed-lanes, article-extraction, output-validator (extraction schema and validator)
**Status:** Merged (#79)
**Requires:** both

### Goal

Cover more articles, with richer extracted facts, so section summaries can run 2–5× longer.

### Acceptance Criteria

- [x] `READER_MAX_PER_CATEGORY` (default from Open Questions) replaces the fixed 8. A fixture with 20 eligible articles in one category summarizes exactly the configured number, chosen by interest score.
- [x] `EXTRACT_MAX_WORDS` defaults to 120, and the extraction schema gains `details`: up to 4 short factual sentences beyond `what_happened`, under the same validator.
- [x] Each run logs its total duration and per-stage durations. A run passing `RUN_DEADLINE_MINUTES` logs a warning (fixture with a stub clock).
- [x] [HUMAN] The PM confirms on hub that one full run finishes within the deadline, and posts the timing.
- [x] The signals golden test passes unchanged.
- [x] All tests pass
- [x] Lint clean

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
**Status:** Ready (supervised)
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
**Status:** Ready (supervised)
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
**Depends on:** feed-lanes, article-extraction, output-validator (`what_happened` and the validator)
**Status:** Ready (supervised)
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
**Status:** Held (deps)
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

## Feature: fetch-contact

**Branch:** `feature/fetch-contact`
**Depends on:** none
**Status:** Not Started
**Requires:** both

### Goal

Every outbound fetch identifies the tool and a way to reach the operator, and the public page shows the same contact.

### Acceptance Criteria

- [ ] All HTTP requests (feeds, article extraction, and later EDGAR and wires) go through one client that sends a single User-Agent built from config: tool name, version, and `CONTACT_EMAIL` or `CONTACT_URL`. A stub server records the header on every request type.
- [ ] With `CONTACT_EMAIL` unset, feed fetches still run with a contact-less User-Agent and log one WARNING.
- [ ] The page build adds a small "Contact" footer line from `CONTACT_EMAIL` when set, and leaves it out when unset. A test builds the page both ways.
- [ ] The address never reaches git or logs: a test scans tracked files and captured logs for the configured value.
- [ ] [HUMAN] The operator creates the alias. The PM sets `CONTACT_EMAIL` on hub, sends a test email to the alias, and confirms it arrived with a phone notification.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/http.py` *(confirm; reuse an existing client if there is one)* | Create | Shared client and User-Agent |
| `src/config.py` | Modify | `CONTACT_EMAIL`, `CONTACT_URL` |
| page template *(confirm path)* | Modify | Contact footer |
| `tests/test_fetch_contact.py` | Create | The criteria above |

### Key Decisions

- **An alias, not a real mailbox.** It forwards to an inbox the operator watches, doesn't reveal the real address, and can be switched off and replaced if it attracts spam.
- **The address is injected at build time.** It appears on the deployed page but never in the public repo.

## Feature: filings-contract

**Branch:** `feature/filings-contract`
**Depends on:** feed-lanes
**Status:** Ready (supervised)
**Requires:** ai

### Goal

Define the filings-lane file and the backfill history file in SIGNAL-SCHEMA, and add a CIK to every stock in the alias map, without changing any existing signal.

### Acceptance Criteria

- [ ] `docs/SIGNAL-SCHEMA.md` gets the next minor version and a new section for `signals-filings.json` and `filings-history.jsonl`, following §8's rules. The section covers:
  - every field and its type
  - the enums: `lane` = `filings`; `event` = `earnings`, `guidance`, `acquisition`, `agreement`, `exec_change`; `origin` = `edgar`, `wire`, `both`
  - the `direction` range [-1, 1], or null
  - `direction_n`, the count of verified components
  - the component fields: `eps_yoy`, `rev_yoy`, `rev_accel`, `guidance_change`
  - `in_model_window` (bool)
  - the `knowable_at` rule from Constraints
- [ ] A JSON schema file for both new files is committed. A test validates the synthetic fixtures against it.
- [ ] `config/ticker_map.toml` gains `cik` for every stock symbol and none for ETFs. A test loads the real file and fails if any stock lacks a 10-digit CIK or any ETF has one.
- [ ] The signals golden test passes unchanged: adding `cik` does not alter matching.
- [ ] The same SIGNAL-SCHEMA text lands in atrade (companion `filings-reader`) the same day.
- [ ] [HUMAN] The PM checks each CIK against EDGAR's company search on hub.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `docs/SIGNAL-SCHEMA.md` | Modify | Version bump, new section |
| `docs/schemas/signals-filings.schema.json` | Create | Machine-checkable schema |
| `config/ticker_map.toml` | Modify | `cik` per stock |
| `tests/fixtures/filings/` | Create | Synthetic files for both formats |
| `tests/test_filings_contract.py` | Create | The criteria above |

### Key Decisions

- **Separate file, not new records in the existing file.** atrade's current reader ignores unknown fields. If filings records arrived in the existing file, they would silently mix into the news shadow arm. A separate file makes that impossible.
- `direction` is defined here independently of `signal-direction`'s field, so this plan doesn't wait on it. The two may be unified later.

## Feature: edgar-fetcher

**Branch:** `feature/edgar-fetcher`
**Depends on:** filings-contract, fetch-contact
**Status:** Ready (supervised)
**Requires:** both

### Goal

Each run, fetch new 8-K filings for the universe's stocks from EDGAR, with their exhibit press releases and exact acceptance times.

### Acceptance Criteria

- [ ] `morning-brief filings fetch` reads the CIKs from the alias map and gets each company's recent filings from EDGAR's JSON submissions API *(PM verifies the endpoint)*. For each new 8-K it stores:
  - accession number
  - CIK and ticker
  - acceptance time (UTC)
  - item numbers
  - the primary document's and Exhibit 99.x's text
  - `origin = edgar`
  - fetch time
- [ ] Only items listed in `FILINGS_ITEMS` are stored (default `2.02,7.01,8.01,1.01,2.01,5.02`). Re-running is idempotent: accession numbers are unique.
- [ ] Acceptance-time conversion is tested on fixtures on both sides of a daylight-saving change.
- [ ] [HUMAN] The PM confirms which zone the API's acceptance time is really in, by comparing one known filing with its EDGAR index page, and records the answer in a code comment and the test.
- [ ] With `CONTACT_EMAIL` unset, the command exits 0, logs one WARNING and stores nothing. A stub HTTP server records that no request was made.
- [ ] Against the stub server:
  - every request carries the User-Agent
  - no more than `SEC_MAX_RPS` requests are sent in any one-second window
  - a 429 triggers backoff
  - repeated 403s stop the run for that day with an ERROR
  - the contact address is absent from captured logs
- [ ] The daily `morning-brief` run calls the fetch step when the `filings` lane is enabled. A fetch failure is logged and never fails the brief.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] With `CONTACT_EMAIL` already set by `fetch-contact`, the PM confirms one real run stores the latest earnings 8-K for at least one stock.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/fetchers/edgar.py` | Create | Client, rate limiter, parsing |
| `src/storage/filings.py` *(confirm)* | Create | `filings` table |
| `src/cli.py` *(confirm)* | Modify | `filings fetch` |
| `src/config.py` | Modify | `SEC_MAX_RPS`, `FILINGS_ITEMS`, lane toggle |
| `tests/fixtures/edgar/` | Create | Synthetic submissions JSON and filings |
| `tests/test_edgar_fetcher.py` | Create | The criteria above |

### Key Decisions

- **Per-company polling of the submissions API**, not the all-filers "latest filings" feed. The universe is small, the volume is a handful of requests per run, and it gives item numbers and acceptance times in one place.
- **Earnings releases are 8-K Item 2.02** with the press release attached as Exhibit 99.1, so EDGAR alone covers both the filing and the release.

## Feature: wire-feeds

**Branch:** `feature/wire-feeds`
**Depends on:** edgar-fetcher
**Status:** Ready (supervised)
**Requires:** both

### Goal

Add press-release wire feeds to the `filings` lane, matched to universe stocks only by exchange-ticker tags, and merged with the EDGAR copy of the same release.

### Acceptance Criteria

- [ ] Feeds with `lanes = ["filings"]` and `kind = "wire"` are fetched by the shared feed fetcher. They never appear in the reader or signals lanes (a fixture proves both).
- [ ] An item matches a stock only through an exchange tag, such as `(NYSE: XYZ)` or `(Nasdaq: XYZ)`, for a universe stock. Alias text is never used. Fixtures:
  - an item that mentions the company name without a tag is not matched
  - a tag for a non-universe ticker is not matched
  - ETFs are never matched
- [ ] Matched items are stored in `filings` with `origin = wire` and their publish time.
- [ ] A wire item and an EDGAR filing for the same ticker are merged into one event (`origin = both`) when both hold:
  - they fall within `MERGE_WINDOW_HOURS` (default 24)
  - their normalized opening text is similar enough (`MERGE_SIMILARITY`, default 0.8)

  The merged event keeps the earlier timestamp as `knowable_at`, and extraction uses the EDGAR text. A fixture pair proves it.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] The PM checks which wire each universe stock uses (from its latest earnings release), adds only verified free, full-text feeds to config, and confirms a real run fetches them.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| feed config *(confirm path)* | Modify | Wire feeds, `filings` lane |
| `src/fetchers/wire.py` | Create | Ticker-tag matching |
| `src/processors/filings_merge.py` | Create | Wire and EDGAR merge |
| `tests/fixtures/wire/` | Create | Synthetic feeds |
| `tests/test_wire_feeds.py` | Create | The criteria above |

### Key Decisions

- Exchange tags are precise. The existing alias-in-text matching is noisy, and this lane must not inherit that.

### Notes

GlobeNewswire publishes public RSS feeds, including site-wide, public-company, subject and industry feeds. PR Newswire publishes RSS and full-text feeds. The PM verifies the current URLs.

## Feature: filing-facts

**Branch:** `feature/filing-facts`
**Depends on:** edgar-fetcher
**Status:** Ready (supervised)
**Requires:** both

### Goal

Extract earnings and guidance numbers from each earnings release, verify them against the text, and compute a bounded direction in code.

### Acceptance Criteria

- [ ] For each stored `2.02` event (or wire earnings release), one local-model call returns JSON matching a strict schema:
  - diluted EPS, current and prior-year period
  - revenue, current and prior-year period
  - the guidance range for the next period or year, if given (low, high, metric, period)
  - each value as written plus its scale ("in millions")

  Invalid JSON is retried once, then stored as failed.
- [ ] Every extracted number is checked against the source text after normalization: commas, currency symbols, parentheses for negatives, and "billion"/"million". An unverified value is set to null and counted. Fixtures:
  - a release whose numbers are all present verifies fully
  - an altered number is nulled
- [ ] Code computes the components:
  - `eps_yoy` and `rev_yoy`, as % change between the current and prior-year values from the same release, so the scale cancels
  - `rev_accel`: this quarter's `rev_yoy` minus the same company's previous quarter's, from stored history
  - `guidance_change`: +1, 0 or -1, comparing this release's guidance midpoint with the same metric and period's midpoint from the company's previous release in the table, beyond `GUIDANCE_TOLERANCE`
- [ ] `direction` = `clip(Σ wᵢ·tanh(componentᵢ / kᵢ), -1, 1)`, over verified components only, with weights renormalized. Weights and `k` come from config. `direction_n` is the number of components used; with none, direction is null.
- [ ] A test with a stub model shows:
  - the prompt contains no instruction to judge good or bad news
  - the model's output can't set `direction` directly (an injected `"direction": 1` in the output is ignored)
- [ ] The call sends `num_ctx` and the deterministic temperature and seed from config, and trims long releases to the financial tables and highlights *(implementation decides how)* within the context budget.
- [ ] [HUMAN] The PM runs it on hub on the last four earnings releases of three universe stocks and posts each extracted value next to the release text in a PR comment.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/filing_facts.py` | Create | Extraction call, verification |
| `src/processors/filing_direction.py` | Create | Components and direction |
| `src/summarizers/schema.py` *(confirm)* | Modify | Extraction schema |
| `src/config.py` | Modify | Weights, `k`, tolerance |
| `tests/test_filing_facts.py` | Create | The criteria above |

### Key Decisions

- **Year-over-year growth is a stand-in for "surprise".** Analyst consensus isn't freely available. Acceleration and guidance changes are kept as separate components so the event study can test each one.
- **Direction is code, not model judgement**, for injection resistance and to limit the model recalling outcomes.

## Feature: filings-signals

**Branch:** `feature/filings-signals`
**Depends on:** filing-facts, wire-feeds, atrade `filings-reader` merged
**Status:** Ready (supervised)
**Requires:** ai

### Goal

Emit `signals-filings.json` each run, next to the existing signals file.

### Acceptance Criteria

- [ ] Each run writes `signals-filings.json` atomically (temp file and rename), with one record per event whose `knowable_at` falls within `FILINGS_EMIT_DAYS` (default 30).
- [ ] The output validates against the committed schema. A test walks every string, and only enum values, tickers, timestamps and pattern-checked accession numbers are allowed.
- [ ] A golden file for the filings output is added (`tests/fixtures/brief_fixture/signals-filings.golden.json`). The existing signals golden test passes unchanged.
- [ ] The run time never appears as `knowable_at`: a fixture whose fetch happens two days after acceptance keeps the acceptance time.
- [ ] With the lane disabled, no file is written and the existing output is byte-identical.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/publishers/signals_filings.py` | Create | Writer |
| `src/cli.py` *(confirm)* | Modify | Call it in the daily run |
| `tests/fixtures/brief_fixture/signals-filings.golden.json` | Create | Golden output |
| `tests/test_filings_signals.py` | Create | The criteria above |

## Feature: filings-backfill

**Branch:** `feature/filings-backfill`
**Depends on:** filing-facts
**Status:** Held (deps)
**Requires:** both

### Goal

Rebuild the filings history from EDGAR's archive so the hypothesis can be tested on years of events, instead of waiting a year for new ones.

### Acceptance Criteria

- [ ] `morning-brief filings backfill --since YYYY-MM-DD` does the following for every universe stock:
  - fetches historical earnings 8-Ks and their exhibits, including the API's older paged files
  - stores them in the same `filings` table
  - extracts facts with the same code as the daily run
  - writes `data/filings-history.jsonl` in the contract format
- [ ] Each record carries `in_model_window` = `knowable_at` earlier than `FILINGS_MODEL_CUTOFF` (runtime config, the configured model's training-data cutoff).
- [ ] Resumable: a second run skips stored accession numbers. A stub clock past `BACKFILL_WINDOW_UTC` stops the run cleanly at the next item, with a summary line.
- [ ] Rate limiting and the User-Agent rule are the same as `edgar-fetcher`'s (shared client; a test shows it).
- [ ] [HUMAN] The PM sets `FILINGS_MODEL_CUTOFF` from the model card, runs the backfill on hub, and posts these counts in the PR:
  - events per stock
  - verification rate
  - `direction_n` distribution
  - in-window vs out-of-window counts
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/fetchers/edgar.py` | Modify | Historical paging |
| `src/processors/filings_backfill.py` | Create | Resumable, window-bound runner |
| `src/cli.py` *(confirm)* | Modify | `filings backfill` |
| `tests/test_filings_backfill.py` | Create | The criteria above |

### Notes

Only events after the model's cutoff are fully free of the model having seen what came next. Earlier events are reported, but kept separate.

## Feature: filing-events

**Branch:** `feature/filing-events`
**Depends on:** filings-signals
**Status:** Ready (supervised)
**Requires:** ai

### Goal

Emit non-earnings 8-K events (agreements, acquisitions, executive changes, standalone guidance updates) as typed events, so they can be tested separately.

### Acceptance Criteria

- [ ] Items map to events in code, with no model call:
  - `1.01` → `agreement`
  - `2.01` → `acquisition`
  - `5.02` → `exec_change`
- [ ] A `7.01` or `8.01` release, or an untagged-item wire release, becomes `guidance` only when `filing-facts` extracts a verified guidance range from it. `guidance_change` and `direction` are computed as for earnings.
- [ ] Non-guidance events have `direction` null. They appear in the output and the history file. A fixture covers each type.
- [ ] The filings golden file is regenerated in this PR with only the new event records added. A field-by-field test shows the earnings records are unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/filing_events.py` | Create | Item-to-event mapping |
| `src/publishers/signals_filings.py` | Modify | Emit new types |
| `tests/test_filing_events.py` | Create | The criteria above |

### Key Decisions

- No direction for agreements, acquisitions or executive changes: their effect isn't well defined in code. The event study measures them by type.
