"""Fetch weekly OHLCV and cumulative daily adj_close for all Nifty 250 symbols.

Writes:
  data/prices/YYYY-WW.csv          — weekly OHLCV snapshot (immutable once written)
  data/prices/daily_adj_close.csv  — cumulative append-only daily adj_close (wide format)

On first run, pulls HISTORY_WEEKS of history for the daily file.
On subsequent runs, appends only rows newer than the last date already present.
The daily file is never trimmed — scripts that read it filter by date window.

Data sources (tried in order):
  1. PRIMARY  : nselib price_volume_data (per-symbol, CSV-based NSE API)
                Handles >365-day ranges internally by chunking requests.
                Raises NSEdataNotFound on failure (caught and retried).
  2. SECONDARY: jugaad-data stock_df (per-symbol, JSON-based NSE API with disk caching)
                Accepts Python date objects directly (no string conversion needed).
  3. TERTIARY : yfinance per-symbol Ticker.history() for symbols that fail both NSE sources.
                Provides adjusted close prices (not available from NSE APIs).

Why separate from fetch_prices.py:
  fetch_prices.py uses yfinance as primary + NSE via Playwright browser session.
  This script inverts that: NSE-native libraries are primary, yfinance is last resort.
  The two scripts are fully independent — neither imports from the other.

Column schema (all output DataFrames):
  symbol, date, open, high, low, close, adj_close, volume
  adj_close = close for NSE sources (NSE does not publish adjusted prices).
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*utcnow.*")

# ── paths and constants ───────────────────────────────────────────────────────

DATA_DIR      = Path(__file__).parent.parent / "data"
PRICES_DIR    = DATA_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
DAILY_FILE    = PRICES_DIR / "daily_adj_close.csv"

FAILURE_THRESHOLD_PCT = 0.08   # abort if > this fraction return no data from any source
HISTORY_WEEKS         = 52     # weeks of history pulled on first run

# ── rate-limit / retry configuration ─────────────────────────────────────────
# NSE blocks aggressive automated traffic. Randomised sleep between calls
# avoids a fixed-interval pattern that Akamai's bot-detection would fingerprint.
NSE_SLEEP_MIN_S = 0.8   # minimum sleep between per-symbol NSE API calls
NSE_SLEEP_MAX_S = 8.9   # maximum sleep (uniform random in this range)

# Per-symbol exponential backoff: waits 2s, 4s before giving up.
# Three attempts recovers from a brief throttle without stalling a 250-symbol run.
NSE_MAX_RETRIES  = 3
NSE_RETRY_BASE_S = 2.0

# Circuit breaker: if NSE_CIRCUIT_BREAKER consecutive symbols all fail,
# the exchange is likely blocking the session — pause before continuing.
NSE_CIRCUIT_BREAKER = 5
NSE_CIRCUIT_PAUSE_S = 30.0

# yfinance fallback: sleep between per-symbol Ticker.history() calls
YF_SLEEP_S = 0.5


# ── shared helpers ────────────────────────────────────────────────────────────


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[str]:
    """Read data/universe.csv and return plain NSE symbols (no .NS suffix).

    Returns a list like ['RELIANCE', 'TCS', ...].
    Exits with code 1 if the universe file is missing.
    """
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return [s.strip() for s in df["symbol"].tolist()]
    except (OSError, pd.errors.ParserError, KeyError) as e:
        print(f"ERROR: Could not read universe.csv: {e}")
        sys.exit(1)


def load_symbol_meta() -> dict[str, str]:
    """Return {symbol: series} from universe.csv (used to handle non-EQ series)."""
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return dict(zip(df["symbol"].str.strip(), df["series"].str.strip(), strict=False))
    except Exception:
        return {}


# ── output writers ────────────────────────────────────────────────────────────


def write_weekly_snapshot(df: pd.DataFrame, week_str: str) -> None:
    """Write the current-week OHLCV snapshot CSV.

    No-ops if the file already exists to preserve immutability.
    Exits with code 1 on write failure.
    """
    out = PRICES_DIR / f"{week_str}.csv"
    if out.exists():
        print(f"  Weekly snapshot {out.name} already exists — skipping overwrite.")
        return
    try:
        df.to_csv(out, index=False)
        print(f"  Weekly snapshot written: {out.name} ({len(df)} rows)")
    except OSError as e:
        print(f"ERROR: Could not write weekly snapshot {out.name}: {e}")
        sys.exit(1)


def update_daily_file(df: pd.DataFrame) -> None:
    """Append new rows to the cumulative daily adj_close wide-format CSV.

    The file uses dates as the row index and symbols as columns.
    Append-only — historical rows are never removed.
    Exits with code 1 on read or write failure.
    """
    adj = df[["symbol", "date", "adj_close"]].copy()
    adj["date"] = pd.to_datetime(adj["date"]).dt.date
    pivot = adj.pivot_table(index="date", columns="symbol", values="adj_close")
    pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.sort_index()

    if DAILY_FILE.exists():
        try:
            existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)

        last_date = existing.index.max()
        new_rows = pivot[pivot.index > last_date]

        if new_rows.empty:
            print(f"  daily_adj_close.csv already up to date (last: {last_date.date()}).")
            return

        combined = pd.concat([existing, new_rows])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()

        try:
            combined.to_csv(DAILY_FILE)
            print(
                f"  Appended {len(new_rows)} new rows to daily_adj_close.csv "
                f"(total: {len(combined)} rows)"
            )
        except OSError as e:
            print(f"ERROR: Could not write daily_adj_close.csv: {e}")
            sys.exit(1)
    else:
        try:
            pivot.to_csv(DAILY_FILE)
            print(
                f"  Created daily_adj_close.csv "
                f"({len(pivot)} rows, {len(pivot.columns)} symbols)"
            )
        except OSError as e:
            print(f"ERROR: Could not create daily_adj_close.csv: {e}")
            sys.exit(1)


# ── SOURCE 1: nselib ─────────────────────────────────────────────────────────
# nselib.capital_market.price_volume_data(symbol, from_date, to_date)
#
# - Date format: "DD-MM-YYYY" strings
# - Handles >365-day date ranges automatically by chunking internally
# - Output columns (after stripping spaces):
#     Symbol, Series, Date, PrevClose, OpenPrice, HighPrice, LowPrice,
#     LastPrice, ClosePrice, AveragePrice, TotalTradedQuantity, Turnover, No.ofTrades
# - Raises nselib.errors.NSEdataNotFound on HTTP failure or empty response


def _is_nse_retryable(exc: Exception) -> bool:
    """Return True if *exc* is a transient NSE error worth retrying.

    Covers HTTP 403/429 (rate-limit/auth), connection resets, and the
    nselib-specific "Resource not available" message from NSEdataNotFound.
    """
    msg = str(exc).lower()
    return any(k in msg for k in ("403", "429", "too many", "connection", "timeout", "resource not"))


def _normalise_nselib_df(raw_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Map nselib price_volume_data columns to the 8-column standard format.

    nselib strips trailing spaces from column names inconsistently across
    versions — we strip all column names defensively before mapping.
    """
    if raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()
    # Column names sometimes have trailing whitespace depending on nselib version
    df.columns = [str(c).strip() for c in df.columns]

    rename_map = {
        "OpenPrice": "open",
        "HighPrice": "high",
        "LowPrice": "low",
        "ClosePrice": "close",
        "TotalTradedQuantity": "volume",
    }
    missing = [c for c in rename_map if c not in df.columns]
    if missing:
        return pd.DataFrame()

    df = df.rename(columns=rename_map)
    df["symbol"] = symbol
    # NSE does not publish adjusted close prices — use raw close as proxy
    df["adj_close"] = df["close"]
    # nselib dates are strings like "17-Apr-2026"; dayfirst=True handles DD-Mon-YYYY
    df["date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")

    needed = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]
    df = df[needed].dropna(subset=["close", "date"])
    df = df[df["close"] > 0]

    # Drop zero-volume rows where close equals the previous day's close.
    # NSE sometimes repeats the prior session's price on exchange holidays.
    zero_vol = (df["volume"] == 0) & (df["close"] == df["close"].shift(1))
    if zero_vol.any():
        df = df[~zero_vol]

    return df.reset_index(drop=True)


