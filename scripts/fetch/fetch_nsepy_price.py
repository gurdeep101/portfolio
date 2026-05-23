"""Fetch weekly OHLCV and cumulative daily adj_close for all Nifty 250 symbols.

Writes:
  data/market/prices/YYYY-WW.csv          — weekly OHLCV snapshot (immutable once written)
  data/market/prices/daily_adj_close.csv  — cumulative append-only daily adj_close (wide format)

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

Column schema (all output DataFrames):
  symbol, date, open, high, low, close, adj_close, volume
  adj_close = close for NSE sources (NSE does not publish adjusted prices).
"""

from __future__ import annotations

import argparse
import calendar
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
warnings.filterwarnings("ignore", message=".*no explicit representation of timezones.*")

# ── paths and constants ───────────────────────────────────────────────────────

DATA_DIR      = Path(__file__).parent.parent.parent / "data"
PRICES_DIR    = DATA_DIR / "market" / "prices"
UNIVERSE_FILE = DATA_DIR / "universe" / "universe.csv"
DAILY_FILE    = PRICES_DIR / "daily_adj_close.csv"

FAILURE_THRESHOLD_PCT = 0.08   # abort if > this fraction return no data from any source
HISTORY_WEEKS         = 52     # weeks of history pulled on first run

# Symbols that were renamed on NSE.  Maps current_symbol → (old_symbol, last_date_under_old_name).
# When --backfill-renames is used, history before the cutoff is fetched under the old name
# and written into daily_adj_close.csv under the current (new) name.
# SHRIRAMFIN: Shriram Finance Ltd was formed Dec 2022 from the merger of Shriram Transport
# Finance (SRTRANSFIN) and Shriram City Union Finance.  Pre-merger data lives under SRTRANSFIN.
SYMBOL_RENAMES: dict[str, tuple[str, date]] = {
    "SHRIRAMFIN": ("SRTRANSFIN", date(2022, 12, 25)),
}

# Symbols with pre-listing data contamination.  Maps symbol → first valid equity date.
# Rows before this date are pre-IPO bond/instrument data returned by the NSE API under
# the same ticker.  They are NaN'd out during fetch and cleaned by --clean-min-dates.
# IREDA: equity IPO Nov 29 2023; data before this date is from IREDA's NSE-listed
# tax-free bonds (different instrument, same ticker).
SYMBOL_MIN_DATES: dict[str, date] = {
    "IREDA": date(2023, 11, 29),
}

# ── rate-limit / retry configuration ─────────────────────────────────────────
# NSE blocks aggressive automated traffic. Randomised sleep between calls
# avoids a fixed-interval pattern that Akamai's bot-detection would fingerprint.
NSE_SLEEP_MIN_S = 0.8   # minimum sleep between per-symbol NSE API calls
NSE_SLEEP_MAX_S = 1.2   # maximum sleep (uniform random in this range)

# Per-symbol exponential backoff: waits 2s, 4s before giving up.
# Three attempts recovers from a brief throttle without stalling a 250-symbol run.
NSE_MAX_RETRIES  = 3
NSE_RETRY_BASE_S = 2.0

# Circuit breaker: if NSE_CIRCUIT_BREAKER consecutive symbols all fail,
# the exchange is likely blocking the session — pause before continuing.
NSE_CIRCUIT_BREAKER = 5
NSE_CIRCUIT_PAUSE_S = 30.0

# Source bypass: if a source accumulates this many cumulative failures, stop calling
# it and pass remaining symbols directly to the next source in the cascade.
# Unlike the circuit breaker (consecutive failures, resets on success), this counter
# is cumulative and never resets — sustained failures indicate the source is
# unreliable for this session.
SOURCE_FAILURE_THRESHOLD = 25

# yfinance fallback: sleep between per-symbol Ticker.history() calls
YF_SLEEP_S = 0.5


# ── shared helpers ────────────────────────────────────────────────────────────


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def subtract_months(d: date, months: int) -> date:
    """Return the date that is `months` calendar months before `d`."""
    month = d.month - (months % 12)
    year  = d.year  - (months // 12)
    if month <= 0:
        month += 12
        year  -= 1
    max_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, max_day))


