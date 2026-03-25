# Morning Brief — Feed and Source Registry

This is the living registry of all data sources. Update status as feeds are tested.

**Status key:** `untested` → `active` → `broken` → `deprecated`

---

## RSS Feeds

### US Politics (factual, non-opinion)

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| AP Politics | `https://apnews.com/politics.rss` | broken | 404 Not Found — URL may have changed |
| ProPublica | `https://www.propublica.org/feeds/propublica/main` | active | Investigative journalism |
| The Intercept | `https://theintercept.com/feed/` | active | Investigative, national security focus |
| NPR Politics | `https://feeds.npr.org/1014/rss.xml` | active | Publicly funded, factual |
| PBS NewsHour | `https://www.pbs.org/newshour/feeds/rss/politics` | active | Publicly funded |
| PolitiFact | `https://www.politifact.com/rss/all/` | active | Fact-checking |
| POLITICO | `https://www.politico.com/rss/politicopicks.xml` | broken | 403 Forbidden — blocks automated access |

### Florida Politics

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| Florida Politics | `https://floridapolitics.com/feed` | active | Premier FL political news |
| Miami Herald Politics | `https://www.miamiherald.com/news/politics-government/index.rss` | broken | Server disconnected without response |
| Tampa Bay Times | `https://www.tampabay.com/news/florida-politics/?outputType=rss` | broken | 500 Internal Server Error |
| Sun Sentinel | TBD — needs RSS discovery | untested | South FL coverage |
| WUSF (Tampa NPR) | TBD — needs RSS discovery | untested | Public radio |

### World News

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | active | UK public broadcaster |
| The Guardian World | `https://www.theguardian.com/world/rss` | active | UK, strong international |
| Al Jazeera | `https://www.aljazeera.com/xml/rss/all.xml` | active | Middle East + global |
| France 24 | `https://www.france24.com/en/rss` | active | French public broadcaster |
| Deutsche Welle | `https://rss.dw.com/rdf/rss-en-all` | active | German public broadcaster |

### Crypto / Web3 / ReFi

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| CoinDesk | `https://www.coindesk.com/arc/outboundfeeds/rss/` | active | Major crypto news |
| Decrypt | `https://decrypt.co/feed` | active | Crypto + Web3 |
| Ethereum Foundation Blog | `https://blog.ethereum.org/en/feed.xml` | active | Official EF updates |
| The Defiant | `https://thedefiant.io/api/feed` | active | DeFi-focused |
| Week in Ethereum | `https://weekinethereumnews.com/feed/` | broken | SSL certificate mismatch |
| CARBON Copy (ReFi) | TBD — check paragraph.xyz | untested | ReFi-specific aggregator |
| ReFi DAO Blog | `https://blog.refidao.com/` | broken | Malformed XML — not well-formed |

### Software Dev / DevOps / AI / ML

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| Hacker News (100+ pts) | `https://hnrss.org/frontpage?points=100` | active | Quality-filtered HN |
| The New Stack | `https://thenewstack.io/feed/` | active | Cloud-native, DevOps |
| Import AI | `https://importai.substack.com/feed` | active | Weekly AI newsletter |
| ArXiv CS.AI | `https://rss.arxiv.org/rss/cs.AI` | active | Academic AI papers |
| Hugging Face Blog | `https://huggingface.co/blog/feed.xml` | active | ML/AI tools and research |
| DevOps.com | `https://devops.com/feed/` | active | DevOps industry |
| TLDR Newsletter | TBD — check for RSS | untested | Daily dev news digest |
| Changelog | `https://changelog.com/feed` | active | Dev news podcast/blog |

### Art / Ceramics / Visual

| Source | Feed URL | Status | Notes |
|--------|----------|--------|-------|
| Hyperallergic | `https://hyperallergic.com/feed/` | active | Contemporary art, ~8/day |
| This is Colossal | `https://www.thisiscolossal.com/feed/` | active | Art, design, visual culture |
| Contemporary Art Daily | `https://contemporaryartdaily.com/feed/` | active | Gallery exhibitions |
| e-flux | `https://www.e-flux.com/rss/` | broken | 404 Not Found |
| Artnet News | `https://news.artnet.com/feed/` | active | Art market and culture |
| r/ceramics | `https://www.reddit.com/r/ceramics/.rss` | broken | 403 Blocked — Reddit blocks automated access |

---

## APIs (Free Tier)

### Financial Data

| Provider | Free Tier | Key Required | Primary Use |
|----------|-----------|-------------|-------------|
| FRED | 120 req/min, no daily cap | Yes (free) | Treasury yields, VIX, economic indicators |
| Finnhub | 60 req/min, no daily cap | Yes (free) | Stock quotes, market news, economic calendar |
| Financial Modeling Prep | 250 req/day | Yes (free) | Sector performance, index quotes |
| yfinance | No key, unreliable | No | Pre-market futures (backup only) |

### Crypto Data

| Provider | Free Tier | Key Required | Primary Use |
|----------|-----------|-------------|-------------|
| CoinGecko Demo | 10,000 req/month, 30/min | Yes (free) | Crypto prices, market data |
| DeFi Llama | Unlimited, no key | No | DeFi TVL, protocol data, yields |
| Etherscan | 100,000 req/day | Yes (free) | ETH gas prices |

### Art

| Provider | Free Tier | Key Required | Primary Use |
|----------|-----------|-------------|-------------|
| Met Museum API | 80 req/sec, no cap | No | Daily artwork (470K+ works, CC0) |
| Art Institute of Chicago | Generous, no cap | No | Alternative daily artwork |
| Cleveland Museum of Art | Generous, no cap | No | Alternative daily artwork |
| Rijksmuseum | Generous | Yes (free) | European art collections |
| Wikimedia POTD | RSS feed, no key | No | Picture of the Day |

---

## MCP Servers (for Claude Pro integration — future)

| Server | URL/Repo | Cost | Use Case |
|--------|----------|------|----------|
| Unusual Whales | `https://unusualwhales.com/public-api/mcp` | Paid | Options flow, dark pool, insider trades |
| CoinGecko MCP | `https://mcp.api.coingecko.com/mcp` | Free | Crypto data via Claude |
| Alpha Vantage MCP | `https://mcp.alphavantage.co/` | Free tier | Stock data via Claude |
| Yahoo Finance MCP | github.com/AgentX-ai/yahoo-finance-server | Free | Market data via Claude |
| feed-mcp | github.com/richardwooding/feed-mcp | Free | RSS feeds via Claude |
| Brave Search MCP | Official Brave | Free tier | News search via Claude |
