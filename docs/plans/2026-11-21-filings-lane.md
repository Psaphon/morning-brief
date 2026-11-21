# Plan: filings-lane

**Plan type:** features
**Target repo:** morning-brief (plus three companion features for atrade, at the end of this file)
**Part:** 1 of 1
**Created:** 2026-11-21

## PM Kickoff

- **First action:**
  - Add morning-brief features 1–8 below to morning-brief's `docs/DEVPLAN.md`, after the brief-reader-v2 features.
  - Add the three **atrade companion features** at the end of this file to atrade's DEVPLAN.
  - Paths marked *(confirm)* are guesses: map them to the real files before queuing.
- **Ship order:**
  1. `fetch-contact` (independent; may go first or in parallel with the contract).
  2. `filings-contract`, landing in both repos the same day.
  3. atrade `filings-reader`.
  4. morning-brief `edgar-fetcher`, `wire-feeds`, `filing-facts`.
  5. morning-brief `filings-signals` (emits the new file).
  6. `filings-backfill`, then atrade `event-study`.
  7. atrade `filings-arm`.
  8. `filing-events` (non-earnings 8-K items) is last and independent. Hold it back if the queue is full; nothing depends on it.
- **Run mode:** supervised for everything except `fetch-contact`, `filings-backfill`'s code and atrade `event-study`, which may go overnight. The rest is a new external service with a fair-access policy, a cross-repo contract change, prompt quality checked against live Ollama on hub, or a strategy change in atrade.
- **Operator setup:**
  - Before `fetch-contact`'s human step: the operator creates a mail alias that forwards to the inbox they actually watch. The PM sets it as `CONTACT_EMAIL` in hub runtime config. It is never committed and never logged.
  - No accounts, keys or payments are needed. EDGAR and the wire RSS feeds are free and need no key.
- **Supersedes / overlaps:**
  - Builds on brief-reader-v2's `feed-lanes`: adds a `filings` lane.
  - Independent of `signal-direction`. This plan defines its own `direction` field in a separate file, so it does not wait on `article-extraction`.
  - The existing `signals` file, its scorer and the existing shadow arm are untouched.
