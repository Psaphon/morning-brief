# Morning Brief

## What This Is

Automated daily news and intelligence dashboard. Runs a batch job at 4:15 AM ET, pulls news from RSS feeds and free APIs, summarizes with a local LLM (Qwen via Ollama), and publishes a mobile-friendly HTML dashboard to Cloudflare Pages.

## Architecture

```
[systemd timer — 4:15 AM ET]
        │
        ▼
[1. Fetch] ── RSS feeds (news, politics, crypto, dev, art)
           ── Free APIs (Finnhub, FRED, CoinGecko, DeFi Llama)
           ── Met Museum API (daily artwork)
        │
        ▼
[2. Process] ── Extract article text (trafilatura)
             ── Deduplicate (URL hash → title fuzzy → content hash)
             ── Store in SQLite
        │
        ▼
[3. Summarize] ── Qwen 2.5 7B via Ollama (local, per-article)
        │
        ▼
[4. Publish] ── Render HTML dashboard (Jinja2)
             ── Rich terminal dashboard (CLI)
             ── Deploy to gh-pages → Cloudflare Pages
```

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| Language | Python 3.11+ | Async support, rich ecosystem |
| RSS parsing | feedparser | Standard, reliable |
| Article extraction | trafilatura | Best open-source full-text extraction |
| Database | SQLite | Single-user, once-daily pipeline |
| Local LLM | Qwen 2.5 7B (Q4_K_M) via Ollama | Free, local, fits RTX 2060 6GB |
| HTTP client | httpx | Async-capable |
| Templating | Jinja2 | Standard Python templating |
| Terminal UI | Rich | Tables, panels, color |
| CLI | Click | Subcommands, options |
| Deployment | Cloudflare Pages + Access | Free, global CDN, auth |

## Project Structure

```
morning-brief/
├── CLAUDE.md
├── docs/
│   ├── DEVPLAN.md          ← feature plan for AI development
│   ├── ROADMAP.md          ← original phase-based roadmap
│   ├── FEEDS.md            ← RSS feed list and categories
│   └── ARCHITECTURE.md
├── src/
│   ├── main.py             ← pipeline entry point
│   ├── cli.py              ← Click CLI (run/dashboard/view/deploy)
│   ├── config.py           ← env-based configuration
│   ├── db.py               ← SQLite helpers
│   ├── fetchers/
│   │   ├── rss.py          ← RSS feed fetcher
│   │   ├── financial.py    ← Finnhub, FRED market data
│   │   ├── crypto.py       ← CoinGecko, DeFi Llama
│   │   ├── art.py          ← Met Museum daily artwork
│   │   └── health.py       ← endpoint health checks
│   ├── processors/
│   │   ├── extractor.py    ← trafilatura full-text extraction
│   │   └── dedup.py        ← deduplication logic
│   ├── summarizers/
│   │   ├── local.py        ← Qwen via Ollama
│   │   └── cloud.py        ← Claude API synthesis (future)
│   └── publishers/
│       ├── html.py         ← Jinja2 HTML rendering
│       ├── terminal.py     ← Rich terminal dashboard
│       └── email.py        ← SendGrid email (future)
├── templates/
│   └── dashboard.html      ← Jinja2 template
├── scripts/
│   └── deploy-dashboard.sh ← push to gh-pages branch
├── tests/
│   ├── test_art.py
│   ├── test_crypto.py
│   ├── test_db.py
│   ├── test_dedup.py
│   ├── test_financial.py
│   ├── test_publishers.py
│   ├── test_real_feeds.py
│   ├── test_rss.py
│   └── test_summarizer.py
└── data/                   ← SQLite DB and output (gitignored)
```

## Constraints

- No paid APIs for bulk work — Ollama handles summarization locally
- One broken feed must never crash the pipeline
- All HTTP requests must be async
- Docker containers: cap_drop ALL, no-new-privileges
- All config via environment variables (`.env` in dev, gitignored)

## Commit Conventions

- Conventional commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`
- Gitflow branching: `main`, `develop`, `feature/*`, `release/*`, `hotfix/*`
- Feature branches merge to develop via PR

## Code Standards

- **Linting & Formatting:**

  **CRITICAL: You MUST run linting and formatting before EVERY commit.** No exceptions.

  ```bash
  ruff check . && ruff format --check .
  ```

  If linting fails, fix ALL issues before committing. Never use `--no-verify` to skip checks.
  A commit that fails lint is a broken commit — treat it as a build failure.

- Tests: `pytest` — all must pass before push
- Use `logging` (not print) for all output
- Use `httpx` for HTTP requests
- Use `pathlib.Path` instead of string paths

## Key Decisions

1. **RSS over news APIs** — free, unlimited, keyless
2. **SQLite over Postgres** — single-user, once-daily
3. **Qwen for summarization, Claude for synthesis** — local for volume, cloud for reasoning
4. **Cloudflare Pages over self-hosting** — no server to maintain, free tier
5. **Once-daily batch, not real-time** — pipeline runs at 4:15 AM ET
6. **Docker for deployment** — develop locally, ship in container

## Configuration

Required:
- `OLLAMA_HOST` — Ollama server URL (default: `http://localhost:11434`)
- `OLLAMA_MODEL` — model name (default: `qwen2.5:7b-instruct-q4_K_M`)

Optional:
- `FINNHUB_API_KEY`, `FRED_API_KEY` — market data
- `DEPLOY_ENABLED` — set to `true` for automatic gh-pages deploy
- `DEPLOY_BRANCH` — gh-pages branch name (default: `gh-pages`)

## Audience and Tone

- Target reader: hiring managers for data engineering / DevOps roles
- README should emphasize: pipeline design, API integration, automation
- Pairs with: log-sentinel (security), impact-etl (data), water-monitor-infra (infrastructure)
