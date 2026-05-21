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

# Verify NSE-native price fetcher dependencies
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
# uv run python scripts/fetch/fetch_nsepy_price.py --limit 10 --dry-run
# uv run python scripts/fetch/fetch_nsepy_price.py --months 24
# Default mode downloads only missing data. With --force, overlapping dates in
# daily_adj_close.csv are refreshed from origin data.

# 3. Fetch benchmark (~5s)
uv run python scripts/fetch/fetch_benchmark.py
# Verify: data/market/benchmark.csv has a row with source=price_index_nse
# Optional args: --force (re-fetch and overwrite all dates; default fills missing dates only)

# 4. Validate data
uv run python scripts/pipeline/validate_data.py
# Expect: PASS (portfolio is all-cash — no held stocks to block on)

# 5. Compute metrics
uv run python scripts/pipeline/compute_metrics.py
# Verify: ranking table prints and looks reasonable
#         data/portfolio/performance.csv has one row
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
then loads an additional 420 calendar days of price history for momentum and
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
| `fetch/fetch_nsepy_price.py` | `uv run python scripts/fetch/fetch_nsepy_price.py` | `--limit N`; `--dry-run`; `--months N`; `--force` — re-fetch target window and refresh overlapping daily rows |
| `fetch/fetch_benchmark.py` | `uv run python scripts/fetch/fetch_benchmark.py` | — |
| `pipeline/validate_data.py` | `uv run python scripts/pipeline/validate_data.py` | — |
| `pipeline/compute_metrics.py` | `uv run python scripts/pipeline/compute_metrics.py` | — |
| `pipeline/update_portfolio.py` | `uv run python scripts/pipeline/update_portfolio.py --decisions PATH` | `--decisions PATH` (required); `--dry-run` — skip writes; `--init` — fresh portfolio (first run) |
| `backtest/backtest.py` | `uv run python scripts/backtest/backtest.py` | `--months N` — months to backtest (1–120); prompts if omitted |

## Project structure

```
CLAUDE.md                              # Agent protocol — the application
pyproject.toml                         # Dependencies (managed by uv)
scripts/
  portfolio_types.py                   # Shared TypedDict schemas
  fetch/
    fetch_universe.py                  # Nifty 250 constituent list (refreshes every 90 days)
    fetch_nsepy_price.py               # Weekly OHLCV + daily adj_close (primary price fetcher)
    fetch_benchmark.py                 # Nifty 250 TRI (or price index fallback)
  pipeline/
    validate_data.py                   # Data quality gate before each session
    compute_metrics.py                 # Stock rankings + portfolio performance
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

## Known limitations

- **Benchmark (Nifty LargeMidcap 250 price index, not TRI)**: the niftyindices.com live JSON endpoint no longer publishes TRI values and no free real-time TRI source exists. `fetch/fetch_benchmark.py` fetches the price index from the NSE JSON endpoint, falling back to nselib then yfinance `^CNX250`. The price index understates the true benchmark return by ~1.5%/year (the index dividend yield), so active return will consistently appear ~1.5%/yr better than reality. The `source` column in `benchmark.csv` records the data origin each session.

- **yfinance reliability**: Yahoo Finance changes its internal API 2–3 times per year. `fetch/fetch_nsepy_price.py` uses yfinance only as a tertiary fallback, but failures can still affect a small subset of symbols. If needed, update yfinance (`uv sync --upgrade-package yfinance`) and retry.

- **Corporate actions**: not automated. Manually check the NSE announcements page for held stocks before each session. Splits and bonus issues will trigger the >40% move flag in `pipeline/validate_data.py` — do not trade flagged stocks until verified.
