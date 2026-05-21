"""Fetch Nifty 250 price index history and backfill data/market/benchmark.csv.

Finds all trading dates present in the price data (daily_adj_close.csv), compares
them with the existing benchmark.csv, and fetches missing dates.

Sources tried in order:
  1. PRIMARY  : yfinance ^CNX250 — with urllib3 retry session (handles rate limits)
  2. SECONDARY: nselib capital_market.index_data — NSE website indicesHistory API

Usage:
    uv run python scripts/fetch/fetch_benchmark.py           # fill missing dates
    uv run python scripts/fetch/fetch_benchmark.py --force   # re-fetch all dates
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DATA_DIR = Path(__file__).parent.parent.parent / "data"
BENCHMARK_FILE = DATA_DIR / "market" / "benchmark.csv"
PRICES_DIR = DATA_DIR / "market" / "prices"
DAILY_ADJ_CLOSE = PRICES_DIR / "daily_adj_close.csv"

# Yahoo Finance ticker for the Nifty 250 price index (NOT total return).
PRICE_INDEX_TICKER = "^CNX250"

# Index name for the nselib capital_market.index_data fallback.
# nselib uppercases it before URL-encoding, so case does not matter.
NIFTY250_INDEX_NAME = "NIFTY LARGEMIDCAP 250"


def get_price_dates() -> list[str]:
    """Return sorted list of unique trading dates (YYYY-MM-DD) from price data.

    Reads the row index of daily_adj_close.csv as the primary source. Falls back
    to scanning all YYYY-WW.csv files if the daily file is missing.
    """
    if DAILY_ADJ_CLOSE.exists():
        try:
            idx = pd.read_csv(DAILY_ADJ_CLOSE, index_col=0, usecols=[0]).index
            dates = pd.to_datetime(idx, errors="coerce").dropna().normalize()
            return sorted({d.date().isoformat() for d in dates})
        except Exception as e:
            print(f"  WARNING: Could not read daily_adj_close.csv ({e}). Scanning weekly files.")

    dates: set[str] = set()
    for f in PRICES_DIR.glob("????-??.csv"):
        try:
            df = pd.read_csv(f, usecols=["date"])
            parsed = pd.to_datetime(df["date"], errors="coerce").dropna().dt.normalize()
            dates.update(d.date().isoformat() for d in parsed)
        except Exception:
            pass
    return sorted(dates)


def load_existing() -> pd.DataFrame:
    """Load the existing benchmark CSV, or return an empty DataFrame with correct columns."""
    if BENCHMARK_FILE.exists():
        try:
            return pd.read_csv(BENCHMARK_FILE)
        except (OSError, pd.errors.ParserError) as e:
            print(f"  WARNING: Could not read existing benchmark.csv ({e}). Starting fresh.")
    return pd.DataFrame(columns=["date", "price_index", "tri_level", "source"])


def _make_retry_session() -> requests.Session:
    """Build a requests session that auto-retries on 429/503 with exponential backoff.

    Wait schedule (urllib3 backoff_factor * 2^(attempt-1)): 0 → 15 → 30 → 60s.
    Retries happen at the HTTP adapter level, before yfinance raises YFRateLimitError.
    """
    retry = Retry(total=4, backoff_factor=15, status_forcelist=[429, 503], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _fetch_yfinance_range(from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch ^CNX250 closing prices for [from_date, to_date] from yfinance.

    Uses a urllib3 retry session to handle HTTP 429 rate limits automatically.
    Returns a DataFrame with YYYY-MM-DD string index and 'price_index' column,
    or an empty DataFrame on failure.
    """
    session = _make_retry_session()
    try:
        ticker = yf.Ticker(PRICE_INDEX_TICKER, session=session)
        hist = ticker.history(
            start=from_date.isoformat(),
            end=(to_date + timedelta(days=1)).isoformat(),
        )
        if hist.empty:
            return pd.DataFrame()
        hist.index = pd.to_datetime(hist.index).normalize().strftime("%Y-%m-%d")
        return hist[["Close"]].rename(columns={"Close": "price_index"})
    except Exception as e:
        if "rate limit" in str(e).lower() or "too many" in str(e).lower():
            print(f"  yfinance still rate-limited after retries.")
        else:
            print(f"  yfinance fetch failed: {e}")
        return pd.DataFrame()


_NSELIB_CHUNK_DAYS = 75  # NSE API silently truncates at 70 rows; 75 days ≈ 55 trading days


