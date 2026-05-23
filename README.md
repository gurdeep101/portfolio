# nifty_agent

AI-driven Indian equity paper portfolio. INR 25,000 notional capital managed against the Nifty LargeMidcap 250 price index benchmark. Runs entirely through weekly Claude Code sessions — no web server, no scheduler, no daemon.

**The agent is `claude`. CLAUDE.md is its protocol. The scripts are its tools.**

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13.

```bash
# Install dependencies
uv sync

# Verify core dependencies
uv run python -c "import yfinance, pandas, numpy; print('OK')"
```

## First run (Phase 1 validation — do this before the first agent session)

Run each step manually and verify the output before trusting the agent to do it.

```bash
# 1. Fetch the Nifty 250 constituent list (~5s)
uv run python scripts/fetch/fetch_universe.py --force
# Verify: data/universe/universe.csv has ~250 rows with correct symbols

# 2. Fetch prices — first run pulls 52 weeks of history
uv run python scripts/fetch/fetch_nsepy_price.py
# Verify: data/market/prices/YYYY-WW.csv exists
#         data/market/prices/daily_adj_close.csv exists with ~250 columns
#         Spot-check RELIANCE, HDFCBANK, TCS closes against NSE bhav copy
#
# Optional args: --limit N, --dry-run, --months N (ensure N months of history present),
#               --force (re-fetch full target window and refresh overlapping daily rows)
# One-time data quality fixes (run once after first fetch, not needed every session):
#   --backfill-renames  fill SHRIRAMFIN pre-merger history from SRTRANSFIN (~10 min)
#   --clean-min-dates   erase IREDA pre-IPO bond data from daily_adj_close.csv (seconds)

# 3. Fetch benchmark (~5s)
uv run python scripts/fetch/fetch_benchmark.py
# Verify: data/market/benchmark.csv has a row with source=price_index_yfinance
# Optional args: --force (re-fetch and overwrite all dates; default fills missing dates only)

# 4. Validate data
uv run python scripts/metrics/validate_data.py
# Expect: PASS (portfolio is all-cash — no held stocks to block on)

# 5. Compute metrics
uv run python scripts/metrics/compute_metrics.py
# Verify: ranking table prints and looks reasonable
#         data/portfolio/performance.csv has one row

# 6. Initialise portfolio (first run only — creates portfolio.json)
uv run python scripts/strategy/update_portfolio.py --init
# Verify: data/portfolio/portfolio.json created with INR 25,000 all-cash
```

Then start the first agent session:

```bash
claude
```

## Weekly sessions

```bash
claude
```

Claude reads CLAUDE.md, runs the 12-step protocol, writes decisions and logs, and commits.

## Backtesting

Run ad-hoc, outside the weekly session loop.

```bash
# Interactive — prompts for number of months
uv run python scripts/backtest/backtest.py

# Non-interactive
uv run python scripts/backtest/backtest.py --months 12
```

`--months` accepts 1–120. The simulation starts that many months before today,
then loads an additional 520 calendar days of price history for momentum and
moving-average warm-up.

Outputs four files in `data/backtest/`:

| File | Contents |
|------|----------|
| `backtest_YYYYMMDD_Nmo.csv` | Weekly NAV and return series |
| `backtest_YYYYMMDD_Nmo_trades.csv` | Per-trade execution log |
| `backtest_YYYYMMDD_Nmo_monthly.csv` | Monthly return matrix |
| `backtest_YYYYMMDD_Nmo_tax.csv` | Annual realised-gain tax table |

**Caveats — treat results as illustrative, not authoritative:**
- **Survivorship bias (reduced)**: stocks with fewer than 5 years of price history are excluded, filtering out recent index additions. Companies delisted or removed before data collection began are still absent, so results remain optimistic relative to a true historical universe.

## Script reference

