# CLAUDE.md — Morning Brief

## What is this project?

Morning Brief is an automated morning news and intelligence dashboard. It runs a batch job once daily (around 4:15 AM ET), pulls news from RSS feeds and free APIs, summarizes everything with a local LLM, and publishes a mobile-friendly dashboard you can read over coffee. It also includes endpoint health monitoring for deployed projects (merged from the Pulse Monitor concept).

## Architecture

```
[Cron/systemd timer — 4:15 AM ET]
        │
        ▼
[1. Fetch] ── RSS feeds (news, politics, crypto, dev, art)
           ── Free APIs (Finnhub, FRED, CoinGecko, DeFi Llama)
           ── Endpoint health checks (your deployed projects)
        │
        ▼
[2. Process] ── Extract article text (trafilatura)
             ── Deduplicate (URL hash → title fuzzy match → content hash)
             ── Store in SQLite
        │
        ▼
[3. Summarize] ── Qwen 2.5 7B via Ollama (local, per-article summaries)
        │
        ▼
[4. Synthesize] ── Claude API (cross-topic narrative briefing) [optional]
        │
        ▼
[5. Publish] ── Render HTML from Jinja2 template
             ── Rich terminal dashboard (quick CLI view)
             ── Deploy to Cloudflare Pages (or local for dev)
             ── Optional: email via SendGrid, Telegram bot
```

## Tech stack

- **Language:** Python 3.11+
- **RSS parsing:** feedparser
- **Article extraction:** trafilatura
- **Database:** SQLite (via built-in sqlite3 module)
- **Local LLM:** Qwen 2.5 7B (Q4_K_M) via Ollama
- **Cloud LLM:** Claude API (for synthesis — added later)
- **HTTP client:** httpx (async-capable)
- **Templating:** Jinja2
- **Terminal UI:** Rich (for CLI dashboard mode)
- **Scheduling:** systemd timer (production) or APScheduler (development)
- **Containerization:** Docker
- **Deployment:** Cloudflare Pages + Cloudflare Access

## Project structure

```
morning-brief/
├── CLAUDE.md
├── README.md
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
│
├── docs/
│   ├── ROADMAP.md
│   ├── FEEDS.md
│   └── ARCHITECTURE.md
│
├── src/
│   ├── __init__.py
│   ├── main.py                ← pipeline entry point
│   ├── cli.py                 ← Rich terminal dashboard (click CLI)
│   ├── config.py              ← loads settings from environment / YAML
│   │
│   ├── fetchers/              ← Stage 1: data collection
│   │   ├── __init__.py
│   │   ├── rss.py             ← RSS feed fetcher
│   │   ├── financial.py       ← market data (Finnhub, FRED)
│   │   ├── crypto.py          ← crypto data (CoinGecko, DeFi Llama)
│   │   ├── art.py             ← daily artwork (Met Museum API)
│   │   └── health.py          ← endpoint health checks (from Pulse)
│   │
│   ├── processors/            ← Stage 2: extraction and dedup
│   │   ├── __init__.py
│   │   ├── extractor.py       ← full-text article extraction
│   │   └── dedup.py           ← deduplication logic
│   │
│   ├── summarizers/           ← Stage 3: LLM summarization
│   │   ├── __init__.py
│   │   ├── local.py           ← Qwen via Ollama
│   │   └── cloud.py           ← Claude API (future)
│   │
│   ├── publishers/            ← Stage 5: output
│   │   ├── __init__.py
│   │   ├── html.py            ← Jinja2 HTML rendering
│   │   ├── terminal.py        ← Rich terminal dashboard
│   │   └── email.py           ← email delivery (future)
│   │
│   └── db.py                  ← SQLite database helpers
│
├── templates/
│   └── dashboard.html         ← Jinja2 template
│
├── data/                      ← SQLite DB and generated output (gitignored)
│   └── .gitkeep
│
└── tests/
    └── __init__.py
```

## Commit Conventions

Follow conventional commits strictly:

- `feat:` -- new feature
- `fix:` -- bug fix
- `docs:` -- documentation only
- `chore:` -- maintenance, dependency updates
- `refactor:` -- code restructuring without behavior change
- `test:` -- adding or updating tests
- `ci:` -- CI/CD changes

## Branching (Gitflow)

This project follows **gitflow**. NEVER commit directly to `main` or `develop`.

### Branch types

| Branch | Purpose | Branches from | Merges into |
|--------|---------|---------------|-------------|
| `main` | Production-ready releases (tagged) | -- | -- |
| `develop` | Integration branch for next release | `main` (initial) | `release/*` |
| `feature/*` | New features and non-urgent work | `develop` | `develop` |
| `release/*` | Release prep (bug fixes, docs only) | `develop` | `main` + `develop` |
| `hotfix/*` | Emergency production fixes | `main` | `main` + `develop` |

### Workflow

1. **Feature work:** `git checkout develop && git checkout -b feature/short-description`
2. Work, commit with conventional commits, push.
3. Open a PR from `feature/short-description` → `develop`.
4. **Release prep:** `git checkout develop && git checkout -b release/vX.Y.Z`
5. Only bug fixes and docs in release branches — no new features.
6. When ready: merge `release/vX.Y.Z` → `main`, tag `vX.Y.Z`, merge back → `develop`.
7. **Hotfix:** `git checkout main && git checkout -b hotfix/description`
8. Fix, merge → `main` (tag), merge → `develop`.

### Branch naming

- `feature/rss-fetcher`, `feature/crypto-api`, `feature/health-monitor`
- `release/v1.0.0`, `release/v1.1.0`
- `hotfix/fix-crash`, `hotfix/patch-auth`

## Linting & Formatting

Run before every commit:

```bash
ruff check . && ruff format --check .
```

If linting fails, fix the issues before committing.

## Docker

- Use `docker compose` (space), NOT `docker-compose` (hyphen).
- Containers run with `--cap-drop=ALL` and `--security-opt=no-new-privileges`.

## Secrets

- NEVER commit secrets, credentials, API keys, or tokens.
- Use `.env.example` with placeholder values; real `.env` is gitignored.
- Check `.gitignore` covers `.env*`, `*.pem`, `*.key`.

## Security

- Pre-commit hooks run gitleaks (secret scanning) and semgrep (static analysis).
- Install hooks: `pre-commit install`
- Run manually: `pre-commit run --all-files`

## Testing

```bash
pytest
```

Run tests before pushing.

## Configuration

All configuration lives in environment variables (loaded from `.env` in development).

Required:
- `OLLAMA_HOST` — URL of the Ollama server (default: `http://localhost:11434`)
- `OLLAMA_MODEL` — model name (default: `qwen2.5:7b-instruct-q4_K_M`)

Optional (added as features are built):
- `FINNHUB_API_KEY`
- `FRED_API_KEY`
- `COINGECKO_API_KEY`
- `CLAUDE_API_KEY`
- `SENDGRID_API_KEY`

## Coding conventions

- Use `logging` (not print) for all output
- Use `httpx` for HTTP requests (async-capable)
- Use `pathlib.Path` instead of string paths
- Type hints are welcome but not required
- Keep functions small and focused
- Handle errors gracefully — a single broken RSS feed should never crash the pipeline
- All HTTP requests must use async
- Store time-series data with ISO 8601 timestamps
- All API integrations must respect rate limits

## Key decisions

1. **RSS over news APIs for primary news.** Free, unlimited, keyless.
2. **SQLite over Postgres.** Single-user, once-daily pipeline.
3. **Qwen for summarization, Claude for synthesis.** Local model for volume, Claude for reasoning.
4. **Cloudflare Pages over local hosting.** No server to maintain, free tier, global CDN.
5. **Once-daily batch job, not real-time.** Pipeline runs at ~4:15 AM ET.
6. **Docker for deployment isolation.** Develop in venv locally, ship in Docker.
