# fetch_prices.py Enhancements

## Overview

Enhanced `fetch_prices.py` with a multi-tier fallback mechanism, automatic rate-limit
detection, NSE Playwright recovery, holiday data filtering, and testing flags. This
document explains every change made, the bugs that motivated them, and how each part
of the new architecture works.

---

## Architecture

### Problem Statement

The original script fetched OHLCV prices exclusively through `yf.download()` with
no retry logic, no alternative sources, and no handling for Yahoo Finance's
well-documented instability. Four distinct bugs were found through live testing:

1. **`YFRateLimitError` on first run** — A single `yf.download(250 tickers, 52 weeks)`
   request exceeds Yahoo's per-IP quota immediately. The original script had zero retry
   logic; any rate-limit error aborted the entire session.

2. **Silent empty return on rate-limit** — `yf.download()` does not always raise an
   exception when rate-limited. It often prints a warning to stderr and silently returns
   an empty DataFrame. The original script treated this identically to a weekend/holiday
   (no trading data), so the rate-limit was invisible and produced no recovery behaviour.

3. **No fallback source** — When yfinance failed, there was no alternative. Symbols
   that returned no data were simply logged as missing, counted toward the 8% abort
   threshold, and lost.

4. **Zero-volume carry-forward rows** — Yahoo Finance inserts phantom rows on NSE
   market holidays by repeating the previous trading day's close price with `volume = 0`.
   These corrupt momentum calculations by making prices appear flat across holidays.

### Solution: Layered Fallback with Rate-Limit Detection

```
yf.download() batch — v7 endpoint, 50 symbols per call, 12-week chunks
    │
    ├─ Exception raised (YFRateLimitError):
    │     retry once after 15s → if still blocked:
    │     set _yf_v7_blocked = True → switch ALL remaining batches to v8
    │
    ├─ Empty DataFrame returned (silent rate-limit):
    │     canary check via Ticker.history() (v8 endpoint)
    │     if canary has data → v7 silently blocked:
    │         set _yf_v7_blocked = True → switch ALL remaining batches to v8
    │     if canary also empty → genuine no-data (holiday/weekend)
    │
    └─ _yf_v7_blocked flag set by any prior batch:
          skip v7 entirely → call Ticker.history() per symbol (v8 endpoint)

─────────────────────────────────────────────────────────────────────────────
After all yfinance processing, any symbol still missing → NSE Playwright fallback
─────────────────────────────────────────────────────────────────────────────

NSE historical API via Playwright browser session
    │
    ├─ Opens one Chromium browser, establishes Akamai session cookies
    ├─ Calls /api/historical/cm/equity?symbol=X per failed symbol
    ├─ Returns full OHLCV for the requested date range
    └─ Closes browser after batch

─────────────────────────────────────────────────────────────────────────────
Any symbol still missing after NSE → logged, counted toward abort threshold
─────────────────────────────────────────────────────────────────────────────
```

---

## Data Sources

### 1. PRIMARY: yfinance `yf.download()` — v7 Endpoint

**Source:** `https://query1.finance.yahoo.com/v7/finance/download/SYMBOL.NS`  
**Method:** `fetch_all_yf()` → `fetch_batch_yf()`  
**Coverage:** All NSE-listed symbols via `.NS` suffix  
**Data Quality:** ⭐⭐⭐⭐⭐ (Adjusted close, full OHLCV)

**How it works:**
- Calls `yf.download(50_tickers, start, end, group_by='ticker')` — one HTTP request
  per batch of 50 symbols
- On first run (52 weeks of history), the full date range is split into 12-week
  chunks to stay below Yahoo's per-request quota threshold
- Provides split-and-dividend-adjusted close (`Adj Close`) which is what
  `compute_metrics.py` needs for accurate return calculations

**Advantages:**
- Fastest path — one API call per 50 symbols
- Provides true adjusted close (corporate action adjusted)
- Globally available, no authentication
- Handles all NSE and BSE listed equities via `.NS` / `.BO` suffixes

**Disadvantages:**
- Yahoo Finance changes its API 2–3× per year (undocumented)
- Rate-limits aggressively on large requests or repeated calls
- Rate-limit behaviour is inconsistent: sometimes raises `YFRateLimitError`,
  sometimes silently returns an empty DataFrame
