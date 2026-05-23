"""Fetch Nifty LargeMidcap 250 constituent list from NSE and write to data/universe/universe.csv.

Uses nselib.indices.constituent_stock_list(), which downloads the index composition
CSV from NSE's archives subdomain (no Akamai bot-protection issues).

Self-throttles: skips if universe.csv is less than MAX_AGE_DAYS old.
Use --force to override. Exits with code 1 on download failure without touching
the existing file.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent.parent.parent / "data"
OUT_FILE = DATA_DIR / "universe" / "universe.csv"

MAX_AGE_DAYS = 90           # refresh cadence — matches NSE semi-annual reindex
MIN_ROWS = 240              # sanity bound: fewer rows → partial download
MAX_ROWS = 260              # sanity bound: more rows → unexpected format change
REINDEX_WARN_THRESHOLD = 5  # flag if symbol count changes by more than this

NSE_INDEX_CATEGORY = "BroadMarketIndices"
NSE_INDEX_NAME = "Nifty LargeMidcap 250"


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


def fetch_constituent_list() -> pd.DataFrame:
    """Download the Nifty LargeMidcap 250 constituent list via nselib.

    nselib downloads the index composition CSV from NSE's archives subdomain,
    which is not behind the same Akamai bot-protection as the live API.

    Returns:
        DataFrame with columns [symbol, company_name, series, isin_code, sector].

    Raises:
        RuntimeError: if nselib raises or returns an empty/malformed DataFrame.
    """
    try:
        from nselib.indices import index_data
    except ImportError:
        raise RuntimeError("nselib not installed. Run: uv sync")

    try:
        raw: pd.DataFrame = index_data.constituent_stock_list(
            NSE_INDEX_CATEGORY, NSE_INDEX_NAME
        )
    except Exception as exc:
        raise RuntimeError(f"nselib fetch failed: {exc}") from exc

    if raw is None or raw.empty:
        raise RuntimeError("nselib returned an empty constituent list")

    df = pd.DataFrame({
        "symbol": raw["Symbol"].str.strip(),
        "company_name": raw["Company Name"],
        "series": raw["Series"],
        "isin_code": raw["ISIN Code"],
        "sector": raw["Industry"],
    })
    return df[df["symbol"].str.len() > 0]


def main() -> None:
    """Fetch the Nifty LargeMidcap 250 constituent list and write to data/universe/universe.csv.

    Skips the download if universe.csv is less than MAX_AGE_DAYS old.
    Exits with code 1 without overwriting the existing file on any failure.
    """
    parser = argparse.ArgumentParser(
        description="Fetch Nifty LargeMidcap 250 universe from NSE"
    )
    parser.add_argument(
        "--force", action="store_true", help="Ignore age check and force re-download"
    )
    args = parser.parse_args()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

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

    # --- Fetch via nselib -----------------------------------------------------
    print(f"Downloading {NSE_INDEX_NAME} list from NSE...")
    try:
        df = fetch_constituent_list()
    except Exception as e:
        print(f"ERROR: Download failed: {e}")
        print("Keeping existing universe.csv unchanged.")
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
