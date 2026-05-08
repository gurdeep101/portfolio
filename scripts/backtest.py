"""Backtest the nifty_agent strategy over a historical period.

Usage:
  uv run python scripts/backtest.py              # interactive: prompts for months
  uv run python scripts/backtest.py --months 18  # non-interactive

Output:
  data/backtest/backtest_YYYYMMDD_Nmo.csv  — one row per weekly rebalance

LIMITATIONS (read before interpreting results):
  1. Fundamentals look-ahead bias: current P/B and ROE are used for all historical
     weeks. Past performance may differ if fundamentals have changed significantly.
  2. Survivorship bias: only current Nifty 250 constituents are simulated; stocks
     delisted or removed from the index are absent from all historical weeks.
  3. Benchmark is ^CNX250 price index (not TRI); active return appears ~1.5 %/yr
     better than reality because dividends are excluded from the benchmark.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))

from compute_metrics import compute_rankings
from portfolio_types import BacktestAnnualTax, BacktestTradeEntry, BacktestWeekResult, FundamentalsEntry

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "market" / "prices"
FUNDAMENTALS_DIR = DATA_DIR / "market" / "fundamentals"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"
DAILY_LOW_FILE = PRICES_DIR / "daily_low.csv"
DAILY_HIGH_FILE = PRICES_DIR / "daily_high.csv"
BACKTEST_RESULTS_DIR = DATA_DIR / "backtest"

INITIAL_CAPITAL: float = 25_000.0
TRANSACTION_COST_PCT: float = 0.001    # 0.1 % per trade side
MIN_BUY_VALUE: float = 500.0           # minimum INR trade size
TARGET_TOP_N: int = 15                 # buy the top N eligible stocks
SELL_RANK_THRESHOLD: int = 30          # sell held stock if rank drops below this
MAX_POSITION_WEIGHT: float = 0.20      # trim / cap at 20 % of NAV
# Calendar days to load before backtest start for MA + momentum warm-up.
# Covers 200-day MA and 52-week (≈260 trading-day) momentum lookback.
MA_WARMUP_DAYS: int = 420
MAX_MONTHS: int = 60
TAX_RATE_PCT: float = 30.0             # flat tax rate on net annual gains (%)
RISK_FREE_RATE_ANNUAL_PCT: float = 6.5 # India 10-yr G-sec approximation for Sharpe
YFINANCE_BATCH_SIZE: int = 50
YFINANCE_BATCH_SLEEP: float = 2.0      # seconds between yfinance batch requests


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_universe() -> list[str]:
    """Return the list of NSE symbols from data/universe/universe.csv."""
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return df["symbol"].tolist()
    except (OSError, pd.errors.ParserError, KeyError) as exc:
        print(f"ERROR: Could not read universe.csv: {exc}")
        sys.exit(1)


def _fetch_prices_from_yfinance(
    start: date, end: date, universe: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Download daily adj-close, low, and high prices for all universe symbols.

    Returns (adj_close_df, low_df, high_df) — all wide-format (index=date, columns=symbols).
    All three series come from the same yf.download call; no extra requests needed.
    Symbols with no data are silently omitted. Batches of YFINANCE_BATCH_SIZE.
    """
    tickers = [f"{sym}.NS" for sym in universe]
    close_frames: list[pd.DataFrame] = []
    low_frames: list[pd.DataFrame] = []
    high_frames: list[pd.DataFrame] = []

    for batch_start in range(0, len(tickers), YFINANCE_BATCH_SIZE):
        batch = tickers[batch_start : batch_start + YFINANCE_BATCH_SIZE]
        try:
            raw = yf.download(
                batch,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            print(f"  WARNING: yfinance batch failed ({batch[0]}…): {exc}")
            time.sleep(5.0)
            continue

        if raw.empty:
            time.sleep(YFINANCE_BATCH_SLEEP)
            continue

        if isinstance(raw.columns, pd.MultiIndex):
            level0 = raw.columns.get_level_values(0)
            if "Close" not in level0:
                time.sleep(YFINANCE_BATCH_SLEEP)
                continue
            close = raw["Close"].copy()
            low  = raw["Low"].copy()  if "Low"  in level0 else pd.DataFrame()
            high = raw["High"].copy() if "High" in level0 else pd.DataFrame()
        else:
            if "Close" not in raw.columns:
                time.sleep(YFINANCE_BATCH_SLEEP)
                continue
            ticker_name = batch[0]
            close = raw[["Close"]].rename(columns={"Close": ticker_name})
            low  = raw[["Low"]].rename(columns={"Low": ticker_name})   if "Low"  in raw.columns else pd.DataFrame()
            high = raw[["High"]].rename(columns={"High": ticker_name}) if "High" in raw.columns else pd.DataFrame()

        ts_index = pd.to_datetime(close.index).tz_localize(None)
        close.columns = [str(c).replace(".NS", "") for c in close.columns]
        close.index = ts_index
        close_frames.append(close)

        for df, frames in ((low, low_frames), (high, high_frames)):
            if not df.empty:
                df.columns = [str(c).replace(".NS", "") for c in df.columns]
                df.index = ts_index
                frames.append(df)

        fetched_so_far = min(batch_start + YFINANCE_BATCH_SIZE, len(tickers))
        print(f"  Fetched {fetched_so_far}/{len(tickers)} symbols…", end="\r", flush=True)
        time.sleep(YFINANCE_BATCH_SLEEP)

    print()  # end the \r progress line

    def _combine(frames: list[pd.DataFrame]) -> pd.DataFrame:
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, axis=1)
        return df.loc[~df.index.duplicated(keep="last")].sort_index()

    return _combine(close_frames), _combine(low_frames), _combine(high_frames)