| Script | Command | Arguments |
|--------|---------|-----------|
| `fetch/fetch_universe.py` | `uv run python scripts/fetch/fetch_universe.py` | `--force` — ignore 90-day age check |
| `fetch/fetch_nsepy_price.py` | `uv run python scripts/fetch/fetch_nsepy_price.py` | `--limit N`; `--dry-run`; `--months N`; `--force`; `--backfill-renames` (one-time symbol-rename fix); `--clean-min-dates` (one-time pre-listing contamination fix) |
| `fetch/fetch_benchmark.py` | `uv run python scripts/fetch/fetch_benchmark.py` | `--force` — re-fetch all dates |
| `metrics/validate_data.py` | `uv run python scripts/metrics/validate_data.py` | `--report` — full per-symbol gap detail and missing benchmark dates |
| `metrics/compute_metrics.py` | `uv run python scripts/metrics/compute_metrics.py` | — |
| `strategy/update_portfolio.py` | `uv run python scripts/strategy/update_portfolio.py --decisions PATH` | `--decisions PATH` (required); `--dry-run`; `--init` (first run) |
| `backtest/backtest.py` | `uv run python scripts/backtest/backtest.py` | `--months N` (1–120); prompts if omitted |

## Project structure

```
CLAUDE.md                              # Agent protocol — the application
pyproject.toml                         # Dependencies (managed by uv)
scripts/
  shared/
    types.py                           # TypedDict schemas shared across all modules
    ranking.py                         # Factor functions + composite ranking engine
  fetch/
    fetch_universe.py                  # Nifty 250 constituent list (refreshes every 90 days)
    fetch_nsepy_price.py               # Weekly OHLCV + daily adj_close (primary price fetcher)
    fetch_benchmark.py                 # Nifty 250 price index (yfinance primary, nselib fallback)
  metrics/
    validate_data.py                   # Data quality gate before each session
    compute_metrics.py                 # Stock rankings + portfolio performance
  strategy/
    update_portfolio.py                # Applies decisions JSON to portfolio state
  backtest/
    backtest.py                        # Historical strategy simulation
tests/
  test_fetch_benchmark.py              # Unit tests for fetch_benchmark (mocked)
  test_fetch_nsepy_price_unit.py       # Unit tests for fetch_nsepy_price (mocked)
  test_compute_metrics_momentum.py     # Unit tests for momentum factor functions
data/
  universe/
    universe.csv                       # Nifty 250 constituents (generated by fetch_universe.py)
  market/                              # Raw source data from exchanges / APIs
    prices/
      YYYY-WW.csv                      # Weekly OHLCV snapshots (generated)
      daily_adj_close.csv              # Cumulative daily adj_close (generated)
      .gitkeep                         # Keeps folder in git before first run
    benchmark.csv                      # Weekly price index levels (generated)
  portfolio/                           # Live portfolio state & performance
    portfolio.json                     # Current holdings, cash, NAV history
    performance.csv                    # Append-only weekly performance record
    .gitkeep                           # Keeps folder in git before first run
  decisions/                           # Weekly trade recommendations
    YYYY-MM-DD.json                    # Structured trade decisions (generated)
    .gitkeep                           # Keeps folder in git before first decision
  backtest/                            # Backtest outputs
    backtest_YYYYMMDD_Nmo.csv          # Weekly NAV & return series (generated)
    backtest_YYYYMMDD_Nmo_trades.csv   # Per-trade execution log (generated)
    backtest_YYYYMMDD_Nmo_monthly.csv  # Monthly return matrix (generated)
    backtest_YYYYMMDD_Nmo_tax.csv      # Annual realized-gain tax table (generated)
    .gitkeep                           # Keeps folder in git before first backtest
logs/
  session_YYYY-MM-DD.md                # Weekly session narrative (generated)
```

## Module dependencies

