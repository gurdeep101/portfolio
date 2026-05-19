"""Fetch P/E ratios for Nifty 250 stocks via nselib (NSE archives).

For each ISO week that has a prices file in data/market/prices/ but no
corresponding fundamentals file, fetches real point-in-time P/E from the
NSE archive for that week's trading date using nselib.capital_market.pe_ratio().

Only P/E is available from NSE archives; all other fundamental fields are omitted.

Writes data/market/fundamentals/YYYY-WW.json per missing week.
Runtime: fast — one network call per week.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from nselib import capital_market as nse_cm

sys.path.insert(0, str(Path(__file__).parent.parent))
from portfolio_types import FundamentalsEntry

DATA_DIR = Path(__file__).parent.parent.parent / "data"
FUNDAMENTALS_DIR = DATA_DIR / "market" / "fundamentals"
PRICES_DIR = DATA_DIR / "market" / "prices"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch P/E ratios for Nifty 250 stocks for all price weeks."
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite even if the output file already exists.",
    )
    return p.parse_args()


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[tuple[str, str]]:
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    import pandas as pd
    try:
        df = pd.read_csv(UNIVERSE_FILE)
    except Exception as e:
        print(f"ERROR: Could not read universe.csv: {e}")
        sys.exit(1)
    sector_col = "sector" if "sector" in df.columns else None
    return [
        (row["symbol"].strip(), str(row[sector_col]) if sector_col else "Unknown")
        for _, row in df.iterrows()
    ]


def weeks_to_process(force: bool) -> list[tuple[str, Path]]:
    """Return sorted list of (week_str, csv_path) that need a fundamentals file."""
    price_files = sorted(PRICES_DIR.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].csv"))
    result = []
    for f in price_files:
        week_str = f.stem
        out_file = FUNDAMENTALS_DIR / f"{week_str}.json"
        if force or not out_file.exists():
            result.append((week_str, f))
    return result


def get_week_date(csv_path: Path) -> date | None:
    """Return the latest trading date found in a prices CSV."""
    import pandas as pd
    try:
        df = pd.read_csv(csv_path, usecols=["date"])
        dates = pd.to_datetime(df["date"], errors="coerce")
        dates = dates.dropna()
        if dates.empty:
            return None
        return dates.max().date()
    except Exception:
        return None


def fetch_pe_from_nselib(d: date) -> dict[str, float | None]:
    """Return symbol → P/E mapping for date *d* using NSE archives.

    Tries *d* first, then steps back up to 3 days to skip weekends/holidays.
    Returns an empty dict if all attempts fail.
    """
    for delta in range(4):
        attempt = d - timedelta(days=delta)
        date_str = attempt.strftime("%d-%m-%Y")
        try:
            df = nse_cm.pe_ratio(date_str)
            if df is None or df.empty:
                continue
            pe_cols = [c for c in df.columns if "P/E" in c or c.upper().replace(" ", "") in ("PE", "SYMBOLPE")]
            if not pe_cols:
                print(f"  WARNING: no P/E column found in nselib response for {date_str}")
                continue
            pe_col = pe_cols[0]
            sym_col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
            result: dict[str, float | None] = {}
            for _, row in df.iterrows():
                sym = str(row[sym_col]).strip()
                try:
                    val = float(row[pe_col])
                    result[sym] = val if val > 0 else None
                except (ValueError, TypeError):
                    result[sym] = None
            print(f"  nselib pe_ratio: {len(result)} symbols (date used: {date_str})")
            return result
        except Exception as e:
            print(f"  WARNING: nselib pe_ratio failed for {date_str}: {e}")
    return {}


def build_fundamentals(
    d: date,
    symbols: list[tuple[str, str]],
    pe_map: dict[str, float | None],
) -> dict[str, FundamentalsEntry]:
    """Build a FundamentalsEntry per symbol from nselib P/E data."""
    fetch_date = d.isoformat()
    return {
        symbol: FundamentalsEntry(
            pe_ratio=pe_map.get(symbol),
            sector=sector,
            fetch_date=fetch_date,
            source="nselib",
        )
        for symbol, sector in symbols
    }


def main() -> None:
    args = parse_args()
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

    weeks = weeks_to_process(args.force)
    if not weeks:
        print("All fundamentals files up to date. Use --force to re-fetch.")
        sys.exit(0)

    print(f"Weeks to process: {len(weeks)} ({'forced' if args.force else 'missing only'})")
    symbols = load_symbols()
    written = 0

    for week_str, csv_path in weeks:
        d = get_week_date(csv_path)
        if d is None:
            print(f"  SKIP {week_str}: could not read trading date from {csv_path.name}")
            continue

        print(f"Processing week {week_str} (date {d})...")
        pe_map = fetch_pe_from_nselib(d)
        if not pe_map:
            print(f"  WARNING: no P/E data returned for {week_str}, writing nulls")

        result = build_fundamentals(d, symbols, pe_map)

        out_file = FUNDAMENTALS_DIR / f"{week_str}.json"
        try:
            with open(out_file, "w") as f:
                json.dump(result, f, indent=2)
            written += 1
            print(f"  OK: {out_file.name} written ({len(result)} symbols)")
        except OSError as e:
            print(f"  ERROR: Could not write {out_file}: {e}")
            sys.exit(1)

    print(f"\nDone. {written} file(s) written.")


if __name__ == "__main__":
    main()