def _load_daily_csv(path: Path) -> pd.DataFrame | None:
    """Load a wide-format daily prices CSV. Returns None on error or if missing."""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
    except (OSError, pd.errors.ParserError) as exc:
        print(f"  WARNING: Could not read {path.name}: {exc}")
        return None


def _merge_prepend(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> pd.DataFrame:
    """Prepend fetched rows that pre-date existing data, then deduplicate."""
    if existing is not None and not existing.empty:
        new_rows = fetched[fetched.index < existing.index.min()]
        combined = pd.concat([new_rows, existing])
    else:
        combined = fetched
    return combined.loc[~combined.index.duplicated(keep="last")].sort_index()


def load_or_extend_price_history(
    required_start: date, universe: list[str]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load daily adj-close, low, and high history, fetching from yfinance if needed.

    All three files are kept in sync. If any is missing or does not reach
    required_start, the missing period is fetched and all files are updated together.

    Args:
        required_start: Earliest date required (backtest_start minus warm-up).
        universe:       NSE symbols to fetch if history is insufficient.

    Returns:
        (adj_close_df, low_df, high_df) — wide-format DataFrames (index=date, columns=symbols).
    """
    existing_close = _load_daily_csv(DAILY_FILE)
    existing_low   = _load_daily_csv(DAILY_LOW_FILE)
    existing_high  = _load_daily_csv(DAILY_HIGH_FILE)

    def _covers(df: pd.DataFrame | None) -> bool:
        return df is not None and not df.empty and df.index.min().date() <= required_start

    if _covers(existing_close) and _covers(existing_low) and _covers(existing_high):
        print(f"Price history OK (earliest available: {existing_close.index.min().date()}).")  # type: ignore[union-attr]
        return existing_close, existing_low, existing_high  # type: ignore[return-value]

    if existing_close is not None and not existing_close.empty:
        earliest = existing_close.index.min().date()
        print(
            f"Price history starts {earliest}, need back to {required_start}. "
            f"Fetching {(earliest - required_start).days} additional calendar days…"
        )
        fetch_start = required_start - timedelta(days=7)
        fetch_end = earliest - timedelta(days=1)
    else:
        fetch_start = required_start - timedelta(days=7)
        fetch_end = date.today()
        print(
            f"No price history found. Fetching {len(universe)} symbols "
            f"from {fetch_start} (this may take 5–15 minutes)…"
        )

    fetched_close, fetched_low, fetched_high = _fetch_prices_from_yfinance(
        fetch_start, fetch_end, universe
    )

    if fetched_close.empty:
        if existing_close is not None and not existing_close.empty:
            print(
                "WARNING: Fetch failed; proceeding with existing history "
                "(backtest period may be truncated)."
            )
            return (
                existing_close,
                existing_low  if existing_low  is not None else pd.DataFrame(),
                existing_high if existing_high is not None else pd.DataFrame(),
            )
        print("ERROR: Could not fetch price history and no existing data found.")
        sys.exit(1)

    combined_close = _merge_prepend(existing_close, fetched_close)
    combined_low   = _merge_prepend(existing_low,   fetched_low)   if not fetched_low.empty   else (existing_low   or pd.DataFrame())
    combined_high  = _merge_prepend(existing_high,  fetched_high)  if not fetched_high.empty  else (existing_high  or pd.DataFrame())

    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    combined_close.to_csv(DAILY_FILE)
    if not combined_low.empty:
        combined_low.to_csv(DAILY_LOW_FILE)
    if not combined_high.empty:
        combined_high.to_csv(DAILY_HIGH_FILE)
    print(
        f"Extended price history saved "
        f"({DAILY_FILE.name}, {DAILY_LOW_FILE.name}, {DAILY_HIGH_FILE.name})."
    )

    return combined_close, combined_low, combined_high


def load_fundamentals() -> dict[str, FundamentalsEntry]:
    """Load the newest fundamentals JSON file from data/market/fundamentals/.

    Warns about look-ahead bias. Returns an empty dict if no file exists;
    rankings will be based on momentum factors only in that case.
    """
    files = sorted(FUNDAMENTALS_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            with open(f) as fh:
                data: dict[str, FundamentalsEntry] = json.load(fh)
            print(
                f"Loaded fundamentals from {f.name}.\n"
                "  NOTE: current P/B and ROE applied to all historical weeks "
                "(look-ahead bias — see module docstring)."
            )
            return data
        except (OSError, json.JSONDecodeError):
            continue

    print(
        "WARNING: No fundamentals file found. "
        "Rankings will use momentum factors only (quality/value weights inactive)."
    )
    return {}


def fetch_benchmark_history(start: date, end: date) -> pd.Series:
    """Fetch ^CNX250 daily close prices from yfinance for the backtest period.

    Returns a timezone-naive, date-indexed Series. Returns an empty Series on
    failure; benchmark comparison columns will be None throughout the output.
    """
    try:
        raw = yf.download(
            "^CNX250",
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
        )
        if raw.empty:
            print("WARNING: Benchmark fetch returned no data.")
            return pd.Series(dtype=float)

        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"].iloc[:, 0]
        else:
            close = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]

        close.index = pd.to_datetime(close.index).tz_localize(None)
        print(f"Benchmark (^CNX250): {len(close)} trading days fetched.")
        return close.sort_index()
    except Exception as exc:
        print(f"WARNING: Could not fetch benchmark history: {exc}")
        return pd.Series(dtype=float)


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

def compute_target_weights(rankings: pd.DataFrame) -> dict[str, float]:
    """Compute score-proportional target weights for the top TARGET_TOP_N stocks.

    Weights are capped iteratively at MAX_POSITION_WEIGHT, with excess
    redistributed proportionally to uncapped positions.
    """
    top = rankings.head(TARGET_TOP_N)
    if top.empty:
        return {}

    total_score = top["composite"].sum()
    weights = (
        top["composite"] / total_score
        if total_score > 0
        else pd.Series(1.0 / len(top), index=top.index)
    )

    for _ in range(len(top) + 1):
        over_mask = weights > MAX_POSITION_WEIGHT
        if not over_mask.any():
            break
        excess = (weights[over_mask] - MAX_POSITION_WEIGHT).sum()
        weights[over_mask] = MAX_POSITION_WEIGHT
        uncapped_mask = ~over_mask
        uncapped_total = weights[uncapped_mask].sum()
        if uncapped_total > 0:
            weights[uncapped_mask] += excess * (weights[uncapped_mask] / uncapped_total)

    return weights.to_dict()


def _compute_nav(state: dict[str, Any], prices: dict[str, float]) -> float:
    """Return total NAV: cash plus marked-to-market value of all holdings."""
    holdings_value = sum(
        info["shares"] * prices.get(sym, info["avg_cost"])
        for sym, info in state["holdings"].items()
    )
    return state["cash"] + holdings_value


def _execute_sell(
    state: dict[str, Any], symbol: str, price: float
) -> float:
    """Fully exit a position. Mutates state in-place. Returns gross proceeds."""
    holding = state["holdings"].pop(symbol, None)
    if holding is None:
        return 0.0
    gross = holding["shares"] * price
    net = gross * (1.0 - TRANSACTION_COST_PCT)
    state["cash"] += net
    return gross


def _trim_position(
    state: dict[str, Any], symbol: str, price: float, nav: float
) -> float:
    """Trim a position to MAX_POSITION_WEIGHT of NAV. Returns gross sell value."""
    holding = state["holdings"].get(symbol)
    if holding is None:
        return 0.0
    current_value = holding["shares"] * price
    target_value = nav * MAX_POSITION_WEIGHT
    if current_value <= target_value:
        return 0.0
    sell_value = current_value - target_value
    shares_to_sell = sell_value / price
    net = sell_value * (1.0 - TRANSACTION_COST_PCT)
    holding["shares"] -= shares_to_sell
    state["cash"] += net
    return sell_value


def _execute_buy(
    state: dict[str, Any],
    symbol: str,
    price: float,
    target_weight: float,
    nav: float,
) -> float:
    """Buy toward target_weight of NAV. Returns gross buy value (0 if skipped).

    Capped at available cash. Skipped if resulting trade < MIN_BUY_VALUE.
    Averages into an existing position if the symbol is already held.
    """
    if price <= 0:
        return 0.0

    current_shares = state["holdings"].get(symbol, {}).get("shares", 0.0)
    current_value = current_shares * price
    buy_value = nav * target_weight - current_value

    if buy_value <= 0:
        return 0.0

    # Cap at available cash (gross spend = buy_value + transaction cost).
    max_buy = state["cash"] / (1.0 + TRANSACTION_COST_PCT)
    buy_value = min(buy_value, max_buy)

    if buy_value < MIN_BUY_VALUE:
        return 0.0

    net_spend = buy_value * (1.0 + TRANSACTION_COST_PCT)
    shares_bought = buy_value / price
    state["cash"] -= net_spend

    if symbol in state["holdings"]:
        h = state["holdings"][symbol]
        total_shares = h["shares"] + shares_bought
        h["avg_cost"] = (h["avg_cost"] * h["shares"] + price * shares_bought) / total_shares
        h["shares"] = total_shares
    else:
        state["holdings"][symbol] = {"shares": shares_bought, "avg_cost": price}

    return buy_value


def _next_trading_day_low(
    daily_low: pd.DataFrame,
    as_of_ts: pd.Timestamp,
    symbol: str,
) -> float | None:
    """Return the daily low for symbol on the first trading day after as_of_ts.

    Used for sell / trim execution price (worst intra-day level = conservative fill).
    Returns None when next-day data is unavailable; caller falls back to Friday close.
    """
    if daily_low.empty or symbol not in daily_low.columns:
        return None
    future = daily_low[daily_low.index > as_of_ts]
    if future.empty:
        return None
    val = future[symbol].iloc[0]
    return float(val) if pd.notna(val) and float(val) > 0 else None


def _next_trading_day_high(
    daily_high: pd.DataFrame,
    as_of_ts: pd.Timestamp,
    symbol: str,
) -> float | None:
    """Return the daily high for symbol on the first trading day after as_of_ts.

    Used for buy execution price (worst intra-day level for a buyer = conservative fill).
    Returns None when next-day data is unavailable; caller falls back to Friday close.
    """
    if daily_high.empty or symbol not in daily_high.columns:
        return None
    future = daily_high[daily_high.index > as_of_ts]
    if future.empty:
        return None
    val = future[symbol].iloc[0]
    return float(val) if pd.notna(val) and float(val) > 0 else None


def _next_trading_day_close(
    daily_prices: pd.DataFrame,
    as_of_ts: pd.Timestamp,
    symbol: str,
) -> float | None:
    """Return the adj-close for symbol on the first trading day after as_of_ts.

    Used to mark portfolio NAV after trades have been executed, keeping execution
    price and NAV valuation on the same trading day.
    Returns None when next-day data is unavailable; caller falls back to Friday close.
    """
    if daily_prices.empty or symbol not in daily_prices.columns:
        return None
    future = daily_prices[daily_prices.index > as_of_ts]
    if future.empty:
        return None
    val = future[symbol].iloc[0]
    return float(val) if pd.notna(val) and float(val) > 0 else None


def _benchmark_level_at(
    benchmark: pd.Series, timestamp: pd.Timestamp
) -> float | None:
    """Return the benchmark level on or before timestamp, or None if unavailable."""
    if benchmark.empty:
        return None
    available = benchmark[benchmark.index <= timestamp]
    return float(available.iloc[-1]) if not available.empty else None


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(
    num_months: int,
    daily_prices: pd.DataFrame,
    daily_low: pd.DataFrame,
    daily_high: pd.DataFrame,
    fundamentals: dict[str, FundamentalsEntry],
    benchmark_series: pd.Series,
) -> tuple[list[BacktestWeekResult], list[BacktestTradeEntry], dict[int, float]]:
    """Simulate the strategy week-by-week.

    Execution prices:
      - Sells / trims : next trading day's low  (worst fill for seller — conservative)
      - Buys          : next trading day's high (worst fill for buyer  — conservative)
      - Fallback      : Friday close when next-day data is unavailable

    NAV marking: next trading day's adj-close so execution price and valuation
    are on the same trading day.

    Returns:
        (weekly_results, trade_log, realized_pnl_by_year)
    """
    today = date.today()
    backtest_start = (pd.Timestamp.today() - pd.DateOffset(months=num_months)).date()
    weekly_dates = pd.date_range(start=backtest_start, end=today, freq="W-FRI")

    state: dict[str, Any] = {
        "cash": INITIAL_CAPITAL,
        "holdings": {},
        "realized_pnl_by_year": {},
    }
    results: list[BacktestWeekResult] = []
    trades: list[BacktestTradeEntry] = []
    prev_nav: float | None = None
    bm_start_level: float | None = None

    print(f"\nRunning {num_months}-month backtest: {backtest_start} → {today}")
    print(f"{'Week':<12} {'NAV (INR)':>12} {'Wk Ret':>8} {'Cum Ret':>9} {'Holdings':>9}")
    print("-" * 55)

    for week_ts in weekly_dates:
        prices_window = daily_prices[daily_prices.index <= week_ts]
        if prices_window.empty:
            continue

        # Friday close — used for rankings, weight checks, and nav_before.
        current_prices: dict[str, float] = {
            sym: float(val)
            for sym, val in prices_window.iloc[-1].items()
            if pd.notna(val) and float(val) > 0
        }

        # Cap to last 300 rows: momentum/MA algorithms need at most ~300 trading days.
        ranking_window = prices_window.iloc[max(0, len(prices_window) - 300):]
        rankings, _ = compute_rankings(fundamentals, ranking_window)

        nav_before = _compute_nav(state, current_prices)
        week_sell_gross: float = 0.0
        week_buy_gross: float = 0.0
        num_sells = 0
        num_buys = 0
        year = week_ts.year

        # --- Sells: rank > SELL_RANK_THRESHOLD. Execution at next-day low. ---
        if not rankings.empty:
            for sym in list(state["holdings"].keys()):
                if sym not in rankings.index:
                    continue
                if int(rankings.loc[sym, "rank"]) > SELL_RANK_THRESHOLD:
                    sell_price = (
                        _next_trading_day_low(daily_low, week_ts, sym)
                        or current_prices.get(sym)
                    )
                    if sell_price is None:
                        continue
                    h = state["holdings"][sym]
                    avg_cost = h["avg_cost"]
                    realized_pnl = (sell_price - avg_cost) * h["shares"]
                    state["realized_pnl_by_year"][year] = (
                        state["realized_pnl_by_year"].get(year, 0.0) + realized_pnl
                    )
                    gross = _execute_sell(state, sym, sell_price)
                    week_sell_gross += gross
                    num_sells += 1
                    trades.append(BacktestTradeEntry(
                        week_date=week_ts.date().isoformat(),
                        action="SELL",
                        symbol=sym,
                        shares=round(h["shares"], 6),
                        execution_price=round(sell_price, 4),
                        gross_value=round(gross, 4),
                        transaction_cost=round(gross * TRANSACTION_COST_PCT, 4),
                        avg_cost_before=round(avg_cost, 4),
                        realized_pnl=round(realized_pnl, 4),
                    ))

        # --- Trims: weight > MAX_POSITION_WEIGHT. Execution at next-day low. ---
        nav_post_sells = _compute_nav(state, current_prices)
        for sym in list(state["holdings"].keys()):
            trim_price = (
                _next_trading_day_low(daily_low, week_ts, sym)
                or current_prices.get(sym)
            )
            if trim_price is None:
                continue
            h = state["holdings"][sym]
            current_value = h["shares"] * trim_price
            if current_value / nav_post_sells <= MAX_POSITION_WEIGHT:
                continue
            shares_to_sell = (current_value - nav_post_sells * MAX_POSITION_WEIGHT) / trim_price
            realized_pnl = (trim_price - h["avg_cost"]) * shares_to_sell
            state["realized_pnl_by_year"][year] = (
                state["realized_pnl_by_year"].get(year, 0.0) + realized_pnl
            )
            gross = _trim_position(state, sym, trim_price, nav_post_sells)
            week_sell_gross += gross
            trades.append(BacktestTradeEntry(
                week_date=week_ts.date().isoformat(),
                action="TRIM",
                symbol=sym,
                shares=round(shares_to_sell, 6),
                execution_price=round(trim_price, 4),
                gross_value=round(gross, 4),
                transaction_cost=round(gross * TRANSACTION_COST_PCT, 4),
                avg_cost_before=round(h["avg_cost"], 4),
                realized_pnl=round(realized_pnl, 4),
            ))

        # --- Buys: top TARGET_TOP_N not at target weight. Execution at next-day high. ---
        nav_pre_buy = _compute_nav(state, current_prices)
        if not rankings.empty:
            target_weights = compute_target_weights(rankings)
            for sym, weight in sorted(target_weights.items(), key=lambda x: x[1], reverse=True):
                buy_price = (
                    _next_trading_day_high(daily_high, week_ts, sym)
                    or current_prices.get(sym)
                )
                if buy_price is None:
                    continue
                shares_before = state["holdings"].get(sym, {}).get("shares", 0.0)
                gross = _execute_buy(state, sym, buy_price, weight, nav_pre_buy)
                if gross > 0:
                    shares_bought = state["holdings"][sym]["shares"] - shares_before
                    week_buy_gross += gross
                    num_buys += 1
                    trades.append(BacktestTradeEntry(
                        week_date=week_ts.date().isoformat(),
                        action="BUY",
                        symbol=sym,
                        shares=round(shares_bought, 6),
                        execution_price=round(buy_price, 4),
                        gross_value=round(gross, 4),
                        transaction_cost=round(gross * TRANSACTION_COST_PCT, 4),
                        avg_cost_before=round(buy_price, 4),
                        realized_pnl=0.0,
                    ))

        # --- Mark NAV at next trading day's close (same day as execution). ---
        mark_prices = {
            sym: (
                _next_trading_day_close(daily_prices, week_ts, sym)
                or current_prices.get(sym, info["avg_cost"])
            )
            for sym, info in state["holdings"].items()
        }
        nav = _compute_nav(state, mark_prices)

        num_holdings = len(state["holdings"])
        cash_pct = (state["cash"] / nav * 100) if nav > 0 else 100.0
        week_cost = (week_sell_gross + week_buy_gross) * TRANSACTION_COST_PCT
        turnover_pct = (
            (week_sell_gross + week_buy_gross) / nav_before * 100
            if nav_before > 0 else 0.0
        )

        weekly_return_pct: float | None = (
            (nav / prev_nav - 1) * 100
            if prev_nav is not None and prev_nav > 0 else None
        )
        cumulative_return_pct = (nav / INITIAL_CAPITAL - 1) * 100

        bm_level = _benchmark_level_at(benchmark_series, week_ts)
        bm_weekly: float | None = None
        bm_cumulative: float | None = None
        active_weekly: float | None = None
        active_cumulative: float | None = None

        if bm_level is not None:
            if bm_start_level is None:
                bm_start_level = bm_level
            if bm_start_level and bm_start_level > 0:
                bm_cumulative = (bm_level / bm_start_level - 1) * 100
                active_cumulative = cumulative_return_pct - bm_cumulative

        if bm_level is not None and results:
            prev_bm = results[-1].get("benchmark_level")
            if prev_bm and float(prev_bm) > 0:
                bm_weekly = (bm_level / float(prev_bm) - 1) * 100
                if weekly_return_pct is not None:
                    active_weekly = weekly_return_pct - bm_weekly

        results.append(BacktestWeekResult(
            week_date=week_ts.date().isoformat(),
            portfolio_nav=round(nav, 2),
            weekly_return_pct=round(weekly_return_pct, 4) if weekly_return_pct is not None else None,
            cumulative_return_pct=round(cumulative_return_pct, 4),
            benchmark_level=round(bm_level, 2) if bm_level is not None else None,
            benchmark_weekly_return_pct=round(bm_weekly, 4) if bm_weekly is not None else None,
            benchmark_cumulative_return_pct=round(bm_cumulative, 4) if bm_cumulative is not None else None,
            active_return_weekly_pct=round(active_weekly, 4) if active_weekly is not None else None,
            active_return_cumulative_pct=round(active_cumulative, 4) if active_cumulative is not None else None,
            num_holdings=num_holdings,
            cash_pct=round(cash_pct, 2),
            num_buys=num_buys,
            num_sells=num_sells,
            turnover_pct=round(turnover_pct, 2),
            transaction_cost_inr=round(week_cost, 2),
        ))
        prev_nav = nav

        wk_str = f"{weekly_return_pct:+.2f}%" if weekly_return_pct is not None else "   N/A"
        print(
            f"{week_ts.strftime('%Y-%W'):<12} {nav:>12,.0f} "
            f"{wk_str:>8} {cumulative_return_pct:>+8.2f}% {num_holdings:>6} stocks"
        )

    return results, trades, state["realized_pnl_by_year"]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_results(
    results: list[BacktestWeekResult], num_months: int, output_dir: Path
) -> Path:
    """Write backtest results to a timestamped CSV. Returns the output path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"backtest_{date.today().strftime('%Y%m%d')}_{num_months}mo.csv"
    output_path = output_dir / filename

    fieldnames = [
        "week_date", "portfolio_nav", "weekly_return_pct", "cumulative_return_pct",
        "benchmark_level", "benchmark_weekly_return_pct", "benchmark_cumulative_return_pct",
        "active_return_weekly_pct", "active_return_cumulative_pct",
        "num_holdings", "cash_pct", "num_buys", "num_sells",
        "turnover_pct", "transaction_cost_inr",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k) for k in fieldnames})

    return output_path