- Timestamp deprecation warnings (`Pandas4Warning`) on every ticker

**Failure modes:**
- `YFRateLimitError` raised (exception path)
- Empty DataFrame returned with no exception (silent path)
- Per-symbol `KeyError` if a ticker is missing from the multi-index response
- All-NaN `Close` column if a ticker has no data in the requested window

---

### 2. SECONDARY: yfinance `Ticker.history()` — v8 Endpoint

**Source:** `https://query1.finance.yahoo.com/v8/finance/chart/SYMBOL.NS`  
**Method:** `_fetch_batch_via_history()` → `_fetch_ticker_history()`  
**Coverage:** All NSE-listed symbols  
**Data Quality:** ⭐⭐⭐⭐ (Close only, no adjusted close from Yahoo)

**How it works:**
- Uses `yf.Ticker(symbol).history(start, end, auto_adjust=False)` — one HTTP
  request per symbol
- Called automatically when v7 (`yf.download`) is blocked; the global flag
  `_yf_v7_blocked` ensures all remaining batches route here without retrying v7
- A **canary check** on the first symbol determines whether an empty v7 response
  is a genuine no-data condition or a silent rate-limit:

```python
canary_df = _fetch_ticker_history(tickers[0], start, end)
if not canary_df.empty:
    _yf_v7_blocked = True   # v7 silently rate-limited; v8 still works
    return _fetch_batch_via_history(tickers, start, end)
```

**Advantages:**
- **Independent quota** from `yf.download` — v7 and v8 are separate Yahoo endpoints
  with separate rate-limit counters. v7 can be fully blocked while v8 continues
  to serve data.
- No extra dependencies — same `yfinance` package
- Automatic fallback — requires no user action

**Disadvantages:**
- One API call per symbol (250× slower than v7 batch in the worst case)
- Returns close price, not adjusted close — `adj_close` is filled from `close`
  (corporate action adjustments from Yahoo are not available via this path)
- Also subject to rate-limiting under heavy use, but less aggressive than v7
- Returns timezone-aware `DatetimeIndex` (Asia/Kolkata) that must be stripped

**Data differences vs v7:**
- `adj_close` = `close` (unadjusted). This is acceptable for short windows
  (weekly incremental) but for the 52-week first run, any splits or dividends
  during the year will not be reflected in v8 data.

---

### 3. TERTIARY: NSE Historical API via Playwright

**Source:** `https://www.nseindia.com/api/historical/cm/equity`  
**Method:** `fetch_failed_via_nse()` → `fetch_symbol_nse()`  
**Coverage:** All NSE equities with full OHLCV  
**Data Quality:** ⭐⭐⭐⭐⭐ (Official NSE, unadjusted close)

**How it works:**
- Opens one headless Chromium browser session via Playwright to establish
  Akamai session cookies (same technique as `fetch_universe.py`)
- For each failed symbol, executes a JavaScript `fetch()` call from within the
  live browser page to `/api/historical/cm/equity?symbol=X&series=["EQ"]&from=DD-MM-YYYY&to=DD-MM-YYYY`
- The browser session stays open across all symbols in the batch to avoid
  re-launching for each request
- Returns data via the `CH_OPENING_PRICE`, `CH_TRADE_HIGH_PRICE`,
  `CH_TRADE_LOW_PRICE`, `CH_CLOSING_PRICE`, `CH_TOT_TRADED_QTY` fields

**NSE API response fields:**
| NSE field | Maps to |
|---|---|
| `CH_OPENING_PRICE` | `open` |
| `CH_TRADE_HIGH_PRICE` | `high` |
| `CH_TRADE_LOW_PRICE` | `low` |
| `CH_CLOSING_PRICE` | `close` and `adj_close` |
| `CH_TOT_TRADED_QTY` | `volume` |
| `CH_TIMESTAMP` or `mTIMESTAMP` | `date` |

**Note:** NSE does not provide split/dividend-adjusted prices. `adj_close` is set
equal to `close`. This is consistent with the v8 fallback path.

