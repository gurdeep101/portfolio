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
uv run python -c "import nselib, jugaad_data; print('NSE libs OK')"
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

# 4. Fetch fundamentals
#    - Current week: 5–15 min (yfinance, P/E + P/B + ROE + market cap)
#    - Historical weeks: fast (one nselib call per week, P/E only; P/B and ROE are null)
uv run python scripts/fetch/fetch_fundamentals.py
# Verify: data/market/fundamentals/YYYY-WW.json exists for the current week
#         At least 180–200 of 250 stocks have valid ROE and PB for the current week
#
# To backfill fundamentals for all historical price weeks (first run):
#   uv run python scripts/fetch/fetch_fundamentals.py
# This creates one JSON per missing week — historical weeks get real point-in-time
# P/E from NSE archives (via nselib); P/B, ROE, and market_cap are null for those weeks.
# Use --force to overwrite all existing files.

# 5. Validate data
uv run python scripts/pipeline/validate_data.py
# Expect: PASS (portfolio is all-cash — no held stocks to block on)

# 6. Compute metrics
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

Outputs four files in `data/backtest/`:

| File | Contents |
|------|----------|
| `backtest_YYYYMMDD_Nmo.csv` | Weekly NAV and return series |
| `backtest_YYYYMMDD_Nmo_trades.csv` | Per-trade execution log |
| `backtest_YYYYMMDD_Nmo_monthly.csv` | Monthly return matrix |
| `backtest_YYYYMMDD_Nmo_tax.csv` | Annual realised-gain tax table |

**Caveats — treat results as illustrative, not authoritative:**
- **Look-ahead bias**: uses the current week's fundamental data (ROE, P/B) for all historical ranking decisions. Stocks that only recently passed the eligibility threshold will appear to have been held for longer than they would have been in live trading.
- **Survivorship bias**: ranks against the current Nifty 250 constituent list. Companies that were dropped from the index (e.g., due to failure or delisting) are excluded from historical analysis, which overstates the universe quality.

## Script reference

| Script | Command | Arguments |
|--------|---------|-----------|
| `fetch/fetch_universe.py` | `uv run python scripts/fetch/fetch_universe.py` | `--force` — ignore 90-day age check |
| `fetch/fetch_nsepy_price.py` | `uv run python scripts/fetch/fetch_nsepy_price.py` | `--limit N`; `--dry-run`; `--months N`; `--force` — re-fetch target window and refresh overlapping daily rows |
| `fetch/fetch_benchmark.py` | `uv run python scripts/fetch/fetch_benchmark.py` | — |
| `fetch/fetch_fundamentals.py` | `uv run python scripts/fetch/fetch_fundamentals.py` | `--force` — re-fetch and overwrite all weeks |
| `pipeline/validate_data.py` | `uv run python scripts/pipeline/validate_data.py` | — |
| `pipeline/compute_metrics.py` | `uv run python scripts/pipeline/compute_metrics.py` | — |
| `pipeline/update_portfolio.py` | `uv run python scripts/pipeline/update_portfolio.py --decisions PATH` | `--decisions PATH` (required); `--dry-run` — skip writes; `--init` — fresh portfolio (first run) |
| `backtest/backtest.py` | `uv run python scripts/backtest/backtest.py` | `--months N` — months to backtest (1–MAX); prompts if omitted |

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
    fetch_fundamentals.py              # P/E, P/B, ROE, market cap — yfinance (current week) + nselib P/E (historical weeks)
  pipeline/
    validate_data.py                   # Data quality gate before each session
    compute_metrics.py                 # Stock rankings + portfolio performance
    update_portfolio.py                # Applies decisions JSON to portfolio state
  backtest/
    backtest.py                        # Historical strategy simulation
tests/
  test_fetch_benchmark.py              # Unit tests for fetch_benchmark (mocked)
  test_fetch_nsepy_price_unit.py       # Unit tests for fetch_nsepy_price (mocked)
  test_fetch_fundamentals_unit.py      # Unit tests for fetch_fundamentals (mocked)
data/
  universe/
    universe.csv                       # Nifty 250 constituents (generated by fetch_universe.py)
  market/                              # Raw source data from exchanges / APIs
    prices/
      YYYY-WW.csv                      # Weekly OHLCV snapshots (generated)
      daily_adj_close.csv              # Cumulative daily adj_close (generated)
      .gitkeep                         # Keeps folder in git before first run
    fundamentals/
      YYYY-WW.json                     # Weekly fundamentals cache (generated)
      .gitkeep                         # Keeps folder in git before first run
    benchmark.csv                      # Weekly price index levels (generated)
    missing_fundamentals_log.csv       # Stocks excluded due to missing ROE/PB (generated)
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

- **Fundamentals coverage**: expect 20–30% of Nifty 250 stocks to have missing ROE or P/B in yfinance for Indian equities. These stocks are excluded from ranking (not imputed). See `data/market/missing_fundamentals_log.csv`.

- **Historical fundamentals (backfill)**: for weeks prior to the current one, only P/E is available (from NSE archives via nselib). P/B, ROE, and market cap are `null` — no free historical source exists for these fields. The backtest's look-ahead bias caveat therefore applies to P/B and ROE for all historical periods.

- **Corporate actions**: not automated. Manually check the NSE announcements page for held stocks before each session. Splits and bonus issues will trigger the >40% move flag in `pipeline/validate_data.py` — do not trade flagged stocks until verified.
