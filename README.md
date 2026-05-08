# nifty_agent

AI-driven Indian equity paper portfolio. INR 25,000 notional capital managed against the Nifty 250 TRI benchmark. Runs entirely through weekly Claude Code sessions — no web server, no scheduler, no daemon.

**The agent is `claude`. CLAUDE.md is its protocol. The scripts are its tools.**

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13.

```bash
# Install dependencies
uv sync

# Verify core dependencies
uv run python -c "import yfinance, pandas, numpy; print('OK')"

# Verify NSE-native price fetcher dependencies
uv run python -c "import nselib, jugaad_data; print('NSE libs OK')"
```

## First run (Phase 1 validation — do this before the first agent session)

Run each step manually and verify the output before trusting the agent to do it.

```bash
# 1. Fetch the Nifty 250 constituent list (~5s)
uv run python scripts/fetch_universe.py --force
# Verify: data/universe/universe.csv has ~250 rows with correct symbols

# 2. Fetch prices — first run pulls 52 weeks of history (60–120 min)
uv run python scripts/fetch_prices.py
# Verify: data/market/prices/YYYY-WW.csv exists
#         data/market/prices/daily_adj_close.csv exists with ~250 columns
#         Spot-check RELIANCE, HDFCBANK, TCS closes against NSE bhav copy
#
# Alternative: use NSE-native libraries instead of yfinance as primary source
# uv run python scripts/fetch_nsepy_price.py
# Produces identical output files. Use when yfinance API is broken.
# To fetch more history than the default 52 weeks, pass --months:
# uv run python scripts/fetch_nsepy_price.py --months 24
# Downloads only what is missing; never deletes existing data.

# 3. Fetch benchmark (~5s)
uv run python scripts/fetch_benchmark.py
# Verify: data/market/benchmark.csv has a row; check value against niftyindices.com

# 4. Fetch fundamentals (5–15 min)
uv run python scripts/fetch_fundamentals.py
# Verify: data/market/fundamentals/YYYY-WW.json exists
#         At least 180–200 of 250 stocks have valid ROE and PB

# 5. Validate data
uv run python scripts/validate_data.py
# Expect: PASS (portfolio is all-cash — no held stocks to block on)

# 6. Compute metrics
uv run python scripts/compute_metrics.py
# Verify: ranking table prints and looks reasonable
#         data/portfolio/performance.csv has one row
```

## Weekly sessions

```bash
claude
```

Claude reads CLAUDE.md, runs the 12-step protocol, writes decisions and logs, and commits.

## Project structure

```
CLAUDE.md                              # Agent protocol — the application
pyproject.toml                         # Dependencies (managed by uv)
scripts/
  fetch_universe.py                    # Nifty 250 constituent list (refreshes every 90 days)
  fetch_prices.py                      # Weekly OHLCV + daily adj_close (yfinance primary)
  fetch_nsepy_price.py                 # Same outputs; NSE-native libraries primary (fallback)
  fetch_benchmark.py                   # Nifty 250 TRI (or price index fallback)
  fetch_fundamentals.py                # P/E, P/B, ROE, market cap via yfinance
  validate_data.py                     # Data quality gate before each session
  compute_metrics.py                   # Stock rankings + portfolio performance
  update_portfolio.py                  # Applies decisions JSON to portfolio state
  backtest.py                          # Historical strategy simulation
  portfolio_types.py                   # Shared TypedDict schemas
data/
  universe/
    universe.csv                       # Nifty 250 constituents
  market/                              # Raw source data from exchanges / APIs
    prices/
      YYYY-WW.csv                      # Weekly OHLCV snapshots (immutable)
      daily_adj_close.csv              # Cumulative daily adj_close (append-only)
      daily_low.csv                    # Daily lows (used by backtest)
      daily_high.csv                   # Daily highs (used by backtest)
    fundamentals/
      YYYY-WW.json                     # Weekly fundamentals cache
    benchmark.csv                      # Weekly TRI/price index levels
    missing_fundamentals_log.csv       # Stocks excluded due to missing ROE or PB
  portfolio/                           # Live portfolio state & performance
    portfolio.json                     # Current holdings, cash, NAV history
    performance.csv                    # Append-only weekly performance record
  decisions/                           # Weekly trade recommendations
    YYYY-MM-DD.json                    # Structured trade decisions (written by Claude)
  backtest/                            # Backtest outputs
    backtest_YYYYMMDD_Nmo.csv          # Weekly NAV & return series
    backtest_YYYYMMDD_Nmo_trades.csv   # Per-trade execution log
    backtest_YYYYMMDD_Nmo_monthly.csv  # Monthly return matrix
    backtest_YYYYMMDD_Nmo_tax.csv      # Annual realized-gain tax table
logs/
  session_YYYY-MM-DD.md                # Weekly session narrative (written by Claude)
```

## Known limitations

- **Nifty 250 TRI**: no reliable free API. The primary source is the NSE live JSON endpoint. If unavailable, the fallback is the `^CNX250` price index via yfinance, which understates the benchmark return by ~1.5%/year (the index dividend yield). Active return will appear ~1.5%/yr better than reality when the fallback is used. The `benchmark_source` column in `performance.csv` records which was used each session.

- **yfinance reliability**: Yahoo Finance changes its internal API 2–3 times per year. When it breaks, `fetch_prices.py` will exit with an error. Fix: update yfinance (`uv sync --upgrade-package yfinance`) and retry. If the breakage persists, run `fetch_nsepy_price.py` instead — it uses nselib and jugaad-data as primary sources and falls back to yfinance only for stragglers.

- **Fundamentals coverage**: expect 20–30% of Nifty 250 stocks to have missing ROE or P/B in yfinance for Indian equities. These stocks are excluded from ranking (not imputed). See `data/market/missing_fundamentals_log.csv`.

- **Corporate actions**: not automated. Manually check the NSE announcements page for held stocks before each session. Splits and bonus issues will trigger the >40% move flag in `validate_data.py` — do not trade flagged stocks until verified.
