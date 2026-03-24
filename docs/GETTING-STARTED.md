# Getting Started — Developing Morning Brief with Containerized Claude Code

## 1. Start the AI Dev Container

The project has a Docker-based Claude Code sandbox in `.ai/`. To use it:

```bash
cd /home/comp/Projects/morning-brief

# Make sure your API key is exported (never stored on disk)
export ANTHROPIC_API_KEY=sk-ant-...

# Start the Claude Code container
dtl ai start --project .

# Or manually:
docker compose -f .ai/docker-compose.yml up -d
```

### Interactive Session (recommended for development)

```bash
# Shell into the Claude Code container
docker compose -f .ai/docker-compose.yml run --rm claude-code

# This drops you into an interactive Claude Code CLI session
# inside /workspace (which is your project directory mounted in)
# Claude has access to all your project files and can edit them
```

### Autonomous Mode (for hands-off tasks)

```bash
# Run a single task and get notified when done
dtl ai run --project . --prompt "Test the RSS pipeline against 5 feeds and report results"

# If you've configured Telegram notifications:
dtl ai config-notify --project . \
    --telegram-token YOUR_BOT_TOKEN \
    --telegram-chat-id YOUR_CHAT_ID --test
```

### Check Status / Stop

```bash
dtl ai status --project .
dtl ai stop --project .
```

---

## 2. Create the Feature Branch (Gitflow)

Before doing any work, branch off `develop`:

```bash
# Make sure you're on develop and it's up to date
git checkout develop
git pull origin develop

# Create your feature branch
git checkout -b feature/test-rss-pipeline

# Push the branch so CI runs on it
git push -u origin feature/test-rss-pipeline
```

---

## 3. Set Up Local Python Environment

You need a local venv to run and test the pipeline (the container is for AI-assisted development, but you run the code locally or in the devcontainer):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Test the RSS Pipeline Against Real Feeds

### Step 1: Quick smoke test — does the feed parser work?

```bash
# Run the existing tests first
pytest tests/ -v
```

### Step 2: Test feed parsing against real FEEDS.md

Open a Python REPL or create a quick test script:

```python
# test_real_feeds.py (don't commit this — it hits the network)
import asyncio
from pathlib import Path
from src.fetchers.rss import parse_feeds_md, fetch_all_feeds

# First, just parse the feed list (no network)
feeds = parse_feeds_md(Path("docs/FEEDS.md"))
print(f"Found {len(feeds)} feeds in FEEDS.md\n")
for f in feeds:
    print(f"  [{f['category']}] {f['source']}: {f['url']}")

# Then fetch a few (hits the network)
async def test_fetch():
    articles = await fetch_all_feeds(Path("docs/FEEDS.md"))
    print(f"\nFetched {len(articles)} total articles")

    # Show breakdown by category
    from collections import Counter
    cats = Counter(a.category for a in articles)
    for cat, count in cats.most_common():
        print(f"  {cat}: {count}")

    # Show first 5 articles
    print("\nSample articles:")
    for a in articles[:5]:
        print(f"  [{a.source}] {a.title}")
        print(f"    {a.url}")

asyncio.run(test_fetch())
```

Run it:
```bash
python test_real_feeds.py
```

### Step 3: Test the full pipeline (fetch → extract → dedup → store)

```bash
# Run the full pipeline
python -m src.main
```

This will:
1. Parse all feeds from docs/FEEDS.md
2. Fetch articles from each feed concurrently
3. Extract full text via trafilatura
4. Deduplicate by URL, title, and content hash
5. Store in data/morning_brief.db

Check the database:
```bash
# Quick peek at what landed
python3 -c "
import sqlite3
db = sqlite3.connect('data/morning_brief.db')
count = db.execute('SELECT COUNT(*) FROM articles').fetchone()[0]
print(f'Total articles: {count}')
cats = db.execute('SELECT category, COUNT(*) FROM articles GROUP BY category ORDER BY COUNT(*) DESC').fetchall()
for cat, n in cats:
    print(f'  {cat}: {n}')
db.close()
"
```

### Step 4: Update feed statuses in FEEDS.md

As you test, update the status column in `docs/FEEDS.md`:
- `untested` → `active` (feed works)
- `untested` → `broken` (feed returns errors or has no entries)

### Step 5: Fix any broken feeds, commit progress

```bash
# Stage specific files
git add docs/FEEDS.md src/fetchers/rss.py tests/

# Commit with conventional commit format
git commit -m "feat: validate RSS feeds against live sources

Tested all feeds in FEEDS.md, updated statuses.
X feeds active, Y feeds broken."

# Push to your feature branch
git push origin feature/test-rss-pipeline
```

---

## 5. When You're Done — PR Back to Develop

```bash
# Push any remaining commits
git push origin feature/test-rss-pipeline

# Create a PR from feature branch → develop
gh pr create \
    --base develop \
    --title "feat: validate RSS pipeline with real feeds" \
    --body "## Summary
- Tested RSS pipeline against live feeds
- Updated FEEDS.md statuses
- Fixed any parsing issues

## Test plan
- [ ] All existing tests pass
- [ ] Pipeline runs end-to-end
- [ ] Articles stored in SQLite correctly"
```

After review/merge, delete the feature branch:
```bash
git checkout develop
git pull origin develop
git branch -d feature/test-rss-pipeline
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Start AI container | `dtl ai start --project .` |
| Interactive AI session | `docker compose -f .ai/docker-compose.yml run --rm claude-code` |
| Autonomous AI task | `dtl ai run --project . --prompt "..."` |
| Stop AI | `dtl ai stop --project .` |
| Run tests | `pytest tests/ -v` |
| Run pipeline | `python -m src.main` |
| Lint | `ruff check . && ruff format --check .` |
| New feature branch | `git checkout develop && git checkout -b feature/name` |
| Create PR | `gh pr create --base develop --title "..."` |
