"""Compute portfolio performance metrics and stock rankings. Prints to stdout.

Also appends a pre-rebalance performance row to data/portfolio/performance.csv each session.

Reads:
  data/portfolio/portfolio.json
  data/market/benchmark.csv
  data/market/prices/YYYY-WW.csv          (latest weekly OHLCV snapshot)
  data/market/prices/daily_adj_close.csv  (cumulative daily adj_close for MA calculation)

Stocks are excluded from ranking (and cannot be bought) if:
  - Fewer than MIN_HISTORY_DAYS of daily price history are available
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from tabulate import tabulate

# Make portfolio_types importable when running as `uv run python scripts/foo.py`
sys.path.insert(0, str(Path(__file__).parent.parent))
from portfolio_types import PerformanceResult, Portfolio

DATA_DIR = Path(__file__).parent.parent.parent / "data"
PRICES_DIR = DATA_DIR / "market" / "prices"
PORTFOLIO_FILE = DATA_DIR / "portfolio" / "portfolio.json"
BENCHMARK_FILE = DATA_DIR / "market" / "benchmark.csv"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"
PERFORMANCE_FILE = DATA_DIR / "portfolio" / "performance.csv"

MA_SHORT = 50           # short moving average window (days) for near-term momentum
MA_LONG = 200           # long moving average window (days) for near-term momentum
MOMENTUM_LOOKBACK_WEEKS = 52   # total lookback for long-term momentum
MOMENTUM_SKIP_WEEKS = 4        # skip the most recent N weeks (12-1 month calculation)
MIN_HISTORY_DAYS = 200         # minimum daily price history required for MA calculation

# Factor weights must sum to 1.0.
WEIGHTS: dict[str, float] = {
    "lt_momentum": 0.15,   # long-term momentum (12-1 month return)
    "nt_momentum": 0.30,   # near-term momentum (50-DMA vs 200-DMA ratio)
    "cross_speed": 0.30,   # golden cross speed (1 / days from death cross to golden cross)
    "cross_peak": 0.25,    # golden cross peak (1 / days from price-200DMA touch to golden cross)
}


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_portfolio() -> Portfolio:
    """Load data/portfolio/portfolio.json, returning an empty all-cash portfolio if not found.

    Exits with code 1 if the file exists but cannot be read or parsed.
    """
    if not PORTFOLIO_FILE.exists():
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


def load_latest_weekly_prices() -> pd.DataFrame | None:
    """Load the most recent weekly prices CSV.

    Returns None if no readable file exists.
    """
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    for f in files:
        try:
            df = pd.read_csv(f)
            if not df.empty:
                return df
        except (OSError, pd.errors.ParserError) as e:
            print(f"  WARNING: Skipping unreadable prices file {f.name}: {e}")
            continue
    return None


def load_daily_prices(lookback_days: int = 520) -> pd.DataFrame | None:
    """Load the cumulative daily adj_close CSV filtered to the last *lookback_days* days.

    Args:
        lookback_days: How many calendar days of history to load for MA calculations.

    Returns:
        Wide-format DataFrame (index=date, columns=symbols), or None if the file
        does not exist.
    """
    if not DAILY_FILE.exists():
        return None
    try:
        df = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
        return df[df.index >= cutoff]
    except (OSError, pd.errors.ParserError) as e:
        print(f"  WARNING: Could not read daily_adj_close.csv: {e}")
        return None


def load_benchmark() -> pd.DataFrame | None:
    """Load data/market/benchmark.csv sorted by date, or None if the file does not exist."""
    if not BENCHMARK_FILE.exists():
        return None
    try:
        df = pd.read_csv(BENCHMARK_FILE, parse_dates=["date"])
        return df.sort_values("date").reset_index(drop=True)
    except (OSError, pd.errors.ParserError) as e:
        print(f"  WARNING: Could not read benchmark.csv: {e}")
        return None


def normalise_series(s: pd.Series[Any]) -> pd.Series[Any]:
    """Min-max normalise a pandas Series to [0, 1].

    Returns a constant 0.5 series when min == max (avoids division by zero).
    """
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series(0.5, index=s.index)
    result: pd.Series[Any] = (s - mn) / (mx - mn)
    return result


def compute_lt_momentum(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute 12-1 month long-term momentum for each symbol.

    Formula: (52-week return) − (4-week return) — removes the short-term
    reversal component to isolate true trend momentum.

    Returns an empty Series if *daily* is None or has insufficient history.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    approx_days_per_week = 5  # trading days

    for sym in daily.columns:
        col = daily[sym].dropna()
        n = len(col)
        lookback_idx = MOMENTUM_LOOKBACK_WEEKS * approx_days_per_week
        skip_idx = MOMENTUM_SKIP_WEEKS * approx_days_per_week

        if n <= lookback_idx or n <= skip_idx:
            continue

        price_now = col.iloc[-1]
        price_4w_ago = col.iloc[-skip_idx]
        price_52w_ago = col.iloc[-lookback_idx]

        if price_52w_ago > 0 and price_4w_ago > 0:
            lt_ret = (price_now / price_52w_ago) - 1
            st_ret = (price_now / price_4w_ago) - 1
            result[sym] = lt_ret - st_ret

    return pd.Series(result)


def compute_nt_momentum(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute near-term momentum as (50-DMA − 200-DMA) / 200-DMA for each symbol.

    Positive values indicate the short MA is above the long MA (bullish trend);
    negative values indicate a bearish trend.

    Excludes symbols with fewer than MIN_HISTORY_DAYS of price history.

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean().iloc[-1]
        ma200 = col.rolling(MA_LONG).mean().iloc[-1]
        if pd.notna(ma50) and pd.notna(ma200) and ma200 > 0:
            result[sym] = (ma50 - ma200) / ma200

    return pd.Series(result)


_SENTINEL = float("inf")  # marks "no prior cross found" — replaced with 75th pct after normalise


def compute_cross_speed(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute Golden Cross speed for each symbol.

    Score = 1 / calendar_days_between(last Death Cross, most recent Golden Cross).
    Fewer days = faster moving-average cycle = higher score.

    Special cases:
      - Currently in Death Cross (50-DMA < 200-DMA): raw = 0.0
      - Golden Cross found but no prior Death Cross in history: raw = _SENTINEL
        (replaced with the 75th percentile of the universe after normalisation)

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean()
        ma200 = col.rolling(MA_LONG).mean()
        valid = ma50.notna() & ma200.notna()
        if not valid.any():
            continue

        above = (ma50 > ma200).astype(int)
        above = above[valid]

        if above.iloc[-1] == 0:
            result[sym] = 0.0
            continue

        transitions = above.diff()
        golden_crosses = transitions.index[transitions == 1]
        if len(golden_crosses) == 0:
            result[sym] = _SENTINEL
            continue

        last_golden = golden_crosses[-1]
        death_crosses_before = transitions.index[
            (transitions == -1) & (transitions.index < last_golden)
        ]
        if len(death_crosses_before) == 0:
            result[sym] = _SENTINEL
            continue

        last_death = death_crosses_before[-1]
        days = (last_golden - last_death).days
        result[sym] = 1.0 / days if days > 0 else 1.0

    return pd.Series(result)


def compute_cross_peak(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute Golden Cross peak for each symbol.

    Score = 1 / calendar_days_between(last day price <= 200-DMA, most recent Golden Cross).
    Fewer days = price breakout and MA confirmation were tightly coupled = higher score.

    Special cases:
      - Currently in Death Cross: raw = 0.0
      - Golden Cross found but price never touched 200-DMA before it: raw = _SENTINEL
        (replaced with the 75th percentile of the universe after normalisation)

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean()
        ma200 = col.rolling(MA_LONG).mean()
        valid = ma50.notna() & ma200.notna()
        if not valid.any():
            continue

        above = (ma50 > ma200).astype(int)
        above = above[valid]
        col_valid = col[valid]
        ma200_valid = ma200[valid]

        if above.iloc[-1] == 0:
            result[sym] = 0.0
            continue

        transitions = above.diff()
        golden_crosses = transitions.index[transitions == 1]
        if len(golden_crosses) == 0:
            result[sym] = _SENTINEL
            continue

        last_golden = golden_crosses[-1]

        # Find the most recent day BEFORE the Golden Cross when price <= 200-DMA.
        before_cross = col_valid.index < last_golden
        price_before = col_valid[before_cross]
        ma200_before = ma200_valid[before_cross]
        touched = price_before.index[price_before <= ma200_before]

        if len(touched) == 0:
            result[sym] = _SENTINEL
            continue

        last_touch = touched[-1]
        days = (last_golden - last_touch).days
        result[sym] = 1.0 / days if days > 0 else 1.0

    return pd.Series(result)


def _apply_sentinel_substitution(
    raw: pd.Series[Any], normalised: pd.Series[Any]
) -> pd.Series[Any]:
    """Replace _SENTINEL entries in *normalised* with the 75th percentile of non-sentinel values."""
    sentinel_mask = raw == _SENTINEL
    if not sentinel_mask.any():
        return normalised
    non_sentinel_norm = normalised[~sentinel_mask]
    p75 = float(non_sentinel_norm.quantile(0.75)) if not non_sentinel_norm.empty else 0.75
    result = normalised.copy()
    result[sentinel_mask] = p75
    return result


def compute_rankings(
    daily: pd.DataFrame | None,
) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Compute composite factor rankings for all eligible symbols.

    Symbols are excluded from ranking (and cannot be bought) if there is
    insufficient price history for MA calculation (< MIN_HISTORY_DAYS days).

    Args:
        daily: Wide-format daily adj_close DataFrame (columns=symbols).

    Returns:
        A tuple of:
          - ranked DataFrame (index=symbol) with factor scores, composite score,
            rank, and MA signal columns
          - list of (symbol, reason) tuples for excluded symbols
    """
    lt_mom = compute_lt_momentum(daily)
    nt_mom = compute_nt_momentum(daily)
    cross_speed = compute_cross_speed(daily)
    cross_peak = compute_cross_peak(daily)

    eligible = lt_mom.index.intersection(nt_mom.index).intersection(
        cross_speed.index
    ).intersection(cross_peak.index)

    excluded: list[tuple[str, str]] = []
    if daily is not None:
        for sym in daily.columns:
            if sym not in eligible:
                excluded.append((sym, "insufficient_price_history"))

    if len(eligible) == 0:
        return pd.DataFrame(), excluded

    df = pd.DataFrame({
        "lt_momentum_raw": lt_mom[eligible],
        "nt_momentum_raw": nt_mom[eligible],
        "cross_speed_raw": cross_speed[eligible],
        "cross_peak_raw": cross_peak[eligible],
    })
    df.index.name = "symbol"

    df["lt_momentum"] = normalise_series(df["lt_momentum_raw"])
    df["nt_momentum"] = normalise_series(df["nt_momentum_raw"])

    def _normalise_with_sentinel(raw_col: pd.Series[Any]) -> pd.Series[Any]:
        """Normalise a column that may contain _SENTINEL values.

        Sentinel entries are temporarily excluded from normalisation, then
        replaced with the 75th percentile of the normalised non-sentinel values.
        """
        sentinel_mask = raw_col == _SENTINEL
        non_sentinel = raw_col[~sentinel_mask]
        norm = normalise_series(non_sentinel)
        # Extend to full index with 0.0 for sentinels, then apply substitution.
        full_norm = norm.reindex(raw_col.index, fill_value=0.0)
        return _apply_sentinel_substitution(raw_col, full_norm)

    df["cross_speed"] = _normalise_with_sentinel(df["cross_speed_raw"])
    df["cross_peak"] = _normalise_with_sentinel(df["cross_peak_raw"])

    df["composite"] = (
        df["lt_momentum"] * WEIGHTS["lt_momentum"]
        + df["nt_momentum"] * WEIGHTS["nt_momentum"]
        + df["cross_speed"] * WEIGHTS["cross_speed"]
        + df["cross_peak"] * WEIGHTS["cross_peak"]
    )
    df = df.sort_values("composite", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    df["ma_signal"] = df["nt_momentum_raw"].apply(
        lambda x: "ABOVE" if x > 0 else "BELOW"
    )

    return df, excluded


def compute_performance(
    portfolio: Portfolio,
    benchmark_df: pd.DataFrame | None,
) -> PerformanceResult:
    """Compute all performance metrics for the portfolio vs benchmark.

    Args:
        portfolio:    Current portfolio state from portfolio.json.
        benchmark_df: Benchmark CSV as a sorted DataFrame, or None if unavailable.

    Returns:
        PerformanceResult with weekly return, inception return, CAGR, and active
        return vs benchmark. Fields are None when data is insufficient.
    """
    nav_history = portfolio.get("nav_history", [])
    total_nav = portfolio.get("total_nav", 0.0)
    inception_date_str = portfolio.get("inception_date")
    initial_capital = portfolio.get("initial_capital", 25000.0)

    result = PerformanceResult(
        nav=total_nav,
        weekly_return_pct=None,
        inception_return_pct=None,
        inception_return_inr=None,
        cagr_pct=None,
        bm_level=None,
        bm_weekly_return_pct=None,
        bm_inception_return_pct=None,
        bm_cagr_pct=None,
        active_weekly=None,
        active_inception=None,
        active_cagr=None,
        bm_source=None,
        weeks_since_inception=len(nav_history),
    )

    # --- Portfolio weekly return (requires at least two NAV history entries) ---
    if len(nav_history) >= 2:
        prev_nav = nav_history[-2]["nav"]
        if prev_nav > 0:
            result["weekly_return_pct"] = (total_nav / prev_nav - 1) * 100

    # --- Inception returns and CAGR ------------------------------------------
    if inception_date_str and total_nav > 0:
        try:
            inception_date = datetime.strptime(inception_date_str, "%Y-%m-%d").date()
        except ValueError:
            inception_date = None

        if inception_date is not None:
            days = (date.today() - inception_date).days
            result["inception_return_pct"] = (total_nav / initial_capital - 1) * 100
            result["inception_return_inr"] = total_nav - initial_capital
            if days > 0:
                result["cagr_pct"] = (
                    (total_nav / initial_capital) ** (365 / days) - 1
                ) * 100

    # --- Benchmark metrics ---------------------------------------------------
    if benchmark_df is not None and not benchmark_df.empty:
        latest_bm = benchmark_df.iloc[-1]
        bm_value = latest_bm.get("tri_level") or latest_bm.get("price_index")
        result["bm_level"] = float(bm_value) if bm_value is not None else None
        result["bm_source"] = str(latest_bm.get("source", "unknown"))

        if len(benchmark_df) >= 2 and bm_value is not None:
            prev_bm_val = benchmark_df.iloc[-2].get("tri_level") or benchmark_df.iloc[-2].get("price_index")
            if prev_bm_val and float(prev_bm_val) > 0:
                result["bm_weekly_return_pct"] = (
                    float(bm_value) / float(prev_bm_val) - 1
                ) * 100

        inception_bm = benchmark_df.iloc[0]
        inception_bm_val = inception_bm.get("tri_level") or inception_bm.get("price_index")
        if inception_bm_val and float(inception_bm_val) > 0 and bm_value is not None:
            result["bm_inception_return_pct"] = (
                float(bm_value) / float(inception_bm_val) - 1
            ) * 100
            try:
                bm_inception_date = datetime.strptime(
                    str(inception_bm.get("date", ""))[:10], "%Y-%m-%d"
                ).date()
                bm_days = (date.today() - bm_inception_date).days
                if bm_days > 0:
                    result["bm_cagr_pct"] = (
                        (float(bm_value) / float(inception_bm_val)) ** (365 / bm_days) - 1
                    ) * 100
            except ValueError:
                pass  # Malformed date in benchmark CSV — skip CAGR calculation.

    # --- Active returns (portfolio minus benchmark) --------------------------
    if result.get("weekly_return_pct") is not None and result.get("bm_weekly_return_pct") is not None:
        result["active_weekly"] = result["weekly_return_pct"] - result["bm_weekly_return_pct"]  # type: ignore[operator]
    if result.get("inception_return_pct") is not None and result.get("bm_inception_return_pct") is not None:
        result["active_inception"] = result["inception_return_pct"] - result["bm_inception_return_pct"]  # type: ignore[operator]
    if result.get("cagr_pct") is not None and result.get("bm_cagr_pct") is not None:
        result["active_cagr"] = result["cagr_pct"] - result["bm_cagr_pct"]  # type: ignore[operator]

    return result


def write_performance_row(perf: PerformanceResult) -> None:
    """Append (or replace today's) performance row in data/portfolio/performance.csv.

    Reads the existing file, removes any row for today, then appends the new row.
    Creates the file with a header on first write.
    """
    today = date.today().isoformat()
    fieldnames = [
        "date", "portfolio_nav", "portfolio_weekly_return_pct",
        "portfolio_inception_return_pct", "portfolio_cagr_pct",
        "benchmark_level", "benchmark_weekly_return_pct",
        "benchmark_inception_return_pct", "benchmark_cagr_pct",
        "active_return_weekly_pct", "active_return_inception_pct",
        "active_return_cagr_pct", "benchmark_source", "weeks_since_inception",
    ]

    def _r(val: float | None, decimals: int = 4) -> float | None:
        """Round a nullable float for CSV storage."""
        return round(val, decimals) if val is not None else None

    row = {
        "date": today,
        "portfolio_nav": _r(perf.get("nav"), 2),
        "portfolio_weekly_return_pct": _r(perf.get("weekly_return_pct")),
        "portfolio_inception_return_pct": _r(perf.get("inception_return_pct")),
        "portfolio_cagr_pct": _r(perf.get("cagr_pct")),
        "benchmark_level": _r(perf.get("bm_level"), 2),
        "benchmark_weekly_return_pct": _r(perf.get("bm_weekly_return_pct")),
        "benchmark_inception_return_pct": _r(perf.get("bm_inception_return_pct")),
        "benchmark_cagr_pct": _r(perf.get("bm_cagr_pct")),
        "active_return_weekly_pct": _r(perf.get("active_weekly")),
        "active_return_inception_pct": _r(perf.get("active_inception")),
        "active_return_cagr_pct": _r(perf.get("active_cagr")),
        "benchmark_source": perf.get("bm_source"),
        "weeks_since_inception": perf.get("weeks_since_inception"),
    }

    existing: list[dict[str, Any]] = []
    if PERFORMANCE_FILE.exists():
        try:
            with open(PERFORMANCE_FILE) as f:
                reader = csv.DictReader(f)
                existing = [r for r in reader if r.get("date") != today]
        except (OSError, csv.Error) as e:
            print(f"  WARNING: Could not read performance.csv: {e}. Will overwrite.")

    try:
        with open(PERFORMANCE_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in existing:
                writer.writerow({k: r.get(k) for k in fieldnames})
            writer.writerow(row)
    except OSError as e:
        # Non-fatal: the portfolio state is not affected if performance.csv fails.
        print(f"  WARNING: Could not write performance.csv: {e}")


def fmt(val: float | None, suffix: str = "", decimals: int = 2) -> str:
    """Format a nullable float as a fixed-precision string with an optional suffix.

    Returns the string 'N/A' for None values.
    """
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}{suffix}"


def main() -> None:
    """Load all data files, compute rankings and performance, print the report, and write performance.csv."""
    portfolio = load_portfolio()
    weekly_prices = load_latest_weekly_prices()
    daily_prices = load_daily_prices()
    benchmark_df = load_benchmark()

    # Compute and persist performance metrics before the rebalance snapshot.
    perf = compute_performance(portfolio, benchmark_df)
    write_performance_row(perf)

    # -------------------------------------------------------------------------
    # Performance summary
    # -------------------------------------------------------------------------
    print("=" * 70)
    print("PERFORMANCE SUMMARY")
    print("=" * 70)
    print(f"Portfolio NAV:   INR {perf.get('nav', 0):,.2f}")
    print(f"Sessions run:    {perf.get('weeks_since_inception', 0)}")
    print()
    print(f"{'Metric':<30} {'Portfolio':>12} {'Benchmark':>12} {'Active':>10}")
    print("-" * 66)
    print(
        f"{'This week return':<30} "
        f"{fmt(perf.get('weekly_return_pct'), '%'):>12} "
        f"{fmt(perf.get('bm_weekly_return_pct'), '%'):>12} "
        f"{fmt(perf.get('active_weekly'), '%'):>10}"
    )
    print(
        f"{'Since inception (total %)':<30} "
        f"{fmt(perf.get('inception_return_pct'), '%'):>12} "
        f"{fmt(perf.get('bm_inception_return_pct'), '%'):>12} "
        f"{fmt(perf.get('active_inception'), '%'):>10}"
    )
    print(
        f"{'Since inception (INR)':<30} "
        f"{fmt(perf.get('inception_return_inr')):>12} "
        f"{'':>12} {'':>10}"
    )
    print(
        f"{'CAGR (annualised)':<30} "
        f"{fmt(perf.get('cagr_pct'), '%'):>12} "
        f"{fmt(perf.get('bm_cagr_pct'), '%'):>12} "
        f"{fmt(perf.get('active_cagr'), '%'):>10}"
    )
    bm_source = perf.get("bm_source", "")
    if bm_source and "price_index" in str(bm_source):
        print(
            "\n  ⚠  Benchmark uses price index (not TRI). "
            "Active return appears ~1.5%/yr better than reality."
        )
    print()

    # -------------------------------------------------------------------------
    # Stock rankings
    # -------------------------------------------------------------------------
    if daily_prices is not None:
        rankings, excluded = compute_rankings(daily_prices)
    else:
        rankings = pd.DataFrame()
        excluded = []

    held_symbols: set[str] = {h["symbol"] for h in portfolio.get("holdings", [])}

    if not rankings.empty:
        # Annotate each row with its trading status.
        def _status(sym: str) -> str:
            if sym in held_symbols:
                rank = int(rankings.loc[sym, "rank"])  # type: ignore[arg-type]
                if rank > 30:
                    return "SELL_CANDIDATE"
                # Death Cross is an explicit sell trigger.
                if str(rankings.loc[sym, "ma_signal"]) == "BELOW":
                    return "SELL_CANDIDATE"
                return "HELD"
            rank = int(rankings.loc[sym, "rank"])  # type: ignore[arg-type]
            return "BUY_CANDIDATE" if rank <= 15 else ""

        rankings["status"] = [_status(sym) for sym in rankings.index]

        top_display = rankings.head(30)
        table_data = [
            [
                int(row["rank"]),
                sym,
                f"{row['lt_momentum']:.3f}",
                f"{row['nt_momentum']:.3f}",
                f"{row['cross_speed']:.3f}",
                f"{row['cross_peak']:.3f}",
                f"{row['composite']:.3f}",
                row["ma_signal"],
                row["status"],
            ]
            for sym, row in top_display.iterrows()
        ]

        print("=" * 80)
        print(f"STOCK RANKINGS (top 30 of {len(rankings)} eligible)")
        print("=" * 80)
        print(tabulate(
            table_data,
            headers=["Rank", "Symbol", "LT Mom", "NT Mom", "GC Speed", "GC Peak",
                     "Composite", "MA", "Status"],
            tablefmt="simple",
        ))
        print()

        if excluded:
            print(f"EXCLUDED THIS WEEK ({len(excluded)} stocks, insufficient price history):")
            truncated = sorted(sym for sym, _ in excluded)[:10]
            suffix = "..." if len(excluded) > 10 else ""
            print(f"  {', '.join(truncated)}{suffix}")
            print()

    # -------------------------------------------------------------------------
    # Current holdings
    # -------------------------------------------------------------------------
    holdings = portfolio.get("holdings", [])
    if holdings:
        print("=" * 70)
        print("CURRENT HOLDINGS")
        print("=" * 70)

        if weekly_prices is not None and not weekly_prices.empty:
            # Build a symbol → latest adj_close map from the most recent prices.
            latest_prices_map: dict[str, float] = {}
            for _, row in weekly_prices.iterrows():
                sym = str(row["symbol"])
                if sym not in latest_prices_map:
                    price = row.get("adj_close") if pd.notna(row.get("adj_close")) else row.get("close")
                    if price is not None:
                        latest_prices_map[sym] = float(price)

            hold_table: list[list[Any]] = []
            for h in sorted(holdings, key=lambda x: x.get("weight", 0), reverse=True):
                sym = h["symbol"]
                current_price = latest_prices_map.get(sym, h.get("current_price", 0.0))
                current_value = h["shares"] * current_price
                cost = h["avg_cost"] * h["shares"]
                ret_pct = (current_value / cost - 1) * 100 if cost > 0 else 0.0
                rank: int | str = (
                    int(rankings.loc[sym, "rank"])  # type: ignore[arg-type]
                    if (not rankings.empty and sym in rankings.index)
                    else "N/A"
                )
                ma_sig = (
                    str(rankings.loc[sym, "ma_signal"])
                    if (not rankings.empty and sym in rankings.index)
                    else "N/A"
                )
                hold_table.append([
                    sym,
                    f"{h['weight']:.1%}",
                    f"{current_price:.2f}",
                    f"{current_value:,.0f}",
                    f"{ret_pct:+.1f}%",
                    rank,
                    ma_sig,
                ])
            print(tabulate(
                hold_table,
                headers=["Symbol", "Weight", "Price", "Value (INR)", "Return", "Rank", "MA"],
                tablefmt="simple",
            ))
        else:
            for h in holdings:
                print(f"  {h['symbol']}: {h.get('weight', 0):.1%} weight, "
                      f"avg cost {h['avg_cost']:.2f}")
        print()

    cash = portfolio.get("current_cash", 0.0)
    total_nav_val = portfolio.get("total_nav", 0.0)
    if total_nav_val > 0:
        print(f"Cash: INR {cash:,.2f} ({cash / total_nav_val:.1%} of NAV)")
    else:
        print(f"Cash: INR {cash:,.2f}")


if __name__ == "__main__":
    main()
