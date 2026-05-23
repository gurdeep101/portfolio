# nifty_agent

Paper-trading Indian equity portfolio. INR 25,000 notional capital.
Benchmark: Nifty LargeMidcap 250 price index. Universe: Nifty 250 constituents.
Cadence: Weekly rebalance. **You are the agent. This file is your protocol.**

Every time `claude` is run in this directory, you execute the session loop below —
top to bottom, in order — without waiting for further prompting.

---

## Session Protocol

### START OF SESSION — run every step, in order

**Step 1 — Read current state**
Read `data/portfolio/portfolio.json`. Report:
- Today's date and ISO week
- Current NAV (INR)
- Number of holdings
- Cash position (INR and % of NAV)
- Inception date and sessions run so far

**Step 2 — Refresh universe (conditional)**
```
uv run python scripts/fetch/fetch_universe.py
```
The script skips automatically if `data/universe/universe.csv` is less than 90 days old.
Add `--force` to override the age check and re-download unconditionally.
If it runs and the symbol count changes by more than 5, flag this prominently in the session log.

**Step 3 — Fetch prices**
```
uv run python scripts/fetch/fetch_nsepy_price.py
```
This writes the weekly OHLCV snapshot and updates the daily adj_close history.
On first run, it pulls 52 weeks of history — expect 5–10 minutes.
Add `--force` to re-fetch the full target window and refresh overlapping rows from origin (useful after a data corruption or missed session).
If exit code is non-zero, STOP and report the error. Do not proceed.

**Step 4 — Fetch benchmark**
```
uv run python scripts/fetch/fetch_benchmark.py
```
Add `--force` to re-fetch and overwrite all dates (default: fills missing dates only).
If it fails, note it in the session log and skip benchmark comparison this session. Continue.

**Step 5 — Fetch fundamentals (SKIP — no longer needed)**
The strategy is pure momentum; P/E data is not used in rankings. Skip this step each session.

**Step 6 — Validate data (gate)**
```
uv run python scripts/metrics/validate_data.py
```
Read output carefully.
- If exit code is 1: STOP. Report the blocking errors to the user. Do not rebalance.
- Warnings: log them and proceed with caution.
- Any stock flagged with a >40% move: do NOT trade that stock. Flag it in the log.

**Step 7 — Compute metrics**
```
uv run python scripts/metrics/compute_metrics.py
```
Read the full output. This script also writes a row to `data/portfolio/performance.csv`.
Pay attention to:
- Performance summary (weekly return, inception return, CAGR vs benchmark)
- The full ranking table and which stocks are BUY_CANDIDATE / SELL_CANDIDATE / HELD
- Excluded stocks (insufficient price history) — note the count

**Step 8 — Reason and decide**
Apply the Investment Strategy below. Think through:
1. Which stocks in the top 15 eligible stocks are not currently held? → BUY candidates
2. Which held stocks have dropped below rank 30, or are in Death Cross (MA=BELOW)? → SELL candidates
3. Which held stocks exceed 20% weight? → TRIM
4. Does the rebalance make sense? Is any trade driven by a data anomaly?
5. Do not trade any stock flagged with a large price move in Step 6.

**Step 9 — Write decisions file**
Write `data/decisions/YYYY-MM-DD.json` using today's date. Use this format exactly:

```json
{
  "session_date": "YYYY-MM-DD",
  "trades": [
    {"action": "BUY", "symbol": "SYMBOL", "target_weight": 0.08, "reason": "one sentence"},
    {"action": "SELL", "symbol": "SYMBOL", "quantity": "ALL", "reason": "one sentence"}
  ],
  "notes": "Any observations about market conditions or data quality"
}
```

If no trades are needed, write an empty trades array with a notes explaining why.

**Step 10 — Execute rebalance**
```
uv run python scripts/strategy/update_portfolio.py --decisions data/decisions/YYYY-MM-DD.json
```
Read the printed summary. Verify NAV is approximately preserved (minus transaction costs).

**Step 11 — Write session log**
Write `logs/session_YYYY-MM-DD.md` following the Log Format section below.

