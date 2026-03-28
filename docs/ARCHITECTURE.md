# Morning Brief — Architecture

## Overview

Morning Brief is a batch pipeline that runs once daily (~4:15 AM ET), collects news and data from multiple sources, summarizes it with a local LLM, and publishes a mobile-friendly dashboard.

## Pipeline Stages

```
[Scheduler]
     │
     ▼
[1. Fetch]──── RSS feeds (36 sources across 6 categories)
           ├── Financial APIs (Finnhub, FRED — Phase 3)
           ├── Crypto APIs (CoinGecko, DeFi Llama — Phase 3)
           ├── Daily artwork (Met Museum API — Phase 5)
           └── Health checks (endpoint monitoring)
     │
     ▼
[2. Process]── Extract full text (trafilatura)
            └── Deduplicate (3 layers: URL hash → title match → content hash)
     │
     ▼
[3. Store]──── SQLite database (articles, market_data, health_checks, artworks)
     │
     ▼
[4. Summarize]─ Per-article summaries (Qwen 2.5 7B via Ollama — Phase 2)
             └── Cross-topic synthesis (Claude API — future)
     │
     ▼
[5. Publish]── HTML dashboard (Jinja2 — Phase 4)
            ├── Terminal dashboard (Rich — Phase 4)
            └── Email digest (SendGrid — future)
```

## Data Flow

All data flows in one direction through the pipeline. Each stage reads from the previous stage's output and writes to the next. If any single source fails (broken RSS feed, API timeout), the pipeline continues with the remaining sources.

## Directory Layout

```
morning-brief/
├── src/
│   ├── main.py              ← pipeline entry point
│   ├── config.py            ← environment-based configuration
│   ├── db.py                ← SQLite database layer
│   ├── fetchers/            ← Stage 1: data collection
│   │   ├── rss.py           ← RSS feed fetcher (active)
│   │   ├── financial.py     ← market data (stub)
│   │   ├── crypto.py        ← crypto data (stub)
│   │   ├── art.py           ← daily artwork (stub)
│   │   └── health.py        ← endpoint monitoring (active)
│   ├── processors/          ← Stage 2: extraction and dedup
│   │   ├── extractor.py     ← full-text extraction (active)
│   │   └── dedup.py         ← deduplication logic (active)
│   ├── summarizers/         ← Stage 4: LLM summarization
│   │   ├── local.py         ← Ollama/Qwen (stub)
│   │   └── cloud.py         ← Claude API (stub)
│   └── publishers/          ← Stage 5: output
│       ├── html.py          ← HTML rendering (stub)
│       ├── terminal.py      ← Rich terminal (stub)
│       └── email.py         ← email delivery (stub)
├── data/                    ← SQLite DB and output (gitignored)
├── templates/               ← Jinja2 templates
├── tests/                   ← unit and integration tests
└── docs/                    ← project documentation
```

## Database Schema

SQLite with four tables:

**articles** — RSS feed content
- `url_hash` (primary key), `url`, `title`, `source`, `category`, `author`
- `published_at`, `fetched_at`, `full_text`, `summary`, `summary_model`, `content_hash`

**market_data** — financial snapshots (Phase 3)
- `symbol`, `price`, `change_pct`, `fetched_at`

**health_checks** — endpoint monitoring results
- `url`, `name`, `status_code`, `response_ms`, `is_up`, `checked_at`

**artworks** — daily artwork picks (Phase 5)
- `object_id`, `title`, `artist`, `date`, `medium`, `image_url`, `fetched_at`

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| RSS over news APIs | Free, unlimited, no API keys needed |
| SQLite over Postgres | Single-user, once-daily batch — no concurrency needs |
| Qwen for summaries, Claude for synthesis | Local model handles volume; cloud model handles reasoning |
| httpx (async) for all HTTP | Concurrent fetching across 30+ sources |
| Three-layer dedup | URL hash catches exact dupes; title match catches reposts; content hash catches syndication |
| Graceful degradation | One broken feed never crashes the pipeline |
| Environment variables for config | Twelve-factor app — no secrets in code |

## Networking

All outbound HTTP only. No inbound ports in the pipeline container. The dashboard is served separately via Cloudflare Pages (Phase 7).

Concurrent HTTP requests are capped at 10 for RSS feeds and 5 for article extraction to avoid overwhelming sources.

## Security

- No secrets stored in code or Docker images
- API keys loaded from environment variables at runtime
- Container runs with `--cap-drop=ALL` and `--no-new-privileges`
- Pre-commit hooks run gitleaks (secret scanning) and semgrep (static analysis)
