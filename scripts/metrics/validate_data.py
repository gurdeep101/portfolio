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
  - Price completeness gaps: universe symbols with missing data in daily_adj_close.csv
  - Benchmark completeness gaps: trading days without benchmark coverage

Flags:
  --report  Print full per-symbol gap detail and all missing benchmark dates.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# Make shared/ importable when running as `uv run python scripts/metrics/validate_data.py`
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.types import Portfolio

DATA_DIR = Path(__file__).parent.parent.parent / "data"
PRICES_DIR = DATA_DIR / "market" / "prices"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"
PORTFOLIO_FILE = DATA_DIR / "portfolio" / "portfolio.json"
BENCHMARK_FILE = DATA_DIR / "market" / "benchmark.csv"

PRICES_MAX_AGE_DAYS = 7       # prices older than this are stale
BENCHMARK_MAX_AGE_DAYS = 7    # benchmark older than this triggers a warning
UNIVERSE_MAX_AGE_DAYS = 30    # universe older than this triggers a warning
LARGE_MOVE_THRESHOLD = 0.40   # flag any stock that moved more than 40% in one week

# Nifty LargeMidcap 250 index base date (base value = 1000). NSE backfilled to
# this date; no benchmark data exists before it in any source.
BENCHMARK_BASE_DATE = date(2005, 4, 1)

# Completeness report: show this many top gap symbols in the summary line
TOP_GAP_SYMBOLS = 10
# Completeness report: show this many sample missing dates per symbol in --report mode
SAMPLE_DATES = 5


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


def load_daily_adj_close() -> pd.DataFrame | None:
    """Load data/market/prices/daily_adj_close.csv (wide format: date index, symbol columns).

    Returns None if the file is missing. Exits with code 1 on parse error.
    The file's date index already has weekends and NSE holidays stripped by
    fetch_nsepy_price.py, so every row is a confirmed NSE trading day.
    """
    if not DAILY_FILE.exists():
        return None
    try:
        df = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).normalize()
        return df
    except (OSError, pd.errors.ParserError) as e:
        print(f"ERROR: Could not read daily_adj_close.csv: {e}")
        sys.exit(1)


def check_price_completeness(
    daily_df: pd.DataFrame,
    universe_syms: set[str],
    verbose: bool = False,
) -> list[str]:
    """Check daily_adj_close.csv for missing price data across universe symbols.

    Uses the file's existing date index as the ground truth of NSE trading days —
    weekends and holidays are already absent from the file, so they are never flagged.
    A NaN for a symbol on a date that IS in the index is a genuine data gap.

    The gap window per symbol is [first_non_nan_date, last_non_nan_date] so symbols
    that haven't been in the universe since inception don't trigger spurious gaps.

    Args:
        daily_df:     Wide-format DataFrame (date index, symbol columns).
        universe_syms: Current universe symbol set.
        verbose:      If True, print per-symbol detail with sample missing dates.

    Returns:
        List of warning strings (empty if everything is clean).
    """
    warnings: list[str] = []

    no_data: list[str] = []
    gap_symbols: list[tuple[str, int, list[str]]] = []  # (symbol, gap_days, sample_dates)

    for sym in sorted(universe_syms):
        if sym not in daily_df.columns:
            no_data.append(sym)
            continue

        col = daily_df[sym]
        first_valid = col.first_valid_index()
        last_valid = col.last_valid_index()

        if first_valid is None:
            # Column exists but is entirely NaN
            no_data.append(sym)
            continue

        window = col.loc[first_valid:last_valid]
        missing_mask = window.isna()
        gap_count = int(missing_mask.sum())

        if gap_count > 0:
            missing_dates = [
                str(d.date()) for d in window.index[missing_mask]
            ]
            gap_symbols.append((sym, gap_count, missing_dates))

    total_syms = len(universe_syms)
    gap_syms_count = len(gap_symbols)
    no_data_count = len(no_data)
    complete_count = total_syms - gap_syms_count - no_data_count
    total_gap_days = sum(g for _, g, _ in gap_symbols)

    # Always print the completeness header directly (not routed through warnings list)
    # so it appears regardless of whether there are gaps.
    parts = [f"PRICE COMPLETENESS: {complete_count}/{total_syms} symbols complete"]
    if no_data_count:
        parts.append(f"{no_data_count} no data")
    if gap_syms_count:
        parts.append(f"{gap_syms_count} have gaps ({total_gap_days} total gap-days)")
    print("  " + " | ".join(parts))

    if no_data_count:
        listed = ", ".join(no_data[:15])
        suffix = f" … (+{no_data_count - 15} more)" if no_data_count > 15 else ""
        print(f"    No data: {listed}{suffix}")
        warnings.append(
            f"Price completeness: {no_data_count} universe symbol(s) have no data in "
            "daily_adj_close.csv. Run fetch_nsepy_price.py."
        )

    if gap_symbols:
        top = sorted(gap_symbols, key=lambda x: x[1], reverse=True)[:TOP_GAP_SYMBOLS]
        top_str = ", ".join(f"{s} ({g}d)" for s, g, _ in top)
        suffix = f" … (+{gap_syms_count - TOP_GAP_SYMBOLS} more)" if gap_syms_count > TOP_GAP_SYMBOLS else ""
        print(f"    Top gaps: {top_str}{suffix}")

        if verbose:
            print("    Per-symbol gap detail:")
            for sym, gap_count, missing_dates in sorted(gap_symbols, key=lambda x: x[1], reverse=True):
                sample = missing_dates[:SAMPLE_DATES]
                more = f" … +{gap_count - SAMPLE_DATES} more" if gap_count > SAMPLE_DATES else ""
                print(f"      {sym:20s}  {gap_count:4d} gap-days  sample: {sample}{more}")
        else:
            print("    Run with --report for full gap detail.")

        warnings.append(
            f"Price completeness: {gap_syms_count} symbol(s) have data gaps "
            f"({total_gap_days} total gap-days). Run fetch_nsepy_price.py --force to refresh."
        )

    return warnings


