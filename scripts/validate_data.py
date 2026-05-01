"""Gate check before the agent reasons over data.

Prints PASS or a list of WARNING/ERROR lines.
Exits with code 1 if any blocking errors are found.

Blocking errors (exit 1):
  - No prices file for the current week
  - Prices file older than 7 days
  - Any held stock has missing price in the latest prices file

Non-blocking warnings (printed but session continues):
  - Benchmark older than 7 days or missing
  - Universe older than 30 days
  - Any held stock no longer in universe (delisted/removed)
  - Any stock with >40% single-week price move (potential corporate action)
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
BENCHMARK_FILE = DATA_DIR / "benchmark.csv"

PRICES_MAX_AGE_DAYS = 7
BENCHMARK_MAX_AGE_DAYS = 7
UNIVERSE_MAX_AGE_DAYS = 30
LARGE_MOVE_THRESHOLD = 0.40


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def latest_prices_file() -> Path | None:
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    for f in files:
        return f
    return None


def load_portfolio() -> dict:
    if not PORTFOLIO_FILE.exists():
        return {"holdings": [], "current_cash": 0}
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


def load_universe_symbols() -> set:
    if not UNIVERSE_FILE.exists():
        return set()
    df = pd.read_csv(UNIVERSE_FILE)
    return set(df["symbol"].str.strip().tolist())


def main():
    errors = []
    warnings = []
    today = date.today()
    week_str = iso_week_str(today)

    prices_file = PRICES_DIR / f"{week_str}.csv"
    if not prices_file.exists():
        prices_file = latest_prices_file()
        if prices_file is None:
            errors.append("No prices file found in data/prices/. Run fetch_prices.py.")
        else:
            warnings.append(f"No prices file for current week {week_str}. Using latest: {prices_file.name}")

    if prices_file and prices_file.exists():
        age = datetime.now() - datetime.fromtimestamp(prices_file.stat().st_mtime)
        if age > timedelta(days=PRICES_MAX_AGE_DAYS):
            errors.append(f"Prices file {prices_file.name} is {age.days} days old (>{PRICES_MAX_AGE_DAYS}). Refresh required.")

    portfolio = load_portfolio()
    held_symbols = [h["symbol"] for h in portfolio.get("holdings", [])]

    if held_symbols and prices_file and prices_file.exists():
        try:
            prices_df = pd.read_csv(prices_file)
            prices_syms = set(prices_df["symbol"].unique())
            missing_held = [s for s in held_symbols if s not in prices_syms]
            if missing_held:
                errors.append(f"BLOCKING: Held stocks with no price data: {missing_held}. Cannot rebalance safely.")
        except Exception as e:
            errors.append(f"Could not read prices file: {e}")

    if not BENCHMARK_FILE.exists():
        warnings.append("data/benchmark.csv not found. Benchmark comparison unavailable this session.")
    else:
        bm_df = pd.read_csv(BENCHMARK_FILE)
        if bm_df.empty:
            warnings.append("benchmark.csv is empty.")
        else:
            last_bm_date = pd.to_datetime(bm_df["date"].max()).date()
            bm_age = (today - last_bm_date).days
            if bm_age > BENCHMARK_MAX_AGE_DAYS:
                warnings.append(f"Benchmark data is {bm_age} days old. Run fetch_benchmark.py.")

    if not UNIVERSE_FILE.exists():
        warnings.append("data/universe.csv not found.")
    else:
        uni_age = (datetime.now() - datetime.fromtimestamp(UNIVERSE_FILE.stat().st_mtime)).days
        if uni_age > UNIVERSE_MAX_AGE_DAYS:
            warnings.append(f"universe.csv is {uni_age} days old (>{UNIVERSE_MAX_AGE_DAYS}). Consider running fetch_universe.py --force.")

        universe_symbols = load_universe_symbols()
        if held_symbols and universe_symbols:
            not_in_universe = [s for s in held_symbols if s not in universe_symbols]
            if not_in_universe:
                warnings.append(f"Held stocks no longer in universe (delisted/removed?): {not_in_universe}")

    if prices_file and prices_file.exists() and held_symbols:
        try:
            prices_df = pd.read_csv(prices_file)
            prices_df["date"] = pd.to_datetime(prices_df["date"])
            for sym in held_symbols:
                sym_df = prices_df[prices_df["symbol"] == sym].sort_values("date")
                if len(sym_df) < 2:
                    continue
                first_close = sym_df["close"].iloc[0]
                last_close = sym_df["close"].iloc[-1]
                if first_close > 0:
                    move = abs(last_close - first_close) / first_close
                    if move > LARGE_MOVE_THRESHOLD:
                        warnings.append(
                            f"LARGE MOVE: {sym} moved {move:.1%} this week (open {first_close:.2f} → close {last_close:.2f}). "
                            "Check for corporate action before trading."
                        )
        except Exception as e:
            warnings.append(f"Could not check for large moves: {e}")

    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  ⚠  {w}")

    if errors:
        print("ERRORS (blocking):")
        for e in errors:
            print(f"  ✗  {e}")
        print("\nVALIDATION FAILED — do not proceed with rebalance.")
        sys.exit(1)

    if not warnings:
        print("PASS: All data quality checks passed.")
    else:
        print("\nPASS (with warnings): Proceed with caution. Review warnings above.")


if __name__ == "__main__":
    main()
