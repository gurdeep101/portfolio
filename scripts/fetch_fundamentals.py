"""Fetch P/E, P/B, ROE, and market cap for Nifty 250 stocks via yfinance.

Writes data/fundamentals/YYYY-WW.json.
Skips symbols fetched within the last 7 days (reads prior week's file).
Appends to data/missing_fundamentals_log.csv for symbols missing ROE or PB.

Runtime: 5–15 minutes. The .info endpoint is sequential — no batch API exists.
"""

import csv
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
MISSING_LOG = DATA_DIR / "missing_fundamentals_log.csv"

CACHE_AGE_DAYS = 7
SLEEP_BETWEEN_CALLS = 0.5


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[tuple[str, str]]:
    """Returns list of (symbol, sector) tuples."""
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    import pandas as pd
    df = pd.read_csv(UNIVERSE_FILE)
    sector_col = "sector" if "sector" in df.columns else None
    result = []
    for _, row in df.iterrows():
        sector = row[sector_col] if sector_col else "Unknown"
        result.append((row["symbol"].strip(), str(sector)))
    return result


def load_prior_cache() -> dict:
    """Load most recent fundamentals file as a warm cache."""
    files = sorted(FUNDAMENTALS_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
            return data
        except Exception:
            continue
    return {}


def is_fresh(entry: dict) -> bool:
    fetch_date_str = entry.get("fetch_date")
    if not fetch_date_str:
        return False
    try:
        fetch_date = datetime.strptime(fetch_date_str, "%Y-%m-%d").date()
        return (date.today() - fetch_date).days < CACHE_AGE_DAYS
    except ValueError:
        return False


def fetch_info(symbol: str) -> dict:
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        info = ticker.info
        if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
            return {}
        return info
    except Exception:
        return {}


def extract_fields(info: dict, symbol: str, sector: str) -> dict:
    market_cap_raw = info.get("marketCap")
    market_cap_cr = round(market_cap_raw / 1e7, 2) if market_cap_raw else None

    return {
        "pe_ratio": info.get("trailingPE"),
        "pb_ratio": info.get("priceToBook"),
        "roe": info.get("returnOnEquity"),
        "market_cap_cr": market_cap_cr,
        "sector": info.get("sector") or sector,
        "fetch_date": date.today().isoformat(),
        "source": "yfinance",
    }


def log_missing(symbol: str, missing_fields: list[str], session_date: str):
    file_exists = MISSING_LOG.exists()
    with open(MISSING_LOG, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "symbol", "missing_field"])
        for field in missing_fields:
            writer.writerow([session_date, symbol, field])


def main():
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

    today = date.today()
    week_str = iso_week_str(today)
    out_file = FUNDAMENTALS_DIR / f"{week_str}.json"

    symbols = load_symbols()
    total = len(symbols)
    print(f"Fetching fundamentals for {total} symbols (week {week_str})")

    cache = load_prior_cache()
    result: dict = {}
    missing_count = 0
    skipped_fresh = 0
    fetched_count = 0

    for i, (symbol, sector) in enumerate(symbols):
        prior = cache.get(symbol, {})
        if prior and is_fresh(prior):
            result[symbol] = prior
            skipped_fresh += 1
            continue

        info = fetch_info(symbol)
        if not info:
            print(f"  [{i+1}/{total}] {symbol}: no data from yfinance")
            result[symbol] = {"fetch_date": today.isoformat(), "source": "yfinance", "error": "no_data"}
            missing_fields = ["roe", "pb_ratio"]
            log_missing(symbol, missing_fields, today.isoformat())
            missing_count += 1
        else:
            entry = extract_fields(info, symbol, sector)
            result[symbol] = entry
            fetched_count += 1

            missing_fields = []
            if entry["roe"] is None:
                missing_fields.append("roe")
            if entry["pb_ratio"] is None:
                missing_fields.append("pb_ratio")
            if missing_fields:
                log_missing(symbol, missing_fields, today.isoformat())
                missing_count += 1

            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{total}] fetched {fetched_count} so far, {missing_count} with missing fields")

        time.sleep(SLEEP_BETWEEN_CALLS)

    with open(out_file, "w") as f:
        json.dump(result, f, indent=2)

    null_roe = sum(1 for v in result.values() if v.get("roe") is None)
    null_pb = sum(1 for v in result.values() if v.get("pb_ratio") is None)
    print(f"\nOK: {out_file.name} written")
    print(f"  Fresh from cache: {skipped_fresh}  Fetched: {fetched_count}  Errors: {missing_count}")
    print(f"  Missing ROE: {null_roe}/{total}  Missing PB: {null_pb}/{total}")
    if null_roe + null_pb > 0:
        print(f"  Exclusions logged to {MISSING_LOG.name}")


if __name__ == "__main__":
    main()
