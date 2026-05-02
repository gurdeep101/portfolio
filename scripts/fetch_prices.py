"""Fetch weekly OHLCV and cumulative daily adj_close for all Nifty 250 symbols.

Writes:
  data/prices/YYYY-WW.csv          — weekly OHLCV snapshot (immutable once written)
  data/prices/daily_adj_close.csv  — cumulative append-only daily adj_close (wide format)

On first run, pulls HISTORY_WEEKS of history for the daily file.
On subsequent runs, appends only rows newer than the last date already present.
The daily file is never trimmed — scripts that read it filter by date window.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

print(f"yfinance version: {yf.__version__}")

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"

BATCH_SIZE = 50               # symbols per yf.download call
FAILURE_THRESHOLD_PCT = 0.08  # abort if more than this fraction of symbols return no data
HISTORY_WEEKS = 52            # weeks of history pulled on first run
SLEEP_MIN_S = 3.0             # minimum random sleep between batches (seconds)
SLEEP_MAX_S = 10.0            # maximum random sleep between batches (seconds)


def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[str]:
    """Read data/universe.csv and return a list of Yahoo Finance tickers (symbol + '.NS').

    Exits with code 1 if the universe file is missing.
    """
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return [s.strip() + ".NS" for s in df["symbol"].tolist()]
    except (OSError, pd.errors.ParserError, KeyError) as e:
        print(f"ERROR: Could not read universe.csv: {e}")
        sys.exit(1)


def fetch_batch(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Download OHLCV for a batch of Yahoo tickers over the date range [start, end].

    Args:
        tickers: List of Yahoo Finance ticker strings (e.g. ['RELIANCE.NS', ...]).
        start:   First date to fetch (inclusive).
        end:     Last date to fetch (inclusive; one day is added internally).

    Returns:
        Long-format DataFrame with columns [symbol, date, open, high, low, close,
        adj_close, volume]. Returns an empty DataFrame if yfinance fails or no
        data is returned for the batch.
    """
    try:
        raw = yf.download(
            tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=False,
        )
    except Exception as e:
        # yfinance raises varied undocumented exceptions depending on Yahoo's
        # API version. Treat any failure as a recoverable batch miss.
        print(f"  WARNING: yf.download failed for batch starting {tickers[0]}: {e}")
        return pd.DataFrame()

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for ticker in tickers:
        try:
            df = raw[ticker].copy() if len(tickers) > 1 else raw.copy()
            if df.empty or df["Close"].isna().all():
                continue
            df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            df["symbol"] = ticker.replace(".NS", "")
            df = df.rename(columns={"adj close": "adj_close"})
            df = df[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]]
            df = df.dropna(subset=["close"])
            frames.append(df)
        except KeyError:
            # Ticker absent in the multi-ticker download response.
            continue

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_all(
    tickers: list[str], start: date, end: date
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch all tickers in batches of BATCH_SIZE with randomised inter-batch sleep.

    Args:
        tickers: Full list of Yahoo Finance ticker strings.
        start:   Start date for the fetch window.
        end:     End date for the fetch window.

    Returns:
        tuple[combined_df, empty_tickers] where *empty_tickers* is the list of
        symbols that returned no data.
    """
    all_frames: list[pd.DataFrame] = []
    empty_tickers: list[str] = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        df = fetch_batch(batch, start, end)

        batch_syms = {t.replace(".NS", "") for t in batch}
        fetched_syms = set(df["symbol"].unique()) if not df.empty else set()
        empty_tickers.extend(batch_syms - fetched_syms)

        if not df.empty:
            all_frames.append(df)

        # Sleep between batches to respect Yahoo Finance rate limits.
        if i + BATCH_SIZE < len(tickers):
            sleep_s = random.uniform(SLEEP_MIN_S, SLEEP_MAX_S)
            time.sleep(sleep_s)

    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    return combined, empty_tickers


def write_weekly_snapshot(df: pd.DataFrame, week_str: str) -> None:
    """Write the current-week OHLCV snapshot CSV.

    No-ops if the file already exists to preserve immutability of weekly snapshots.
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

    The file uses dates as the row index and symbols as columns. It is
    append-only — historical rows are never removed.

    Exits with code 1 on read or write failure (the daily file is critical).
    """
    # Pivot to wide format: index=date, columns=symbol, values=adj_close.
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


def main() -> None:
    """Fetch prices for all Nifty 250 symbols and write OHLCV + daily adj_close files.

    On first run pulls HISTORY_WEEKS of daily history. On subsequent runs appends
    only rows newer than the last date in daily_adj_close.csv.
    Exits with code 1 if more than FAILURE_THRESHOLD_PCT of symbols return no data.
    """
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    tickers = load_symbols()
    total = len(tickers)
    print(f"Universe: {total} symbols")

    today = date.today()
    week_str = iso_week_str(today)

    # --- Determine fetch window ----------------------------------------------
    if DAILY_FILE.exists():
        try:
            existing_daily = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
            last_daily_date = existing_daily.index.max().date()
            start = last_daily_date + timedelta(days=1)
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

    # --- Fetch ---------------------------------------------------------------
    print(f"Fetching data for {total} symbols in batches of {BATCH_SIZE}...")
    df, empty = fetch_all(tickers, start, today)

    if df.empty:
        print("ERROR: No data returned for any symbol. Check yfinance / network connectivity.")
        sys.exit(1)

    empty_pct = len(empty) / total
    if empty_pct > FAILURE_THRESHOLD_PCT:
        print(
            f"ERROR: {len(empty)}/{total} ({empty_pct:.1%}) symbols returned no data — "
            f"above the {FAILURE_THRESHOLD_PCT:.0%} abort threshold."
        )
        print(f"  First 20 empty symbols: {sorted(empty)[:20]}")
        sys.exit(1)

    if empty:
        print(f"WARNING: {len(empty)} symbols returned no data: {sorted(empty)}")

    # --- Write outputs -------------------------------------------------------
    # Weekly snapshot: only include rows from the current ISO week.
    weekly_out = PRICES_DIR / f"{week_str}.csv"
    if not weekly_out.exists():
        week_start = today - timedelta(days=today.weekday())
        week_df = df[pd.to_datetime(df["date"]).dt.date >= week_start]
        if not week_df.empty:
            write_weekly_snapshot(week_df, week_str)
        else:
            print(f"WARNING: No data for current week {week_str} — snapshot not written.")
    else:
        print(f"Weekly snapshot {week_str}.csv already exists — skipping.")

    update_daily_file(df)
    print(f"Done. Fetched data for {len(df['symbol'].unique())} symbols.")


if __name__ == "__main__":
    main()
