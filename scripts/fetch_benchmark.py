"""Fetch Nifty 250 TRI (or price index fallback) and append to data/benchmark.csv.

Primary source: NSE live indices JSON endpoint (has TRI values).
Fallback: yfinance ^CNX250 (price index only — ~1.5%/yr lower than TRI).

Appends one row per session. Safe to run multiple times — deduplicates by date.
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
BENCHMARK_FILE = DATA_DIR / "benchmark.csv"

NSE_JSON_URL = "https://iislliveblob.niftyindices.com/jsonfiles/LiveIndicesWatch.json"
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.niftyindices.com/",
}
NIFTY250_NAMES = {"NIFTY 250", "Nifty 250", "NIFTY250"}
PRICE_INDEX_TICKER = "^CNX250"


def fetch_tri_from_nse() -> tuple[float | None, float | None]:
    """Returns (price_index, tri_level) from NSE JSON. None if unavailable."""
    try:
        resp = requests.get(NSE_JSON_URL, headers=NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("data", []) or data.get("Data", [])
        for entry in entries:
            name = entry.get("indexName", "") or entry.get("Index Name", "")
            if name in NIFTY250_NAMES or "250" in name:
                price = entry.get("current") or entry.get("indexValue") or entry.get("Current")
                tri = entry.get("triValue") or entry.get("tri") or entry.get("TRI")
                try:
                    return (float(price) if price else None, float(tri) if tri else None)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"  NSE JSON fetch failed: {e}")
    return None, None


def fetch_price_index_from_yfinance() -> float | None:
    try:
        ticker = yf.Ticker(PRICE_INDEX_TICKER)
        hist = ticker.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        print(f"  yfinance fallback failed: {e}")
        return None


def load_existing() -> pd.DataFrame:
    if BENCHMARK_FILE.exists():
        return pd.read_csv(BENCHMARK_FILE)
    return pd.DataFrame(columns=["date", "price_index", "tri_level", "source"])


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    print("Fetching benchmark data...")
    price_index, tri_level = fetch_tri_from_nse()

    source = "unknown"
    if tri_level is not None:
        source = "TRI"
        print(f"  NSE TRI: {tri_level:.2f}  Price index: {price_index}")
    elif price_index is not None:
        source = "price_index_nse"
        print(f"  NSE price index (no TRI): {price_index:.2f}")
    else:
        print("  NSE JSON unavailable. Trying yfinance fallback...")
        price_index = fetch_price_index_from_yfinance()
        if price_index is not None:
            source = "price_index_yfinance"
            print(f"  yfinance ^CNX250: {price_index:.2f}")
            print("  WARNING: Using price index as benchmark (not TRI). ~1.5%/yr bias vs true TRI.")
        else:
            print("ERROR: Could not fetch benchmark from any source.")
            sys.exit(1)

    df = load_existing()

    new_row = pd.DataFrame([{
        "date": today,
        "price_index": price_index,
        "tri_level": tri_level,
        "source": source,
    }])

    df = pd.concat([df, new_row], ignore_index=True)
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date")
    df.to_csv(BENCHMARK_FILE, index=False)

    print(f"OK: Benchmark row written for {today} (source: {source})")


if __name__ == "__main__":
    main()
