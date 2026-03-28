# Morning Brief — Roadmap

## Phase 0: Documentation and Project Setup
*Get the project scaffolded and ready to build.*

- [x] Finalize CLAUDE.md, ROADMAP.md, FEEDS.md
- [x] Create ARCHITECTURE.md
- [x] Create repo with directory structure
- [x] Set up Python venv and requirements.txt with initial dependencies
- [x] Create .env.example with all config variables
- [x] Create Dockerfile and docker-compose.yml (basic — just runs the pipeline)
- [ ] Verify Ollama is running locally with Qwen 2.5 7B model
- [x] Create src/main.py skeleton that runs stages in order with logging

## Phase 1: RSS Fetching and Storage
*Get news articles flowing into the database.*

- [x] Implement src/fetchers/rss.py — fetch and parse RSS feeds from FEEDS.md
- [x] Implement src/db.py — SQLite schema, insert, query, dedup by URL hash
- [x] Implement src/processors/extractor.py — trafilatura full-text extraction
- [x] Implement src/processors/dedup.py — URL normalization + title fuzzy match
- [x] Wire into main.py — fetch → extract → dedup → store
- [x] Test with 5-10 feeds, verify articles land in SQLite correctly
- [x] Add per-feed error handling (one broken feed doesn't crash the pipeline)
- [x] Add basic logging (structured, with timestamps and feed names)

## Phase 2: Local LLM Summarization
*Summarize articles with Qwen via Ollama.*

- [ ] Implement src/summarizers/local.py — send articles to Ollama, get summaries
- [ ] Create summarization prompt template (concise, factual, 2-3 sentences)
- [ ] Handle long articles — truncate to ~3000 tokens before sending
- [ ] Store summaries back in SQLite (summary column, summary_model column)
- [ ] Wire into main.py — fetch → extract → dedup → store → summarize
- [ ] Test quality: run on 20 articles, review summaries for accuracy
- [ ] Measure timing: how long does a full batch take?

## Phase 3: Financial and Crypto Data
*Add market context alongside news.*

- [ ] Implement src/fetchers/financial.py — pull from Finnhub and/or FRED
  - [ ] Index closes (SPY, QQQ, DIA)
  - [ ] Treasury yields (FRED: DGS2, DGS10)
  - [ ] VIX (FRED: VIXCLS)
- [ ] Implement src/fetchers/crypto.py — pull from CoinGecko and DeFi Llama
  - [ ] Top crypto prices (BTC, ETH + watchlist)
  - [ ] Ethereum gas price (Etherscan)
  - [ ] DeFi TVL snapshot
- [ ] Store structured data in SQLite (separate tables or JSON blobs)
- [ ] Wire into main.py as a parallel fetch stage

## Phase 4: Dashboard Output
*Render a mobile-friendly HTML dashboard.*

- [ ] Create templates/dashboard.html — Jinja2 template with sections per category
- [ ] Implement src/publishers/html.py — load data from SQLite, render template
- [ ] Make it mobile-responsive (viewport meta, CSS media queries)
- [ ] Sections: Top Stories, US Politics, Florida, World, Markets, Crypto, Dev/AI, Daily Art
- [ ] Wire into main.py as the final stage
- [ ] Test by opening output HTML on phone browser

## Phase 5: Daily Artwork
*A nice touch — surface one artwork each morning.*

- [ ] Implement src/fetchers/art.py — Met Museum API random artwork by date seed
- [ ] Include ceramics search option alongside general art
- [ ] Display artwork image + title + artist + date + medium in dashboard
- [x] Add contemporary art RSS feeds (Hyperallergic, This is Colossal)

## Phase 6: Scheduling and Docker Deployment
*Automate the pipeline to run unattended.*

- [ ] Finalize Dockerfile — multi-stage build, includes all dependencies
- [ ] Set up docker-compose.yml with volume mounts for data/ and .env
- [ ] Create systemd timer (or cron job) to trigger at 4:15 AM ET
- [ ] Add health check: verify output file was generated and is recent
- [ ] Add failure alerting (simple: write to a log file, check on wake-up)
- [ ] Test full end-to-end: timer fires → Docker runs → dashboard generated

## Phase 7: Secure Mobile Access
*Deploy dashboard where you can actually reach it from your phone.*

- [ ] Set up Cloudflare Pages (connect to GitHub repo)
- [ ] Configure Cloudflare Access for authentication (free, 1 user)
- [ ] Pipeline pushes generated HTML to repo → auto-deploys
- [ ] Verify access from phone with auth
- [ ] Alternative: Tailscale for private network access (simpler, no public exposure)

---

## Future / Nice-to-Have (no timeline)

- [ ] Claude API synthesis — cross-topic narrative briefing
- [ ] Email digest via SendGrid
- [ ] Telegram bot delivery
- [ ] Unusual Whales MCP integration (paid — add when ready)
- [ ] Dynamic watchlist / ticker surfacing
- [ ] Historical trend tracking (how did yesterday's briefing compare to today?)
- [ ] MCP server wrapping dashboard data (queryable from Claude Pro)
- [ ] Congressional and insider trade tracking
- [ ] Obsidian export for knowledge management