def compute_annual_taxes(
    realized_pnl_by_year: dict[int, float],
    tax_rate_pct: float = TAX_RATE_PCT,
) -> list[BacktestAnnualTax]:
    """Compute annual tax liability based on realized gains only.

    Losses are accumulated and carried forward to offset future years' gains.
    Tax = max(0, gross_realized_gain - prior_carryforward) * tax_rate_pct / 100.

    This is purely informational — it does not modify NAV or return figures.

    Args:
        realized_pnl_by_year: Year → sum of realized P&L from all sells/trims.
        tax_rate_pct:         Flat rate applied to net taxable gain.

    Returns:
        One BacktestAnnualTax entry per calendar year with realized activity.
    """
    tax_rows: list[BacktestAnnualTax] = []
    carryforward: float = 0.0

    for year in sorted(realized_pnl_by_year):
        gross = realized_pnl_by_year[year]
        net = gross - carryforward
        if net > 0:
            liability = net * tax_rate_pct / 100.0
            new_carryforward = 0.0
        else:
            liability = 0.0
            new_carryforward = abs(net)

        tax_rows.append(BacktestAnnualTax(
            year=year,
            gross_realized_gain_inr=round(gross, 2),
            prior_loss_carryforward_inr=round(carryforward, 2),
            net_taxable_gain_inr=round(max(0.0, net), 2),
            tax_rate_pct=tax_rate_pct,
            tax_liability_inr=round(liability, 2),
            loss_to_carryforward_inr=round(new_carryforward, 2),
        ))
        carryforward = new_carryforward

    return tax_rows