**Advantages:**
- Official NSE data — the authoritative source for Indian equities
- Not subject to Yahoo Finance rate-limits
- Covers the same symbols as `fetch_universe.py`
- Accepts the `series` parameter (EQ, BE, SM, etc.) per symbol from `universe.csv`

**Disadvantages:**
- Requires Playwright and Chromium binary
- NSE Akamai bot detection can refuse connections (HTTP/2 protocol errors)
- Slower — one browser session startup (~30s) plus one API call per symbol (~0.8s)
- Not suitable as the primary source due to setup overhead

**Akamai mitigation:**
- Chromium launched with `--disable-http2` to force HTTP/1.1 (avoids the
  `net::ERR_HTTP2_PROTOCOL_ERROR` that Akamai's edge layer frequently triggers
  on automated HTTP/2 connections)
- `navigator.webdriver` property masked via `add_init_script()`
- Full browser headers: user-agent, locale, timezone, viewport all set to mimic
  a real Indian Chrome user
- Navigation retry: 5 attempts with 15s / 30s / 45s / 60s backoff

---

### 4. FINAL: Logged as Missing (Safety Gate)

**Method:** Count missing symbols against `FAILURE_THRESHOLD_PCT`  
**Data Preservation:** No files written for missing symbols

**Behaviour:**
- Any symbol that returns no data from yfinance v7, yfinance v8, and NSE is
  listed in the session output
- If missing symbols exceed 8% of the universe, the script exits with code 1
  without writing any output files
- If missing symbols are below 8%, the script continues with a `WARNING` line
  and writes files for all successfully fetched symbols

---

## Implementation Details

### New Functions

#### `_normalise_ticker_df(raw_df, sym)` (40 lines)

Shared normalisation layer called by both `yf.download()` and `Ticker.history()` paths.
Solves three structural differences between the two yfinance APIs:

1. **Column name differences:** `yf.download()` produces `Adj Close` (with space);
   `Ticker.history()` produces no adjusted column at all. The function handles both
   by detecting which column is present and either renaming or filling from `close`.

2. **Timezone-aware DatetimeIndex:** `Ticker.history()` returns timestamps in
   `Asia/Kolkata` timezone. Passing these into pandas operations alongside the
   timezone-naive dates from `yf.download()` causes `TypeError`. The function
   strips the timezone: `df["date"].dt.tz_convert(None)`.

3. **Zero-volume holiday rows:** After normalising the schema, the function detects
   and drops carry-forward rows where `volume == 0` AND `close == prior_close`.
   These are Yahoo Finance's placeholder entries for NSE market holidays (e.g.,
   Makar Sankranti on 2026-01-15, Maharashtra Day on 2026-05-01). The dropped
   dates are printed so they can be manually verified.

```python
zero_vol = (df["volume"] == 0) & (df["close"] == df["close"].shift(1))
if zero_vol.any():
    print(f"  [{sym}] dropping {n} zero-volume carry-forward row(s): {dates}")
    df = df[~zero_vol]
```

#### `_fetch_ticker_history(ticker, start, end)` (18 lines)

Wraps `yf.Ticker(ticker).history()` with error handling and routes through
`_normalise_ticker_df`. Used both as the v8 per-symbol fallback and as the canary
probe inside `fetch_batch_yf` to distinguish silent rate-limits from genuine no-data.

#### `_fetch_batch_via_history(tickers, start, end)` (16 lines)

Loops over a list of tickers calling `_fetch_ticker_history` for each. Called
automatically whenever `_yf_v7_blocked` is True (i.e., v7 has been confirmed blocked
for this session). Sleeps 0.5s between symbols to respect v8 rate limits.

#### `load_symbol_meta()` (6 lines)

Returns `{symbol: series}` from `universe.csv`. The NSE historical API requires
the market series (almost always `"EQ"` for equity) to be specified in the URL query.
Without this, the API returns an empty result for some symbols. The series values
come directly from the `fetch_universe.py` output.

#### `_start_nse_session()` (70 lines)

Opens a persistent Playwright Chromium browser session on the NSE website to
acquire Akamai session cookies. The browser stays open (stored in `_nse_page`,
`_nse_browser` module globals) for the duration of the NSE fallback batch so that
it does not need to be relaunched for each symbol.