**Step 12 — Commit**
```
git add data/ logs/
git commit -m "session YYYY-MM-DD: NAV INR X,XXX (+X.X% wk, +X.X% incep) vs bm +X.X%"
```
Replace X values with the actual numbers from the performance summary.

### END OF SESSION checklist
- [ ] portfolio.json updated
- [ ] session log written with all required sections
- [ ] decisions JSON written
- [ ] git commit made with performance numbers in message
- [ ] NAV reported before and after rebalance

---

## Portfolio Rules — HARD CONSTRAINTS

These are non-negotiable. Enforce them even if the strategy suggests otherwise.

- **Capital**: INR 25,000 notional. No leverage.
- **Min positions**: 10 stocks as a default target. Override allowed only if the strategy
  signals more sells than there are eligible stocks to replace them — portfolio may drop
  below 10 or go fully to cash when ranking/eligibility dictates it.
- **Max positions**: 20 stocks.
- **Max single position**: 20% of NAV. Trim any holding that exceeds this.
- **Min position size**: INR 500. Do not buy if the trade value is below this.
- **No short selling. No derivatives.**
- **Universe only**: stocks must be in current `data/universe/universe.csv` at time of trade.
- **Large moves**: if a stock moved >40% in the past week (flagged by validate_data.py),
  do NOT trade it. Flag it in the session log for manual review.
- **Transaction cost**: 0.1% per trade side. Already deducted by strategy/update_portfolio.py.
- **Rebalance once per session only.**

---

## Investment Strategy

Rank all eligible Nifty 250 stocks by composite score each week. Total weight = 100%.

| Factor | Weight | Formula |
|---|---|---|
| Long-term momentum | 15% | (52-week return) − (4-week return), normalised |
| Near-term momentum | 30% | (50-DMA − 200-DMA) / 200-DMA, normalised |
| Golden Cross speed | 30% | 1 / days(last Death Cross → most recent Golden Cross), normalised |
| Golden Cross peak | 25% | 1 / days(last price-200-DMA touch → most recent Golden Cross), normalised |

**Golden Cross speed**: measures how quickly the 50-DMA cycled from bearish (below 200-DMA) to bullish (above 200-DMA). Fewer days = faster recovery = higher score. Stocks currently in Death Cross score 0. Stocks that have always been above (no prior Death Cross in history) receive the 75th-percentile score.

**Golden Cross peak**: measures how tightly coupled the price breakout above the 200-DMA was with the 50-DMA confirmation. Fewer days = more decisive momentum = higher score. Same edge-case handling as speed.

**Eligibility**: a stock is excluded from ranking (and cannot be bought) if:
- Fewer than 200 days of price history available

**Target portfolio**: top 15 eligible stocks by composite score, weighted by score, capped at 20%.

**Sell rule**: sell any held stock that either:
- drops below rank 30 in the eligible ranking, OR
- is in a Death Cross (50-DMA < 200-DMA) — explicit momentum exit trigger

**Buy rule**: buy any stock in the top 15 not currently held.

**Trim rule**: trim any position exceeding 20% of NAV to exactly 20%.

**Cash rule**: if eligible sells exceed eligible buys, the portfolio may hold cash. Going
fully to cash is acceptable if no eligible stock meets the criteria.

---

## Tool Inventory

### scripts/fetch/fetch_universe.py
- **Writes**: `data/universe/universe.csv` (symbol, company_name, series, isin_code, sector)
- **Args**: none (or `--force` to override 90-day age check)
- **Exit 0**: success or skipped (too fresh). Exit 1: download failed (file unchanged).

### scripts/fetch/fetch_nsepy_price.py
- **Writes**:
  - `data/market/prices/YYYY-WW.csv` — weekly OHLCV snapshot
  - `data/market/prices/daily_adj_close.csv` — cumulative daily adj_close (append-only)
