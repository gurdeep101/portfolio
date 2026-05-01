# nifty_agent

Paper-trading Indian equity portfolio. INR 25,000 notional capital.
Benchmark: Nifty 250 TRI. Universe: Nifty 250 constituents.
Cadence: Weekly rebalance. **You are the agent. This file is your protocol.**

Every time `claude` is run in this directory, you execute the session loop below —
top to bottom, in order — without waiting for further prompting.

---

## Session Protocol

### START OF SESSION — run every step, in order

**Step 1 — Read current state**
Read `data/portfolio.json`. Report:
- Today's date and ISO week
- Current NAV (INR)
- Number of holdings
- Cash position (INR and % of NAV)
- Inception date and sessions run so far

**Step 2 — Refresh universe (conditional)**
```
python scripts/fetch_universe.py
```
The script skips automatically if `data/universe.csv` is less than 90 days old.
If it runs and the symbol count changes by more than 5, flag this prominently in the session log.

**Step 3 — Fetch prices**
```
python scripts/fetch_prices.py
```
This writes the weekly OHLCV snapshot and updates the daily adj_close history.
On first run, it pulls 52 weeks of history — expect 60–120 minutes.
If exit code is non-zero, STOP and report the error. Do not proceed.

**Step 4 — Fetch benchmark**
```
python scripts/fetch_benchmark.py
```
If it fails, note it in the session log and skip benchmark comparison this session. Continue.

**Step 5 — Fetch fundamentals (conditional)**
```
python scripts/fetch_fundamentals.py
```
The script skips symbols fetched within the last 7 days. On first run, expect 5–15 minutes.
If it fails entirely, use the most recent `data/fundamentals/` file and log a warning.

**Step 6 — Validate data (gate)**
```
python scripts/validate_data.py
```
Read output carefully.
- If exit code is 1: STOP. Report the blocking errors to the user. Do not rebalance.
- Warnings: log them and proceed with caution.
- Any stock flagged with a >40% move: do NOT trade that stock. Flag it in the log.

**Step 7 — Compute metrics**
```
python scripts/compute_metrics.py
```
Read the full output. This script also writes a row to `data/performance.csv`.
Pay attention to:
- Performance summary (weekly return, inception return, CAGR vs benchmark)
- The full ranking table and which stocks are BUY_CANDIDATE / SELL_CANDIDATE / HELD
- Excluded stocks (missing ROE/PB or insufficient history) — note the count

**Step 8 — Reason and decide**
Apply the Investment Strategy below. Think through:
1. Which stocks in the top 15 eligible stocks are not currently held? → BUY candidates
2. Which held stocks have dropped below rank 30? → SELL candidates
3. Which held stocks exceed 20% weight? → TRIM
4. Does the rebalance make sense? Is any trade driven by a data anomaly?
5. Do not trade any stock flagged with a large price move in Step 6.

**Step 9 — Write decisions file**
Write `decisions/YYYY-MM-DD.json` using today's date. Use this format exactly:

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
python scripts/update_portfolio.py --decisions decisions/YYYY-MM-DD.json
```
Read the printed summary. Verify NAV is approximately preserved (minus transaction costs).

**Step 11 — Write session log**
Write `logs/session_YYYY-MM-DD.md` following the Log Format section below.

**Step 12 — Commit**
```
git add data/ decisions/ logs/
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
- **Universe only**: stocks must be in current `data/universe.csv` at time of trade.
- **Large moves**: if a stock moved >40% in the past week (flagged by validate_data.py),
  do NOT trade it. Flag it in the session log for manual review.
- **Transaction cost**: 0.1% per trade side. Already deducted by update_portfolio.py.
- **Rebalance once per session only.**

---

## Investment Strategy

Rank all eligible Nifty 250 stocks by composite score each week. Total weight = 100%.

| Factor | Weight | Formula |
|---|---|---|
| Long-term momentum | 20% | (52-week return) − (4-week return), normalised |
| Near-term momentum | 20% | (50-DMA − 200-DMA) / 200-DMA, normalised |
| Quality (ROE) | 30% | ROE normalised across eligible universe |
| Value (1/PB) | 30% | 1/PB normalised across eligible universe |

**Eligibility**: a stock is excluded from ranking (and cannot be bought) if:
- ROE is missing
- P/B ratio is missing
- Fewer than 200 days of price history available