def _fetch_nselib_range(from_date: date, to_date: date) -> pd.DataFrame:
    """Fetch Nifty LargeMidcap 250 closing prices via nselib (NSE indicesHistory API).

    The NSE indicesHistory endpoint silently truncates responses to 70 rows, so this
    function chunks the request into 75-calendar-day slices (~55 trading days each).
    Returns a DataFrame with YYYY-MM-DD string index and 'price_index' column,
    or an empty DataFrame on failure.
    """
    try:
        from nselib import capital_market
    except ImportError:
        print("  nselib not installed — skipping nselib fallback.")
        return pd.DataFrame()

    try:
        chunks = []
        chunk_start = from_date
        total_days = (to_date - from_date).days + 1
        n_chunks = max(1, (total_days + _NSELIB_CHUNK_DAYS - 1) // _NSELIB_CHUNK_DAYS)
        if n_chunks > 1:
            print(f"  Fetching {n_chunks} chunks from NSE API...")
        while chunk_start <= to_date:
            chunk_end = min(chunk_start + timedelta(days=_NSELIB_CHUNK_DAYS - 1), to_date)
            df_chunk = capital_market.index_data(
                NIFTY250_INDEX_NAME,
                from_date=chunk_start.strftime("%d-%m-%Y"),
                to_date=chunk_end.strftime("%d-%m-%Y"),
            )
            if not df_chunk.empty:
                chunks.append(df_chunk)
            chunk_start = chunk_end + timedelta(days=1)

        if not chunks:
            return pd.DataFrame()

        df = pd.concat(chunks, ignore_index=True)
        # TIMESTAMP column is in DD-MMM-YYYY format (e.g. "15-MAY-2026").
        df["_date"] = pd.to_datetime(df["TIMESTAMP"], format="%d-%b-%Y").dt.strftime("%Y-%m-%d")
        df = df.set_index("_date")[["CLOSE_INDEX_VAL"]].rename(columns={"CLOSE_INDEX_VAL": "price_index"})
        df["price_index"] = pd.to_numeric(df["price_index"], errors="coerce")
        return df.dropna()
    except Exception as e:
        print(f"  nselib index fetch failed: {e}")
        return pd.DataFrame()


def fetch_index_range(from_date: date, to_date: date) -> tuple[pd.DataFrame, str]:
    """Fetch Nifty 250 price index for [from_date, to_date]. Returns (df, source_label).

    Tries yfinance first; falls back to nselib if yfinance is unavailable.
    Returns (empty DataFrame, "") if all sources fail.
    """
    print("  Trying yfinance (^CNX250)...")
    df = _fetch_yfinance_range(from_date, to_date)
    if not df.empty:
        return df, "price_index_yfinance"

    print("  yfinance unavailable. Trying nselib (nseindia.com) fallback...")
    df = _fetch_nselib_range(from_date, to_date)
    if not df.empty:
        return df, "price_index_nse"

    return pd.DataFrame(), ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch and overwrite all dates.")
    args = parser.parse_args()

    BENCHMARK_FILE.parent.mkdir(parents=True, exist_ok=True)

    price_dates = get_price_dates()
    if not price_dates:
        print("WARNING: No price dates found. Run fetch_nsepy_price.py first.")
        sys.exit(0)

    df = load_existing()
    existing_dates = set(df["date"].astype(str))

    target_dates = price_dates if args.force else [d for d in price_dates if d not in existing_dates]

    if not target_dates:
        print("All benchmark dates are up to date. Use --force to refresh.")
        sys.exit(0)

    print(f"Fetching {len(target_dates)} benchmark date(s)...")
    from_date = date.fromisoformat(min(target_dates))
    to_date = date.fromisoformat(max(target_dates))

    hist, source = fetch_index_range(from_date, to_date)
    if hist.empty:
        print("ERROR: Could not fetch benchmark data from any source.")
        sys.exit(1)
    hist = hist[~hist.index.duplicated(keep="last")]

    new_rows = []
    skipped = []
    for d in target_dates:
        if d in hist.index:
            new_rows.append({
                "date": d,
                "price_index": float(hist.loc[d, "price_index"]),
                "tri_level": None,
                "source": source,
            })
        else:
            skipped.append(d)

    if skipped:
        for d in skipped:
            print(f"  WARNING: No data for {d} (market holiday or data not yet available).")

    if args.force:
        df = df[~df["date"].astype(str).isin(set(target_dates))]

    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date").reset_index(drop=True)

    try:
        df.to_csv(BENCHMARK_FILE, index=False)
    except OSError as e:
        print(f"ERROR: Could not write {BENCHMARK_FILE}: {e}")
        sys.exit(1)

    print(f"OK: {len(new_rows)} benchmark row(s) written (source: {source}), {len(skipped)} date(s) skipped.")


if __name__ == "__main__":
    main()
