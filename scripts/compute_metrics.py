"""Compute portfolio performance metrics and stock rankings. Prints to stdout.

Also appends a pre-rebalance row to data/performance.csv.

Reads:
  data/portfolio.json
  data/benchmark.csv
  data/prices/YYYY-WW.csv          (latest weekly snapshot)
  data/prices/daily_adj_close.csv  (cumulative daily history for MA calc)
  data/fundamentals/YYYY-WW.json   (latest)

Stocks are excluded from ranking if:
  - ROE is missing
  - P/B is missing
  - Fewer than 200 days of price history (insufficient for 200-DMA)
"""

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from tabulate import tabulate

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "prices"
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
BENCHMARK_FILE = DATA_DIR / "benchmark.csv"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"
PERFORMANCE_FILE = DATA_DIR / "performance.csv"

MA_SHORT = 50
MA_LONG = 200
MOMENTUM_LOOKBACK_WEEKS = 52
MOMENTUM_SKIP_WEEKS = 4
MIN_HISTORY_DAYS = 200

WEIGHTS = {
    "lt_momentum": 0.20,
    "nt_momentum": 0.20,
    "quality": 0.30,
    "value": 0.30,
}


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_portfolio() -> dict:
    if not PORTFOLIO_FILE.exists():
        return {"holdings": [], "current_cash": 0, "total_nav": 0, "inception_date": None, "initial_capital": 25000, "nav_history": []}
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