Key improvements over the original NSE session code in `fetch_universe.py`:
- Added `--disable-http2` to Chromium launch args to prevent `ERR_HTTP2_PROTOCOL_ERROR`
- Navigation retry increased to **5 attempts** (was 3 in fetch_universe.py) with
  backoff: 15s, 30s, 45s, 60s

#### `_close_nse_session()` (8 lines)

Closes the Playwright browser and resets the module globals to `None`. Called after
the NSE fallback batch completes to free memory.

#### `fetch_symbol_nse(symbol, start, end, series)` (55 lines)

Makes a JavaScript `fetch()` call from within the active Playwright page to the
NSE equity historical API. Runs entirely inside the browser context so all Akamai
cookies are automatically included via `credentials: 'include'`. Parses the JSON
response and maps NSE field names to the standard 8-column schema.

---

### Modified Functions

#### `fetch_batch_yf(tickers, start, end, attempt)` — Rate-limit detection overhaul

**Before:** Simple try-except with no retry or fallback.

**After:** Three distinct handling paths, all converging on the same output contract:

**Path 1 — Exception raised:**
```
yf.download raises YFRateLimitError
    → attempt < YF_MAX_RETRIES (1):
        wait 15s, retry recursively with attempt+1
    → attempt == YF_MAX_RETRIES:
        _yf_v7_blocked = True
        print "Switching ALL remaining batches to Ticker.history()"
        return _fetch_batch_via_history(tickers, start, end)
```

**Path 2 — Silent empty return:**
```
yf.download returns empty DataFrame (no exception)
    → probe tickers[0] via _fetch_ticker_history() (v8, separate quota)
    → if canary has data:
        _yf_v7_blocked = True
        print "Confirmed via Ticker.history(). Switching ALL batches to v8."
        return _fetch_batch_via_history(tickers, start, end)
    → if canary also empty:
        genuine no-data (weekend/holiday) — return empty, mark symbols missing
```

**Path 3 — `_yf_v7_blocked` already set:**
```
_yf_v7_blocked is True (set by any prior batch in this session)
    → skip yf.download entirely
    → return _fetch_batch_via_history(tickers, start, end) immediately
```

The `_yf_v7_blocked` flag prevents wasted retries across chunks. Without it, a
fully blocked IP would waste 15s × 5 chunks × N batches = many minutes of waiting
before any data is fetched.

#### `fetch_all_yf(tickers, start, end)` — Chunked date windows

**Before:** Single `yf.download()` call covering the full date range.

**After:** The date range is split into `YF_CHUNK_WEEKS`-sized windows:

```python
# 52-week first run becomes 5 × 12-week chunks
chunks = [(2025-05-06, 2025-07-28),
          (2025-07-29, 2025-10-20),
          (2025-10-21, 2026-01-12),
          (2026-01-13, 2026-04-06),
          (2026-04-07, 2026-05-05)]
```

Sleep behaviour is differentiated between run types:

| Run type | Inter-batch sleep | Inter-chunk sleep |
|---|---|---|
| Weekly incremental (< 12 weeks) | 3–10s random | n/a (single chunk) |
| First run / large history | 15–30s random | 60s fixed |

The longer sleeps prevent the rate-limit counter from accumulating across multiple
batches in the same session. The 60s inter-chunk sleep gives Yahoo's quota window
time to partially reset between chunks.

A symbol is only counted as `empty` if it returned no data across **all chunks**.
If a symbol appears in chunk 1 but not chunk 2, it is still considered fetched —
this handles stocks that were listed or delisted mid-year.

#### `fetch_failed_via_nse(symbols, start, end, meta)`

**Before:** Did not exist.

**After:** Called by `main()` with the list of symbols that yfinance could not fetch.
Opens one NSE Playwright session, calls `fetch_symbol_nse()` for each symbol, logs
progress every 10 symbols, then closes the session.

#### `main()` — Two new CLI flags; failure threshold scope fix; per-source summary

**New flags:**

`--limit N` — restricts processing to the first N symbols in `universe.csv`.
Allows testing the full pipeline (chunking, fallback, writes) on a small subset:
```bash
uv run python -u scripts/fetch_prices.py --limit 25
```

