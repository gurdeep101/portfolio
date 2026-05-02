"""Fetch Nifty 250 TRI (or price index fallback) and append to data/benchmark.csv.

Primary source: NSE live indices JSON endpoint (contains TRI values).
Fallback:       yfinance ^CNX250 (price index only — ~1.5%/yr lower than TRI
                due to dividends; active return will appear inflated when this
                fallback is used).

Appends one deduplicated row per session. Safe to run multiple times.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
BENCHMARK_FILE = DATA_DIR / "benchmark.csv"

# NSE live indices endpoint — contains both price index and TRI for all indices.
NSE_JSON_URL = "https://iislliveblob.niftyindices.com/jsonfiles/LiveIndicesWatch.json"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.niftyindices.com/",
}

# Accepted index name variants in the NSE JSON response.
NIFTY250_NAMES: set[str] = {"NIFTY 250", "Nifty 250", "NIFTY250"}

# Yahoo Finance ticker for the Nifty 250 price index (NOT total return).
PRICE_INDEX_TICKER = "^CNX250"


def fetch_tri_from_nse() -> tuple[float | None, float | None]:
    """Fetch the Nifty 250 price index and TRI from the NSE live JSON endpoint.

    Returns:
        tuple[price_index, tri_level] — either value may be None if the field
        is absent or unparseable in the response.
    """
    try:
        resp = requests.get(NSE_JSON_URL, headers=NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("data", []) or data.get("Data", [])
        for entry in entries:
            name = entry.get("indexName", "") or entry.get("Index Name", "")
            if name in NIFTY250_NAMES or "250" in str(name):
                price = (
                    entry.get("current")
                    or entry.get("indexValue")
                    or entry.get("Current")
                )
                tri = entry.get("triValue") or entry.get("tri") or entry.get("TRI")
                try:
                    return (
                        float(price) if price is not None else None,
                        float(tri) if tri is not None else None,
                    )
                except (ValueError, TypeError):
                    # Field present but not numeric — skip this entry.
                    pass

    except (requests.RequestException, ValueError, KeyError) as e:
        # requests.RequestException: network/HTTP error.
        # ValueError: JSON decode failed or float() conversion failed.
        # KeyError: unexpected response structure.
        print(f"  NSE JSON fetch failed: {e}")

    return None, None


def fetch_price_index_from_yfinance() -> float | None:
    """Fetch the Nifty 250 price index level from yfinance (^CNX250).

    This is a fallback when the NSE TRI endpoint is unavailable. The price index
    understates the true benchmark return by ~1.5%/yr (dividend yield).

    Returns:
        Most recent closing price, or None if yfinance is unavailable.
    """
    try:
        ticker = yf.Ticker(PRICE_INDEX_TICKER)
        hist = ticker.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        # yfinance raises varied, undocumented exception types depending on the
        # Yahoo Finance API version. Catching broadly is intentional here.
        print(f"  yfinance fallback failed: {e}")
        return None


def load_existing() -> pd.DataFrame:
    """Load the existing benchmark CSV, or return an empty DataFrame with correct columns.

    Returns a fresh empty DataFrame (rather than raising) so that the caller
    can safely concat without checking for file existence.
    """
    if BENCHMARK_FILE.exists():
        try:
            return pd.read_csv(BENCHMARK_FILE)
        except (OSError, pd.errors.ParserError) as e:
            print(f"  WARNING: Could not read existing benchmark.csv ({e}). Starting fresh.")

    return pd.DataFrame(columns=["date", "price_index", "tri_level", "source"])


def main() -> None:
    """Fetch the latest Nifty 250 benchmark level and append it to data/benchmark.csv.

    Tries the NSE TRI endpoint first; falls back to yfinance price index.
    Exits with code 1 if both sources fail.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    print("Fetching benchmark data...")
    price_index, tri_level = fetch_tri_from_nse()

    # Determine which data we got and set the source label accordingly.
    source: str
    if tri_level is not None:
        source = "TRI"
        print(f"  NSE TRI: {tri_level:.2f}  |  Price index: {price_index}")
    elif price_index is not None:
        source = "price_index_nse"
        print(f"  NSE price index (no TRI available): {price_index:.2f}")
    else:
        print("  NSE JSON unavailable. Trying yfinance fallback...")
        price_index = fetch_price_index_from_yfinance()
        if price_index is not None:
            source = "price_index_yfinance"
            print(f"  yfinance ^CNX250: {price_index:.2f}")
            print(
                "  WARNING: Using price index as benchmark (not TRI). "
                "Active return will appear ~1.5%/yr better than reality."
            )
        else:
            print("ERROR: Could not fetch benchmark from any source.")
            sys.exit(1)

    # --- Append deduplicated row to benchmark.csv ----------------------------
    df = load_existing()

    new_row = pd.DataFrame(
        [{"date": today, "price_index": price_index, "tri_level": tri_level, "source": source}]
    )
    df = pd.concat([df, new_row], ignore_index=True)
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date").reset_index(drop=True)

    try:
        df.to_csv(BENCHMARK_FILE, index=False)
    except OSError as e:
        print(f"ERROR: Could not write {BENCHMARK_FILE}: {e}")
        sys.exit(1)

    print(f"OK: Benchmark row written for {today} (source: {source})")


if __name__ == "__main__":
    main()
