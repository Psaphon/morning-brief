# AI Development Prompt — relevance-scoring

**Branch:** `feature/relevance-scoring`
**Base:** `develop`

Read `CLAUDE.md` for project context and `docs/DEVPLAN.md` for full acceptance criteria.
Do NOT push — the host workflow handles push and PR.

## What to build

A heuristic article scoring engine that ranks articles by relevance without using an LLM. This determines which articles are most important for summarization and the daily briefing. Must run in milliseconds.

### 1. Create `src/processors/scorer.py`

Create a new module with:

```python
@dataclass
class ScoredArticle:
    article: dict
    score: float
    factors: dict[str, float]  # breakdown of contributing factors

def score_articles(articles: list[dict], config: ScoringConfig | None = None) -> list[ScoredArticle]:
    """Score and rank articles by relevance. Returns sorted list, highest score first."""
```

**Scoring factors** (each returns a 0.0–1.0 value, multiplied by its weight):

1. **Source reputation** (weight: 0.25) — Map source names to reputation scores. Defaults: major outlets (BBC, NPR, ProPublica, PBS) = 1.0; mid-tier (CoinDesk, The Guardian, Al Jazeera) = 0.8; niche/blogs = 0.5. Make the mapping a dict constant that's easy to update.

2. **Recency** (weight: 0.2) — Score based on `published_at` or `fetched_at`. Articles from the last 6 hours = 1.0, linearly decaying to 0.0 at 48 hours.

3. **Cross-source coverage** (weight: 0.25) — Reuse the `_normalize_title()` function from `src/processors/dedup.py` to detect when multiple sources cover the same story. 2+ sources = 1.0, 1 source = 0.3.

4. **Category priority** (weight: 0.2) — Configurable weights per category. Defaults: `politics` = 1.0, `florida` = 1.0, `world` = 0.8, `crypto` = 0.7, `tech` = 0.6, `art` = 0.4.

5. **Title keyword signals** (weight: 0.1) — Boost for keywords: "breaking", "exclusive", "investigation", "urgent". Score 1.0 if any present, 0.0 otherwise.

**ScoringConfig** should be a dataclass with the weights and mappings above as defaults, so they can be overridden.

**Category-balanced output:** Add a helper function:
```python
def select_top_articles(scored: list[ScoredArticle], per_category: int = 5, total: int = 25) -> list[ScoredArticle]:
    """Select top articles ensuring each category gets at least `per_category` slots."""
```

### 2. Modify `src/db.py`

- Add `score REAL` column to the `articles` table schema
- Add idempotent `ALTER TABLE` migration (same pattern as `last_seen_at`)
- Add `update_score(article_id: int, score: float) -> None` method
- Add index: `CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(score);`

### 3. Modify `src/main.py`

Wire scoring into the pipeline between Stage 2 (dedup) and Stage 3 (summarization):

```python
# Stage 2.5: Score articles by relevance
logger.info("Scoring articles by relevance...")
from .processors.scorer import score_articles, select_top_articles
# Score today's articles from DB
todays = db.get_todays_articles()
scored = score_articles(todays)
for sa in scored:
    db.update_score(sa.article["id"], sa.score)
top = select_top_articles(scored)
logger.info("Scored %d articles, selected top %d for summarization", len(scored), len(top))
```

### 4. Create `tests/test_scorer.py`

Test each scoring factor independently:
- Source reputation: known source scores higher than unknown
- Recency: recent article scores higher than old one
- Cross-source: article appearing in 2+ sources scores higher
- Category priority: politics scores higher than art
- Title keywords: "breaking" in title boosts score
- Category balance: `select_top_articles` returns articles from all categories
- Default config produces reasonable scores (all between 0.0 and 1.0)

## Commit message

```
feat: add heuristic relevance scoring for article prioritization

Score articles by source reputation, recency, cross-source coverage,
category priority, and title signals. Category-balanced selection
ensures diverse briefing content. No LLM required.
```

## Rules

- Run `ruff check . && ruff format --check .` before committing
- Run `pytest tests/ -v` before committing
- Do NOT push
- Do NOT modify files outside `src/processors/scorer.py`, `src/db.py`, `src/main.py`, and `tests/test_scorer.py`
- Import `_normalize_title` from `src/processors/dedup.py` — do NOT duplicate it