- **File in FEATURE-REQUESTS (not features):**
  - atrade: the news shadow arm turns over about 19% of equity a day, because ranks churn on about 12 noisy daily signals. Consider hysteresis or a wider band.
  - morning-brief: alias-in-text matching produces false matches (for example, a carmaker's results article matched a search company). Consider word-boundary and context checks.
  - Every other repo with a public page: the same contact footer as `fetch-contact`, built from runtime config.

## Open Questions

- {default: 2016-01-01} How far back should the backfill go? About ten years gives roughly 40 events per stock.
- {default: as written in `event-study`} The pre-registered success test. It must be fixed before the first event-study run, so the bar can't be moved after seeing results.
- {default: 20 trading days} How long a filing signal stays "fresh" for the filings arm. Post-earnings drift is usually described over weeks, not days.
- {default: include only feeds the PM verifies as free and full-text} Which wires to use. Seed list: GlobeNewswire and PR Newswire. Business Wire only if a usable free feed exists.

## Constraints

- **Existing signals untouched.** The existing signals file, `src/processors/scorer.py`, its weights, retention and the news-lane feeds are unchanged. The signals golden test passes unchanged in every PR of this plan. Filings and wire items never enter the `signals` lane.
- **Separate output file.** Filings-lane signals go to a new file (`signals-filings.json`, next to the existing one), described in its own SIGNAL-SCHEMA section. A consumer that only knows the existing file cannot pick them up by accident.
- **Point-in-time.**
  - Every filing's `knowable_at` is EDGAR's acceptance time. Every wire item's `knowable_at` is its own publish time, and a merged event takes the earlier of the two.
  - It is never the fetch time, run time or `emitted_at`, and never a date with no time.
  - A timestamp without an explicit zone, or later than the fetch time, is rejected and logged.
- **Original values only.** Facts come from the text of the filing or release as first published. Never use later-revised values (for example XBRL company-facts, which hold the latest restated figure). A backtest that uses revised numbers is using information that didn't exist on the day.
- **No free text reaches atrade.** Both new files hold only enumerated fields, numbers, timestamps, tickers, and IDs validated by pattern (accession number). A test walks every string in them.
- **The model extracts; code decides.** The local model only copies numbers and enumerated labels out of the text. Code checks every number appears in the source text, and computes direction. The model is never asked whether news is good or bad. This limits both prompt injection from a crafted release and the model "remembering" what a stock did after a past release.
- **SEC fair access.**
  - Every EDGAR request sends the shared User-Agent from `fetch-contact`, which includes `CONTACT_EMAIL`. If `CONTACT_EMAIL` is unset, the lane is disabled with one WARNING and the run still succeeds.
  - Requests are rate-limited to `SEC_MAX_RPS` (default 5), with a hard cap at the SEC's published limit (10 per second today; config, not prose).
  - 429 or 403 responses get exponential backoff, and after the configured retries the fetcher stops for the run.
  - The contact address never appears in logs.
- **Network.** Outbound HTTPS only, to EDGAR hosts and the configured wire hosts. Nothing binds or listens. No credentials are involved.
- **Storage.**
  - Filings, wire items and extracted facts live in a `filings` table in morning-brief's database, which sits under `/data` so it's covered by the nightly backups.
  - The 2-day article retention does not apply to this table: the guidance comparison and backfill need history.
  - Source text is kept, compressed, for re-extraction.
- **GPU window.**
  - Daily extraction runs inside morning-brief's existing run and counts toward its `RUN_DEADLINE_MINUTES`.
  - The backfill runs only inside `BACKFILL_WINDOW_UTC` (default 05:00–07:45 UTC, clear of the tenants in PROJECTS-CONTEXT). It is resumable and stops cleanly at the window's end.
- **Hygiene.** CIKs are public company identifiers and fine to commit. The contact address, the model's training-cutoff date and the wire feed list are runtime config.

## Features

## Feature: fetch-contact

**Branch:** `feature/fetch-contact`
**Depends on:** none
**Status:** Not Started
**Requires:** both

### Goal

Every outbound fetch identifies the tool and a way to reach the operator, and the public page shows the same contact.

### Acceptance Criteria

- [ ] All HTTP requests (feeds, article extraction, and later EDGAR and wires) go through one client that sends a single User-Agent built from config: tool name, version, and `CONTACT_EMAIL` or `CONTACT_URL`. A stub server records the header on every request type.
- [ ] With `CONTACT_EMAIL` unset, feed fetches still run with a contact-less User-Agent and log one WARNING.
- [ ] The page build adds a small "Contact" footer line from `CONTACT_EMAIL` when set, and leaves it out when unset. A test builds the page both ways.
- [ ] The address never reaches git or logs: a test scans tracked files and captured logs for the configured value.
- [ ] [HUMAN] The operator creates the alias. The PM sets `CONTACT_EMAIL` on hub, sends a test email to the alias, and confirms it arrived with a phone notification.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/http.py` *(confirm; reuse an existing client if there is one)* | Create | Shared client and User-Agent |
| `src/config.py` | Modify | `CONTACT_EMAIL`, `CONTACT_URL` |
| page template *(confirm path)* | Modify | Contact footer |
| `tests/test_fetch_contact.py` | Create | The criteria above |

### Key Decisions

- **An alias, not a real mailbox.** It forwards to an inbox the operator watches, doesn't reveal the real address, and can be switched off and replaced if it attracts spam.
- **The address is injected at build time.** It appears on the deployed page but never in the public repo.

## Feature: filings-contract

**Branch:** `feature/filings-contract`
**Depends on:** feed-lanes
**Status:** Not Started
**Requires:** ai

### Goal

Define the filings-lane file and the backfill history file in SIGNAL-SCHEMA, and add a CIK to every stock in the alias map, without changing any existing signal.

### Acceptance Criteria

- [ ] `docs/SIGNAL-SCHEMA.md` gets the next minor version and a new section for `signals-filings.json` and `filings-history.jsonl`, following §8's rules. The section covers:
  - every field and its type
  - the enums: `lane` = `filings`; `event` = `earnings`, `guidance`, `acquisition`, `agreement`, `exec_change`; `origin` = `edgar`, `wire`, `both`
  - the `direction` range [-1, 1], or null
  - `direction_n`, the count of verified components
  - the component fields: `eps_yoy`, `rev_yoy`, `rev_accel`, `guidance_change`
  - `in_model_window` (bool)
  - the `knowable_at` rule from Constraints
- [ ] A JSON schema file for both new files is committed. A test validates the synthetic fixtures against it.
- [ ] `config/ticker_map.toml` gains `cik` for every stock symbol and none for ETFs. A test loads the real file and fails if any stock lacks a 10-digit CIK or any ETF has one.
- [ ] The signals golden test passes unchanged: adding `cik` does not alter matching.
- [ ] The same SIGNAL-SCHEMA text lands in atrade (companion `filings-reader`) the same day.
- [ ] [HUMAN] The PM checks each CIK against EDGAR's company search on hub.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `docs/SIGNAL-SCHEMA.md` | Modify | Version bump, new section |
| `docs/schemas/signals-filings.schema.json` | Create | Machine-checkable schema |
| `config/ticker_map.toml` | Modify | `cik` per stock |
| `tests/fixtures/filings/` | Create | Synthetic files for both formats |
| `tests/test_filings_contract.py` | Create | The criteria above |

### Key Decisions

- **Separate file, not new records in the existing file.** atrade's current reader ignores unknown fields. If filings records arrived in the existing file, they would silently mix into the news shadow arm. A separate file makes that impossible.
- `direction` is defined here independently of `signal-direction`'s field, so this plan doesn't wait on it. The two may be unified later.

## Feature: edgar-fetcher

**Branch:** `feature/edgar-fetcher`
**Depends on:** filings-contract, fetch-contact
**Status:** Not Started
**Requires:** both

### Goal

Each run, fetch new 8-K filings for the universe's stocks from EDGAR, with their exhibit press releases and exact acceptance times.

### Acceptance Criteria

- [ ] `morning-brief filings fetch` reads the CIKs from the alias map and gets each company's recent filings from EDGAR's JSON submissions API *(PM verifies the endpoint)*. For each new 8-K it stores:
  - accession number
  - CIK and ticker
  - acceptance time (UTC)
  - item numbers
  - the primary document's and Exhibit 99.x's text
  - `origin = edgar`
  - fetch time
- [ ] Only items listed in `FILINGS_ITEMS` are stored (default `2.02,7.01,8.01,1.01,2.01,5.02`). Re-running is idempotent: accession numbers are unique.
- [ ] Acceptance-time conversion is tested on fixtures on both sides of a daylight-saving change.
- [ ] [HUMAN] The PM confirms which zone the API's acceptance time is really in, by comparing one known filing with its EDGAR index page, and records the answer in a code comment and the test.
- [ ] With `CONTACT_EMAIL` unset, the command exits 0, logs one WARNING and stores nothing. A stub HTTP server records that no request was made.
- [ ] Against the stub server:
  - every request carries the User-Agent
  - no more than `SEC_MAX_RPS` requests are sent in any one-second window
  - a 429 triggers backoff
  - repeated 403s stop the run for that day with an ERROR
  - the contact address is absent from captured logs
- [ ] The daily `morning-brief` run calls the fetch step when the `filings` lane is enabled. A fetch failure is logged and never fails the brief.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] With `CONTACT_EMAIL` already set by `fetch-contact`, the PM confirms one real run stores the latest earnings 8-K for at least one stock.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/fetchers/edgar.py` | Create | Client, rate limiter, parsing |
| `src/storage/filings.py` *(confirm)* | Create | `filings` table |
| `src/cli.py` *(confirm)* | Modify | `filings fetch` |
| `src/config.py` | Modify | `SEC_MAX_RPS`, `FILINGS_ITEMS`, lane toggle |
| `tests/fixtures/edgar/` | Create | Synthetic submissions JSON and filings |
| `tests/test_edgar_fetcher.py` | Create | The criteria above |

### Key Decisions

- **Per-company polling of the submissions API**, not the all-filers "latest filings" feed. The universe is small, the volume is a handful of requests per run, and it gives item numbers and acceptance times in one place.
- **Earnings releases are 8-K Item 2.02** with the press release attached as Exhibit 99.1, so EDGAR alone covers both the filing and the release.

## Feature: wire-feeds

**Branch:** `feature/wire-feeds`
**Depends on:** edgar-fetcher
**Status:** Not Started
**Requires:** both

### Goal

Add press-release wire feeds to the `filings` lane, matched to universe stocks only by exchange-ticker tags, and merged with the EDGAR copy of the same release.

### Acceptance Criteria

- [ ] Feeds with `lanes = ["filings"]` and `kind = "wire"` are fetched by the shared feed fetcher. They never appear in the reader or signals lanes (a fixture proves both).
- [ ] An item matches a stock only through an exchange tag, such as `(NYSE: XYZ)` or `(Nasdaq: XYZ)`, for a universe stock. Alias text is never used. Fixtures:
  - an item that mentions the company name without a tag is not matched
  - a tag for a non-universe ticker is not matched
  - ETFs are never matched
- [ ] Matched items are stored in `filings` with `origin = wire` and their publish time.
- [ ] A wire item and an EDGAR filing for the same ticker are merged into one event (`origin = both`) when both hold:
  - they fall within `MERGE_WINDOW_HOURS` (default 24)
  - their normalized opening text is similar enough (`MERGE_SIMILARITY`, default 0.8)

  The merged event keeps the earlier timestamp as `knowable_at`, and extraction uses the EDGAR text. A fixture pair proves it.
- [ ] The signals golden test passes unchanged.
- [ ] [HUMAN] The PM checks which wire each universe stock uses (from its latest earnings release), adds only verified free, full-text feeds to config, and confirms a real run fetches them.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| feed config *(confirm path)* | Modify | Wire feeds, `filings` lane |
| `src/fetchers/wire.py` | Create | Ticker-tag matching |
| `src/processors/filings_merge.py` | Create | Wire and EDGAR merge |
| `tests/fixtures/wire/` | Create | Synthetic feeds |
| `tests/test_wire_feeds.py` | Create | The criteria above |

### Key Decisions

- Exchange tags are precise. The existing alias-in-text matching is noisy, and this lane must not inherit that.

### Notes

GlobeNewswire publishes public RSS feeds, including site-wide, public-company, subject and industry feeds. PR Newswire publishes RSS and full-text feeds. The PM verifies the current URLs.

## Feature: filing-facts

**Branch:** `feature/filing-facts`
**Depends on:** edgar-fetcher
**Status:** Not Started
**Requires:** both

### Goal

Extract earnings and guidance numbers from each earnings release, verify them against the text, and compute a bounded direction in code.

### Acceptance Criteria

- [ ] For each stored `2.02` event (or wire earnings release), one local-model call returns JSON matching a strict schema:
  - diluted EPS, current and prior-year period
  - revenue, current and prior-year period
  - the guidance range for the next period or year, if given (low, high, metric, period)
  - each value as written plus its scale ("in millions")

  Invalid JSON is retried once, then stored as failed.
- [ ] Every extracted number is checked against the source text after normalization: commas, currency symbols, parentheses for negatives, and "billion"/"million". An unverified value is set to null and counted. Fixtures:
  - a release whose numbers are all present verifies fully
  - an altered number is nulled
- [ ] Code computes the components:
  - `eps_yoy` and `rev_yoy`, as % change between the current and prior-year values from the same release, so the scale cancels
  - `rev_accel`: this quarter's `rev_yoy` minus the same company's previous quarter's, from stored history
  - `guidance_change`: +1, 0 or -1, comparing this release's guidance midpoint with the same metric and period's midpoint from the company's previous release in the table, beyond `GUIDANCE_TOLERANCE`
- [ ] `direction` = `clip(Σ wᵢ·tanh(componentᵢ / kᵢ), -1, 1)`, over verified components only, with weights renormalized. Weights and `k` come from config. `direction_n` is the number of components used; with none, direction is null.
- [ ] A test with a stub model shows:
  - the prompt contains no instruction to judge good or bad news
  - the model's output can't set `direction` directly (an injected `"direction": 1` in the output is ignored)
- [ ] The call sends `num_ctx` and the deterministic temperature and seed from config, and trims long releases to the financial tables and highlights *(implementation decides how)* within the context budget.
- [ ] [HUMAN] The PM runs it on hub on the last four earnings releases of three universe stocks and posts each extracted value next to the release text in a PR comment.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/filing_facts.py` | Create | Extraction call, verification |
| `src/processors/filing_direction.py` | Create | Components and direction |
| `src/summarizers/schema.py` *(confirm)* | Modify | Extraction schema |
| `src/config.py` | Modify | Weights, `k`, tolerance |
| `tests/test_filing_facts.py` | Create | The criteria above |

### Key Decisions

- **Year-over-year growth is a stand-in for "surprise".** Analyst consensus isn't freely available. Acceleration and guidance changes are kept as separate components so the event study can test each one.
- **Direction is code, not model judgement**, for injection resistance and to limit the model recalling outcomes.

## Feature: filings-signals

**Branch:** `feature/filings-signals`
**Depends on:** filing-facts, wire-feeds, atrade `filings-reader` merged
**Status:** Not Started
**Requires:** ai

### Goal

Emit `signals-filings.json` each run, next to the existing signals file.

### Acceptance Criteria

- [ ] Each run writes `signals-filings.json` atomically (temp file and rename), with one record per event whose `knowable_at` falls within `FILINGS_EMIT_DAYS` (default 30).
- [ ] The output validates against the committed schema. A test walks every string, and only enum values, tickers, timestamps and pattern-checked accession numbers are allowed.
- [ ] A golden file for the filings output is added (`tests/fixtures/brief_fixture/signals-filings.golden.json`). The existing signals golden test passes unchanged.
- [ ] The run time never appears as `knowable_at`: a fixture whose fetch happens two days after acceptance keeps the acceptance time.
- [ ] With the lane disabled, no file is written and the existing output is byte-identical.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/publishers/signals_filings.py` | Create | Writer |
| `src/cli.py` *(confirm)* | Modify | Call it in the daily run |
| `tests/fixtures/brief_fixture/signals-filings.golden.json` | Create | Golden output |
| `tests/test_filings_signals.py` | Create | The criteria above |

## Feature: filings-backfill

**Branch:** `feature/filings-backfill`
**Depends on:** filing-facts
**Status:** Not Started
**Requires:** both

### Goal

Rebuild the filings history from EDGAR's archive so the hypothesis can be tested on years of events, instead of waiting a year for new ones.

### Acceptance Criteria

- [ ] `morning-brief filings backfill --since YYYY-MM-DD` does the following for every universe stock:
  - fetches historical earnings 8-Ks and their exhibits, including the API's older paged files
  - stores them in the same `filings` table
  - extracts facts with the same code as the daily run
  - writes `data/filings-history.jsonl` in the contract format
- [ ] Each record carries `in_model_window` = `knowable_at` earlier than `FILINGS_MODEL_CUTOFF` (runtime config, the configured model's training-data cutoff).
- [ ] Resumable: a second run skips stored accession numbers. A stub clock past `BACKFILL_WINDOW_UTC` stops the run cleanly at the next item, with a summary line.
- [ ] Rate limiting and the User-Agent rule are the same as `edgar-fetcher`'s (shared client; a test shows it).
- [ ] [HUMAN] The PM sets `FILINGS_MODEL_CUTOFF` from the model card, runs the backfill on hub, and posts these counts in the PR:
  - events per stock
  - verification rate
  - `direction_n` distribution
  - in-window vs out-of-window counts
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/fetchers/edgar.py` | Modify | Historical paging |
| `src/processors/filings_backfill.py` | Create | Resumable, window-bound runner |
| `src/cli.py` *(confirm)* | Modify | `filings backfill` |
| `tests/test_filings_backfill.py` | Create | The criteria above |

### Notes

Only events after the model's cutoff are fully free of the model having seen what came next. Earlier events are reported, but kept separate.

## Feature: filing-events

**Branch:** `feature/filing-events`
**Depends on:** filings-signals
**Status:** Not Started
**Requires:** ai

### Goal

Emit non-earnings 8-K events (agreements, acquisitions, executive changes, standalone guidance updates) as typed events, so they can be tested separately.

### Acceptance Criteria

- [ ] Items map to events in code, with no model call:
  - `1.01` → `agreement`
  - `2.01` → `acquisition`
  - `5.02` → `exec_change`
- [ ] A `7.01` or `8.01` release, or an untagged-item wire release, becomes `guidance` only when `filing-facts` extracts a verified guidance range from it. `guidance_change` and `direction` are computed as for earnings.
- [ ] Non-guidance events have `direction` null. They appear in the output and the history file. A fixture covers each type.
- [ ] The filings golden file is regenerated in this PR with only the new event records added. A field-by-field test shows the earnings records are unchanged.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/processors/filing_events.py` | Create | Item-to-event mapping |
| `src/publishers/signals_filings.py` | Modify | Emit new types |
| `tests/test_filing_events.py` | Create | The criteria above |

### Key Decisions

- No direction for agreements, acquisitions or executive changes: their effect isn't well defined in code. The event study measures them by type.

---

## Companion features for atrade's DEVPLAN (not morning-brief's)

## Feature: filings-reader

**Branch:** `feature/filings-reader`
**Depends on:** none (merge before morning-brief `filings-signals`)
**Status:** Not Started
**Requires:** ai

### Goal

atrade loads and validates `signals-filings.json` and `filings-history.jsonl`, with the existing signal path untouched.

### Acceptance Criteria

- [ ] `docs/SIGNAL-SCHEMA.md` matches morning-brief's copy (same version, same text).
- [ ] A valid fixture file loads. Records with an unknown enum, `direction` outside [-1, 1], a non-UTC or zone-less timestamp, or a string field outside the schema are rejected and logged, and the rest of the file still loads.
- [ ] A missing file is treated as "no filings signals" with one INFO line.
- [ ] Filings records are stored with source `morning-brief-filings`. A test shows the existing news shadow arm and the paper account read none of them.
- [ ] The existing point-in-time test passes, and a new one shows no filings record is usable before its `knowable_at`.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `docs/SIGNAL-SCHEMA.md` | Modify | Same text as morning-brief |
| `src/atrade/signals/filings.py` *(confirm)* | Create | Loader, validator |
| `tests/test_filings_reader.py` | Create | The criteria above |

## Feature: event-study

**Branch:** `feature/event-study`
**Depends on:** filings-reader
**Status:** Not Started
**Requires:** both

### Goal

Test the pre-registered hypothesis ("fundamentals news improves on the baseline") on filings events, separately for each component and event type.

### Acceptance Criteria

- [ ] **Before any code runs on real data**, `docs/BRIEF.md` records the success test as written in Notes. A test fails if the document lacks it.
- [ ] `atrade event-study --history PATH` computes, for each event, the abnormal return over 1, 5 and 20 trading days:
  - **abnormal return:** the stock's return minus `BENCHMARK_SYMBOL`'s (config)
  - **entry:** the open of the first session whose atrade run time is after `knowable_at`
- [ ] It reports mean, standard error, t-stat and count for:
  - positive vs negative `direction`
  - each component's sign
  - each non-earnings event type

  Each split is shown for out-of-model-window events (decisive) and in-window events (shown, not decisive). Output is a table plus `data/event-study-<date>.json`, and the command exits 0.
- [ ] Prices come from the existing market-data client. Synthetic fixtures with a known planted effect recover it (t > 2), and a no-effect fixture does not.
- [ ] An event whose entry would come before `knowable_at` is impossible: a test feeds one with `knowable_at` after the open and checks the entry moves to the next session.
- [ ] [HUMAN] The PM runs it on the backfill on hub and posts the table in a PR comment, plainly worded for the operator.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `docs/BRIEF.md` | Modify | Pre-registered test |
| `src/atrade/research/event_study.py` | Create | The study |
| `src/atrade/cli.py` *(confirm)* | Modify | `event-study` command |
| `tests/test_event_study.py` | Create | The criteria above |

### Notes

**Pre-registered success test (default):** on out-of-model-window earnings events, the mean 5-day abnormal return for positive-direction events minus that of negative-direction events is above zero with t ≥ 2. Anything less is reported as "not shown", not as "no edge". Expect "inconclusive" while out-of-window events are few: the universe's stocks produce only a few dozen earnings events a year.

## Feature: filings-arm

**Branch:** `feature/filings-arm`
**Depends on:** filings-reader
**Status:** Not Started
**Requires:** ai

### Goal

A second shadow portfolio tilted only by filings-lane direction, tracked beside the paper account and the news shadow arm.

### Acceptance Criteria

- [ ] Weight for a ticker with a fresh event (`knowable_at` within `FILINGS_FRESH_DAYS` trading days, default 20, newest per ticker) = `1 + FILINGS_TILT·direction` times equal weight (`FILINGS_TILT` default 0.5). Tickers with no fresh event, null direction, or ETFs stay at 1.0×. Weights are normalised.
- [ ] It uses the same rebalance band and cost model as the news shadow arm, read from the same config, and never places orders (a test with a stub broker records none).
- [ ] It starts from the paper account's equity on its first day. The weekly scorecard gains a third column with equity, trades, turnover and cost.
- [ ] A test shows the news shadow arm's and the paper account's outputs are unchanged by its presence.
- [ ] All tests pass
- [ ] Lint clean

### Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/atrade/strategy/shadow_filings.py` | Create | The arm |
| `src/atrade/strategy/shadow.py` | Modify | Share band/cost code only if needed |
| `src/atrade/report/scorecard.py` *(confirm)* | Modify | Third column |
| `tests/test_filings_arm.py` | Create | The criteria above |

### Key Decisions

- The tilt uses the direction's value, not a rank. Events are rare, so ranking a few tickers would churn just like the news arm does.
