"""Fetch Nifty 250 constituent list from NSE and write to data/universe.csv.

Self-throttles: skips the download if universe.csv is less than 90 days old.
Use --force to override. Exits with code 1 on download failure without touching
the existing file.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_FILE = DATA_DIR / "universe.csv"
NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty250list.csv"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
}
MAX_AGE_DAYS = 90
MIN_ROWS = 240
MAX_ROWS = 260
REINDEX_WARN_THRESHOLD = 5


def is_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=max_age_days)


def load_existing_symbols(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
        return set(df["symbol"].tolist())
    except Exception:
        return set()


def fetch(url: str) -> pd.DataFrame:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    from io import StringIO
    df = pd.read_csv(StringIO(resp.text))
    return df


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {
        "Symbol": "symbol",
        "Company Name": "company_name",
        "Series": "series",
        "ISIN Code": "isin_code",
        "Industry": "sector",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    required = ["symbol", "company_name", "isin_code"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns after normalisation: {missing}. Got: {list(df.columns)}")
    df["symbol"] = df["symbol"].str.strip()
    return df[["symbol", "company_name", "series", "isin_code", "sector"] if "sector" in df.columns else ["symbol", "company_name", "series", "isin_code"]]


def main():
    parser = argparse.ArgumentParser(description="Fetch Nifty 250 universe from NSE")
    parser.add_argument("--force", action="store_true", help="Ignore age check and force download")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not args.force and is_fresh(OUT_FILE, MAX_AGE_DAYS):
        age_days = (datetime.now() - datetime.fromtimestamp(OUT_FILE.stat().st_mtime)).days
        print(f"universe.csv is {age_days} days old (< {MAX_AGE_DAYS}). Skipping. Use --force to override.")
        sys.exit(0)

    prev_symbols = load_existing_symbols(OUT_FILE)

    print(f"Downloading Nifty 250 list from NSE...")
    try:
        df = fetch(NSE_URL)
    except requests.RequestException as e:
        print(f"ERROR: Download failed: {e}")
        print("Keeping existing universe.csv unchanged.")
        sys.exit(1)

    try:
        df = normalise(df)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    row_count = len(df)
    if not (MIN_ROWS <= row_count <= MAX_ROWS):
        print(f"ERROR: Row count {row_count} outside expected range {MIN_ROWS}–{MAX_ROWS}. Aborting.")
        sys.exit(1)

    new_symbols = set(df["symbol"].tolist())
    if prev_symbols:
        added = new_symbols - prev_symbols
        removed = prev_symbols - new_symbols
        if len(added) + len(removed) > REINDEX_WARN_THRESHOLD:
            print(f"WARNING: Large index change detected!")
            print(f"  Added ({len(added)}): {sorted(added)}")
            print(f"  Removed ({len(removed)}): {sorted(removed)}")
            print("  This may indicate a reindex event — verify manually.")
        elif added or removed:
            print(f"  Index changes: +{sorted(added)} -{sorted(removed)}")

    df.to_csv(OUT_FILE, index=False)
    print(f"OK: {row_count} stocks written to {OUT_FILE}")


if __name__ == "__main__":
    main()