**Target portfolio**: top 15 eligible stocks by composite score, weighted by score, capped at 20%.

**Sell rule**: sell any held stock that drops below rank 30 in the eligible ranking.

**Buy rule**: buy any stock in the top 15 not currently held.

**Trim rule**: trim any position exceeding 20% of NAV to exactly 20%.

**Cash rule**: if eligible sells exceed eligible buys, the portfolio may hold cash. Going
fully to cash is acceptable if no eligible stock meets the criteria.

---

## Tool Inventory

### scripts/fetch_universe.py
- **Writes**: `data/universe.csv` (symbol, company_name, series, isin_code, sector)
- **Args**: none (or `--force` to override 90-day age check)
- **Exit 0**: success or skipped (too fresh). Exit 1: download failed (file unchanged).

### scripts/fetch_prices.py
- **Writes**:
  - `data/prices/YYYY-WW.csv` — weekly OHLCV snapshot
  - `data/prices/daily_adj_close.csv` — cumulative daily adj_close (append-only)
- **Args**: none
- **Exit 0**: success. Exit 1: >8% of symbols failed.

### scripts/fetch_benchmark.py
- **Writes**: appends to `data/benchmark.csv` (date, price_index, tri_level, source)
- **Args**: none
- **Exit 0**: success. Exit 1: all sources failed.

### scripts/fetch_fundamentals.py
- **Writes**: `data/fundamentals/YYYY-WW.json`; appends to `data/missing_fundamentals_log.csv`
- **Args**: none
- **Runtime**: 5–15 minutes on full run; much faster if recent cache exists.

### scripts/validate_data.py
- **Reads**: prices, benchmark, universe, portfolio.json
- **Prints**: PASS / WARNING / ERROR lines
- **Exit 0**: pass (possibly with warnings). Exit 1: blocking error.

### scripts/compute_metrics.py
- **Reads**: all data files
- **Prints**: performance summary, ranking table, holdings table
- **Writes**: appends to `data/performance.csv`
- **Args**: none

### scripts/update_portfolio.py
- **Args**: `--decisions PATH` (required), `--dry-run` (optional), `--init` (first run only)
- **Reads**: decisions JSON, portfolio.json, latest prices CSV
- **Writes**: portfolio.json (in-place)

---

## Log Format

Each `logs/session_YYYY-MM-DD.md` must contain all of these sections:

```
# Session YYYY-MM-DD (Week YYYY-WW)

## Data Quality
- Universe: N stocks, last updated YYYY-MM-DD
- Prices: N symbols fetched, M missing/failed
- Benchmark: latest value = X (date Y, source: TRI/price_index)
- Fundamentals: N symbols with valid ROE+PB, M excluded
- Validate output: PASS / PASS with warnings / FAILED
- Any large-move flags or anomalies

## Performance Summary
- This week: portfolio +X.X%  |  benchmark +X.X%  |  active +X.X%
- Inception: portfolio +X.X% (INR +X,XXX, CAGR X.X%)  |  benchmark +X.X% (CAGR X.X%)  |  active CAGR +X.X%
- [Note if benchmark is price index, not TRI]

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

## Data Source Notes (fragility)

- **universe.csv**: downloaded from NSE archive. FRAGILE — NSE blocks scrapers.
  If download fails, keep existing file. Check if symbol count changed after a successful refresh.

- **Prices**: yfinance `.NS` tickers. FRAGILE — Yahoo Finance changes its API 2–3× per year.
  If >10% of held stocks have missing prices, do NOT rebalance. Stop and report.
  `fetch_prices.py` prints the yfinance version — note it in the session log.

- **Benchmark TRI**: no reliable free API. Primary source is NSE JSON endpoint.
  Fallback is yfinance `^CNX250` (price index, not TRI). The price index is ~1.5%/yr
  lower than TRI due to dividends — active return will appear ~1.5%/yr better than reality.
  Always note which source was used.

- **Fundamentals**: yfinance `.info` — 20–30% null rates for Indian stocks are normal.
  Stocks with missing ROE or PB are excluded from ranking, not imputed.
  Check `data/missing_fundamentals_log.csv` if exclusion rate seems unusually high.

- **Corporate actions**: not automated. Manually check the NSE announcements page
  for held stocks before any session. Flag any splits, bonuses, or mergers in the log.