def _benchmark_missing_days(
    daily_df: pd.DataFrame,
    bm_df: pd.DataFrame,
) -> list[pd.Timestamp]:
    """Return trading days (from daily_df.index) not covered in bm_df.

    Pure function — no file I/O. Used by check_benchmark_completeness and unit tests.

    Args:
        daily_df: Wide-format daily adj_close DataFrame (index = NSE trading days).
        bm_df:    Benchmark DataFrame with 'date' and 'price_index' columns.

    Returns:
        Sorted list of Timestamps for trading days missing from benchmark coverage.
    """
    trading_days = set(daily_df.index.normalize())
    # Exclude dates before the index base date — benchmark data genuinely doesn't
    # exist there (the Nifty LargeMidcap 250 was backfilled only to BENCHMARK_BASE_DATE).
    cutoff = pd.Timestamp(BENCHMARK_BASE_DATE)
    trading_days = {d for d in trading_days if d >= cutoff}
    bm_df = bm_df.copy()
    bm_df["date"] = pd.to_datetime(bm_df["date"]).dt.normalize()
    covered = set(bm_df.loc[bm_df["price_index"].notna(), "date"])
    return sorted(trading_days - covered)


def check_benchmark_completeness(
    daily_df: pd.DataFrame,
    verbose: bool = False,
) -> list[str]:
    """Check benchmark.csv coverage against the NSE trading days in daily_adj_close.csv.

    Uses the same ground truth: any date in daily_df.index is a confirmed trading day.
    Missing benchmark rows on those dates are flagged.

    Args:
        daily_df: Wide-format daily adj_close DataFrame (used only for its date index).
        verbose:  If True, list all missing benchmark dates (up to 30).

    Returns:
        List of warning strings (empty if benchmark is fully covered).
    """
    warnings: list[str] = []
    total_trading_days = len(daily_df.index)

    if not BENCHMARK_FILE.exists():
        # Already handled by section 3 of main(); skip duplicate warning.
        return warnings

    try:
        bm_df = pd.read_csv(BENCHMARK_FILE, parse_dates=["date"])
    except (OSError, pd.errors.ParserError, KeyError) as e:
        warnings.append(f"Benchmark completeness: could not read benchmark.csv: {e}")
        return warnings

    if bm_df.empty or "price_index" not in bm_df.columns:
        return warnings

    missing_dates = _benchmark_missing_days(daily_df, bm_df)
    missing_count = len(missing_dates)
    covered_count = total_trading_days - missing_count

    print(
        f"  BENCHMARK COMPLETENESS: {covered_count:,}/{total_trading_days:,} trading days covered"
        + (f" | {missing_count} missing" if missing_count else "")
    )

    if missing_count:
        first_missing = str(missing_dates[0].date())
        last_missing = str(missing_dates[-1].date())

        if verbose:
            listed = [str(d.date()) for d in missing_dates[:30]]
            suffix = f" … +{missing_count - 30} more" if missing_count > 30 else ""
            print(f"    Missing dates: {listed}{suffix}")
        else:
            print(f"    Missing: {first_missing} … {last_missing}  (run --report for full list)")

        warnings.append(
            f"Benchmark missing {missing_count} trading day(s) "
            f"(first: {first_missing}, last: {last_missing}). Run fetch_benchmark.py."
        )

    return warnings


def main() -> None:
    """Run all data-quality gate checks and exit 0 (pass) or 1 (blocking error found)."""
    parser = argparse.ArgumentParser(
        description="Data quality gate — run before each rebalance session."
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print full per-symbol gap detail and all missing benchmark dates.",
    )
    args = parser.parse_args()

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
    # 6. Price and benchmark completeness  (warnings only)
    # -------------------------------------------------------------------------
    daily_df = load_daily_adj_close()
    universe_symbols_for_completeness = load_universe_symbols()

    if daily_df is not None:
        print("\nCompleteness report:")
        if universe_symbols_for_completeness:
            warnings.extend(
                check_price_completeness(daily_df, universe_symbols_for_completeness, args.report)
            )
        warnings.extend(check_benchmark_completeness(daily_df, args.report))
        print()
    else:
        warnings.append(
            "daily_adj_close.csv not found — cannot run completeness checks. "
            "Run fetch_nsepy_price.py first."
        )

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
