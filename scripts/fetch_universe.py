"""Fetch Nifty 250 constituent list from NSE and write to data/universe.csv.

Self-throttles: skips the download if universe.csv is less than 90 days old.
Use --force to override. Exits with code 1 on download failure without touching
the existing file.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_FILE = DATA_DIR / "universe.csv"
NSE_URL = "https://archives.nseindia.com/content/indices/ind_nifty250list.csv"

# Browser-like headers required — NSE blocks the default Python user-agent.
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

MAX_AGE_DAYS = 90           # refresh cadence — matches NSE semi-annual reindex
MIN_ROWS = 240              # sanity bound: fewer rows → partial download
MAX_ROWS = 260              # sanity bound: more rows → unexpected format change
REINDEX_WARN_THRESHOLD = 5  # flag if symbol count changes by more than this


def is_fresh(path: Path, max_age_days: int) -> bool:
    """Return True if *path* exists and was last modified within *max_age_days* days."""
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=max_age_days)


def load_existing_symbols(path: Path) -> set[str]:
    """Return the set of symbol strings from an existing universe CSV.

    Returns an empty set if the file is missing, unreadable, or malformed.
    """
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
        return set(df["symbol"].tolist())
    except (OSError, pd.errors.ParserError, KeyError, ValueError):
        return set()


def fetch(url: str) -> pd.DataFrame:
    """Download the CSV at *url* with NSE-compatible headers and parse it.

    Raises:
        requests.RequestException: on any network or HTTP error.
        ValueError: if the response body cannot be parsed as CSV.
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    from io import StringIO
    return pd.read_csv(StringIO(resp.text))


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Rename NSE column names to internal snake_case names.

    Validates that required columns are present after renaming.

    Raises:
        ValueError: if required columns are missing after renaming.
    """
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
        raise ValueError(
            f"Missing columns after normalisation: {missing}. Got: {list(df.columns)}"
        )

    df["symbol"] = df["symbol"].str.strip()

    # Keep only known columns that are present; sector may be absent in some NSE formats.
    keep = ["symbol", "company_name", "series", "isin_code"]
    if "sector" in df.columns:
        keep.append("sector")
    return df[keep]


def main() -> None:
    """Download the Nifty 250 constituent list and write to data/universe.csv.

    Skips the download if universe.csv is less than MAX_AGE_DAYS old.
    Exits with code 1 without overwriting the existing file on any failure.
    """
    parser = argparse.ArgumentParser(description="Fetch Nifty 250 universe from NSE")
    parser.add_argument(
        "--force", action="store_true", help="Ignore age check and force re-download"
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # --- Age check -----------------------------------------------------------
    if not args.force and is_fresh(OUT_FILE, MAX_AGE_DAYS):
        age_days = (
            datetime.now() - datetime.fromtimestamp(OUT_FILE.stat().st_mtime)
        ).days
        print(
            f"universe.csv is {age_days} days old (< {MAX_AGE_DAYS}). "
            "Skipping. Use --force to override."
        )
        sys.exit(0)

    prev_symbols = load_existing_symbols(OUT_FILE)

    # --- Download ------------------------------------------------------------
    print("Downloading Nifty 250 list from NSE...")
    try:
        df = fetch(NSE_URL)
    except requests.RequestException as e:
        print(f"ERROR: Download failed: {e}")
        print("Keeping existing universe.csv unchanged.")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: Could not parse downloaded CSV: {e}")
        sys.exit(1)

    # --- Normalise columns ---------------------------------------------------
    try:
        df = normalise(df)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # --- Row count sanity check ----------------------------------------------
    row_count = len(df)
    if not (MIN_ROWS <= row_count <= MAX_ROWS):
        print(
            f"ERROR: Row count {row_count} is outside expected range "
            f"{MIN_ROWS}–{MAX_ROWS}. Aborting to avoid overwriting good data."
        )
        sys.exit(1)

    # --- Reindex change detection --------------------------------------------
    new_symbols = set(df["symbol"].tolist())
    if prev_symbols:
        added = new_symbols - prev_symbols
        removed = prev_symbols - new_symbols
        delta = len(added) + len(removed)
        if delta > REINDEX_WARN_THRESHOLD:
            print("WARNING: Large index change detected — possible reindex event!")
            print(f"  Added   ({len(added)}): {sorted(added)}")
            print(f"  Removed ({len(removed)}): {sorted(removed)}")
            print("  Verify this change is intentional before proceeding.")
        elif added or removed:
            print(f"  Minor index change: +{sorted(added)} -{sorted(removed)}")

    # --- Write ---------------------------------------------------------------
    try:
        df.to_csv(OUT_FILE, index=False)
    except OSError as e:
        print(f"ERROR: Could not write {OUT_FILE}: {e}")
        sys.exit(1)

    print(f"OK: {row_count} stocks written to {OUT_FILE}")


if __name__ == "__main__":
    main()
