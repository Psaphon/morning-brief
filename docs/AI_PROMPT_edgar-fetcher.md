# AI Development Prompt — edgar-fetcher

**Branch:** `feature/edgar-fetcher`
**Base:** `develop`

Read `CLAUDE.md` for project context, `docs/DEVPLAN.md` (`## Feature: edgar-fetcher`) for
the acceptance criteria, and `docs/SIGNAL-SCHEMA.md` §12 for the filings-lane contract.
Do NOT push — the host workflow handles push and PR.

This feature fetches and **stores** filings. It does not extract numbers, compute
direction or write `signals-filings.json` (later features do that).

## Facts the PM verified against live EDGAR on 2026-11-22 (build on these)

- **Endpoint:** `https://data.sec.gov/submissions/CIK{cik10}.json`. Use
  `filings.recent`, a dict of parallel arrays: `accessionNumber`, `filingDate`,
  `acceptanceDateTime`, `form`, `items` (comma-separated string, e.g. `"2.02,7.01"`),
  `primaryDocument`, and others to ignore. `filings.files` lists older paged files; the
  daily fetch ignores it (the backfill feature uses it).
- **Acceptance time zone:** `acceptanceDateTime` (e.g. `2026-10-30T10:32:50.000Z`) is
  **true UTC**. Checked on two XOM earnings 8-Ks against their EDGAR index pages, which
  show **US Eastern** time: `2026-10-30 06:32:50` (EDT, UTC-4) = API `10:32:50Z`, and
  `2026-01-30 06:31:24` (EST, UTC-5) = API `11:31:24Z`. Put this in a code comment and
  base the DST test on exactly these two pairs.
- **Exhibits:** file names are arbitrary (e.g. `livef8k3q26991.htm`), so find them by
  type. Fetch the filing index page
  `https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dashes}/{accession}-index.htm`
  and parse its document table (columns: Seq, Description, Document, Type, Size) with
  `html.parser`. Keep rows whose Type matches `EX-99.*`. `index.json` in the same folder
  lists files but has no type, so don't use it for this.
- Primary document: `.../{acc_no_dashes}/{primaryDocument}`.

## What to build

### `src/fetchers/edgar.py`

- An async EDGAR client built on `src/http.py`'s `make_client` (shared User-Agent with
  the contact). **Unlike other fetchers, EDGAR must not run contact-less:** if
  `CONTACT_EMAIL` is unset, `fetch` logs one WARNING, sends no request, stores nothing
  and returns normally (exit 0 from the CLI).
- A rate limiter: at most `SEC_MAX_RPS` requests in any rolling one-second window
  (default 5, values above 10 are clamped to 10 with a WARNING; 10 is the SEC's
  published ceiling and lives in config, not prose).
- Retries: 429 and 5xx get exponential backoff with jitter up to `SEC_MAX_RETRIES`
  (default 3). 403 also backs off, and if 403s persist past the retries, stop the
  **whole** fetch for this run with one ERROR (the SEC may be blocking us); do not keep
  hammering other CIKs.
- Never log the contact address or the full User-Agent.
- For each stock in `config/ticker_map.toml` (use `cik`; ignore `former_ciks` here):
  read `filings.recent`, keep `form == "8-K"` only (not `8-K/A`: original values only),
  with `acceptanceDateTime` within `FILINGS_LOOKBACK_DAYS` (default 7) and at least one
  item in `FILINGS_ITEMS` (default `2.02,7.01,8.01,1.01,2.01,5.02`). Skip accession
  numbers already stored *before* fetching documents.
- Time rules (contract §12.5): parse `acceptanceDateTime` as UTC; reject and log a value
  with no zone, or one later than the fetch time. Never substitute fetch time.

### Storage: a `filings` table in `src/db.py`

Follow `db.py`'s existing `CREATE TABLE IF NOT EXISTS` + migration style. Columns:
`accession` (UNIQUE), `cik`, `ticker`, `form`, `items` (the stored, filtered item list),
`accepted_at` (UTC ISO with `Z`), `origin` (`'edgar'`), `fetched_at`, `primary_doc_url`,
`primary_html` and `exhibits` (compressed, `zlib` BLOBs), `primary_text`, and
`exhibit_text` (plain text via the existing `html_to_text` in `src/fetchers/rss.py`;
each exhibit's text prefixed by its type, e.g. `[EX-99.1]`). This table is **not**
touched by the 2-day article retention; add a test that the prune leaves it alone.

### Config (`src/config.py`)

`FILINGS_ENABLED` (default **false**: the PM turns it on after a live check),
`SEC_MAX_RPS`, `SEC_MAX_RETRIES`, `FILINGS_ITEMS`, `FILINGS_LOOKBACK_DAYS`. Document each
in `env.example`.

### CLI and daily run

- `morning-brief filings fetch` (a `filings` Click group with a `fetch` command) prints
  one summary line: CIKs polled, filings new / skipped / rejected.
- The daily `run_pipeline` calls the fetch when `FILINGS_ENABLED` is true, inside a
  try/except that logs and continues: a fetch failure never fails the brief. Record its
  duration in the existing run timing so it counts toward `RUN_DEADLINE_MINUTES`.

### Tests (`tests/test_edgar_fetcher.py`, fixtures in `tests/fixtures/edgar/`)

Synthetic submissions JSON and index pages shaped like the real ones above (no real
company text). Serve them from a local stub HTTP server (see how
`tests/test_signals_golden.py` fakes the HTTP boundary), not by mocking the client:
- the DST pairs above convert correctly; zone-less and future times are rejected;
- item filtering, `8-K/A` skipped, lookback window, idempotent re-run (second run
  stores 0 and requests no documents for known accessions);
- every request carries the User-Agent; no one-second window exceeds `SEC_MAX_RPS`;
  a 429 then 200 succeeds after backoff; persistent 403 stops the run with one ERROR;
- `CONTACT_EMAIL` unset → exit 0, one WARNING, the stub server saw zero requests;
- the contact address never appears in captured logs;
- exhibits are found by Type (an `EX-99.1` with an arbitrary file name is fetched, an
  `EX-101.SCH` is not);
- a fetch exception inside `run_pipeline` is logged and the brief still completes.

Use short sleeps via an injectable clock/sleep so the tests stay fast. Do not put fake
executables in `/tmp` (hub's `/tmp` is noexec); use `tmp_path` for files only.

## Rules

- `tests/test_signals_golden.py` must pass **unchanged**; don't regenerate golden files.
- Run `ruff check . && ruff format --check .` and `pytest tests/ -q` before EVERY commit.
- Do NOT push. Do NOT touch the scorer, signals output, feeds or `docs/SIGNAL-SCHEMA.md`.
- No real names, addresses or contact emails in code, fixtures or logs.
- Commit message:

```
feat: EDGAR 8-K fetcher for the filings lane

Polls the submissions API per universe CIK, stores new 8-Ks (filtered by
FILINGS_ITEMS) with UTC acceptance times and their EX-99 exhibits in a new
filings table. Rate-limited, backs off on 429/403, never runs without a
contact User-Agent, and is off by default (FILINGS_ENABLED).
```