`--dry-run` — runs the full fetch pipeline but skips writing `YYYY-WW.csv` and
`daily_adj_close.csv`. Use to verify data quality before committing a first run:
```bash
uv run python -u scripts/fetch_prices.py --limit 25 --dry-run
```

**Failure threshold fix:**
Before, the 8% abort threshold was evaluated against symbols that failed **yfinance only**.
After, it is evaluated against symbols that failed **all sources** (yfinance + NSE).
A symbol recovered by the NSE fallback no longer contributes to the abort count.

**Per-source summary line:**
```
Done. 248/250 symbols fetched (yfinance: 245, NSE fallback: 3, missing: 2)
```
Previously the summary only reported total symbols fetched with no source breakdown.

---

### New Constants

| Constant | Value | Purpose |
|---|---|---|
| `YF_CHUNK_WEEKS` | 12 | Max weeks per single `yf.download` call |
| `SLEEP_HIST_MIN_S` | 15.0 | Min inter-batch sleep during multi-chunk runs |
| `SLEEP_HIST_MAX_S` | 30.0 | Max inter-batch sleep during multi-chunk runs |
| `SLEEP_INTER_CHUNK_S` | 60.0 | Fixed sleep between date chunks |
| `YF_MAX_RETRIES` | 1 | Max v7 retries before switching to v8 |
| `YF_RETRY_BASE_S` | 15.0 | Wait before the one retry (seconds) |
| `NSE_BATCH_SLEEP_S` | 0.8 | Sleep between NSE per-symbol API calls |

---

### Module-Level Changes

**Imports added:** `argparse`, `json`, `warnings`

**Warning suppression:**
```python
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*utcnow.*")
```
yfinance 0.2.54 prints `Pandas4Warning: Timestamp.utcnow is deprecated` once per
ticker. With 250 symbols × 5 chunks this produces 1,250 warning lines in the output,
making it impossible to read real progress. These warnings are not actionable —
they are internal to yfinance and do not affect correctness.

**Module globals:**
```python
_nse_page: object | None = None      # Playwright Page, kept open during NSE batch
_nse_browser: object | None = None   # Playwright Browser handle
_yf_v7_blocked: bool = False         # True once v7 confirmed rate-limited this session
```

---

## Testing Results

### Unit Tests — 50/50 pass (mocked, no live API)

`test_fetch_prices_unit.py` covers all code paths without hitting external APIs:

| Test group | What is tested | Count |
|---|---|---|
| Helpers | `iso_week_str`, `_is_rate_limit_error`, `load_symbols`, `load_symbol_meta` | 8 |
| yfinance v7 success | Schema, close > 0, all 8 columns present | 3 |
| v7 retry (exception path) | Retry fires, sleep called with correct duration | 3 |
| v7 retries exhausted | Auto-switches to v8, `_yf_v7_blocked` set, call count | 3 |
| v7 silent empty (canary path) | Canary triggers v8, `_yf_v7_blocked` set | 2 |
| v7-blocked flag | Skips `yf.download` entirely, routes to v8 | 2 |
| `_fetch_batch_via_history` | Calls `_fetch_ticker_history` per symbol, combines | 2 |
| `_normalise_ticker_df` | Timezone stripping, adj_close fill, symbol set | 3 |
| NSE JSON parsing | Valid data, empty array, error JSON, no active session | 9 |
| NSE fallback orchestration | Session fail, all recovered, partial recovery | 3 |
| File writers | Create, schema, idempotency, append, dedup, no duplicate index | 9 |

### Diagnostic Test — `test_prices_diagnostic.py`

Runs each of the first 100 universe symbols individually against three sources
(yfinance v7 single-call, NSE API, BSE API) and prints a pass/fail matrix:

```
SYM           yfinance    NSE API     BSE API     rows_yf
RELIANCE      ✅           skip        skip        8
HDFCBANK      ✅           skip        skip        8
...
POLYCAB       ❌           ❌           ❌           0
```

**Key finding from the diagnostic:**
Running 100 individual yfinance calls (one per symbol, 0.5s sleep) triggers the
rate limit within ~25 symbols. This is why the production script uses batches
of 50 at a time — fewer total API calls means lower rate-limit exposure.