def write_tax_results(
    tax_rows: list[BacktestAnnualTax], num_months: int, output_dir: Path
) -> Path:
    """Write annual tax liabilities to a separate CSV. Returns the output path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"backtest_{date.today().strftime('%Y%m%d')}_{num_months}mo_tax.csv"
    output_path = output_dir / filename

    fieldnames = [
        "year", "gross_realized_gain_inr", "prior_loss_carryforward_inr",
        "net_taxable_gain_inr", "tax_rate_pct", "tax_liability_inr",
        "loss_to_carryforward_inr",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(tax_rows)

    return output_path


def write_trade_log(
    trades: list[BacktestTradeEntry], num_months: int, output_dir: Path
) -> Path:
    """Write the per-trade log to a separate CSV. Returns the output path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"backtest_{date.today().strftime('%Y%m%d')}_{num_months}mo_trades.csv"
    output_path = output_dir / filename

    fieldnames = [
        "week_date", "action", "symbol", "shares",
        "execution_price", "gross_value", "transaction_cost",
        "avg_cost_before", "realized_pnl",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(trades)

    return output_path


_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def compute_monthly_returns(
    results: list[BacktestWeekResult],
) -> dict[str, dict[int, dict[str, float | None]]]:
    """Derive monthly returns from the weekly NAV series.

    For each calendar month, take the last available weekly NAV (and benchmark
    level). Monthly return = (last_this_month / last_prev_month − 1) × 100.

    Returns a dict with keys "portfolio" and "benchmark", each mapping
    year → {month_name: return_pct | None}.
    """
    if not results:
        return {"portfolio": {}, "benchmark": {}}

    # Index weekly results by YYYY-MM.
    by_month: dict[str, BacktestWeekResult] = {}
    for row in results:
        ym = row["week_date"][:7]  # "YYYY-MM"
        by_month[ym] = row          # last week in the month wins

    sorted_months = sorted(by_month)

    portfolio_monthly: dict[int, dict[str, float | None]] = {}
    benchmark_monthly: dict[int, dict[str, float | None]] = {}
    prev_nav: float | None = None
    prev_bm: float | None = None

    for ym in sorted_months:
        row = by_month[ym]
        year = int(ym[:4])
        month_idx = int(ym[5:7])
        month_name = _MONTH_NAMES[month_idx - 1]

        nav = row["portfolio_nav"]
        monthly_ret: float | None = (
            (nav / prev_nav - 1) * 100 if prev_nav is not None and prev_nav > 0 else None
        )
        portfolio_monthly.setdefault(year, {})[month_name] = (
            round(monthly_ret, 2) if monthly_ret is not None else None
        )
        prev_nav = nav

        bm = row.get("benchmark_level")
        bm_ret: float | None = (
            (float(bm) / prev_bm - 1) * 100
            if bm is not None and prev_bm is not None and prev_bm > 0
            else None
        )
        benchmark_monthly.setdefault(year, {})[month_name] = (
            round(bm_ret, 2) if bm_ret is not None else None
        )
        prev_bm = float(bm) if bm is not None else prev_bm

    # Add annual return column for each year.
    for series in (portfolio_monthly, benchmark_monthly):
        for year, months in series.items():
            valid = [v for v in months.values() if v is not None]
            if valid:
                annual = (
                    np.prod([1 + v / 100 for v in valid]) - 1
                ) * 100
                months["Annual"] = round(float(annual), 2)
            else:
                months["Annual"] = None

    return {"portfolio": portfolio_monthly, "benchmark": benchmark_monthly}


def write_monthly_returns(
    monthly_data: dict[str, dict[int, dict[str, float | None]]],
    num_months: int,
    output_dir: Path,
) -> Path:
    """Write the monthly returns matrix CSV. Returns the output path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"backtest_{date.today().strftime('%Y%m%d')}_{num_months}mo_monthly.csv"
    output_path = output_dir / filename

    col_order = _MONTH_NAMES + ["Annual"]
    fieldnames = ["series", "year"] + col_order

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for series_name, by_year in monthly_data.items():
            for year in sorted(by_year):
                row: dict[str, Any] = {"series": series_name, "year": year}
                row.update(by_year[year])
                writer.writerow(row)

    return output_path


def _print_monthly_returns(
    monthly_data: dict[str, dict[int, dict[str, float | None]]],
) -> None:
    """Print the portfolio monthly return matrix to stdout."""
    portfolio = monthly_data.get("portfolio", {})
    if not portfolio:
        return

    col_order = _MONTH_NAMES + ["Annual"]
    header = f"{'Year':<6}" + "".join(f"{m:>7}" for m in col_order)

    print()
    print("=" * len(header))
    print("MONTHLY RETURNS — Portfolio (%)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for year in sorted(portfolio):
        months = portfolio[year]
        row_str = f"{year:<6}"
        for m in col_order:
            v = months.get(m)
            row_str += f"{v:>+7.1f}" if v is not None else f"{'':>7}"
        print(row_str)


def _print_tax_summary(tax_rows: list[BacktestAnnualTax]) -> None:
    """Print the annual tax table."""
    print()
    print("=" * 70)
    print(f"ANNUAL TAX SUMMARY  ({TAX_RATE_PCT:.0f}% flat rate on net realized gains)")
    print("=" * 70)
    print(
        f"{'Year':<6} {'Realized Gain':>14} {'Carryforward':>13} "
        f"{'Taxable':>12} {'Tax Due':>10} {'Carry Next':>11}"
    )
    print("-" * 70)
    total_tax = 0.0
    for row in tax_rows:
        print(
            f"{row['year']:<6} "
            f"{row['gross_realized_gain_inr']:>+14,.0f} "
            f"{row['prior_loss_carryforward_inr']:>13,.0f} "
            f"{row['net_taxable_gain_inr']:>12,.0f} "
            f"{row['tax_liability_inr']:>10,.0f} "
            f"{row['loss_to_carryforward_inr']:>11,.0f}"
        )
        total_tax += row["tax_liability_inr"]
    print("-" * 70)
    print(f"{'Total tax liability':>57} {total_tax:>10,.0f}")
    print()
    print(
        "  NOTE: Tax is based on realized gains/losses only (sells + trims).\n"
        "  Losses carry forward to offset future years. Does not affect NAV figures."
    )


def _print_summary(results: list[BacktestWeekResult]) -> None:
    """Print a concise performance summary after the simulation completes."""
    if not results:
        print("No results to summarise.")
        return

    navs = [r["portfolio_nav"] for r in results]
    weekly_returns = [
        r["weekly_return_pct"] for r in results if r.get("weekly_return_pct") is not None
    ]

    final_nav = navs[-1]
    total_return_pct = (final_nav / INITIAL_CAPITAL - 1) * 100
    total_return_inr = final_nav - INITIAL_CAPITAL
    num_weeks = len(results)
    num_years = num_weeks / 52.0
    cagr = ((final_nav / INITIAL_CAPITAL) ** (1.0 / num_years) - 1) * 100 if num_years > 0 else None

    # Max drawdown.
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for nav in navs:
        peak = max(peak, nav)
        max_dd = max(max_dd, (peak - nav) / peak * 100)

    # --- Risk-adjusted return metrics ---
    rf_weekly = RISK_FREE_RATE_ANNUAL_PCT / 52.0 / 100.0
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    info_ratio: float | None = None

    if len(weekly_returns) > 1:
        wr = np.array(weekly_returns)
        mean_wk = float(np.mean(wr))
        std_wk = float(np.std(wr, ddof=1))
        if std_wk > 0:
            sharpe = (mean_wk - rf_weekly) / std_wk * np.sqrt(52)
        downside = wr[wr < 0]
        if len(downside) > 1:
            std_down = float(np.std(downside, ddof=1))
            if std_down > 0:
                sortino = (mean_wk - rf_weekly) / std_down * np.sqrt(52)
        if max_dd > 0 and cagr is not None:
            calmar = cagr / max_dd

    active_returns = [
        r["active_return_weekly_pct"]
        for r in results
        if r.get("active_return_weekly_pct") is not None
    ]
    if len(active_returns) > 1:
        ar = np.array(active_returns)
        std_ar = float(np.std(ar, ddof=1))
        if std_ar > 0:
            info_ratio = float(np.mean(ar)) / std_ar * np.sqrt(52)

    # --- Win / loss stats ---
    wins = [r for r in weekly_returns if r > 0]
    losses = [r for r in weekly_returns if r < 0]
    win_rate = len(wins) / len(weekly_returns) * 100 if weekly_returns else None
    avg_win = float(np.mean(wins)) if wins else None
    avg_loss = float(np.mean(losses)) if losses else None

    # Max consecutive losing weeks.
    max_consec_loss = 0
    cur_consec = 0
    for r in weekly_returns:
        if r < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    bm_cum = results[-1].get("benchmark_cumulative_return_pct")
    bm_cagr: float | None = None
    if bm_cum is not None and num_years > 0:
        bm_cagr = ((1 + bm_cum / 100) ** (1.0 / num_years) - 1) * 100
    active_cum = results[-1].get("active_return_cumulative_pct")

    total_txn_cost = sum(r.get("transaction_cost_inr", 0.0) for r in results)
    avg_turnover = float(np.mean([r.get("turnover_pct", 0.0) for r in results]))

    def _fmt(v: float | None, sfx: str = "", decimals: int = 2) -> str:
        return f"{v:.{decimals}f}{sfx}" if v is not None else "N/A"

    print()
    print("=" * 66)
    print("BACKTEST SUMMARY")
    print("=" * 66)
    print(f"  Period:          {results[0]['week_date']} → {results[-1]['week_date']}")
    print(f"  Weeks simulated: {num_weeks}")
    print()
    print(f"{'Metric':<34} {'Portfolio':>12} {'Benchmark':>12} {'Active':>8}")
    print("-" * 66)
    print(
        f"{'Total return':<34} {_fmt(total_return_pct, '%'):>12} "
        f"{_fmt(bm_cum, '%'):>12} {_fmt(active_cum, '%'):>8}"
    )
    print(f"{'CAGR':<34} {_fmt(cagr, '%'):>12} {_fmt(bm_cagr, '%'):>12}")
    print(f"{'Max drawdown':<34} {_fmt(-max_dd, '%'):>12}")
    print(f"{'Sharpe (ann., rf={RISK_FREE_RATE_ANNUAL_PCT:.1f}%)':<34} {_fmt(sharpe):>12}")
    print(f"{'Sortino (ann.)':<34} {_fmt(sortino):>12}")
    print(f"{'Calmar (CAGR/MaxDD)':<34} {_fmt(calmar):>12}")
    print(f"{'Information ratio':<34} {_fmt(info_ratio):>12}")
    print()
    print(f"{'Win rate':<34} {_fmt(win_rate, '%'):>12}")
    print(f"{'Avg weekly win':<34} {_fmt(avg_win, '%'):>12}")
    print(f"{'Avg weekly loss':<34} {_fmt(avg_loss, '%'):>12}")
    print(f"{'Max consec. losing weeks':<34} {max_consec_loss:>12}")
    print()
    print(f"  Final NAV:               INR {final_nav:>10,.2f}")
    print(f"  Total P&L:               INR {total_return_inr:>+10,.2f}")
    print(f"  Total transaction costs: INR {total_txn_cost:>10,.2f}")
    print(f"  Avg weekly turnover:     {avg_turnover:.1f}%")
    print()
    print(
        "  ⚠  Results use current fundamentals (look-ahead bias) and current\n"
        "     Nifty 250 universe (survivorship bias). Treat with caution."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _prompt_months() -> int:
    """Interactively prompt for the number of backtest months, validating the input."""
    while True:
        raw = input(f"How many months to backtest? [1–{MAX_MONTHS}]: ").strip()
        try:
            n = int(raw)
            if 1 <= n <= MAX_MONTHS:
                return n
            print(f"  Please enter a number between 1 and {MAX_MONTHS}.")
        except ValueError:
            print("  Please enter a whole number (e.g. 18).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest the nifty_agent strategy over a historical period.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "LIMITATIONS: uses current fundamentals (look-ahead bias) and current\n"
            "Nifty 250 universe (survivorship bias). See module docstring for details."
        ),
    )
    parser.add_argument(
        "--months", type=int, metavar="N",
        help=f"Number of months to backtest (1–{MAX_MONTHS}). Prompts if omitted.",
    )
    args = parser.parse_args()

    if args.months is not None:
        if not (1 <= args.months <= MAX_MONTHS):
            parser.error(f"--months must be between 1 and {MAX_MONTHS}")
        num_months = args.months
    else:
        num_months = _prompt_months()

    today = date.today()
    backtest_start = (pd.Timestamp.today() - pd.DateOffset(months=num_months)).date()
    data_start = backtest_start - timedelta(days=MA_WARMUP_DAYS)

    print(f"\nBacktest configuration:")
    print(f"  Period:       {backtest_start} → {today} ({num_months} months)")
    print(f"  Data needed:  from {data_start} ({MA_WARMUP_DAYS}-day warm-up for MA/momentum)")

    universe = load_universe()
    print(f"  Universe:     {len(universe)} symbols")

    daily_prices, daily_low, daily_high = load_or_extend_price_history(data_start, universe)
    fundamentals = load_fundamentals()

    print("Fetching benchmark history (^CNX250)…")
    benchmark_series = fetch_benchmark_history(backtest_start - timedelta(days=7), today)

    results, trades, realized_pnl_by_year = run_backtest(
        num_months, daily_prices, daily_low, daily_high, fundamentals, benchmark_series
    )

    if not results:
        print(
            "ERROR: No results generated. "
            "Ensure price data covers the requested period."
        )
        sys.exit(1)

    monthly_data = compute_monthly_returns(results)
    tax_rows = compute_annual_taxes(realized_pnl_by_year)

    results_path  = write_results(results, num_months, BACKTEST_RESULTS_DIR)
    trades_path   = write_trade_log(trades, num_months, BACKTEST_RESULTS_DIR)
    monthly_path  = write_monthly_returns(monthly_data, num_months, BACKTEST_RESULTS_DIR)
    tax_path      = write_tax_results(tax_rows, num_months, BACKTEST_RESULTS_DIR)

    _print_summary(results)
    _print_monthly_returns(monthly_data)
    _print_tax_summary(tax_rows)

    print(f"\nOutput files:")
    print(f"  Weekly results:  {results_path}")
    print(f"  Trade log:       {trades_path}")
    print(f"  Monthly returns: {monthly_path}")
    print(f"  Tax summary:     {tax_path}")


if __name__ == "__main__":
    main()
