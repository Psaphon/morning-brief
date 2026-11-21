# AI Development Prompt — feed-lanes

**Branch:** `feature/feed-lanes`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: feed-lanes` for the full acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Give each feed explicit lanes, so the reader page, atrade's signals and R&D material can each change without affecting the others:
- `reader` feeds the briefing and the dashboard.
- `signals` feeds the per-ticker signals.
- `rnd` is stored in the DB only. It is for the future `radar-flags` feature and is never shown in the briefing or on the page.

### Facts about the current code (verified by the PM)

- **Feeds** come from `docs/FEEDS.md` via `parse_feeds_md()` in `src/fetchers/rss.py` (`config.feeds_path`). Each feed is `{source, url, category}`. `source` (the feed name, e.g. `ArXiv CS.AI`) is the **feed id** everywhere: `articles.source` in the DB, signal provenance, and `[feeds."<name>"]` in the operator's interests TOML.
- **There is no existing `signals = false` flag in the code.** The migration criterion therefore reduces to the rule "a feed with no `lanes` gets `["reader", "signals"]`". Implement that, and say in the PR body that there was nothing to migrate.
- **`run_pipeline()` in `src/main.py` has three article paths:**
  1. **Signals:** `emit_signals(articles=todays, ...)`, where `todays = db.get_todays_articles()` (Stage 2.6).
  2. **Summarization/briefing:** `candidates = db.get_unsummarized_articles()` → `filter_by_feed_minimum` → dedupe → `select_balanced_articles`. `generate_daily_briefing(db, ...)` then reads summaries from the DB.
  3. **Dashboard:** `all_articles = db.get_todays_articles()` passed to `render_dashboard`.
- Scoring (`score_articles(todays)`) stays as is for all lanes; lanes only decide where articles are *used*.

### Implement

1. **`config/feed_lanes.toml`** (new, committed):
   ```toml
   default = ["reader", "signals"]

   [feeds."ArXiv CS.AI"]
   lanes = ["rnd"]
   ```
   - List **every** active RSS feed from `docs/FEEDS.md` explicitly with its lanes, so the file is auditable. Today every feed except arXiv is `["reader", "signals"]`.
   - Valid lanes are exactly `reader`, `signals` and `rnd`. An unknown lane, an empty list or a non-list raises a clear `ValueError` at load.
   - Add a test that every feed parsed from `docs/FEEDS.md` has an entry. The test fails if someone adds a feed without lanes.

2. **`src/processors/lanes.py`** (new):
   - `load_feed_lanes(path: Path = DEFAULT_LANES_PATH, overlay: Path | None = None) -> FeedLanes`.
   - `FeedLanes.lanes_for(source: str) -> frozenset[str]` returns the default for unknown sources.
   - `filter_lane(articles, lane, feed_lanes)` keeps the articles whose `source` has that lane.
   - **Overlay:** `$MORNING_BRIEF_FEEDS_OVERLAY` (a path, read in `src/config.py`) uses the same TOML shape and merges over the committed file, matched by feed id.
     - An overlay entry replaces that feed's `lanes`.
     - An overlay may also add a brand-new feed with `url` and `category` plus `lanes`. Those feeds are appended to the fetch list.
     - A missing overlay file is skipped with exactly one INFO log line. A malformed overlay raises `ValueError`, as for the committed file.

3. **Wire it into `run_pipeline()`.** Load once, near `load_interests()`.
   - Signals: `emit_signals(articles=filter_lane(todays, "signals", lanes), ...)`.
   - Summarization: filter `candidates` to `reader` before `filter_by_feed_minimum`.
   - Dashboard: `filter_lane(all_articles, "reader", lanes)`.
   - Briefing: check `generate_daily_briefing` in `src/summarizers/local.py`. If it reads summarized articles from the DB without going through the filtered candidates, filter its input to `reader` too, so a previously summarized `rnd` article can never reach the briefing.
   - `rnd` articles must still be fetched, extracted and stored. Only their use is gated.
   - Overlay feeds with a `url` are fetched along with the `FEEDS.md` feeds.

4. **Fix `parse_feeds_md()`: read only the `## RSS Feeds` section.** Today it also turns the `## APIs (Free Tier)` and `## MCP Servers` table rows into RSS feeds, because their URLs are in backticks and their third column matches `\w+`. The pipeline then fetches MCP and API endpoints as RSS every run (seen on hub: `Unusual Whales` and `CoinGecko MCP` "feeds").
   - Stop collecting rows at the next `## ` heading after `## RSS Feeds`.
   - Test with a fixture `FEEDS.md` containing all three sections.

5. **`docker-compose.yml`:** add `MORNING_BRIEF_FEEDS_OVERLAY=/app/interests/feeds-overlay.toml`. The `${HOME}/.config/morning-brief` directory is already mounted read-only at `/app/interests`. A missing file is fine (INFO line).

6. **`interests.example.toml` / README:** a short note that feed lanes live in `config/feed_lanes.toml` and can be overridden per host with the overlay file. Include a commented example.

### Tests (`tests/test_feed_lanes.py`)

- Default rule: a source not in the file gets `reader` and `signals`.
- **arXiv:** with the committed `config/feed_lanes.toml`, a fixture run of the selection path drops `ArXiv CS.AI` articles from the reader set (dashboard list and summarization candidates) and from the signals input. They are still present in the DB table.
- **Signals-only feed:** a fixture feed with `lanes = ["signals"]` shows up in the signals output and is absent from the page article list.
- **Reader-only feed:** the reverse of the signals-only test.
- **Overlay:**
  - It overrides lanes by feed id.
  - It adds a new feed with a url.
  - A missing overlay file logs one INFO line and changes nothing (use `caplog`).
  - Malformed lanes raise `ValueError`.
- **Coverage:** every `docs/FEEDS.md` feed has an entry in `config/feed_lanes.toml`.
- **Parser:** `parse_feeds_md` ignores the APIs and MCP sections.
- **Golden test:** `tests/test_signals_golden.py` must pass **unchanged**. Do not edit it or its fixtures. If it fails, your lane wiring changed signal output for non-`rnd` feeds; fix the code, not the test.

Follow the existing test style (see `tests/test_interest_filter.py`). No network in tests.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Do NOT modify files outside this scope:
  - `src/config.py`, `src/main.py`, `src/fetchers/rss.py`, `src/processors/lanes.py`
  - `src/summarizers/local.py` (only for the briefing input filter, if needed)
  - `config/feed_lanes.toml`, `docker-compose.yml`, `interests.example.toml`, README
  - `tests/test_feed_lanes.py`, plus a parser test in `tests/test_rss.py` if that fits better
  - `docs/DEVPLAN.md` (status line only)
- Do not change `docs/SIGNAL-SCHEMA.md`, the signal output format or `config/ticker_map.toml`.
- No personal or location details in code, comments, fixtures or the commit message. Use synthetic feed names such as `Fixture Reader Feed` in tests.
- Set `**Status:**` for feed-lanes in `docs/DEVPLAN.md` to `In Progress`. The PM sets `Merged`.
- Commit message:
  ```
  feat: feed-lanes — reader, signals and rnd lanes per feed

  Feeds get explicit lanes in config/feed_lanes.toml (default reader+signals),
  with a per-host overlay via MORNING_BRIEF_FEEDS_OVERLAY. arXiv moves to rnd:
  stored, but kept out of the briefing, the page and signals. parse_feeds_md
  now reads only the RSS section, so API/MCP rows are no longer fetched as feeds.
  ```
