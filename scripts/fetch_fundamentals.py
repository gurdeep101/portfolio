"""Fetch P/E, P/B, ROE, and market cap for Nifty 250 stocks via yfinance.

Writes data/fundamentals/YYYY-WW.json.
Skips symbols fetched within the last CACHE_AGE_DAYS days by re-using the
most recent fundamentals file as a warm cache.
Appends to data/missing_fundamentals_log.csv for any symbol with null ROE or PB —
those stocks are excluded from ranking each week.

Runtime: 5–15 minutes on a full run (sequential .info calls, no batch API).
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yfinance as yf

# Make portfolio_types importable when running as `uv run python scripts/foo.py`
sys.path.insert(0, str(Path(__file__).parent))
from portfolio_types import FundamentalsEntry

DATA_DIR = Path(__file__).parent.parent / "data"
FUNDAMENTALS_DIR = DATA_DIR / "fundamentals"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
MISSING_LOG = DATA_DIR / "missing_fundamentals_log.csv"

CACHE_AGE_DAYS = 7       # skip symbols fetched more recently than this
SLEEP_BETWEEN_CALLS = 0.5  # seconds between yfinance .info calls to avoid rate-limits


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[tuple[str, str]]:
    """Read data/universe.csv and return a list of (symbol, sector) pairs.

    Exits with code 1 if the universe file is missing.
    """
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    import pandas as pd
    try:
        df = pd.read_csv(UNIVERSE_FILE)
    except (OSError, Exception) as e:
        print(f"ERROR: Could not read universe.csv: {e}")
        sys.exit(1)
    sector_col = "sector" if "sector" in df.columns else None
    return [
        (row["symbol"].strip(), str(row[sector_col]) if sector_col else "Unknown")
        for _, row in df.iterrows()
    ]


def load_prior_cache() -> dict[str, FundamentalsEntry]:
    """Load the most recent fundamentals JSON file as a warm cache.

    Iterates candidate files newest-first and returns the first one that
    can be read successfully. Returns an empty dict if none exist or are readable.
    """
    files = sorted(FUNDAMENTALS_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            with open(f) as fh:
                data: dict[str, FundamentalsEntry] = json.load(fh)
            return data
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARNING: Could not read cache file {f.name}: {e}")
            continue
    return {}


def is_fresh(entry: FundamentalsEntry) -> bool:
    """Return True if *entry* has a fetch_date within CACHE_AGE_DAYS of today."""
    fetch_date_str = entry.get("fetch_date")
    if not fetch_date_str:
        return False
    try:
        fetch_date = datetime.strptime(fetch_date_str, "%Y-%m-%d").date()
        return (date.today() - fetch_date).days < CACHE_AGE_DAYS
    except ValueError:
        return False


def fetch_info(symbol: str) -> dict[str, Any]:
    """Fetch the raw yfinance .info dict for *symbol* (NSE-suffixed internally).

    Returns an empty dict if yfinance returns no data or raises any exception.
    yfinance raises varied, undocumented exception types depending on the Yahoo
    Finance API version — catching broadly here is intentional.
    """
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        info = ticker.info
        # A minimal health check: at least one price field must be present.
        if not info or (
            info.get("regularMarketPrice") is None
            and info.get("currentPrice") is None
        ):
            return {}
        return dict(info)  # cast to plain dict[str, Any] to satisfy return type
    except Exception as e:
        # yfinance raises varied undocumented exceptions; log and continue.
        print(f"  WARNING: yfinance .info failed for {symbol}: {e}")
        return {}


def extract_fields(info: dict[str, Any], symbol: str, sector: str) -> FundamentalsEntry:
    """Extract relevant fundamental fields from a raw yfinance info dict.

    Args:
        info:   Raw dict from yf.Ticker(symbol).info.
        symbol: NSE symbol string (without .NS suffix).
        sector: Fallback sector string used if not present in *info*.

    Returns:
        FundamentalsEntry with pe_ratio, pb_ratio, roe, market_cap_cr, sector,
        fetch_date, and source populated (values may be None).
    """
    market_cap_raw = info.get("marketCap")
    # yfinance returns market cap in the local currency for .NS tickers (INR).
    # Convert to INR crore (1 crore = 10,000,000).
    market_cap_cr = round(market_cap_raw / 1e7, 2) if market_cap_raw else None

    return FundamentalsEntry(
        pe_ratio=info.get("trailingPE"),
        pb_ratio=info.get("priceToBook"),
        roe=info.get("returnOnEquity"),
        market_cap_cr=market_cap_cr,
        sector=info.get("sector") or sector,
        fetch_date=date.today().isoformat(),
        source="yfinance",
    )


def log_missing(symbol: str, missing_fields: list[str], session_date: str) -> None:
    """Append missing-field rows to data/missing_fundamentals_log.csv.

    Creates the file with a header row on first write.
    """
    file_exists = MISSING_LOG.exists()
    try:
        with open(MISSING_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "symbol", "missing_field"])
            for field in missing_fields:
                writer.writerow([session_date, symbol, field])
    except OSError as e:
        # Non-fatal: the main data pipeline can continue without the log.
        print(f"  WARNING: Could not write to missing_fundamentals_log.csv: {e}")


def main() -> None:
    """Fetch fundamentals for all Nifty 250 symbols and write to data/fundamentals/YYYY-WW.json.

    Uses a CACHE_AGE_DAYS-day in-memory cache to avoid refetching recently
    updated symbols. Logs any symbol with null ROE or PB to the missing log.
    """
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

    today = date.today()
    week_str = iso_week_str(today)
    out_file = FUNDAMENTALS_DIR / f"{week_str}.json"

    symbols = load_symbols()
    total = len(symbols)
    print(f"Fetching fundamentals for {total} symbols (week {week_str})")

    cache = load_prior_cache()
    result: dict[str, FundamentalsEntry] = {}
    missing_count = 0
    skipped_fresh = 0
    fetched_count = 0

    for i, (symbol, sector) in enumerate(symbols):
        prior = cache.get(symbol, FundamentalsEntry())
        if prior and is_fresh(prior):
            result[symbol] = prior
            skipped_fresh += 1
            continue

        info = fetch_info(symbol)
        if not info:
            result[symbol] = FundamentalsEntry(
                fetch_date=today.isoformat(), source="yfinance", error="no_data"
            )
            log_missing(symbol, ["roe", "pb_ratio"], today.isoformat())
            missing_count += 1
        else:
            entry = extract_fields(info, symbol, sector)
            result[symbol] = entry
            fetched_count += 1

            # Identify and log any missing key fundamental fields.
            missing_fields = [
                field
                for field in ("roe", "pb_ratio")
                if entry.get(field) is None
            ]
            if missing_fields:
                log_missing(symbol, missing_fields, today.isoformat())
                missing_count += 1

        # Progress heartbeat every 25 symbols so the user can see activity.
        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{total}] fetched {fetched_count}, {missing_count} with missing fields")

        time.sleep(SLEEP_BETWEEN_CALLS)

    # --- Write output --------------------------------------------------------
    try:
        with open(out_file, "w") as f:
            json.dump(result, f, indent=2)
    except OSError as e:
        print(f"ERROR: Could not write {out_file}: {e}")
        sys.exit(1)

    null_roe = sum(1 for v in result.values() if v.get("roe") is None)
    null_pb = sum(1 for v in result.values() if v.get("pb_ratio") is None)

    print(f"\nOK: {out_file.name} written")
    print(f"  Fresh from cache: {skipped_fresh}  Fetched: {fetched_count}  Errors: {missing_count}")
    print(f"  Missing ROE: {null_roe}/{total}  Missing PB: {null_pb}/{total}")
    if null_roe + null_pb > 0:
        print(f"  Exclusions logged to {MISSING_LOG.name}")


if __name__ == "__main__":
    main()
