# Development Plan: Morning Brief

**Status:** In Progress
**Created:** 2026-03-28
**Updated:** 2026-03-28

## Overview

Morning Brief is an automated daily news and intelligence dashboard. It fetches RSS feeds, financial/crypto data, and artwork, summarizes articles with a local LLM (Qwen via Ollama), renders an HTML dashboard, and deploys to Cloudflare Pages. This plan covers the remaining work to reach a production-ready v1.0.

## Constraints

- Python 3.11+, async HTTP via httpx
- SQLite only (single-user, once-daily batch)
- Ollama for summarization (no paid API for bulk work)
- No secrets in code — all config via environment variables
- One broken feed must never crash the pipeline
- Docker deployment with cap_drop ALL, no-new-privileges

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

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `Dockerfile` | Modify | Multi-stage build with all dependencies |
| `docker-compose.yml` | Modify | Volume mounts, security opts, network config |
| `tests/test_docker.py` | Create | Test that container builds and pipeline runs |

### Key Decisions

- Ollama runs on the host, not in the container (GPU access)
- Container accesses Ollama via host network or `host.docker.internal`
- SQLite DB persists via volume mount to `data/`

---

## Feature: systemd-scheduling

**Branch:** `feature/systemd-scheduling`
**Depends on:** docker-deployment
**Status:** Not Started
**Requires:** both

### Goal

Create a systemd timer that triggers the pipeline at 4:15 AM ET daily, with health checks and failure logging.

### Acceptance Criteria

- [ ] `morning-brief.service` systemd unit runs `docker compose up` (or direct python)
- [ ] `morning-brief.timer` triggers at 4:15 AM ET daily
- [ ] Health check: after run, verify dashboard.html exists and was modified within last hour
- [ ] On failure: log to `~/.local/share/morning-brief/failures.log` with timestamp and exit code
- [ ] [HUMAN] Enable timer: `systemctl --user enable --now morning-brief.timer`
- [ ] Tests cover health check logic
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `morning-brief.service` | Create | systemd user unit |
| `morning-brief.timer` | Create | systemd timer for 4:15 AM ET |
| `scripts/health-check.sh` | Create | Post-run validation |

### Key Decisions

- systemd user units (not system-level) — no root needed
- Timer uses `OnCalendar=*-*-* 04:15:00` with timezone set via `TZ=America/New_York` in service
- Health check is a separate script called after the main run

---

## Feature: cloudflare-access

**Branch:** `feature/cloudflare-access`
**Depends on:** docker-deployment
**Status:** Not Started
**Requires:** human

### Goal

Set up Cloudflare Pages to serve the dashboard and Cloudflare Access for authentication so only you can view it from your phone.

### Acceptance Criteria

- [ ] [HUMAN] Cloudflare Pages connected to GitHub repo, watching gh-pages branch
- [ ] [HUMAN] Cloudflare Access configured with email-based auth (free, 1 user)
- [ ] [HUMAN] Verify dashboard loads on phone with authentication
- [ ] [HUMAN] robots.txt serves Disallow all (already in deploy script)

### Key Decisions

- Cloudflare Pages over self-hosting — no server to maintain, free tier, global CDN
- Cloudflare Access for auth over HTTP basic — zero-trust, free for 1 user, works with phone browsers

### Notes

- Alternative: Tailscale for private network access (simpler, no public exposure)
- Deploy script already pushes to gh-pages with robots.txt
- No files to create or modify — this is entirely manual infrastructure setup

---

## Feature: summarization-quality

**Branch:** `feature/summarization-quality`
**Depends on:** none
**Status:** Not Started
**Requires:** both

### Goal

Validate and tune Ollama summarization quality. Measure batch timing and adjust prompt/truncation if needed.

### Acceptance Criteria

- [ ] [HUMAN] Review 20 article summaries for accuracy, conciseness, and factual correctness
- [ ] Measure and log total summarization time per batch run
- [ ] Add timing metrics to pipeline output (articles/minute, total duration)
- [ ] Adjust prompt template if quality review reveals issues
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | Add timing metrics, adjust prompt if needed |
| `src/main.py` | Modify | Log batch timing summary |
| `tests/test_summarizer.py` | Modify | Add timing metric tests |

### Notes

- Depends on Ollama being installed and running (separate project: Psaphon/ollama)
- Qwen 2.5 7B Q4_K_M on RTX 2060 6GB — expect ~2-3 articles/minute

---

## Feature: daily-briefing