def load_symbols() -> list[str]:
    """Read data/universe/universe.csv and return plain NSE symbols (no .NS suffix).

    Returns a list like ['RELIANCE', 'TCS', ...].
    Exits with code 1 if the universe file is missing.
    """
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe/universe.csv not found. Run fetch_universe.py first.")
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


def update_daily_file(
    df: pd.DataFrame, force: bool = False, total_symbols: int = 0
) -> None:
    """Append new rows to the cumulative daily adj_close wide-format CSV.

    The file uses dates as the row index and symbols as columns.
    Default mode is append-only (historical rows are never removed).
    Force mode refreshes overlapping dates from newly fetched origin data.
    Exits with code 1 on read or write failure.

    total_symbols: size of the full fetch universe (len(symbols) in main).
    Used to reject dates where too few symbols have data — prevents a
    partial yfinance-only batch from injecting phantom dates into the index.
    """
    adj = df[["symbol", "date", "adj_close"]].copy()
    adj["date"] = pd.to_datetime(adj["date"]).dt.date
    adj = adj[adj["date"].apply(lambda d: d.weekday() < 5)]  # drop Sat/Sun
    pivot = adj.pivot_table(index="date", columns="symbol", values="adj_close")
    pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.sort_index()

    # Guard: reject dates where fewer than 40% of the full universe has data.
    # A date with only a handful of symbols is either an NSE holiday that
    # slipped through, or a sparse historical batch (e.g. yfinance weekly).
    # Both cases corrupt the date index used as the ground-truth trading calendar.
    # Use the universe file for the quorum basis — not just the current batch size —
    # so that partial runs (--limit N) cannot inject phantom dates.
    try:
        universe_size = len(pd.read_csv(UNIVERSE_FILE, usecols=["symbol"]))
    except Exception:
        universe_size = total_symbols if total_symbols > 0 else len(pivot.columns)
    min_quorum = max(5, universe_size * 2 // 5)  # 40% of full universe → 100 for 250 syms
    row_coverage = pivot.notna().sum(axis=1)
    thin_dates = (row_coverage < min_quorum).sum()
    if thin_dates:
        print(
            f"  Dropping {thin_dates} thin date(s) with < {min_quorum} symbols "
            f"(quorum={min_quorum}, universe={universe_size})."
        )
    pivot = pivot[row_coverage >= min_quorum]

    # Apply min-date overrides: erase pre-listing data for symbols in SYMBOL_MIN_DATES.
    for sym, min_dt in SYMBOL_MIN_DATES.items():
        if sym in pivot.columns:
            pivot.loc[pivot.index < pd.Timestamp(min_dt), sym] = float("nan")

    if pivot.empty:
        print("  No rows survive quorum filter — nothing to write.")
        return

    if DAILY_FILE.exists():
        try:
            existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)

        new_rows = pivot if force else pivot[~pivot.index.isin(existing.index)]

        if new_rows.empty:
            last_date = existing.index.max()
            print(f"  daily_adj_close.csv already up to date (last: {last_date.date()}).")
            return

        if force:
            overlap_count = int(existing.index.isin(new_rows.index).sum())
            keep_existing = existing[~existing.index.isin(new_rows.index)]
            combined = pd.concat([keep_existing, new_rows])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()

            try:
                combined.to_csv(DAILY_FILE)
                print(
                    f"  Refreshed {len(new_rows)} rows from origin in daily_adj_close.csv "
                    f"({overlap_count} replaced, total: {len(combined)} rows, "
                    f"range: {combined.index.min().date()} to {combined.index.max().date()})"
                )
            except OSError as e:
                print(f"ERROR: Could not write daily_adj_close.csv: {e}")
                sys.exit(1)
            return

        combined = pd.concat([existing, new_rows])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()

        try:
            combined.to_csv(DAILY_FILE)
            print(
                f"  Added {len(new_rows)} new rows to daily_adj_close.csv "
                f"(total: {len(combined)} rows, "
                f"range: {combined.index.min().date()} to {combined.index.max().date()})"
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


def check_snapshot_sync() -> set[str]:
    """Return ISO week strings that have daily data but no OHLCV snapshot file.

    Used to detect divergence between daily_adj_close.csv and the per-week
    YYYY-WW.csv files. An empty set means the two are in sync.
    """
    if not DAILY_FILE.exists():
        return set()
    try:
        existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
    except (OSError, pd.errors.ParserError):
        return set()
    covered_weeks = {iso_week_str(ts.date()) for ts in existing.index}
    return {w for w in covered_weeks if not (PRICES_DIR / f"{w}.csv").exists()}


def write_historical_snapshots(df: pd.DataFrame) -> None:
    """Write YYYY-WW.csv for every ISO week in *df* that is missing a snapshot.

    Groups the DataFrame by ISO week and calls write_weekly_snapshot() for each
    week without an existing file. Safe to call redundantly — write_weekly_snapshot
    already no-ops if the target file exists.
    """
    tmp = df.copy()
    tmp["_date"] = pd.to_datetime(tmp["date"])
    tmp = tmp[tmp["_date"].dt.dayofweek < 5]  # drop Sat/Sun
    tmp = tmp.drop(columns="_date")
    tmp["_week"] = pd.to_datetime(tmp["date"]).apply(lambda d: iso_week_str(d.date()))
    for week_str, week_df in tmp.groupby("_week"):
        write_weekly_snapshot(week_df.drop(columns="_week"), str(week_str))


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

    # Drop zero-volume rows — NSE returns stale prices on market holidays
    # (exchange closure). Nifty 250 stocks are liquid; genuine zero-volume
    # trading days do not occur. Any zero-volume row is a holiday carry-forward.
    df = df[df["volume"] > 0]

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
    total_failures = 0
    total = len(symbols)

    print(f"\nFetching prices via nselib ({total} symbols)…")

    for i, sym in enumerate(symbols, 1):
        if total_failures >= SOURCE_FAILURE_THRESHOLD:
            remaining = symbols[i - 1:]
            failed.extend(remaining)
            print(
                f"  nselib: {SOURCE_FAILURE_THRESHOLD} cumulative failures — "
                f"bypassing {len(remaining)} remaining symbols, escalating to jugaad-data."
            )
            break

        df = fetch_symbol_nselib(sym, start, end)

        if df.empty:
            failed.append(sym)
            consecutive_failures += 1
            total_failures += 1
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
    total_failures = 0

    for i, sym in enumerate(symbols, 1):
        if total_failures >= SOURCE_FAILURE_THRESHOLD:
            remaining = symbols[i - 1:]
            still_missing.extend(remaining)
            print(
                f"  jugaad-data: {SOURCE_FAILURE_THRESHOLD} cumulative failures — "
                f"bypassing {len(remaining)} remaining symbols, escalating to yfinance."
            )
            break

        series = meta.get(sym, "EQ")
        df = fetch_symbol_jugaad(sym, start, end, series)
        if df.empty:
            still_missing.append(sym)
            total_failures += 1
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
# Uses per-symbol Ticker.history() (v8/chart endpoint), which is simpler and
# sufficient for a small set of stragglers. Provides adjusted close prices
# unlike NSE sources.


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


# ── cascade helper ────────────────────────────────────────────────────────────


def _run_cascade(
    symbols: list[str],
    meta: dict[str, str],
    start: date,
    end: date,
) -> tuple[pd.DataFrame, list[str]]:
    """Run the nselib → jugaad-data → yfinance cascade for one date range.

    Returns (combined_df, missing_symbols). Does not apply the failure-threshold
    check — that is done by the caller across all ranges.
    """
    # SOURCE 1: nselib
    df_nselib, nselib_failed = fetch_all_nselib(symbols, start, end)
    nselib_got = len(df_nselib["symbol"].unique()) if not df_nselib.empty else 0
    print(f"nselib: {nselib_got} succeeded, {len(nselib_failed)} failed")

    # SOURCE 2: jugaad-data fallback
    df_jugaad:            pd.DataFrame = pd.DataFrame()
    jugaad_still_missing: list[str]    = []
    if nselib_failed:
        df_jugaad, jugaad_still_missing = fetch_failed_via_jugaad(
            nselib_failed, start, end, meta
        )

    # SOURCE 3: yfinance fallback
    df_yf:            pd.DataFrame = pd.DataFrame()
    yf_still_missing: list[str]    = []
    if jugaad_still_missing:
        df_yf, yf_still_missing = fetch_failed_via_yfinance(jugaad_still_missing, start, end)

    frames = [f for f in [df_nselib, df_jugaad, df_yf] if not f.empty]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # all_missing = final set with no data from any source
    all_missing: list[str] = (
        yf_still_missing     if jugaad_still_missing else
        jugaad_still_missing if nselib_failed        else
        []
    )
    return combined, all_missing


# ── symbol-rename backfill ────────────────────────────────────────────────────


def backfill_renamed_symbols(symbols: list[str], meta: dict[str, str]) -> None:
    """Fetch pre-rename history for symbols that changed NSE tickers.

    For each entry in SYMBOL_RENAMES, checks whether the current symbol has
    NaN gaps in daily_adj_close.csv before the rename cutoff date.  If gaps
    exist, fetches the old symbol name via the nselib→jugaad→yfinance cascade
    and writes the data into daily_adj_close.csv under the new symbol name.

    This is a one-shot operation — run with --backfill-renames once to fix the
    historical record.  Normal weekly fetches do not call this function.
    """
    if not DAILY_FILE.exists():
        print("  daily_adj_close.csv does not exist — run a normal fetch first.")
        return

    try:
        existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
    except (OSError, pd.errors.ParserError) as e:
        print(f"  ERROR reading daily_adj_close.csv: {e}")
        return

    for new_sym, (old_sym, cutoff) in SYMBOL_RENAMES.items():
        if new_sym not in symbols:
            continue

        print(f"\n── Backfilling {new_sym} from old symbol {old_sym} (pre-{cutoff}) ──")

        # Identify gap dates before the cutoff where new_sym is NaN
        pre_cutoff = existing[existing.index <= pd.Timestamp(cutoff)]
        if new_sym in pre_cutoff.columns:
            gaps = pre_cutoff[pre_cutoff[new_sym].isna()]
        else:
            gaps = pre_cutoff  # column doesn't exist at all — backfill everything

        if gaps.empty:
            print(f"  {new_sym}: No pre-rename gaps found — skipping.")
            continue

        gap_start = gaps.index.min().date()
        gap_end   = gaps.index.max().date()
        print(
            f"  {new_sym} has {len(gaps)} gap-day(s) before cutoff, "
            f"fetching {old_sym} from {gap_start} to {gap_end}…"
        )

        old_meta = {old_sym: meta.get(new_sym, "EQ")}
        df_old, missing = _run_cascade([old_sym], old_meta, gap_start, gap_end)

        if df_old.empty:
            print(f"  WARNING: No data returned for {old_sym} — backfill failed.")
            continue

        # Relabel old symbol as new symbol before writing
        df_old = df_old.copy()
        df_old["symbol"] = new_sym

        rows_before = len(existing)
        update_daily_file(df_old, force=True, total_symbols=len(symbols))
        try:
            updated = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        except Exception:
            updated = existing
        print(
            f"  {new_sym}: backfill complete — "
            f"daily_adj_close.csv grew from {rows_before} to {len(updated)} rows."
        )


# ── pre-listing data cleanup ──────────────────────────────────────────────────


def clean_min_dates() -> None:
    """Remove pre-listing contamination rows from daily_adj_close.csv.

    For each symbol in SYMBOL_MIN_DATES, sets all cells before the listed min date
    to NaN.  This is a one-shot operation: run with --clean-min-dates once to purge
    bond-instrument data (or other pre-IPO artefacts) stored under the equity ticker.
    Future fetches will not re-introduce contaminated rows because update_daily_file()
    applies the same SYMBOL_MIN_DATES filter before writing.
    """
    if not DAILY_FILE.exists():
        print("  daily_adj_close.csv does not exist — nothing to clean.")
        return

    try:
        df = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
    except (OSError, pd.errors.ParserError) as e:
        print(f"  ERROR reading daily_adj_close.csv: {e}")
        return

    total_cleared = 0
    for sym, min_dt in SYMBOL_MIN_DATES.items():
        if sym not in df.columns:
            print(f"  {sym}: not in daily_adj_close.csv — skipping.")
            continue
        mask = df.index < pd.Timestamp(min_dt)
        cleared = int(mask.sum() - df.loc[mask, sym].isna().sum())
        if cleared == 0:
            print(f"  {sym}: no pre-{min_dt} data to clear.")
            continue
        df.loc[mask, sym] = float("nan")
        total_cleared += cleared
        print(f"  {sym}: cleared {cleared} pre-listing cell(s) (before {min_dt}).")

    if total_cleared == 0:
        print("  Nothing to clean — all symbols already have no pre-listing data.")
        return

    try:
        df.to_csv(DAILY_FILE)
        print(f"  daily_adj_close.csv updated ({total_cleared} cell(s) cleared).")
    except OSError as e:
        print(f"  ERROR writing daily_adj_close.csv: {e}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Fetch prices for all Nifty 250 symbols using NSE-native libraries.

    On first run pulls HISTORY_WEEKS of daily history. On subsequent runs
    appends only rows newer than the last date in daily_adj_close.csv.

    When --months N is supplied the script ensures at least N months of history
    are present in daily_adj_close.csv, downloading only the missing portion.
    When --force is supplied the script re-downloads the full target window
    from origin sources and refreshes overlapping daily rows.

    Keeps weekly OHLCV snapshot files (YYYY-WW.csv) in sync with
    daily_adj_close.csv — warns on divergence and auto-writes missing snapshots.

    Exits with code 1 if more than FAILURE_THRESHOLD_PCT of symbols return
    no data from any source (nselib + jugaad-data + yfinance combined).

    Flags:
      --limit N      Only fetch the first N symbols (for testing).
      --dry-run      Fetch and report but do not write any files.
      --months N     Ensure at least N months of history are present.
      --force        Re-fetch full target window and refresh overlapping daily rows.
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
    parser.add_argument(
        "--months", type=int, default=None,
        help=(
            "Ensure at least N months of history are present. "
            "Downloads only missing data. Defaults to HISTORY_WEEKS on first run if omitted."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch full target window and refresh overlapping daily rows from origin sources",
    )
    parser.add_argument(
        "--backfill-renames", action="store_true",
        help=(
            "One-time fix: fetch pre-rename history for symbols in SYMBOL_RENAMES "
            "(e.g. SHRIRAMFIN from SRTRANSFIN before Dec 2022) and merge into daily_adj_close.csv."
        ),
    )
    parser.add_argument(
        "--clean-min-dates", action="store_true",
        help=(
            "One-time fix: erase pre-listing data for symbols in SYMBOL_MIN_DATES "
            "(e.g. IREDA bond data before Nov 2023 equity IPO) from daily_adj_close.csv."
        ),
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

    # ── sync check: warn if any week has daily data but no snapshot file ───────
    missing_snapshots = check_snapshot_sync()
    if missing_snapshots:
        print(
            f"WARNING: {len(missing_snapshots)} week(s) have daily data but no OHLCV snapshot. "
            f"They will be written after this fetch. "
            f"({', '.join(sorted(missing_snapshots)[:5])}"
            + (" …" if len(missing_snapshots) > 5 else "") + ")"
        )

    # ── determine fetch ranges ────────────────────────────────────────────────
    # historical_range: (start, end) for backfill, or the full initial fetch.
    # forward_range:    (start, end) for the incremental forward update.
    # Either may be None if that gap does not exist.
    historical_range: tuple[date, date] | None = None
    forward_range:    tuple[date, date] | None = None

    requested_start: date | None = (
        subtract_months(today, args.months) if args.months is not None else None
    )
    if requested_start is not None:
        print(f"[--months {args.months}] History target: {requested_start}")

    if args.force:
        force_start = requested_start if requested_start is not None else (
            today - timedelta(weeks=HISTORY_WEEKS)
        )
        historical_range = (force_start, today)
        print(
            f"[--force] Re-fetching full target range from origin: "
            f"{force_start} to {today}."
        )
    elif DAILY_FILE.exists():
        try:
            existing_daily  = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
            existing_start  = existing_daily.index.min().date()
            last_daily_date = existing_daily.index.max().date()
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)

        print(
            f"Daily file exists. Coverage: {existing_start} to {last_daily_date} "
            f"({len(existing_daily)} rows)."
        )

        # Backfill gap: requested history goes further back than what we have
        if requested_start is not None and requested_start < existing_start:
            backfill_end = existing_start - timedelta(days=1)
            historical_range = (requested_start, backfill_end)
            print(f"  Backfill gap:  {requested_start} to {backfill_end} (will download)")

        # Forward gap: we are behind today
        fwd_start = last_daily_date + timedelta(days=1)
        if fwd_start <= today:
            forward_range = (fwd_start, today)
            print(f"  Forward gap:   {fwd_start} to {today} (will download)")
        else:
            print("  Forward gap:   none (already current)")

    else:
        # First run — no existing data at all
        init_start = requested_start if requested_start is not None else (
            today - timedelta(weeks=HISTORY_WEEKS)
        )
        historical_range = (init_start, today)
        print(f"First run. Fetching from {init_start} to {today}.")

    if historical_range is None and forward_range is None:
        print("Already up to date.")
        return

    # ── fetch each gap via the cascade ────────────────────────────────────────
    all_frames:   list[pd.DataFrame] = []
    all_missing:  list[str]          = []

    for label, gap in [("backfill", historical_range), ("forward", forward_range)]:
        if gap is None:
            continue
        gap_start, gap_end = gap
        print(f"\n── Fetching {label} range: {gap_start} to {gap_end} ──")
        df_gap, missing_gap = _run_cascade(symbols, meta, gap_start, gap_end)
        if not df_gap.empty:
            all_frames.append(df_gap)
        all_missing = list(set(all_missing) | set(missing_gap))

    df = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()

    if df.empty:
        print("ERROR: No data returned for any symbol from any source.")
        sys.exit(1)

    # ── failure threshold check (across all gaps combined) ────────────────────
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
        current_week_rows = df[
            pd.to_datetime(df["date"]).apply(lambda d: iso_week_str(d.date())) == week_str
        ]
        print("  Would write snapshots for all ISO weeks in fetched data.")
        print(f"  Would write: {PRICES_DIR}/{week_str}.csv  ({len(current_week_rows)} rows for current week)")
        if args.force:
            print("  Would refresh overlapping dates in daily_adj_close.csv from fetched origin data.")
        print(f"  Would update: {DAILY_FILE}")
    else:
        # Write OHLCV snapshots for every week in the fetched data (backfill + forward).
        # write_weekly_snapshot() no-ops for files that already exist.
        write_historical_snapshots(df)

        update_daily_file(df, force=args.force, total_symbols=total)

    # ── symbol-rename backfill (one-time, optional) ───────────────────────────
    if args.backfill_renames and not args.dry_run:
        backfill_renamed_symbols(symbols, meta)

    # ── pre-listing data cleanup (one-time, optional) ─────────────────────────
    if args.clean_min_dates and not args.dry_run:
        print("\n── Cleaning pre-listing contamination from daily_adj_close.csv ──")
        clean_min_dates()

    # ── summary ───────────────────────────────────────────────────────────────
    symbols_fetched = len(df["symbol"].unique())
    dry_tag = " [dry-run, no files written]" if args.dry_run else ""
    print(
        f"\nDone{dry_tag}. {symbols_fetched}/{total} symbols fetched, "
        f"{len(all_missing)} missing."
    )
    if all_missing:
        print(f"Missing symbols: {sorted(all_missing)}")


if __name__ == "__main__":
    main()