def fetch_symbol_nselib(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Fetch historical OHLCV for one symbol via nselib with exponential-backoff retry.

    Args:
        symbol: Plain NSE symbol string, e.g. 'RELIANCE' (no .NS suffix).
        start:  Fetch window start (inclusive).
        end:    Fetch window end (inclusive).

    Returns:
        DataFrame with 8 standard columns, or empty DataFrame on failure.
    """
    try:
        from nselib import capital_market
    except ImportError:
        print("  nselib not installed — skipping nselib source.")
        return pd.DataFrame()

    # nselib expects DD-MM-YYYY strings, not date objects
    from_str = start.strftime("%d-%m-%Y")
    to_str = end.strftime("%d-%m-%Y")

    for attempt in range(NSE_MAX_RETRIES):
        try:
            raw = capital_market.price_volume_data(symbol, from_str, to_str)
            if raw is None or (hasattr(raw, "empty") and raw.empty):
                return pd.DataFrame()
            return _normalise_nselib_df(raw, symbol)
        except Exception as exc:
            if _is_nse_retryable(exc) and attempt < NSE_MAX_RETRIES - 1:
                wait_s = NSE_RETRY_BASE_S * (2 ** attempt)
                time.sleep(wait_s)
                continue
            # Non-retryable error or retries exhausted — give up for this symbol
            return pd.DataFrame()

    return pd.DataFrame()


def fetch_all_nselib(
    symbols: list[str], start: date, end: date
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch all symbols via nselib with throttling, progress logging, and circuit breaker.

    Args:
        symbols: List of plain NSE symbols (no .NS suffix).
        start:   Fetch window start.
        end:     Fetch window end.

    Returns:
        (combined_df, failed_symbols) where failed_symbols are passed to the
        jugaad-data fallback in the next stage.
    """
    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    consecutive_failures = 0
    total = len(symbols)

    print(f"\nFetching prices via nselib ({total} symbols)…")

    for i, sym in enumerate(symbols, 1):
        df = fetch_symbol_nselib(sym, start, end)

        if df.empty:
            failed.append(sym)
            consecutive_failures += 1
            if consecutive_failures >= NSE_CIRCUIT_BREAKER:
                # Burst of failures suggests the session is being blocked — pause
                # to let NSE's rate-limit window reset before continuing.
                print(
                    f"  Circuit breaker: {NSE_CIRCUIT_BREAKER} consecutive failures, "
                    f"pausing {NSE_CIRCUIT_PAUSE_S:.0f}s…"
                )
                time.sleep(NSE_CIRCUIT_PAUSE_S)
                consecutive_failures = 0
        else:
            frames.append(df)
            consecutive_failures = 0

        if i % 25 == 0 or i == total:
            print(f"  nselib progress: {i}/{total} ({len(frames)} ok, {len(failed)} failed)")

        # Randomised sleep between calls to avoid a fixed-interval fingerprint
        time.sleep(random.uniform(NSE_SLEEP_MIN_S, NSE_SLEEP_MAX_S))

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, failed


# ── SOURCE 2: jugaad-data ────────────────────────────────────────────────────
# jugaad_data.nse.stock_df(symbol, from_date, to_date, series='EQ')
#
# - Date format: Python date objects (unlike nselib which needs strings)
# - Built-in disk caching via platformdirs (re-runs skip API calls for cached dates)
# - Output columns:
#     DATE, SERIES, OPEN, HIGH, LOW, PREV. CLOSE, LTP, CLOSE, VWAP,
#     VOLUME, VALUE, NO OF TRADES, DELIVERY QTY, DELIVERY %, SYMBOL
# - No internal date chunking — relies on NSE's own range limits per request


def _normalise_jugaad_df(raw_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Map jugaad-data stock_df columns to the 8-column standard format."""
    if raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()
    rename_map = {
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "VOLUME": "volume",
        "DATE": "date",
    }
    missing = [c for c in rename_map if c not in df.columns]
    if missing:
        return pd.DataFrame()

    df = df.rename(columns=rename_map)
    df["symbol"] = symbol
    # NSE does not publish adjusted close prices — use raw close as proxy
    df["adj_close"] = df["close"]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    needed = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]
    df = df[needed].dropna(subset=["close", "date"])
    df = df[df["close"] > 0]

    # Same holiday carry-forward guard as nselib normalisation
    zero_vol = (df["volume"] == 0) & (df["close"] == df["close"].shift(1))
    if zero_vol.any():
        df = df[~zero_vol]

    return df.reset_index(drop=True)


def fetch_symbol_jugaad(
    symbol: str, start: date, end: date, series: str = "EQ"
) -> pd.DataFrame:
    """Fetch historical OHLCV for one symbol via jugaad-data.

    Args:
        symbol: Plain NSE symbol string, e.g. 'RELIANCE'.
        start:  Fetch window start (Python date object — jugaad-data expects dates, not strings).
        end:    Fetch window end.
        series: Market series, almost always 'EQ'. Some SME stocks use 'BE' or 'SM'.

    Returns:
        DataFrame with 8 standard columns, or empty DataFrame on failure.
    """
    try:
        from jugaad_data.nse import stock_df
    except ImportError:
        return pd.DataFrame()

    try:
        raw = stock_df(symbol, start, end, series=series)
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return pd.DataFrame()
        return _normalise_jugaad_df(raw, symbol)
    except Exception:
        return pd.DataFrame()


def fetch_failed_via_jugaad(
    symbols: list[str], start: date, end: date, meta: dict[str, str]
) -> tuple[pd.DataFrame, list[str]]:
    """Recover symbols that failed nselib via jugaad-data.

    Called only for the subset of symbols that returned empty from nselib.
    Uses the market series from universe.csv (meta dict) so SME/BE series
    symbols are fetched with the correct series parameter.

    Args:
        symbols: Plain NSE symbols that nselib could not fetch.
        start:   Fetch window start.
        end:     Fetch window end.
        meta:    Dict {symbol: series} loaded from universe.csv.

    Returns:
        (recovered_df, still_missing_symbols)
    """
    if not symbols:
        return pd.DataFrame(), []

    print(f"\n  jugaad-data fallback: recovering {len(symbols)} symbols…")
    frames: list[pd.DataFrame] = []
    still_missing: list[str] = []

    for i, sym in enumerate(symbols, 1):
        series = meta.get(sym, "EQ")
        df = fetch_symbol_jugaad(sym, start, end, series)
        if df.empty:
            still_missing.append(sym)
        else:
            frames.append(df)
        if i % 10 == 0:
            print(f"    jugaad-data progress: {i}/{len(symbols)} ({len(frames)} recovered)")
        time.sleep(random.uniform(NSE_SLEEP_MIN_S, NSE_SLEEP_MAX_S))

    print(
        f"  jugaad-data fallback complete: recovered {len(frames)} symbols, "
        f"{len(still_missing)} still missing."
    )
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, still_missing


# ── SOURCE 3: yfinance fallback ───────────────────────────────────────────────
# Last resort for symbols that both NSE-native libraries could not fetch.
# Uses per-symbol Ticker.history() (v8/chart endpoint) rather than the batch
# yf.download() used in fetch_prices.py — simpler and sufficient for a small
# set of stragglers. Provides adjusted close prices unlike NSE sources.


def _normalise_yf_ticker_df(raw_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalise a single-symbol DataFrame from yf.Ticker.history() to 8-col format."""
    df = raw_df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]

    if "adj close" in df.columns:
        df = df.rename(columns={"adj close": "adj_close"})
    elif "adj_close" not in df.columns:
        df["adj_close"] = df["close"]

    # Ticker.history() returns timezone-aware DatetimeIndex — strip the tz
    if (
        "date" in df.columns
        and pd.api.types.is_datetime64_any_dtype(df["date"])
        and hasattr(df["date"].dtype, "tz")
        and df["date"].dtype.tz is not None
    ):
        df["date"] = df["date"].dt.tz_convert(None)

    df["symbol"] = symbol

    needed = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        return pd.DataFrame()

    df = df[needed].dropna(subset=["close"])

    zero_vol = (df["volume"] == 0) & (df["close"] == df["close"].shift(1))
    if zero_vol.any():
        df = df[~zero_vol]

    return df


def fetch_failed_via_yfinance(
    symbols: list[str], start: date, end: date
) -> tuple[pd.DataFrame, list[str]]:
    """Recover remaining failures via yfinance Ticker.history() (per-symbol).

    Args:
        symbols: Plain NSE symbols that failed both nselib and jugaad-data.
        start:   Fetch window start.
        end:     Fetch window end.

    Returns:
        (recovered_df, still_missing_symbols)
    """
    if not symbols:
        return pd.DataFrame(), []

    print(f"\n  yfinance fallback: recovering {len(symbols)} symbols…")
    frames: list[pd.DataFrame] = []
    still_missing: list[str] = []

    for sym in symbols:
        # yfinance requires ".NS" suffix to identify NSE-listed stocks
        ticker = sym + ".NS"
        try:
            raw = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
            if raw is None or raw.empty or raw["Close"].isna().all():
                still_missing.append(sym)
            else:
                df = _normalise_yf_ticker_df(raw, sym)
                if df.empty:
                    still_missing.append(sym)
                else:
                    frames.append(df)
        except Exception:
            still_missing.append(sym)

        time.sleep(YF_SLEEP_S)

    recovered = len(frames)
    print(
        f"  yfinance fallback complete: recovered {recovered} symbols, "
        f"{len(still_missing)} still missing."
    )
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, still_missing


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Fetch prices for all Nifty 250 symbols using NSE-native libraries.

    On first run pulls HISTORY_WEEKS of daily history. On subsequent runs
    appends only rows newer than the last date in daily_adj_close.csv.

    Exits with code 1 if more than FAILURE_THRESHOLD_PCT of symbols return
    no data from any source (nselib + jugaad-data + yfinance combined).

    Flags:
      --limit N    Only fetch the first N symbols (for testing).
      --dry-run    Fetch and report but do not write any files.
    """
    parser = argparse.ArgumentParser(
        description="Fetch NSE prices via nselib + jugaad-data (yfinance fallback)"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process the first N symbols (testing)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch but do not write output files"
    )
    args = parser.parse_args()

    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    symbols = load_symbols()   # plain NSE symbols, no .NS suffix
    meta    = load_symbol_meta()

    if args.limit:
        symbols = symbols[: args.limit]
        meta    = {k: v for k, v in meta.items() if k in symbols}
        print(f"[--limit {args.limit}] Testing with first {len(symbols)} symbols only.")

    total    = len(symbols)
    today    = date.today()
    week_str = iso_week_str(today)

    print(f"Universe: {total} symbols")

    # Determine fetch window: incremental from last known date, or full history on first run
    if DAILY_FILE.exists():
        try:
            existing_daily  = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
            last_daily_date = existing_daily.index.max().date()
            start           = last_daily_date + timedelta(days=1)
            print(f"Daily file exists. Fetching from {start} to {today}.")
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)
    else:
        start = today - timedelta(weeks=HISTORY_WEEKS)
        print(f"First run. Fetching {HISTORY_WEEKS} weeks of history from {start} to {today}.")

    if start > today:
        print("Already up to date.")
        return

    # ── SOURCE 1: nselib ─────────────────────────────────────────────────────
    df_nselib, nselib_failed = fetch_all_nselib(symbols, start, today)

    nselib_got = len(df_nselib["symbol"].unique()) if not df_nselib.empty else 0
    print(f"nselib: {nselib_got} succeeded, {len(nselib_failed)} failed")

    # ── SOURCE 2: jugaad-data fallback ───────────────────────────────────────
    df_jugaad            = pd.DataFrame()
    jugaad_still_missing: list[str] = []

    if nselib_failed:
        df_jugaad, jugaad_still_missing = fetch_failed_via_jugaad(
            nselib_failed, start, today, meta
        )

    # ── SOURCE 3: yfinance fallback ──────────────────────────────────────────
    df_yf            = pd.DataFrame()
    yf_still_missing: list[str] = []

    if jugaad_still_missing:
        df_yf, yf_still_missing = fetch_failed_via_yfinance(jugaad_still_missing, start, today)

    # ── combine results ───────────────────────────────────────────────────────
    frames = [f for f in [df_nselib, df_jugaad, df_yf] if not f.empty]
    df     = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # all_missing is the final set of symbols with no data from any source
    all_missing = yf_still_missing if jugaad_still_missing else (
        jugaad_still_missing if nselib_failed else []
    )

    if df.empty:
        print("ERROR: No data returned for any symbol from any source.")
        sys.exit(1)

    # ── failure threshold check ───────────────────────────────────────────────
    missing_pct = len(all_missing) / total
    if missing_pct > FAILURE_THRESHOLD_PCT:
        print(
            f"ERROR: {len(all_missing)}/{total} ({missing_pct:.1%}) symbols returned no data "
            f"from any source — above the {FAILURE_THRESHOLD_PCT:.0%} abort threshold."
        )
        print(f"  First 20 missing: {sorted(all_missing)[:20]}")
        sys.exit(1)

    if all_missing:
        print(
            f"WARNING: {len(all_missing)} symbols missing from all sources: "
            f"{sorted(all_missing)[:20]}" + (" …" if len(all_missing) > 20 else "")
        )

    # ── write outputs ─────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n[--dry-run] Skipping file writes.")
        print(f"  Would write: {PRICES_DIR}/{week_str}.csv  ({len(df)} rows for current week)")
        print(f"  Would update: {DAILY_FILE}")
    else:
        weekly_out = PRICES_DIR / f"{week_str}.csv"
        if not weekly_out.exists():
            # Slice to the current ISO week only for the weekly snapshot
            week_start = today - timedelta(days=today.weekday())
            week_df    = df[pd.to_datetime(df["date"]).dt.date >= week_start]
            if not week_df.empty:
                write_weekly_snapshot(week_df, week_str)
            else:
                print(f"WARNING: No data for current week {week_str} — snapshot not written.")
        else:
            print(f"Weekly snapshot {week_str}.csv already exists — skipping.")

        update_daily_file(df)

    # ── summary ───────────────────────────────────────────────────────────────
    symbols_fetched = len(df["symbol"].unique())
    nselib_set = set(df_nselib["symbol"].unique()) if not df_nselib.empty else set()
    jugaad_set = set(df_jugaad["symbol"].unique()) if not df_jugaad.empty else set()
    yf_set     = set(df_yf["symbol"].unique())     if not df_yf.empty     else set()
    dry_tag    = " [dry-run, no files written]"    if args.dry_run        else ""
    print(
        f"\nDone{dry_tag}. {symbols_fetched}/{total} symbols fetched "
        f"(nselib: {len(nselib_set)}, jugaad-data: {len(jugaad_set)}, "
        f"yfinance: {len(yf_set)}, missing: {len(all_missing)})"
    )
    if all_missing:
        print(f"Missing symbols: {sorted(all_missing)}")


if __name__ == "__main__":
    main()