- **Args**: `--limit N` (first N symbols only), `--dry-run` (fetch but skip writes),
  `--months N` (ensure at least N months of history are present; downloads only missing data),
  `--force` (re-fetch full target window and refresh overlapping daily rows from origin),
  `--backfill-renames` (one-time fix: fetch pre-rename history for symbols in SYMBOL_RENAMES,
  e.g. SHRIRAMFIN from SRTRANSFIN before Dec 2022),
  `--clean-min-dates` (one-time fix: erase pre-listing contamination for symbols in SYMBOL_MIN_DATES,
  e.g. IREDA bond data before Nov 2023 equity IPO)
- **Exit 0**: success. Exit 1: >8% of symbols failed.
- **Primary source**: nselib → jugaad-data → yfinance (per-symbol fallback chain).
- **Note**: NSE sources do not provide adjusted close prices; `adj_close` equals `close`.
- **Sync check**: on every run, warns if any ISO week has daily data in `daily_adj_close.csv`
  but no corresponding `YYYY-WW.csv` snapshot, and auto-writes the missing snapshots.

### scripts/fetch/fetch_benchmark.py
- **Writes**: `data/market/benchmark.csv` (date, price_index, tri_level, source)
- **Args**: `--force` — re-fetch and overwrite all dates (default: fill missing dates only)
- **Exit 0**: success or already up to date. Exit 1: yfinance fetch failed entirely.
- **Behaviour**: reads all trading dates from `data/market/prices/daily_adj_close.csv`, compares with existing benchmark rows, and fetches missing dates. Source is `price_index_yfinance` when yfinance succeeds; `price_index_nse` when the nselib fallback is used.
- **Holiday cleanup**: after each successful fetch, any date present in the stock price calendar but absent from the Nifty 250 benchmark is confirmed as an NSE market holiday and is automatically removed from `daily_adj_close.csv`. This fixes phantom dates injected by yfinance returning stale carry-forward prices on closed days.