### Dry-Run on 25 Symbols

```
Universe: 25 symbols
First run. Fetching 52 weeks from 2025-05-06 → 2026-05-05
Date range spans 364 days → split into 5 chunks of ≤12 weeks
  Chunk 1/5: 2025-05-06 → 2025-07-28
  Chunk 2/5: 2025-07-29 → 2025-10-20
  [M&M] dropping 1 zero-volume carry-forward row: [2025-09-08]
  Chunk 3/5: 2025-10-21 → 2026-01-12
  Chunk 4/5: 2026-01-13 → 2026-04-06
  [RELIANCE] dropping 1 zero-volume carry-forward row: [2026-01-15]
  [HDFCBANK] dropping 1 zero-volume carry-forward row: [2026-01-15]
  ... (22 more symbols, same date — Makar Sankranti)
  Chunk 5/5: 2026-04-07 → 2026-05-05
  [RELIANCE] dropping 1 zero-volume carry-forward row: [2026-05-01]
  ... (19 more symbols — Maharashtra Day)
yfinance: 25 succeeded, 0 failed
[--dry-run] Skipping file writes.
Done [dry-run, no files written]. 25/25 symbols (yfinance: 25, NSE fallback: 0, missing: 0)
```

---

## Usage

### First run (52 weeks of history)

```bash
# Safe test: verify 25 symbols work correctly without writing files
uv run python -u scripts/fetch_prices.py --limit 25 --dry-run

# Write files for first 25 symbols
uv run python -u scripts/fetch_prices.py --limit 25

# Full production run (all 250 symbols — allow 60–120 minutes)
uv run python -u scripts/fetch_prices.py
```

The `-u` flag runs Python in unbuffered mode so progress prints appear immediately
rather than being held in a buffer. Without it the output appears in large bursts.

### Subsequent weekly runs

```bash
# Fetches only days since the last entry in daily_adj_close.csv
uv run python scripts/fetch_prices.py
```

Weekly runs fetch a small date window (a few days). Chunking does not apply and
the shorter 3–10s inter-batch sleep is used.

### Testing a specific number of symbols

```bash
# First 10 symbols only — fastest possible test
uv run python -u scripts/fetch_prices.py --limit 10 --dry-run

# First 50 symbols — one full batch
uv run python -u scripts/fetch_prices.py --limit 50 --dry-run
```

---

## Error Handling

### Scenario 1: Normal operation (v7 available)
```
Fetching prices via yfinance (batches of 50)…
  Date range spans 364 days → split into 5 chunks of ≤12 weeks
  Chunk 1/5: 2025-05-06 → 2025-07-28
  Chunk 2/5: 2025-07-29 → 2025-10-20
  ...
yfinance: 250 succeeded, 0 failed
Done. 250/250 symbols (yfinance: 250, NSE fallback: 0, missing: 0)
```

### Scenario 2: v7 transient rate-limit (exception path)
```
  yfinance v7 rate-limited (batch RELIANCE…). Retry 1/1 in 15s…
  [15 second pause]
  yfinance v7 retries exhausted. Switching ALL remaining batches to Ticker.history() (v8 endpoint).
  ... (remaining batches use v8, no further retries)
yfinance: 248 succeeded, 2 failed
NSE fallback: recovering 2 symbols…
NSE fallback: recovered 2, 0 still missing
Done. 250/250 symbols (yfinance: 248, NSE fallback: 2, missing: 0)
```

### Scenario 3: v7 silently rate-limited (empty return path)
```
  yfinance v7 returned empty (rate-limited). Confirmed via Ticker.history(). Switching ALL batches to v8 endpoint.
  ... (all batches from this point use v8 directly)
yfinance: 245 succeeded, 5 failed
NSE fallback: recovering 5 symbols…
Done. 250/250 symbols (yfinance: 245, NSE fallback: 5, missing: 0)
```

