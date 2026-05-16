"""Fetch Nifty 250 TRI (or price index fallback) and append to data/market/benchmark.csv.

Data sources (tried in order):
  1. PRIMARY  : NSE live indices JSON endpoint (contains TRI values).
  2. SECONDARY: nselib live index performances (price index only).
  3. TERTIARY : yfinance ^CNX250 (price index only — ~1.5%/yr lower than TRI
                due to dividends; active return will appear inflated when this
                fallback is used).

Appends one deduplicated row per session. Safe to run multiple times.
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent.parent.parent / "data"
BENCHMARK_FILE = DATA_DIR / "market" / "benchmark.csv"

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
# The Nifty LargeMidcap 250 (= "Nifty 250") appears as "NIFTY LARGEMID250" in
# the niftyindices.com live feed; older aliases kept for forward-compat.
NIFTY250_NAMES: set[str] = {
    "NIFTY LARGEMID250",
    "NIFTY LARGEMIDCAP 250",
    "NIFTY LARGE MIDCAP 250",
    "NIFTY 250",
    "Nifty 250",
    "NIFTY250",
}

# Yahoo Finance ticker for the Nifty 250 price index (NOT total return).
PRICE_INDEX_TICKER = "^CNX250"

# NSE retry configuration mirrors fetch_nsepy_price.py, but this script makes
# one index-level request rather than 250 per-symbol calls.
NSE_MAX_RETRIES = 3
NSE_RETRY_BASE_S = 2.0


def _is_nse_retryable(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient NSE/network failure."""
    msg = str(exc).lower()
    return any(k in msg for k in ("403", "429", "too many", "connection", "timeout", "resource not"))


def fetch_tri_from_nse() -> tuple[float | None, float | None]:
    """Fetch the Nifty 250 price index and TRI from the NSE live JSON endpoint.

    Returns:
        tuple[price_index, tri_level] — either value may be None if the field
        is absent or unparseable in the response.
    """
    for attempt in range(NSE_MAX_RETRIES):
        try:
            resp = requests.get(NSE_JSON_URL, headers=NSE_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            entries = data.get("data", []) or data.get("Data", [])
            for entry in entries:
                name = entry.get("indexName", "") or entry.get("Index Name", "")
                if name in NIFTY250_NAMES:
                    # The live feed uses "last"; older snapshots used "current".
                    # Values may be comma-formatted strings ("16,610.25") — strip commas.
                    price_raw = (
                        entry.get("last")
                        or entry.get("current")
                        or entry.get("indexValue")
                        or entry.get("Current")
                    )
                    tri_raw = entry.get("triValue") or entry.get("tri") or entry.get("TRI")
                    try:
                        price = float(str(price_raw).replace(",", "")) if price_raw is not None else None
                        tri = float(str(tri_raw).replace(",", "")) if tri_raw is not None else None
                        return price, tri
                    except (ValueError, TypeError):
                        # Field present but not numeric — skip this entry.
                        pass

        except (requests.RequestException, ValueError, KeyError) as e:
            # requests.RequestException: network/HTTP error.
            # ValueError: JSON decode failed or float() conversion failed.
            # KeyError: unexpected response structure.
            if _is_nse_retryable(e) and attempt < NSE_MAX_RETRIES - 1:
                time.sleep(NSE_RETRY_BASE_S * (2 ** attempt))
                continue
            print(f"  NSE JSON fetch failed: {e}")
            return None, None

    return None, None


def _normalise_nselib_index_df(raw_df: pd.DataFrame) -> float | None:
    """Return the Nifty 250 price index level from nselib all-indices data."""
    if raw_df.empty or "index" not in raw_df.columns or "last" not in raw_df.columns:
        return None

    names = raw_df["index"].astype(str).str.upper()
    # Primary name in the NSE allIndices feed is "NIFTY LARGEMID250"; broader
    # variants kept as fallback in case the feed is updated.
    matches = raw_df[
        names.isin({n.upper() for n in NIFTY250_NAMES})
        | names.str.contains("LARGEMID250", na=False)
    ]
    if matches.empty:
        return None

    value = pd.to_numeric(matches["last"], errors="coerce").dropna()
    if value.empty:
        return None

    price = float(value.iloc[0])
    return price if price > 0 else None


def fetch_price_index_from_nselib() -> float | None:
    """Fetch the Nifty 250 price index level from nselib.

    nselib uses NSE's allIndices endpoint and does not provide TRI for this
    index, so this is an NSE-origin price-index fallback only.
    """
    try:
        from nselib import indices
    except ImportError:
        print("  nselib not installed — skipping nselib benchmark fallback.")
        return None

    try:
        raw = indices.live_index_performances()
        return _normalise_nselib_index_df(raw)
    except Exception as e:
        print(f"  nselib benchmark fallback failed: {e}")
        return None


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
    """Fetch the latest Nifty 250 benchmark level and append it to data/market/benchmark.csv.

    Tries the NSE TRI endpoint first; falls back to NSE price index via nselib,
    then yfinance price index. Exits with code 1 if all sources fail.
    """
    BENCHMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
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
        print("  NSE JSON unavailable. Trying nselib price-index fallback...")
        price_index = fetch_price_index_from_nselib()
        if price_index is not None:
            source = "price_index_nse"
            print(f"  nselib NIFTY 250 price index: {price_index:.2f}")
        else:
            print("  nselib unavailable. Trying yfinance fallback...")
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