### scripts/metrics/validate_data.py
- **Reads**: prices (daily_adj_close.csv + weekly snapshots), benchmark.csv, universe.csv, portfolio.json
- **Prints**: PASS / WARNING / ERROR lines; price and benchmark completeness summary
- **Args**: `--report` — print full per-symbol gap detail and all missing benchmark dates
- **Exit 0**: pass (possibly with warnings). Exit 1: blocking error.
- **Completeness checks**: scans `daily_adj_close.csv` for NaN gaps per universe symbol (within each symbol's active window); cross-checks benchmark coverage against the same trading-day index. Weekends and NSE holidays are never flagged — they are absent from the file's date index by construction.

### scripts/metrics/compute_metrics.py
- **Reads**: all data files
- **Prints**: performance summary, ranking table, holdings table
- **Writes**: appends to `data/portfolio/performance.csv`
- **Args**: none
- **Ranking logic**: imported from `scripts/shared/ranking.py`

### scripts/strategy/update_portfolio.py
- **Args**: `--decisions PATH` (required), `--dry-run` (optional), `--init` (first run only)
- **Reads**: decisions JSON, portfolio.json, latest prices CSV
- **Writes**: portfolio.json (in-place)

### scripts/shared/ranking.py
- **Purpose**: factor functions and composite ranking engine — no project dependencies
- **Exports**: `compute_rankings()`, `compute_lt_momentum()`, `compute_nt_momentum()`,
  `compute_cross_speed()`, `compute_cross_peak()`, `normalise_series()`, `WEIGHTS`, constants
- **Used by**: `scripts/metrics/compute_metrics.py`, `scripts/backtest/backtest.py`

### scripts/shared/types.py
- **Purpose**: all TypedDict definitions for JSON schemas (`Portfolio`, `PerformanceResult`,
  `BacktestWeekResult`, `DecisionsFile`, etc.)
- **Used by**: all pipeline, strategy, and backtest scripts

### scripts/backtest/backtest.py
- **Args**: `--months N` (optional integer, 1–120; prompts interactively if omitted)
- **Data window**: simulates the requested months back from today and loads an extra
  520 calendar days for momentum / moving-average warm-up.
- **Writes**:
  - `data/backtest/backtest_YYYYMMDD_Nmo.csv` — weekly NAV & return series
  - `data/backtest/backtest_YYYYMMDD_Nmo_trades.csv` — per-trade execution log
  - `data/backtest/backtest_YYYYMMDD_Nmo_monthly.csv` — monthly return matrix
  - `data/backtest/backtest_YYYYMMDD_Nmo_tax.csv` — annual realized-gain tax table
- **Limitations**: uses the current Nifty 250 universe; stocks with <5yr price history are excluded to reduce (not eliminate) survivorship bias. Results are illustrative, not authoritative.
- **When to use**: ad-hoc, outside the weekly session protocol.

---

## Log Format

Each `logs/session_YYYY-MM-DD.md` must contain all of these sections:

```
# Session YYYY-MM-DD (Week YYYY-WW)

## Data Quality
- Universe: N stocks, last updated YYYY-MM-DD
- Prices: N symbols fetched, M missing/failed
- Benchmark: latest value = X (date Y, source: price_index_yfinance)
- Validate output: PASS / PASS with warnings / FAILED
- Any large-move flags or anomalies

## Performance Summary
- This week: portfolio +X.X%  |  benchmark +X.X%  |  active +X.X%
- Inception: portfolio +X.X% (INR +X,XXX, CAGR X.X%)  |  benchmark +X.X% (CAGR X.X%)  |  active CAGR +X.X%
- NOTE: benchmark is price index (not TRI) — active return overstated by ~1.5%/yr

## Portfolio Snapshot (before rebalance)
- NAV: INR X,XXX.XX
- Holdings: N stocks
- Cash: INR X,XXX.XX (X.X% of NAV)

## Rankings (top 20 eligible)
[paste table from compute_metrics.py output]

## Rebalance Decision
### Sells
- SYMBOL: reason (was rank X, dropped to rank Y)

### Buys
- SYMBOL: target weight X%, reason (rank Z, composite score X.XXX)

### No action
[if applicable: explain why no trades were made]

## Portfolio Snapshot (after rebalance)
- NAV: INR X,XXX.XX
- Holdings: N stocks
- Cash: INR X,XXX.XX (X.X% of NAV)
- Estimated transaction costs this session: INR X.XX

## Notes for Next Session
- Any observations, flags, or things to watch
- Corporate actions to verify manually (if any)
```

---

## Known Data Gaps

`validate_data.py` reports price gaps and missing benchmark dates. Most are explained
by one of the four root causes below. This context helps distinguish fixable gaps from
expected ones before triggering a costly `--force` re-fetch.

### Category A — Phantom NSE market holidays

**Root cause**: yfinance returns stale carry-forward prices for individual stocks on
NSE market holidays (Independence Day, Republic Day, Christmas, Eid, etc.) with
non-zero volume, bypassing the zero-volume filter and passing the 40%-quorum check.
The Nifty 250 index is never published on holidays, so benchmark.csv correctly has
no entry for those dates while daily_adj_close.csv incorrectly does.

**Fix** (automatic): `fetch_benchmark.py` now removes these phantom dates from
daily_adj_close.csv after every successful benchmark fetch. To clean the existing
backlog immediately, run `fetch_benchmark.py --force`.

### Category B — Month-end boundary gaps (IRFC, RECLTD, IDFCFIRSTB, NTPC, SBIN, PFC, HUDCO, MUTHOOTFIN)

**Root cause**: nselib's `price_volume_data` API returns monthly chunks and frequently
omits the last day of each month plus ~15–21 trading days into the next month. Because
the normal fetch appends from the last known date forward, these slots are never
revisited once skipped. jugaad-data (second-tier source) sometimes fills them in, but
not reliably.

**Fix**: Re-fetch the full history to give all three sources a chance to fill the gaps:
```
uv run python scripts/fetch/fetch_nsepy_price.py --force --months 240
```
This takes 30–60 minutes for 250 symbols. Run ad-hoc when gap counts are unacceptable.

### Category C — TATACAP: 11.5-year gap (stock not listed on NSE)

**Root cause**: Tata Capital Financial Services (TATACAP) was listed on NSE only for
narrow windows:
- 2009-03-16 → 2012-06-18 (listed)
- 2012-06-18 → 2024-01-08 (not listed / suspended — 4,221 days)
- 2024-01-08 → 2024-05-02 (listed again)
- 2024-05-02 → 2025-10-13 (not listed again)
- 2025-10-13 → present (listed)

These gaps are real absences from the exchange, not data fetching errors. The
`MIN_HISTORY_DAYS = 200` guard in ranking.py already excludes TATACAP from rankings
whenever it has insufficient continuous history.

**Fix**: None — accept as-is.

### Category D — SHRIRAMFIN: symbol rename after merger

**Root cause**: The symbol SHRIRAMFIN (Shriram Finance Ltd) only came into existence
in December 2022, when Shriram Transport Finance and Shriram City Union Finance merged.
Querying nselib for SHRIRAMFIN on pre-merger dates returns inconsistent partial data;
the full pre-2022 history lives under the old symbol SRTRANSFIN.

**Fix** (one-time, ~10 minutes):
```
uv run python scripts/fetch/fetch_nsepy_price.py --backfill-renames
```
This fetches SRTRANSFIN from the start of the data window to Dec 2022 and writes it
into daily_adj_close.csv under the SHRIRAMFIN column. Run once; subsequent normal
fetches will maintain SHRIRAMFIN going forward.

### Category E — Small scattered gaps (≤42 days: PAGEIND, APLAPOLLO, BALKRISIND, CHOLAFIN, UNOMINDA, etc.)

**Root cause**: Transient API call failures, network hiccups, or the zero-volume filter
dropping a legitimate thin-trading day. Isolated incidents. Includes:
- **CHOLAFIN** (42 gap-days in May–Dec 2023): scattered API failures, not month-end pattern
- **UNOMINDA** (29 gap-days): data correctly covers from 2007 under the current ticker
  (renamed from MINDAIND but backfill not needed); gaps are ordinary scattered misses

**Fix**: `uv run python scripts/fetch/fetch_nsepy_price.py --force` (re-fetches last 52
weeks; usually sufficient to fill sub-42-day gaps).

### Category F — IREDA: pre-IPO bond-market data contamination

**Root cause**: IREDA (Indian Renewable Energy Development Agency Ltd) had its equity
IPO on November 29 2023. However, `daily_adj_close.csv` contains IREDA data from 2014
because nselib returned data for IREDA's NSE-listed tax-free bonds under the same ticker
before the equity listing. Bond prices are not equity prices; using them for momentum
would give meaningless signals.

**Evidence**: 262 gap-days scattered across 2014–2023, with no data on equity trading days
in that window. Since IREDA has been trading equity for ~18 months, the bond-era data
currently lies outside the computation window (520 calendar days), but backtests with
start dates before Nov 2023 will use wrong prices.

**Fix** (one-time):
```
uv run python scripts/fetch/fetch_nsepy_price.py --clean-min-dates
```
This sets all IREDA cells before 2023-11-29 to NaN in daily_adj_close.csv. Future
fetches will not re-introduce the contamination — `update_daily_file()` applies the
`SYMBOL_MIN_DATES` filter on every write.

---

## Data Source Notes (fragility)

- **data/universe/universe.csv**: downloaded from NSE archive. FRAGILE — NSE blocks scrapers.
  If download fails, keep existing file. Check if symbol count changed after a successful refresh.

- **Prices**: `fetch/fetch_nsepy_price.py` uses nselib first, jugaad-data second,
  and yfinance `.NS` tickers only as a tertiary fallback. FRAGILE — NSE blocks
  scrapers and Yahoo Finance changes its API 2–3× per year. If >10% of held
  stocks have missing prices, do NOT rebalance. Stop and report.

- **Benchmark**: Nifty LargeMidcap 250 **price index** (not TRI). The niftyindices.com
  live JSON endpoint no longer publishes TRI values; no free real-time TRI source exists.
  The price index understates the true benchmark return by ~1.5%/yr (the index dividend yield),
  so active return will appear ~1.5%/yr better than reality. This is a permanent known limitation.
  `fetch/fetch_benchmark.py` source will always be `price_index_nse` or `price_index_yfinance`.

- **Corporate actions**: not automated. Manually check the NSE announcements page
  for held stocks before any session. Flag any splits, bonuses, or mergers in the log.