**Branch:** `feature/daily-briefing`
**Depends on:** summarization-quality
**Status:** Not Started
**Requires:** ai

### Goal

After all articles are summarized, combine today's summaries into a single ~500-1000 word daily briefing using Ollama. This runs locally at no cost and gives the dashboard a cohesive narrative instead of just a list of individual summaries.

### Acceptance Criteria

- [ ] New function in `src/summarizers/local.py` takes all today's summaries grouped by category and produces one combined briefing
- [ ] Briefing is ~500-1000 words, covering the key themes across all categories
- [ ] Briefing stored in SQLite (`daily_briefings` table with date, content, model, generated_at)
- [ ] Appears as the first section of the dashboard ("Today's Briefing")
- [ ] Appears in terminal output as a Rich panel
- [ ] Skips gracefully if Ollama is unreachable (dashboard still renders without briefing)
- [ ] Only generates once per day (check if today's briefing already exists)
- [ ] Tests cover prompt construction, storage, and skip logic
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | Add `generate_daily_briefing()` function |
| `src/db.py` | Modify | Add `daily_briefings` table, insert/query methods |
| `src/main.py` | Modify | Wire briefing generation after summarization stage |
| `src/publishers/html.py` | Modify | Pass briefing to template |
| `src/publishers/terminal.py` | Modify | Render briefing as Rich panel |
| `templates/dashboard.html` | Modify | Add briefing section at top |
| `tests/test_briefing.py` | Create | Test prompt, storage, skip logic |

### Key Decisions

- Uses Ollama (same model as per-article summaries) — no paid API, runs locally
- Prompt groups summaries by category (politics, markets, crypto, tech, world, etc.) so the model can identify cross-cutting themes
- 500-1000 words target set via prompt instruction, not hard truncation
- One briefing per day — if re-run, reuse existing briefing from DB

### Notes

- This is the default briefing. claude-synthesis (next feature) is an optional upgrade that replaces it with a higher-quality Claude-generated version when an API key is available.
- Qwen 2.5 7B should handle this well — it's one call with ~2000-3000 words of input summaries

---

## Feature: claude-synthesis

**Branch:** `feature/claude-synthesis`
**Depends on:** daily-briefing
**Status:** Not Started
**Requires:** ai

### Goal

Optional upgrade: when `CLAUDE_API_KEY` is set, replace the Ollama daily briefing with a Claude-generated cross-topic narrative that connects themes across politics, markets, tech, and world events with higher reasoning quality.

### Acceptance Criteria

- [ ] `src/summarizers/cloud.py` calls Claude API with all summaries as context
- [ ] Generates a 3-5 paragraph narrative briefing connecting themes across categories
- [ ] Gated behind `CLAUDE_API_KEY` env var — skips gracefully if not set
- [ ] Briefing appears as the first section of the dashboard ("Today's Briefing")
- [ ] Rate-limited: one API call per pipeline run
- [ ] Tests cover prompt construction and graceful skip
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/cloud.py` | Modify | Implement Claude synthesis call |
| `src/publishers/html.py` | Modify | Add briefing section to template |
| `templates/dashboard.html` | Modify | Briefing section styling |
| `tests/test_cloud.py` | Create | Test prompt construction and skip logic |

### Key Decisions

- Use Claude API directly (httpx), not anthropic SDK — keeps deps lighter
- Prompt includes all summaries grouped by category
- Max ~4000 tokens input to keep costs low

---

## Feature: email-digest

**Branch:** `feature/email-digest`
**Depends on:** claude-synthesis
**Status:** Not Started
**Requires:** ai

### Goal

Send a daily email digest with the briefing and top stories, as an alternative to checking the web dashboard.

### Acceptance Criteria

- [ ] `src/publishers/email.py` sends HTML email via SendGrid API
- [ ] Email includes: narrative briefing (if available), top 5 stories per category, market snapshot
- [ ] Gated behind `SENDGRID_API_KEY` env var — skips gracefully if not set
- [ ] Configurable recipient via `EMAIL_TO` env var
- [ ] Tests cover email formatting and graceful skip
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/publishers/email.py` | Modify | Implement SendGrid email delivery |
| `src/main.py` | Modify | Wire email into publish stage |
| `tests/test_email.py` | Create | Test email formatting |

### Key Decisions

- SendGrid free tier (100 emails/day) — more than enough for daily digest
- HTML email reuses dashboard template styles
- Plain text fallback for email clients that don't render HTML