```
shared/ranking.py  ←── metrics/compute_metrics.py
shared/ranking.py  ←── backtest/backtest.py
shared/types.py    ←── metrics/compute_metrics.py
shared/types.py    ←── metrics/validate_data.py
shared/types.py    ←── strategy/update_portfolio.py
shared/types.py    ←── backtest/backtest.py

fetch/*            ── no internal imports (read external APIs, write data/)
metrics/*          ── reads data/, imports shared/
strategy/*         ── reads data/, imports shared/
backtest/*         ── reads data/, imports shared/
```

## Known limitations

- **Benchmark (Nifty LargeMidcap 250 price index, not TRI)**: the niftyindices.com live JSON endpoint no longer publishes TRI values and no free real-time TRI source exists. `fetch/fetch_benchmark.py` fetches the price index from yfinance (`^CNX250`) with automatic retry on rate limits, falling back to nselib (NSE indicesHistory API) if yfinance is unavailable. The price index understates the true benchmark return by ~1.5%/year (the index dividend yield), so active return will consistently appear ~1.5%/yr better than reality. The `source` column in `benchmark.csv` records the data origin each session.

- **yfinance reliability**: Yahoo Finance changes its internal API 2–3 times per year. `fetch/fetch_nsepy_price.py` uses yfinance only as a tertiary fallback, but failures can still affect a small subset of symbols. If needed, update yfinance (`uv sync --upgrade-package yfinance`) and retry.

- **Corporate actions**: not automated. Manually check the NSE announcements page for held stocks before each session. Splits and bonus issues will trigger the >40% move flag in `metrics/validate_data.py` — do not trade flagged stocks until verified.

## Data gap reference

`metrics/validate_data.py --report` shows per-symbol gap counts. Six root causes (A–F) account for all reported gaps:

| Category | Affected symbols | Cause | Fix |
|----------|-----------------|-------|-----|
| **A — Phantom NSE holidays** | All (inflates benchmark missing count) | yfinance returns stale carry-forward prices on closed market days, passing the quorum filter; benchmark has no value for those dates | Automatic: `fetch_benchmark.py` now removes phantom dates after each successful run. Run `fetch_benchmark.py --force` to clean the backlog |
| **B — Month-end boundary gaps** | IRFC, RECLTD, IDFCFIRSTB, NTPC, SBIN, PFC, HUDCO, MUTHOOTFIN | nselib omits the last trading day of each calendar month + ~15–21 days into the next month in its response chunks; skipped dates are never revisited in normal append mode | `uv run python scripts/fetch/fetch_nsepy_price.py --force --months 240` (30–60 min) |
| **C — Not listed on NSE** | TATACAP | Tata Capital was suspended/delisted for 11.5 years (Jun 2012 – Jan 2024); gaps reflect real absence from the exchange, not a fetch error | None — accept as-is; `MIN_HISTORY_DAYS` guard already excludes it from rankings when history is insufficient |
| **D — Symbol rename after merger** | SHRIRAMFIN | SHRIRAMFIN only exists from Dec 2022; pre-merger history is under old symbol SRTRANSFIN, which nselib returns incompletely when queried as SHRIRAMFIN | `uv run python scripts/fetch/fetch_nsepy_price.py --backfill-renames` (one-time, ~10 min) |
| **E — Small scattered gaps** | PAGEIND, APLAPOLLO, BALKRISIND, CHOLAFIN, UNOMINDA, and others with ≤42 gap-days | Transient API failures or zero-volume filter over-triggering. CHOLAFIN (42d) had API failures in 2023; UNOMINDA (29d) data is correctly present from 2007 under the current ticker | `uv run python scripts/fetch/fetch_nsepy_price.py --force` |
| **F — Pre-IPO bond-market contamination** | IREDA | IREDA equity IPO was Nov 29 2023, but nselib returned data under the same ticker for IREDA's NSE-listed tax-free bonds from 2014 — bond prices stored as equity prices, giving 262 spurious gap-days across 2014–2023 | `uv run python scripts/fetch/fetch_nsepy_price.py --clean-min-dates` (one-time, seconds) |