def load_latest_fundamentals() -> dict:
    files = sorted(FUNDAMENTALS_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            with open(f) as fh:
                return json.load(fh)
        except Exception:
            continue
    return {}


def load_latest_weekly_prices() -> pd.DataFrame | None:
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    for f in files:
        try:
            df = pd.read_csv(f)
            if not df.empty:
                return df
        except Exception:
            continue
    return None


def load_daily_prices(lookback_days: int = 300) -> pd.DataFrame | None:
    if not DAILY_FILE.exists():
        return None
    df = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
    return df[df.index >= cutoff]


def load_benchmark() -> pd.DataFrame | None:
    if not BENCHMARK_FILE.exists():
        return None
    df = pd.read_csv(BENCHMARK_FILE, parse_dates=["date"])
    return df.sort_values("date")


def normalise_series(s: pd.Series) -> pd.Series:
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series(0.5, index=s.index)
    return (s - mn) / (mx - mn)


def compute_lt_momentum(daily: pd.DataFrame) -> pd.Series:
    """12-1 month return: 52-week return minus last 4-week return."""
    if daily is None or daily.empty:
        return pd.Series(dtype=float)
    result = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MOMENTUM_LOOKBACK_WEEKS * 5:
            continue
        price_now = col.iloc[-1]
        price_4w_ago = col.iloc[-MOMENTUM_SKIP_WEEKS * 5] if len(col) > MOMENTUM_SKIP_WEEKS * 5 else None
        price_52w_ago = col.iloc[-MOMENTUM_LOOKBACK_WEEKS * 5] if len(col) > MOMENTUM_LOOKBACK_WEEKS * 5 else None
        if price_52w_ago and price_52w_ago > 0 and price_4w_ago and price_4w_ago > 0:
            lt_ret = (price_now / price_52w_ago) - 1
            st_ret = (price_now / price_4w_ago) - 1
            result[sym] = lt_ret - st_ret
    return pd.Series(result)


def compute_nt_momentum(daily: pd.DataFrame) -> pd.Series:
    """(50-DMA - 200-DMA) / 200-DMA for each symbol."""
    if daily is None or daily.empty:
        return pd.Series(dtype=float)
    result = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean().iloc[-1]
        ma200 = col.rolling(MA_LONG).mean().iloc[-1]
        if pd.notna(ma50) and pd.notna(ma200) and ma200 > 0:
            result[sym] = (ma50 - ma200) / ma200
    return pd.Series(result)


def compute_rankings(fundamentals: dict, daily: pd.DataFrame) -> pd.DataFrame:
    lt_mom = compute_lt_momentum(daily)
    nt_mom = compute_nt_momentum(daily)

    rows = []
    excluded = []
    for sym, info in fundamentals.items():
        if info.get("error"):
            excluded.append((sym, "no_data"))
            continue
        roe = info.get("roe")
        pb = info.get("pb_ratio")
        if roe is None:
            excluded.append((sym, "missing_roe"))
            continue
        if pb is None:
            excluded.append((sym, "missing_pb"))
            continue
        if sym not in lt_mom.index or sym not in nt_mom.index:
            excluded.append((sym, "insufficient_price_history"))
            continue
        rows.append({
            "symbol": sym,
            "roe": roe,
            "pb": pb,
            "lt_momentum_raw": lt_mom[sym],
            "nt_momentum_raw": nt_mom[sym],
            "sector": info.get("sector", "Unknown"),
        })

    if not rows:
        return pd.DataFrame(), excluded

    df = pd.DataFrame(rows).set_index("symbol")
    df["lt_momentum"] = normalise_series(df["lt_momentum_raw"])
    df["nt_momentum"] = normalise_series(df["nt_momentum_raw"])
    df["quality"] = normalise_series(df["roe"])
    df["value"] = normalise_series(1.0 / df["pb"].replace(0, np.nan))

    df["composite"] = (
        df["lt_momentum"] * WEIGHTS["lt_momentum"]
        + df["nt_momentum"] * WEIGHTS["nt_momentum"]
        + df["quality"] * WEIGHTS["quality"]
        + df["value"] * WEIGHTS["value"]
    )
    df = df.sort_values("composite", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    df["ma_signal"] = df["nt_momentum_raw"].apply(lambda x: "ABOVE" if x > 0 else "BELOW")

    return df, excluded


def compute_performance(portfolio: dict, benchmark_df: pd.DataFrame | None) -> dict:
    nav_history = portfolio.get("nav_history", [])
    total_nav = portfolio.get("total_nav", 0)
    inception_date_str = portfolio.get("inception_date")
    initial_capital = portfolio.get("initial_capital", 25000)

    result = {
        "nav": total_nav,
        "weekly_return_pct": None,
        "inception_return_pct": None,
        "inception_return_inr": None,
        "cagr_pct": None,
        "bm_level": None,
        "bm_weekly_return_pct": None,
        "bm_inception_return_pct": None,
        "bm_cagr_pct": None,
        "active_weekly": None,
        "active_inception": None,
        "active_cagr": None,
        "bm_source": None,
        "weeks_since_inception": len(nav_history),
    }

    if len(nav_history) >= 2:
        prev_nav = nav_history[-2]["nav"] if len(nav_history) >= 2 else nav_history[0]["nav"]
        if prev_nav > 0:
            result["weekly_return_pct"] = (total_nav / prev_nav - 1) * 100

    if inception_date_str and total_nav > 0:
        inception_date = datetime.strptime(inception_date_str, "%Y-%m-%d").date()
        days = (date.today() - inception_date).days
        result["inception_return_pct"] = (total_nav / initial_capital - 1) * 100
        result["inception_return_inr"] = total_nav - initial_capital
        if days > 0:
            result["cagr_pct"] = ((total_nav / initial_capital) ** (365 / days) - 1) * 100

    if benchmark_df is not None and not benchmark_df.empty:
        latest_bm = benchmark_df.iloc[-1]
        bm_value = latest_bm.get("tri_level") or latest_bm.get("price_index")
        result["bm_level"] = bm_value
        result["bm_source"] = latest_bm.get("source", "unknown")

        if len(benchmark_df) >= 2:
            prev_bm = benchmark_df.iloc[-2]
            prev_val = prev_bm.get("tri_level") or prev_bm.get("price_index")
            if prev_val and prev_val > 0 and bm_value:
                result["bm_weekly_return_pct"] = (bm_value / prev_val - 1) * 100

        inception_bm = benchmark_df.iloc[0]
        inception_bm_val = inception_bm.get("tri_level") or inception_bm.get("price_index")
        if inception_bm_val and inception_bm_val > 0 and bm_value:
            result["bm_inception_return_pct"] = (bm_value / inception_bm_val - 1) * 100
            inception_date_str2 = str(inception_bm.get("date", ""))[:10]
            try:
                inception_date2 = datetime.strptime(inception_date_str2, "%Y-%m-%d").date()
                days2 = (date.today() - inception_date2).days
                if days2 > 0:
                    result["bm_cagr_pct"] = ((bm_value / inception_bm_val) ** (365 / days2) - 1) * 100
            except ValueError:
                pass

    if result["weekly_return_pct"] is not None and result["bm_weekly_return_pct"] is not None:
        result["active_weekly"] = result["weekly_return_pct"] - result["bm_weekly_return_pct"]
    if result["inception_return_pct"] is not None and result["bm_inception_return_pct"] is not None:
        result["active_inception"] = result["inception_return_pct"] - result["bm_inception_return_pct"]
    if result["cagr_pct"] is not None and result["bm_cagr_pct"] is not None:
        result["active_cagr"] = result["cagr_pct"] - result["bm_cagr_pct"]

    return result


def write_performance_row(perf: dict):
    today = date.today().isoformat()
    file_exists = PERFORMANCE_FILE.exists()
    fieldnames = [
        "date", "portfolio_nav", "portfolio_weekly_return_pct", "portfolio_inception_return_pct",
        "portfolio_cagr_pct", "benchmark_level", "benchmark_weekly_return_pct",
        "benchmark_inception_return_pct", "benchmark_cagr_pct",
        "active_return_weekly_pct", "active_return_inception_pct", "active_return_cagr_pct",
        "benchmark_source", "weeks_since_inception",
    ]
    row = {
        "date": today,
        "portfolio_nav": round(perf["nav"], 2) if perf["nav"] else None,
        "portfolio_weekly_return_pct": round(perf["weekly_return_pct"], 4) if perf["weekly_return_pct"] is not None else None,
        "portfolio_inception_return_pct": round(perf["inception_return_pct"], 4) if perf["inception_return_pct"] is not None else None,
        "portfolio_cagr_pct": round(perf["cagr_pct"], 4) if perf["cagr_pct"] is not None else None,
        "benchmark_level": round(perf["bm_level"], 2) if perf["bm_level"] else None,
        "benchmark_weekly_return_pct": round(perf["bm_weekly_return_pct"], 4) if perf["bm_weekly_return_pct"] is not None else None,
        "benchmark_inception_return_pct": round(perf["bm_inception_return_pct"], 4) if perf["bm_inception_return_pct"] is not None else None,
        "benchmark_cagr_pct": round(perf["bm_cagr_pct"], 4) if perf["bm_cagr_pct"] is not None else None,
        "active_return_weekly_pct": round(perf["active_weekly"], 4) if perf["active_weekly"] is not None else None,
        "active_return_inception_pct": round(perf["active_inception"], 4) if perf["active_inception"] is not None else None,
        "active_return_cagr_pct": round(perf["active_cagr"], 4) if perf["active_cagr"] is not None else None,
        "benchmark_source": perf["bm_source"],
        "weeks_since_inception": perf["weeks_since_inception"],
    }

    existing = []
    if file_exists:
        with open(PERFORMANCE_FILE) as f:
            reader = csv.DictReader(f)
            existing = [r for r in reader if r.get("date") != today]

    with open(PERFORMANCE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in existing:
            writer.writerow({k: r.get(k) for k in fieldnames})
        writer.writerow(row)


def fmt(val, suffix="", decimals=2):
    if val is None:
        return "N/A"
    return f"{val:.{decimals}f}{suffix}"


def main():
    portfolio = load_portfolio()
    fundamentals = load_latest_fundamentals()
    weekly_prices = load_latest_weekly_prices()
    daily_prices = load_daily_prices(lookback_days=400)
    benchmark_df = load_benchmark()

    perf = compute_performance(portfolio, benchmark_df)
    write_performance_row(perf)

    print("=" * 70)
    print("PERFORMANCE SUMMARY")
    print("=" * 70)
    print(f"Portfolio NAV:   INR {perf['nav']:,.2f}")
    print(f"Sessions run:    {perf['weeks_since_inception']}")
    print()
    print(f"{'Metric':<30} {'Portfolio':>12} {'Benchmark':>12} {'Active':>10}")
    print("-" * 66)
    print(f"{'This week return':<30} {fmt(perf['weekly_return_pct'], '%'):>12} {fmt(perf['bm_weekly_return_pct'], '%'):>12} {fmt(perf['active_weekly'], '%'):>10}")
    print(f"{'Since inception (total %)':<30} {fmt(perf['inception_return_pct'], '%'):>12} {fmt(perf['bm_inception_return_pct'], '%'):>12} {fmt(perf['active_inception'], '%'):>10}")
    print(f"{'Since inception (INR)':<30} {fmt(perf['inception_return_inr']):>12} {'':>12} {'':>10}")
    print(f"{'CAGR (annualised)':<30} {fmt(perf['cagr_pct'], '%'):>12} {fmt(perf['bm_cagr_pct'], '%'):>12} {fmt(perf['active_cagr'], '%'):>10}")
    if perf["bm_source"] and "price_index" in str(perf["bm_source"]):
        print(f"\n  ⚠  Benchmark uses price index (not TRI). ~1.5%/yr upward bias in active return.")
    print()

    if fundamentals and daily_prices is not None:
        rankings, excluded = compute_rankings(fundamentals, daily_prices)
    else:
        rankings = pd.DataFrame()
        excluded = []

    held_symbols = {h["symbol"] for h in portfolio.get("holdings", [])}

    if not rankings.empty:
        rankings["status"] = rankings.index.map(
            lambda s: "HELD" if s in held_symbols else (
                "BUY_CANDIDATE" if rankings.loc[s, "rank"] <= 15 else ""
            )
        )
        sell_candidates = {
            h["symbol"] for h in portfolio.get("holdings", [])
            if h["symbol"] in rankings.index and rankings.loc[h["symbol"], "rank"] > 30
        }
        rankings.loc[sell_candidates, "status"] = "SELL_CANDIDATE"

        top_display = rankings.head(30)
        table_data = []
        for sym, row in top_display.iterrows():
            table_data.append([
                row["rank"],
                sym,
                f"{row['lt_momentum']:.3f}",
                f"{row['nt_momentum']:.3f}",
                f"{row['quality']:.3f}",
                f"{row['value']:.3f}",
                f"{row['composite']:.3f}",
                row["ma_signal"],
                row["status"],
            ])

        print("=" * 70)
        print(f"STOCK RANKINGS (top 30 of {len(rankings)} eligible)")
        print("=" * 70)
        print(tabulate(
            table_data,
            headers=["Rank", "Symbol", "LT Mom", "NT Mom", "Quality", "Value", "Composite", "MA", "Status"],
            tablefmt="simple",
        ))
        print()

        if excluded:
            print(f"EXCLUDED THIS WEEK ({len(excluded)} stocks):")
            by_reason: dict[str, list] = {}
            for sym, reason in excluded:
                by_reason.setdefault(reason, []).append(sym)
            for reason, syms in by_reason.items():
                print(f"  {reason}: {', '.join(sorted(syms)[:10])}{'...' if len(syms) > 10 else ''}")
            print()

    holdings = portfolio.get("holdings", [])
    if holdings:
        print("=" * 70)
        print("CURRENT HOLDINGS")
        print("=" * 70)

        if weekly_prices is not None and not weekly_prices.empty:
            latest_prices_map = {}
            for _, row in weekly_prices.iterrows():
                sym = row["symbol"]
                if sym not in latest_prices_map:
                    latest_prices_map[sym] = row["adj_close"] if pd.notna(row.get("adj_close")) else row["close"]

            hold_table = []
            for h in sorted(holdings, key=lambda x: x.get("weight", 0), reverse=True):
                sym = h["symbol"]
                current_price = latest_prices_map.get(sym, h.get("current_price", 0))
                current_value = h["shares"] * current_price
                cost = h["avg_cost"] * h["shares"]
                ret_pct = (current_value / cost - 1) * 100 if cost > 0 else 0
                rank = int(rankings.loc[sym, "rank"]) if (not rankings.empty and sym in rankings.index) else "N/A"
                ma_sig = rankings.loc[sym, "ma_signal"] if (not rankings.empty and sym in rankings.index) else "N/A"
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
                print(f"  {h['symbol']}: {h['weight']:.1%} weight, avg cost {h['avg_cost']:.2f}")
        print()

    cash = portfolio.get("current_cash", 0)
    total_nav = portfolio.get("total_nav", 0)
    print(f"Cash: INR {cash:,.2f} ({cash/total_nav:.1%} of NAV)" if total_nav > 0 else f"Cash: INR {cash:,.2f}")


if __name__ == "__main__":
    main()
