# AI Development Prompt — social-feeds

**Branch:** `feature/social-feeds`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` → `## Feature: social-feeds` for the acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

Support journalist-direct sources:
- Bluesky and Mastodon profile RSS (`post`).
- Substack and Ghost newsletters (`newsletter`).
- Ordinary feeds stay `article`.

### Facts about the current code (verified by the PM)

- **Feed config:**
  - `docs/FEEDS.md` is parsed by `parse_feeds_md()` in `src/fetchers/rss.py`, giving `{source, url, category}`.
  - Lanes come from `config/feed_lanes.toml` plus the host overlay `$MORNING_BRIEF_FEEDS_OVERLAY`, via `load_feed_lanes()`/`load_lanes_for_run()` in `src/processors/lanes.py`. Each entry is `[feeds."<source>"]` with `lanes`.
  - The overlay may add new feeds with `url` and `category`, and those are fetched (`FeedLanes.extra_feeds`).
  - **Put `kind` on the same per-feed entries:** `kind = "article" | "post" | "newsletter"`, default `article`, unknown values raise `ValueError` like bad lanes. Expose it as `FeedLanes.kind_for(source)`.
  - **A `post` feed with no explicit `lanes` defaults to `["reader"]` only**, not the global default. This keeps posts out of signals, and the golden test depends on it.
- **Fetch:** `fetch_feed()` builds `RawArticle(url, title, source, category, author, published, summary)` from feedparser entries. `summary` is the RSS summary only; there is no field for `content:encoded` yet.
- **Page fetch:** `extract_single()` in `src/processors/extractor.py` fetches the page, runs trafilatura and falls back to `article.summary`. `_article_to_dict()` makes the DB row.
- **Extraction:**
  - `summarize_single()` in `src/summarizers/local.py` calls Ollama with `EXTRACTION_SCHEMA` (`what_happened`, `details`, `entities`, `numbers`, `event_date`, `status`, `is_roundup`).
  - `run_pipeline()` then validates via `ValidatorRun` (`src/validators/grounding.py`) and saves with `db.save_extraction(...)`.
  - The test stub `tests/stub_ollama.py` records generate calls (`generate_calls`).
- **Source audit:** `src/processors/source_audit.py` `classify(...)` returns `ok/truncated/paywalled/failing`. `AUDIT_MIN_WORDS` (default 150) is read in `src/cli.py`, and `DEFAULT_MIN_WORDS = 150` is in the module.

### Implement

1. **`kind` in lanes config** (above). Document it in `config/feed_lanes.toml`'s header comment, and add a commented overlay example to `interests.example.toml`. **No real accounts or journalists are committed.** They go in the host overlay (plan Notes).

2. **Fetch:** add `content: str | None` to `RawArticle`, filled from the entry's `content[0].value` when present (HTML stripped to text with trafilatura or a simple tag strip). Carry the feed's `kind` on `RawArticle` (or look it up downstream from `source`), whichever is cleaner.

3. **`newsletter`:** in `extract_single`, if `content` has at least `AUDIT_MIN_WORDS` words, use it as `full_text` and skip the page fetch. Otherwise behave exactly like `article`.

4. **`post`:**
   - **Grouping first:** before extraction, posts from the same feed and the same `author` (fall back to the feed name when there's no author), published within `POST_GROUP_HOURS` (env, default 12) of the previous post in the group, are merged into one RawArticle:
     - text: the posts' texts joined in time order;
     - url: the first post's URL;
     - title: the first post's first line, cut to 100 characters;
     - published: the earliest time.
   - Implement as a pure function in a new `src/processors/posts.py` (`group_posts(raw_articles, hours)`) and call it in `run_pipeline()` before `extract_articles`.
   - **No page fetch:** `full_text` = the post text (use `content` or `summary`, HTML stripped).
   - **No extraction call:** in the summarize step, a `post` article becomes an extraction dict without calling Ollama:
     - `what_happened` = the post text trimmed with `trim_words(text, 120)` (this also applies the 2-sentence cap);
     - `details` = [], `entities` = [], `numbers` = [], `event_date` = null, `status` = `"happened"`, `is_roundup` = false.
   - **"Validated against itself":** run the validator with the post text as the source. It passes trivially but keeps the counts honest. Save via `db.save_extraction` with model `"post"`.
   - The stub must record **no** generate call for post articles (test this).

5. **`source-audit`:** feeds with `kind = "post"` get verdict `ok-post` when they have items and are not failing. Posts are short by design, so the short-share and median-words rules don't apply to them. `failing` still applies.

6. `POST_GROUP_HOURS` lives in `src/config.py`.

### Tests (`tests/test_social_feeds.py`), with synthetic fixtures in `tests/fixtures/social/`

- `bluesky_profile.xml`: a Bluesky-style profile RSS with 4 posts. Three of them are from one author within 2 hours (a thread), and one is 20 hours later.
- `mastodon_profile.xml`: Mastodon-style RSS with HTML in the description.
- `newsletter.xml`: a Substack/Ghost-style feed with full `content:encoded` (> 150 words) on one item and a teaser-only item.
- **Assertions:**
  - The Bluesky fixture groups to 2 source entries.
  - Post `what_happened` is ≤ 120 words, and the stub records zero generate calls for posts.
  - The full-content newsletter item makes no page fetch (use an `httpx.MockTransport` that fails on any request for that URL), and the teaser item does fetch.
  - A `post` feed without lanes has lanes `{"reader"}`.
  - The audit gives `ok-post` for a post feed of short items.
  - `kind = "tweet"` raises `ValueError`.
- `tests/test_signals_golden.py` must pass **unchanged**.

No network. Follow `tests/test_feed_lanes.py` and `tests/test_extraction.py` style.

## Rules

- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit. Both must be clean.
- Do NOT push.
- Scope:
  - `src/processors/lanes.py`, `src/processors/posts.py`, `src/processors/extractor.py`, `src/processors/source_audit.py`
  - `src/fetchers/rss.py`, `src/summarizers/local.py`, `src/main.py`, `src/config.py`
  - `config/feed_lanes.toml` (header comment only), `interests.example.toml`
  - `tests/fixtures/social/`, `tests/test_social_feeds.py`, `tests/stub_ollama.py` (only if needed)
  - `docs/DEVPLAN.md` (status line: `In Progress`)
- Do not change signals, `docs/SIGNAL-SCHEMA.md` or `config/ticker_map.toml`. Do not add real feeds or accounts.
- No personal or location details. Fixture authors and handles are invented (e.g. `reporter.example`).
- Commit message:
  ```
  feat: social-feeds — Bluesky/Mastodon posts and full-content newsletters

  Feeds get a kind (article, post, newsletter) in the lanes config. Posts skip
  page fetch and the extraction call, are grouped per author within
  POST_GROUP_HOURS, and default to the reader lane only. Newsletters with full
  content in the feed skip the page fetch. source-audit reports ok-post.
  ```