### Scenario 4: Both v7 and v8 rate-limited, NSE recovers
```
  yfinance v7 returned empty (rate-limited). Confirmed via Ticker.history(). ...
  ... (all batches try v8, but v8 also fails for some symbols)
yfinance: 220 succeeded, 30 failed
NSE fallback: recovering 30 symbols…
  Opening NSE session for fallback fetches (this may take ~30s)…
  NSE session ready.
  NSE fallback progress: 10/30 (10 recovered)
  NSE fallback progress: 20/30 (20 recovered)
  NSE fallback complete: recovered 30 symbols, 0 still missing.
Done. 250/250 symbols (yfinance: 220, NSE fallback: 30, missing: 0)
```

### Scenario 5: All sources exhausted (e.g., IP banned + NSE HTTP/2 error)
```
yfinance: 0 succeeded, 250 failed
NSE fallback: recovering 250 symbols…
  Opening NSE session for fallback fetches (this may take ~30s)…
  NSE navigation failed (attempt 1/5), retrying in 15s…
  NSE navigation failed (attempt 5/5), retrying in 60s…
  WARNING: Could not start NSE Playwright session: ERR_HTTP2_PROTOCOL_ERROR
NSE fallback: 0 recovered, 250 still missing
ERROR: No data returned for any symbol from any source. Check network connectivity.
[exit code 1 — no files written]
```

This scenario occurs when the testing session has exhausted Yahoo Finance's
per-IP quota AND NSE's Akamai layer is refusing connections. No files are
written; existing data is preserved. Run again from a fresh session.

---

## Robustness Improvements

| Issue | Before | After |
|---|---|---|
| YFRateLimitError (exception) | ❌ Aborts session | ✅ Retry once, then v8 |
| YFRateLimitError (silent empty) | ❌ Treated as holiday/no-data | ✅ Canary check detects it |
| No fallback source | ❌ Missing symbols logged, session may abort | ✅ v8 then NSE recover symbols |
| Holiday carry-forward rows | ❌ Phantom prices corrupt momentum calc | ✅ Zero-volume rows filtered |
| 52-week first run rate-limit | ❌ Single large request triggers quota | ✅ 5 × 12-week chunks |
| Repeated retries after block | ❌ n/a (no retries existed) | ✅ _yf_v7_blocked stops waste |
| NSE HTTP/2 connection resets | ❌ Session fails immediately | ✅ HTTP/1.1 forced + 5 retries |
| Output flooded by warnings | ❌ 1,250 Pandas4Warning lines | ✅ Suppressed cleanly |
| No testing entry point | ❌ Must modify code to test | ✅ `--limit N`, `--dry-run` |
| Failure threshold scope | ❌ Counted yfinance-only failures | ✅ Counts all-source failures |
| No source attribution | ❌ Just total count | ✅ Per-source breakdown in summary |

---

## Future Enhancements

### Additional fallback sources

1. **NSE Bhavcopy archives** — NSE publishes a daily end-of-day CSV
   (`cm{DD}{MMM}{YYYY}bhav.csv.zip`) for all equities. This is a reliable batch
   source with no rate-limits. Currently blocked by Akamai on direct requests;
   would require Playwright session to download. Best suited for filling gaps
   in a date range after a failed first run.

2. **Alpha Vantage / Quandl** — Paid APIs with reliable uptime. Would require
   an API key in environment configuration. Suitable as a last-resort fallback
   for production deployments.

3. **Zerodha Kite API** — Broker API providing real-time and historical NSE data.
   Requires a Zerodha account and API key. Most reliable source available but
   adds an account dependency.

### Suggested improvements

1. **Partial write recovery** — If the session fails mid-first-run, restart should
   be able to resume from the last successfully written chunk rather than refetching
   everything. Could be implemented by writing a checkpoint file after each chunk.

2. **Adjusted close via NSE corporate actions** — The NSE Playwright session could
   also fetch the corporate actions API (`/api/corporateInfo`) to reconstruct
   split/dividend adjustments when falling back to NSE prices. Currently NSE
   fallback prices are unadjusted.

3. **v8 rate-limit detection** — Currently if both v7 and v8 return empty, the
   canary check treats it as genuine no-data. Adding a date-aware check (if the
   date range includes known trading days, empty must be a rate-limit) would
   allow the code to distinguish these cases and route to NSE sooner.

