# Morning Brief

Automated morning news and intelligence dashboard. Pulls news from RSS feeds and free APIs, summarizes with a local LLM, and publishes a mobile-friendly dashboard you can read over coffee.

## Features

- **News aggregation** — RSS feeds across 7 categories (US politics, Florida, world, crypto, dev/AI, art)
- **Market data** — Stock indices, treasury yields, VIX, crypto prices, DeFi TVL
- **LLM summarization** — Per-article summaries via Qwen 2.5 (local, via Ollama)
- **Endpoint monitoring** — Health checks for your deployed projects
- **Daily artwork** — Random artwork from the Met Museum API
- **Multiple outputs** — Mobile-friendly HTML dashboard, Rich terminal UI, email (future)
- **Deployment** — Cloudflare Pages with Cloudflare Access authentication

## Quick Start

```bash
# Local development
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit with your API keys

# Run the pipeline
python -m src.main

# Terminal dashboard
python -m src.cli
```

## Docker

```bash
docker compose up --build
```

## Configuration

Copy `.env.example` to `.env` and fill in your values. See `CLAUDE.md` for full configuration reference.

**On hub** the scheduled pipeline runs from `~/.local/share/morning-brief-pipeline` (set up by
`scripts/install-user-units.sh`). Its `.env` holds one secret, `DASHBOARD_HMAC_KEY` (a copy lives on
the SECRETS USB), plus three plain settings to recreate after a rebuild:

| Setting | Value on hub |
|---|---|
| `OLLAMA_HOST` | `http://100.124.149.0:11435` (hub-only TCP route to Ollama for containers) |
| `OLLAMA_MODEL` | the model pulled on hub (see `scripts/pull-model.sh`) |
| `DEPLOY_ENABLED` | `true` (the unit publishes the dashboard from the host) |

Signals for atrade are on by default (`SIGNALS_ENABLED`, set in `docker-compose.yml`) and land in
`data/signals/`. The last run's full output is `~/.local/state/morning-brief-last-run.log`.

## Documentation

- [CLAUDE.md](CLAUDE.md) — AI context, architecture, conventions
- [docs/ROADMAP.md](docs/ROADMAP.md) — Phased build plan
- [docs/FEEDS.md](docs/FEEDS.md) — RSS feed and API source registry

## License

MIT
