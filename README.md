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

## Documentation

- [CLAUDE.md](CLAUDE.md) — AI context, architecture, conventions
- [docs/ROADMAP.md](docs/ROADMAP.md) — Phased build plan
- [docs/FEEDS.md](docs/FEEDS.md) — RSS feed and API source registry

## License

MIT
