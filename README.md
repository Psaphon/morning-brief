# Morning Brief

Automated morning news and intelligence dashboard. Pulls news from RSS feeds and free APIs, summarizes with a local LLM, and publishes a mobile-friendly dashboard you can read over coffee.

## Features

- **News aggregation** — RSS feeds across 7 categories (US politics, Florida, world, crypto, dev/AI, art)
- **Market data** — Stock indices, treasury yields, VIX, crypto prices, DeFi TVL
- **LLM summarization** — Per-article summaries via a local model (gemma4:e4b by default, via Ollama)
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

Each signal record (contract 1.2.0) also has an optional `direction`: the mean of a per-ticker
news sentiment (-1 bad, 0 neutral, +1 good for that company) over the contributing articles, with
`direction_n` counting them. It comes from the existing extraction call (no extra model call), so
it is sparse: only signal articles that were extracted this run contribute, and the rest give
`direction: null`, `direction_n: 0`. Signals are written after extraction for that reason. `score`
is unchanged.

### Claude section writer (optional)

By default (`SUMMARY_WRITER=local`) the briefing is one local-model call, as before. With
`SUMMARY_WRITER=claude` each reader section gets one Claude Messages API call. The request holds
extractions only (id, title, source, `what_happened`, `details`): no full text and no URLs. The reply
is split at `[[id]]` citations and every chunk is checked in code against the articles it cites;
chunks with no citation, an unknown id or an ungrounded number or name are dropped. A section that
loses more than `WRITER_MAX_DROP_SHARE` of its chunks, errors or times out is written by the local
model instead, and is marked `"writer": "local fallback"` in the segment map (`cited_text` keeps the
markers for the page).

| Setting | Default |
|---|---|
| `SUMMARY_WRITER` | `local` (`local` or `claude`; anything else means `local`, with a warning) |
| `WRITER_MODEL` | `claude-sonnet-5-5` |
| `WRITER_API_URL` | `https://api.anthropic.com/v1/messages` |
| `WRITER_MIN_WORDS` / `WRITER_MAX_WORDS` | `250` / `450` per section |
| `WRITER_MAX_DROP_SHARE` | `0.3` |
| `WRITER_TIMEOUT_S` | `120` |
| `WRITER_MONTHLY_CAP_USD` | `10` |
| `WRITER_PRICE_IN_PER_MTOK` / `WRITER_PRICE_OUT_PER_MTOK` | unset: set them in the host's `.env`; with either unset the writer logs one warning and runs local |

**The API key is never in `.env` or the environment.** It is read from the file
`$CREDENTIALS_DIRECTORY/anthropic_api_key` (systemd `LoadCredential=`). If the variable, file or its
content is missing the run uses the local writer and logs one line naming the credential, never its
value. `scripts/run-pipeline.sh` adds `docker-compose.credentials.yml` (a read-only mount at
`/run/credentials`) only when that file exists on the host.

The host-side `LoadCredential=anthropic_api_key:<path>` drop-in for `morning-brief.service` is not in
this repo; the operator adds it in the hub repo once the key exists.

Spend guard: each call's tokens are priced from the settings above and stored in the `api_usage`
table. When the UTC month-to-date total reaches `WRITER_MONTHLY_CAP_USD`, the remaining sections use
the local writer until the month rolls over. The run logs one warning and puts
`"alerts": ["writer spend cap reached YYYY-MM"]` in `data/last_run.json` the first time that month;
`scripts/health-check.sh` forwards each alert once through `/usr/local/sbin/hub-alert` (skipped if
it is not executable). With the writer on, `last_run.json` also has a `writer` block (sections,
claude, fallback, dropped by reason, tokens, cost).

The old whole-brief path keyed by `ANTHROPIC_API_KEY` in the environment was removed.

### Feed lanes

Each feed has lanes in `config/feed_lanes.toml`: `reader` (briefing and page), `signals` (per-ticker
signals for atrade) and `rnd` (stored only, never shown). A feed with no entry gets
`["reader", "signals"]`. Override or add feeds per host with an overlay file, named by
`MORNING_BRIEF_FEEDS_OVERLAY` (in Docker: `~/.config/morning-brief/feeds-overlay.toml`). It uses the same
shape; an entry replaces that feed's `lanes`, and an entry with `url` and `category` adds a new feed.
A missing overlay file is skipped. See the commented example in `interests.example.toml`.

## Documentation

- [CLAUDE.md](CLAUDE.md) — AI context, architecture, conventions
- [docs/ROADMAP.md](docs/ROADMAP.md) — Phased build plan
- [docs/FEEDS.md](docs/FEEDS.md) — RSS feed and API source registry

## License

MIT
