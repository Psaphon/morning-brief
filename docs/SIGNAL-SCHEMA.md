# Signal Schema — morning-brief → atrade

**Status:** APPROVED 2026-09-01. Implementation may proceed on both sides
(producer first — see §11).
**Schema version:** `1.0.0`
**Producer:** `morning-brief`, feature `signal-emit` (`src/publishers/signals.py`)
**Consumer:** `atrade`, feature `signal-schema-and-ingest` (`src/atrade/signals/`)

This file is the single definition of the contract. It is duplicated verbatim into
`morning-brief/docs/SIGNAL-SCHEMA.md` on merge; if the two copies ever disagree, this
one (in the consumer repo, where correctness costs money) wins.

---

## 1. What this contract is for

morning-brief scores *articles* for relevance. atrade trades *tickers*. This contract
is the mapping between them: a point-in-time, per-ticker score that atrade can replay
historically without leaking information that was not knowable at the time.

Everything below exists to protect one property:

> **A backtest replaying these artifacts must see exactly what a live run would have
> seen at that moment — no more.**

---

## 2. The artifact

| Property | Value |
|---|---|
| Path | `data/signals/signals-<YYYY-MM-DD>.json` (relative to morning-brief's data root) |
| Encoding | UTF-8 JSON, LF line endings |
| Write mode | **Atomic** — write to `signals-<date>.json.tmp`, `fsync`, then `rename()` |
| Cardinality | One file per calendar date (UTC), rewritten in full if the day is re-emitted |
| Failure mode | Emitting is gated and non-fatal. A failure here must never break the brief. |

Atomic write is required because atrade may read while morning-brief writes. A partial
file must never be observable. `rename()` on the same filesystem is atomic on Linux;
the `.tmp` file must live in the same directory as the target.

### Top-level shape

```json
{
  "schema_version": "1.0.0",
  "emitted_at": "2026-09-02T09:31:04Z",
  "universe_ref": "atrade/config/universe.toml@<git-sha>",
  "signals": [
    {
      "ticker": "MSFT",
      "score": 0.7314,
      "knowable_at": "2026-09-02T06:12:00Z",
      "article_count": 3,
      "provenance": [
        {"article_id": 41827, "source": "reuters", "url_hash": "9f2c…", "published_at": "2026-09-02T06:12:00Z"},
        {"article_id": 41833, "source": "bloomberg", "url_hash": "1ab7…", "published_at": "2026-09-02T05:40:00Z"},
        {"article_id": 41902, "source": "ap", "url_hash": "77de…", "published_at": "2026-09-01T22:15:00Z"}
      ]
    }
  ]
}
```

---

## 3. Field definitions

### Envelope

| Field | Type | Required | Rule |
|---|---|---|---|
| `schema_version` | string | yes | Semver. Consumer **must** reject a major version it does not implement. |
| `emitted_at` | string | yes | ISO-8601 UTC, `Z`-suffixed. Wall-clock time the file was written. **Never** used as `knowable_at`. |
| `universe_ref` | string | yes | Which universe this was scored against. See §6. |
| `signals` | array | yes | May be empty. An empty array is a valid, meaningful result (no news matched). |

### Signal record

| Field | Type | Required | Rule |
|---|---|---|---|
| `ticker` | string | yes | Uppercase. **Must** be a member of the referenced universe. |
| `score` | number | yes | Bounded `[0.0, 1.0]`, 4 decimal places. See §4. |
| `knowable_at` | string | yes | ISO-8601 UTC. **The point-in-time control.** See §5. |
| `article_count` | integer | yes | Number of contributing articles. Must equal `len(provenance)`. |
| `provenance` | array | yes | Non-empty. See §7. |

**No free text anywhere in a signal record.** No headline, no summary, no model output.
Article text reaches this file only as an opaque `url_hash` and a numeric score. This is
deliberate: news sources are untrusted input, and a prompt-injection payload in a headline
must have no path into anything atrade evaluates.

---

## 4. Score semantics

`score` is a bounded, rank-able aggregate of the relevance scores of the articles that
matched this ticker. It is **not** a directional signal — it carries no buy/sell opinion,
only "how much relevant news flow does this ticker have right now".

morning-brief's article relevance score is already bounded `[0.0, 1.0]`: it is a weighted
sum of five factors each in `[0, 1]`, with weights summing to `1.0`
(`src/processors/scorer.py`, `ScoringConfig`). The per-ticker aggregate must preserve that
bound.

**Aggregation is the mean of contributing article scores**, rounded to 4 decimals. Mean, not
sum: a sum would make "many mediocre articles" outrank "one excellent article" purely on
volume, and would break the `[0, 1]` bound. Volume is carried separately and honestly in
`article_count`, so a consumer that wants to weight by volume can — explicitly.

### The reproducibility rule — read this before writing a backtest

One of morning-brief's five scoring factors is **recency**, computed against `now` at
scoring time (`_score_recency`, which takes `now` as a parameter). This means **an article's
relevance score is not reproducible after the fact** — rescoring the same article tomorrow
yields a lower recency component and therefore a different score.

That is not look-ahead bias (recency uses only past information), but it has a hard
consequence:

> **The emitted artifact is the point-in-time record of truth. A backtest must replay the
> stored `signals-<date>.json` files. It must NEVER recompute scores from morning-brief's
> article archive** — doing so silently produces scores that were never actually observable
> on that date.

atrade's backtest harness must read only from the `signals` table populated by ingest.

---

## 5. `knowable_at` — the point-in-time control

`knowable_at` is the earliest timestamp at which **all** information contributing to this
signal was publicly available. It is the single most important field in this contract.

```
knowable_at = max(article.published_at or article.fetched_at  for article in provenance)
```

**`max`, not `min`.** The signal is only fully knowable once its *last* contributing article
exists. Taking the minimum would date a three-article signal to its earliest article and
backdate information that had not yet been published.

### The nullable-`published_at` fallback

morning-brief's `articles` table declares `published_at TEXT` (**nullable**) and
`fetched_at TEXT NOT NULL` (`src/db.py`). Many RSS sources omit a publish date.

- If `published_at` is present, use it.
- If absent, fall back to `fetched_at` — the time morning-brief first saw the article, which
  is a strictly *later* (more conservative) bound. Never earlier.
- **Never** fall back to `now`, to `emitted_at`, or to the file date. All three are
  look-ahead leaks.

### Consumer obligations

- A record with a missing, empty, or unparseable `knowable_at` **must be rejected**, not
  defaulted. atrade's `signals.knowable_at` is `NOT NULL` precisely to make this
  unrepresentable (`src/atrade/db.py`).
- A record whose `knowable_at` is **after** `emitted_at` must be rejected as malformed —
  it claims to know something from the future.
- Rejected records are logged with the reason and counted; they never silently vanish.

---

## 6. Universe binding

The ticker universe is owned by **atrade** (`config/universe.toml`). morning-brief's
`config/ticker_map.toml` maps keywords/entities onto those same symbols.

`universe_ref` records which universe revision the file was scored against, so a backtest can
detect that the universe changed mid-history rather than silently comparing incomparable
periods.

- A `ticker` not in the referenced universe **must be rejected** by the consumer.
- Articles matching no ticker contribute to no signal. They are not an error.
- When the universe changes, `ticker_map.toml` must be updated in the same change. Keeping
  them in sync is a `[HUMAN]` review point, not an automated guarantee.

---

## 7. Provenance

Each entry records one contributing article:

| Field | Type | Rule |
|---|---|---|
| `article_id` | integer | morning-brief's `articles.id`. Local to morning-brief; opaque to atrade. |
| `source` | string | Lowercased publisher name, e.g. `reuters`. |
| `url_hash` | string | morning-brief's `articles.url_hash`. Stable identifier without carrying the URL. |
| `published_at` | string \| null | The article's own timestamp, `null` when the source omitted it. |

Provenance exists so a surprising trade can be traced to the specific articles that caused
it. `published_at` is retained per-article (including its `null`) so an auditor can see
*which* articles used the `fetched_at` fallback.

---

## 8. Versioning

Semver on `schema_version`.

- **Patch** — clarifications, no wire change.
- **Minor** — new **optional** field. Consumers ignore unknown fields; a 1.0.0 consumer must
  read a 1.1.0 file without error.
- **Major** — anything else: removing a field, changing a type, changing the meaning of
  `knowable_at` or the aggregation rule. Consumer rejects unknown majors outright.

Both repos bump in the same coordinated change. The producer must never emit a version the
consumer has not shipped support for.

---

## 9. Required change to atrade's `signals` table

The current table (`src/atrade/db.py`) predates this contract and cannot hold it:

```sql
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT    NOT NULL,
    score       REAL    NOT NULL,
    knowable_at TEXT    NOT NULL,
    source      TEXT    NOT NULL,   -- single value; cannot hold multi-article provenance
    ingested_at TEXT    NOT NULL
);
```

It has no `schema_version` column and only a scalar `source`, while provenance is a list.
`signal-schema-and-ingest` must migrate to:

```sql
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker         TEXT    NOT NULL,
    score          REAL    NOT NULL,
    knowable_at    TEXT    NOT NULL,  -- ISO-8601; NOT NULL is the point-in-time control
    schema_version TEXT    NOT NULL,
    source         TEXT    NOT NULL,  -- producer name, e.g. 'morning-brief'
    provenance     TEXT    NOT NULL,  -- JSON array, per §7
    article_count  INTEGER NOT NULL,
    universe_ref   TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL,
    UNIQUE(ticker, knowable_at, source)
);
```

The `UNIQUE` constraint makes ingest idempotent: re-ingesting a day's file must not double
rows. Re-emission of the same date is expected and must be safe.

`positions`, `orders`, and `equity_curve` are unaffected.

---

## 10. Sign-off decisions

Signed off by the user 2026-09-01. These answer the draft's four open questions and are
now part of the contract, not proposals. Do not reopen them without a version bump.

1. **Aggregation is the mean.** Confirmed as specified in §4. `article_count` carries volume
   separately; a strategy that wants to weight by volume must do so explicitly. Changing this
   is a **major** version bump (§8) because the strategy layer builds on the semantic.

2. **Drop and recreate the `signals` table.** Verified 2026-09-01: no atrade database exists
   on disk — not in the repo, not under `~/.local/share/atrade`. atrade is pre-first-run, so
   there are zero rows to preserve. `signal-schema-and-ingest` applies §9's schema directly;
   no `ALTER` migration is required.

3. **Retention: keep `data/signals/*.json` indefinitely, and back it up.** These files are the
   only replayable record of what was knowable on a given date (§4's reproducibility rule),
   so deletion is irreversible data loss, not a cleanup. No pruning, no rolling window.

   **Open follow-up task — the backup is not wired.** hub's `scripts/data-backup.sh` archives
   `$DATA_DIR` (default `/data`), while morning-brief's data root currently lives inside its
   worktree at `/home/comp/Projects/morning-brief/data`. That path is **not** in the backup
   set today. Bringing it in is a separate piece of work and must not be assumed done.

4. **Universe drift stays a `[HUMAN]` review point for v1.** No cross-repo CI check is built
   now: v1 is paper-only, the universe changes rarely, and a missed sync costs nothing real.
   §6's rule stands — when the universe changes, `ticker_map.toml` changes in the same review.

   **Revisit before any live-money run.** The failure mode is silent (a ticker simply stops
   producing signals, with no error), which is exactly the kind of fault that is cheap to
   tolerate on paper and expensive to tolerate live.

---

## 11. Implementation order

Both DEVPLANs require the producer to ship before the consumer:

1. morning-brief `signal-emit` — writes `data/signals/signals-<date>.json` per §2–§7.
2. atrade `signal-schema-and-ingest` — migrates the table per §9, reads and validates.

Then the atrade cascade: `strategy-and-risk-gate` → `backtest-harness` + `paper-loop` →
`track-record-report` → `docs-and-readme`.
