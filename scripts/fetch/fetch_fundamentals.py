"""Fetch P/E, P/B, ROE, and market cap for Nifty 250 stocks.

For the current ISO week: fetches via yfinance (P/E, P/B, ROE, market_cap).
For historical weeks (matching files in data/market/prices/): fetches real
point-in-time P/E from NSE archives via nselib; P/B, ROE, and market_cap are
null (not available from any free historical source).

Writes data/market/fundamentals/YYYY-WW.json for each week that has a
corresponding data/market/prices/YYYY-WW.csv but no fundamentals file yet.
Appends to data/market/missing_fundamentals_log.csv for any symbol with null
ROE or PB on the current week.

Runtime: historical weeks are fast (one nselib call per week); the current
week takes 5–15 minutes (sequential yfinance .info calls, no batch API).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
from nselib import capital_market as nse_cm

sys.path.insert(0, str(Path(__file__).parent.parent))
from portfolio_types import FundamentalsEntry

DATA_DIR = Path(__file__).parent.parent.parent / "data"
FUNDAMENTALS_DIR = DATA_DIR / "market" / "fundamentals"
PRICES_DIR = DATA_DIR / "market" / "prices"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"
MISSING_LOG = DATA_DIR / "market" / "missing_fundamentals_log.csv"

CACHE_AGE_DAYS = 7
SLEEP_BETWEEN_CALLS = 0.5


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch fundamentals for Nifty 250 stocks for all price weeks."
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
    except (OSError, Exception) as e:
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
        last = df["date"].max()
        return datetime.strptime(str(last), "%Y-%m-%d").date()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Historical (nselib) path
# ---------------------------------------------------------------------------

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
            # Find the P/E column (column names come from NSE CSV, not hardcoded)
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


def build_historical_fundamentals(
    d: date,
    symbols: list[tuple[str, str]],
    pe_map: dict[str, float | None],
) -> dict[str, FundamentalsEntry]:
    """Build a FundamentalsEntry for each symbol from nselib P/E data.

    P/B, ROE, and market_cap are null — not available historically.
    """
    fetch_date = d.isoformat()
    result: dict[str, FundamentalsEntry] = {}
    for symbol, sector in symbols:
        result[symbol] = FundamentalsEntry(
            pe_ratio=pe_map.get(symbol),
            pb_ratio=None,
            roe=None,
            market_cap_cr=None,
            sector=sector,
            fetch_date=fetch_date,
            source="nselib",
        )
    return result


# ---------------------------------------------------------------------------
# Current-week (yfinance) path — logic unchanged from original main()
# ---------------------------------------------------------------------------

def load_prior_cache() -> dict[str, FundamentalsEntry]:
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
    fetch_date_str = entry.get("fetch_date")
    if not fetch_date_str:
        return False
    try:
        fetch_date = datetime.strptime(fetch_date_str, "%Y-%m-%d").date()
        return (date.today() - fetch_date).days < CACHE_AGE_DAYS
    except ValueError:
        return False


def fetch_info(symbol: str) -> dict[str, Any]:
    try:
        ticker = yf.Ticker(f"{symbol}.NS")
        info = ticker.info
        if not info or (
            info.get("regularMarketPrice") is None
            and info.get("currentPrice") is None
        ):
            return {}
        return dict(info)
    except Exception as e:
        print(f"  WARNING: yfinance .info failed for {symbol}: {e}")
        return {}


def fetch_roe_from_financials(symbol: str) -> float | None:
    try:
        import pandas as pd
        ticker = yf.Ticker(f"{symbol}.NS")
        fin = ticker.financials
        bs = ticker.balance_sheet
        if fin is None or fin.empty or bs is None or bs.empty:
            return None
        net_income: float | None = None
        for key in ("Net Income", "NetIncome", "Net Income Common Stockholders"):
            if key in fin.index:
                val = fin.loc[key].iloc[0]
                if pd.notna(val):
                    net_income = float(val)
                    break
        equity: float | None = None
        for key in ("Stockholders Equity", "Total Stockholder Equity",
                    "Common Stock Equity", "Stockholders' Equity"):
            if key in bs.index:
                val = bs.loc[key].iloc[0]
                if pd.notna(val):
                    equity = float(val)
                    break
        if net_income is None or equity is None or equity == 0:
            return None
        return net_income / equity
    except Exception:
        return None


def extract_fields(info: dict[str, Any], symbol: str, sector: str) -> FundamentalsEntry:
    market_cap_raw = info.get("marketCap")
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
    file_exists = MISSING_LOG.exists()
    try:
        with open(MISSING_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["date", "symbol", "missing_field"])
            for field in missing_fields:
                writer.writerow([session_date, symbol, field])
    except OSError as e:
        print(f"  WARNING: Could not write to missing_fundamentals_log.csv: {e}")


def fetch_with_yfinance(symbols: list[tuple[str, str]]) -> dict[str, FundamentalsEntry]:
    """Fetch fundamentals for all symbols via yfinance with warm-cache logic."""
    today = date.today()
    total = len(symbols)
    cache = load_prior_cache()
    result: dict[str, FundamentalsEntry] = {}
    missing_count = 0
    skipped_fresh = 0
    fetched_count = 0
    roe_recovered = 0

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
            fetched_count += 1

            if entry.get("roe") is None:
                time.sleep(SLEEP_BETWEEN_CALLS)
                roe_calc = fetch_roe_from_financials(symbol)
                if roe_calc is not None:
                    entry["roe"] = roe_calc
                    roe_recovered += 1

            result[symbol] = entry

            missing_fields = [
                field for field in ("roe", "pb_ratio") if entry.get(field) is None
            ]
            if missing_fields:
                log_missing(symbol, missing_fields, today.isoformat())
                missing_count += 1

        if (i + 1) % 25 == 0:
            print(f"  [{i + 1}/{total}] fetched {fetched_count}, {missing_count} with missing fields")

        time.sleep(SLEEP_BETWEEN_CALLS)

    null_roe = sum(1 for v in result.values() if v.get("roe") is None)
    null_pb = sum(1 for v in result.values() if v.get("pb_ratio") is None)
    print(f"  Fresh from cache: {skipped_fresh}  Fetched: {fetched_count}  Errors: {missing_count}")
    print(f"  ROE recovered via financials fallback: {roe_recovered}")
    print(f"  Missing ROE: {null_roe}/{total}  Missing PB: {null_pb}/{total}")
    if null_roe + null_pb > 0:
        print(f"  Exclusions logged to {MISSING_LOG.name}")
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    FUNDAMENTALS_DIR.mkdir(parents=True, exist_ok=True)

    today_week = iso_week_str(date.today())
    weeks = weeks_to_process(args.force)

    if not weeks:
        print("All fundamentals files up to date. Use --force to re-fetch.")
        sys.exit(0)

    print(f"Weeks to process: {len(weeks)} ({'forced' if args.force else 'missing only'})")
    symbols = load_symbols()

    for week_str, csv_path in weeks:
        out_file = FUNDAMENTALS_DIR / f"{week_str}.json"

        if week_str == today_week:
            print(f"\nProcessing current week {week_str} via yfinance ({len(symbols)} symbols)...")
            result = fetch_with_yfinance(symbols)
        else:
            d = get_week_date(csv_path)
            if d is None:
                print(f"  SKIP {week_str}: could not read trading date from {csv_path.name}")
                continue
            print(f"Processing historical week {week_str} (date {d}) via nselib...")
            pe_map = fetch_pe_from_nselib(d)
            if not pe_map:
                print(f"  WARNING: no P/E data returned for {week_str}, writing nulls")
            result = build_historical_fundamentals(d, symbols, pe_map)

        try:
            with open(out_file, "w") as f:
                json.dump(result, f, indent=2)
            print(f"  OK: {out_file.name} written ({len(result)} symbols)")
        except OSError as e:
            print(f"  ERROR: Could not write {out_file}: {e}")
            sys.exit(1)

    print(f"\nDone. {len(weeks)} file(s) written.")


if __name__ == "__main__":
    main()