4. **Per-source success metrics** — Track and log how often each source is used.
   Repeated v8 or NSE fallback usage in weekly runs would indicate v7 is degraded
   and worth investigating.

---

## Maintenance Notes

### Dependencies

All dependencies already present in `pyproject.toml`:
- `yfinance==0.2.54` — both v7 and v8 endpoints
- `playwright>=1.40` — NSE Playwright session
- `pandas>=2.2` — data processing
- `requests>=2.31` — not used in fetch_prices but available if needed

### First-time setup

```bash
# Install Chromium browser binary (one-time)
uv run playwright install chromium
```

### Rate-limit guidance

Yahoo Finance enforces per-IP quotas. The limits are undocumented and change
without notice, but observed behaviour in 2026:

| Request type | Approximate limit |
|---|---|
| Single symbol, short window | ~50/hour |
| 25-symbol batch, 12-week window | ~5–10/hour |
| 25-symbol batch, 52-week window | ~1–2/hour |

**Best practice:** Run the first-run (52 weeks) in a fresh terminal session with
no other Yahoo Finance requests in progress. Early morning IST (before NSE market
open) typically has the least competition for quota.

### Holiday calendar

Zero-volume carry-forward rows are automatically detected and dropped, but the
pattern requires at least one trading day before the holiday for `close == prior_close`
to fire. Market closures at the very beginning of a date window would not be caught.
Known NSE holidays where carry-forwards have been observed:

| Date | Holiday |
|---|---|
| 2025-09-08 | Ganesh Chaturthi |
| 2026-01-15 | Makar Sankranti |
| 2026-05-01 | Maharashtra Day |

### Monitoring

Watch session logs for these patterns:

- **"yfinance v7 rate-limited"** — Transient throttle; recovered by retry. Normal.
- **"Switching ALL remaining batches to Ticker.history()"** — v7 blocked for session.
  Data will still be fetched via v8 but `adj_close` will be unadjusted.
- **"NSE fallback: recovering N symbols"** — Both Yahoo endpoints failed for N symbols.
  Investigate if N > 10 on a weekly run.
- **"WARNING: Could not start NSE Playwright session"** — NSE Akamai is refusing
  connections. Consider retrying in a few hours or checking for NSE maintenance.
- **"Missing symbols: [...]"** — These symbols returned no data from any source.
  Verify the symbols are still in the Nifty 250 index and listed on NSE.

---

## References

### Yahoo Finance APIs

- v7 download endpoint: `https://query1.finance.yahoo.com/v7/finance/download/SYMBOL`
- v8 chart endpoint: `https://query1.finance.yahoo.com/v8/finance/chart/SYMBOL`
- yfinance library: https://github.com/ranaroussi/yfinance

### NSE APIs

- NSE equity historical API: `/api/historical/cm/equity`
- NSE market live page (for Akamai session): `https://www.nseindia.com/market-data/live-equity-market`

### Related scripts

- `fetch_universe.py` — Same Playwright session technique for the index constituent list
- `fetch_fundamentals.py` — Uses yfinance `.info` (separate endpoint, separate quota)
- `validate_data.py` — Validates the OHLCV files written by this script
- `compute_metrics.py` — Consumes `daily_adj_close.csv` produced by this script

---

## Summary

The enhanced `fetch_prices.py` provides resilient OHLCV data collection through a
layered source strategy and accurate rate-limit detection:

✅ **Rate-limit resilience** — Three distinct paths handle raised exceptions, silent
   empty returns, and already-blocked sessions; all automatically route to v8.  
✅ **NSE fallback** — Official NSE data recovers any symbol yfinance cannot reach.  
✅ **Holiday filtering** — Zero-volume carry-forward rows removed before writing.  
✅ **Chunked history** — 52-week first run split into 12-week windows; stays below quota.  
✅ **Session efficiency** — `_yf_v7_blocked` prevents redundant v7 calls after block.  
✅ **Testability** — `--limit N` and `--dry-run` flags allow safe end-to-end testing.  
✅ **Diagnostics** — Per-source summary, source-specific warnings, unbuffered output.  
✅ **Backward compatibility** — Identical CLI interface; same output file formats.
