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
**Status:** Not Started
**Requires:** ai

### Goal

Finalize Dockerfile and docker-compose.yml so the full pipeline runs unattended in a container with persistent data.

### Acceptance Criteria

- [ ] Multi-stage Dockerfile: build stage installs deps, runtime stage is slim
- [ ] docker-compose.yml mounts `data/` volume for SQLite persistence and `.env` for config
- [ ] `docker compose up` runs the full pipeline end-to-end
- [ ] Container uses cap_drop ALL and no-new-privileges
- [ ] Container can reach Ollama on the host network (for summarization)
- [ ] Output dashboard.html is accessible from the host via volume mount
- [ ] All tests pass inside the container
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `Dockerfile` | Modify | Multi-stage build with all dependencies |
| `docker-compose.yml` | Modify | Volume mounts, security opts, network config |

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

### Notes

- Alternative: Tailscale for private network access (simpler, no public exposure)
- Deploy script already pushes to gh-pages with robots.txt

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
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/summarizers/local.py` | Modify | Add timing metrics, adjust prompt if needed |
| `src/main.py` | Modify | Log batch timing summary |

### Notes

- Depends on Ollama being installed and running (separate project: Psaphon/ollama)
- Qwen 2.5 7B Q4_K_M on RTX 2060 6GB — expect ~2-3 articles/minute

---

## Feature: claude-synthesis

**Branch:** `feature/claude-synthesis`
**Depends on:** summarization-quality
**Status:** Not Started
**Requires:** ai

### Goal

Add optional Claude API integration that reads all article summaries and generates a cross-topic narrative briefing — connecting themes across politics, markets, tech, and world events.

### Acceptance Criteria

- [ ] `src/summarizers/cloud.py` calls Claude API with all summaries as context
- [ ] Generates a 3-5 paragraph narrative briefing connecting themes across categories
- [ ] Gated behind `CLAUDE_API_KEY` env var — skips gracefully if not set
- [ ] Briefing appears as the first section of the dashboard ("Today's Briefing")
- [ ] Rate-limited: one API call per pipeline run
- [ ] Tests cover prompt construction and graceful skip
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
