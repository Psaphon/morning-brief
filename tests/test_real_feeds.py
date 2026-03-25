"""Live RSS feed integration test — hits the network."""

import asyncio
from collections import Counter
from pathlib import Path

from src.fetchers.rss import fetch_all_feeds, parse_feeds_md

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
    cats = Counter(a.category for a in articles)
    for cat, count in cats.most_common():
        print(f"  {cat}: {count}")

    # Show first 5 articles
    print("\nSample articles:")
    for a in articles[:5]:
        print(f"  [{a.source}] {a.title}")
        print(f"    {a.url}")


asyncio.run(test_fetch())
