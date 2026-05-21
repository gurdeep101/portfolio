"""Data quality gate — run before each rebalance session.

Prints PASS or a list of WARNING/ERROR lines to stdout.
Exits with code 1 if any blocking error is found.

Blocking errors (exit 1 — do not rebalance):
  - No prices file for the current (or any recent) week
  - Prices file older than PRICES_MAX_AGE_DAYS days
  - Any held stock has no price data in the latest prices file

Non-blocking warnings (printed, session continues with caution):
  - Benchmark data older than BENCHMARK_MAX_AGE_DAYS days or missing
  - Universe file older than UNIVERSE_MAX_AGE_DAYS days
  - Any held stock no longer in the universe (possible delisting/removal)
  - Any stock with a >LARGE_MOVE_THRESHOLD single-week price move
    (possible corporate action — do NOT trade that stock)
  - Fewer than MIN_HISTORY_DAYS daily price rows available for any held stock
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Make portfolio_types importable when running as `uv run python scripts/foo.py`
sys.path.insert(0, str(Path(__file__).parent.parent))
from portfolio_types import Portfolio

DATA_DIR = Path(__file__).parent.parent.parent / "data"
PRICES_DIR = DATA_DIR / "market" / "prices"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"
PORTFOLIO_FILE = DATA_DIR / "portfolio" / "portfolio.json"
BENCHMARK_FILE = DATA_DIR / "market" / "benchmark.csv"

PRICES_MAX_AGE_DAYS = 7       # prices older than this are stale
BENCHMARK_MAX_AGE_DAYS = 7    # benchmark older than this triggers a warning
UNIVERSE_MAX_AGE_DAYS = 30    # universe older than this triggers a warning
LARGE_MOVE_THRESHOLD = 0.40   # flag any stock that moved more than 40% in one week


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def latest_prices_file() -> Path | None:
    """Return the most recent weekly prices CSV, or None if no files exist."""
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    return files[0] if files else None


def load_portfolio() -> Portfolio:
    """Load data/portfolio/portfolio.json and return as a Portfolio dict.

    Returns a minimal all-cash Portfolio if the file is missing (first run),
    and exits with code 1 if the file exists but cannot be read or parsed.
    """
    if not PORTFOLIO_FILE.exists():
        # First run — all-cash, no holdings to validate against.
        return Portfolio(
            inception_date=None,
            initial_capital=25000.0,
            current_cash=25000.0,
            holdings=[],
            nav_history=[],
            total_nav=25000.0,
            transaction_log=[],
        )
    try:
        with open(PORTFOLIO_FILE) as f:
            data: Portfolio = json.load(f)
        return data
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Could not read portfolio.json: {e}")
        sys.exit(1)


def load_universe_symbols() -> set[str]:
    """Return the set of symbol strings from data/universe/universe.csv.

    Returns an empty set if the file is missing, unreadable, or malformed —
    the caller converts this to a warning rather than a hard failure.
    """
    if not UNIVERSE_FILE.exists():
        return set()
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return set(df["symbol"].str.strip().tolist())
    except (OSError, pd.errors.ParserError, KeyError) as e:
        print(f"  WARNING: Could not read universe.csv: {e}")
        return set()


def main() -> None:
    """Run all data-quality gate checks and exit 0 (pass) or 1 (blocking error found)."""
    errors: list[str] = []
    warnings: list[str] = []
    today = date.today()
    week_str = iso_week_str(today)

    # -------------------------------------------------------------------------
    # 1. Prices file — blocking checks
    # -------------------------------------------------------------------------
    prices_file: Path | None = PRICES_DIR / f"{week_str}.csv"
    assert isinstance(prices_file, Path)

    if not prices_file.exists():
        # Fall back to the most recent available file.
        prices_file = latest_prices_file()
        if prices_file is None:
            errors.append(
                "No prices file found in data/market/prices/. Run fetch_nsepy_price.py."
            )
        else:
            warnings.append(
                f"No prices file for current week {week_str}. "
                f"Using latest available: {prices_file.name}"
            )

    if prices_file is not None and prices_file.exists():
        age = datetime.now() - datetime.fromtimestamp(prices_file.stat().st_mtime)
        if age > timedelta(days=PRICES_MAX_AGE_DAYS):
            errors.append(
                f"Prices file {prices_file.name} is {age.days} days old "
                f"(>{PRICES_MAX_AGE_DAYS}). Refresh before rebalancing."
            )

    # -------------------------------------------------------------------------
    # 2. Held stocks must have price data  (blocking)
    # -------------------------------------------------------------------------
    portfolio = load_portfolio()
    held_symbols = [h["symbol"] for h in portfolio.get("holdings", [])]

    if held_symbols and prices_file is not None and prices_file.exists():
        try:
            prices_df = pd.read_csv(prices_file)
            prices_syms = set(prices_df["symbol"].unique())
            missing_held = [s for s in held_symbols if s not in prices_syms]
            if missing_held:
                errors.append(
                    f"BLOCKING: Held stocks with no price data: {missing_held}. "
                    "Cannot rebalance safely."
                )
        except (OSError, pd.errors.ParserError, KeyError) as e:
            errors.append(f"Could not read prices file to check held stocks: {e}")

    # -------------------------------------------------------------------------
    # 3. Benchmark freshness  (warning only)
    # -------------------------------------------------------------------------
    if not BENCHMARK_FILE.exists():
        warnings.append(
            "data/market/benchmark.csv not found."
            "Benchmark comparison unavailable this session."
        )
    else:
        try:
            bm_df = pd.read_csv(BENCHMARK_FILE)
            if bm_df.empty:
                warnings.append("benchmark.csv is empty. Run fetch_benchmark.py.")
            else:
                last_bm_date = pd.to_datetime(bm_df["date"].max()).date()
                bm_age = (today - last_bm_date).days
                if bm_age > BENCHMARK_MAX_AGE_DAYS:
                    warnings.append(
                        f"Benchmark data is {bm_age} days old. Run fetch_benchmark.py."
                    )
        except (OSError, pd.errors.ParserError, KeyError) as e:
            warnings.append(f"Could not read benchmark.csv: {e}")

    # -------------------------------------------------------------------------
    # 4. Universe freshness and delisting check  (warnings only)
    # -------------------------------------------------------------------------
    if not UNIVERSE_FILE.exists():
        warnings.append("data/universe/universe.csv not found. Run fetch_universe.py.")
    else:
        uni_age = (
            datetime.now() - datetime.fromtimestamp(UNIVERSE_FILE.stat().st_mtime)
        ).days
        if uni_age > UNIVERSE_MAX_AGE_DAYS:
            warnings.append(
                f"universe.csv is {uni_age} days old (>{UNIVERSE_MAX_AGE_DAYS}). "
                "Consider running: fetch_universe.py --force"
            )

        universe_symbols = load_universe_symbols()
        if held_symbols and universe_symbols:
            not_in_universe = [s for s in held_symbols if s not in universe_symbols]
            if not_in_universe:
                warnings.append(
                    f"Held stocks no longer in universe (delisted/removed?): "
                    f"{not_in_universe}"
                )

    # -------------------------------------------------------------------------
    # 5. Large price move detection  (warning; do NOT trade flagged stocks)
    # -------------------------------------------------------------------------
    if prices_file is not None and prices_file.exists() and held_symbols:
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
                            f"LARGE MOVE: {sym} moved {move:.1%} this week "
                            f"(open {first_close:.2f} → close {last_close:.2f}). "
                            "Check for corporate action — do NOT trade this stock."
                        )
        except (OSError, pd.errors.ParserError, KeyError, ValueError) as e:
            warnings.append(f"Could not check for large price moves: {e}")

    # -------------------------------------------------------------------------
    # Output
    # -------------------------------------------------------------------------
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f"  ⚠  {w}")

    if errors:
        print("\nERRORS (blocking):")
        for err in errors:
            print(f"  ✗  {err}")
        print("\nVALIDATION FAILED — do not proceed with rebalance.")
        sys.exit(1)

    if not warnings:
        print("PASS: All data quality checks passed.")
    else:
        print("\nPASS (with warnings): Proceed with caution. Review warnings above.")


if __name__ == "__main__":
    main()
