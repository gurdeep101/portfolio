"""Fetch weekly OHLCV and cumulative daily adj_close for all Nifty 250 symbols.

Writes:
  data/prices/YYYY-WW.csv          — weekly OHLCV snapshot (immutable once written)
  data/prices/daily_adj_close.csv  — cumulative append-only daily adj_close (wide format)

On first run, pulls 52 weeks of history for the daily file.
On subsequent runs, appends only rows newer than the last date already in the file.
"""

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

BATCH_SIZE = 50
FAILURE_THRESHOLD_PCT = 0.08
HISTORY_WEEKS = 52


def iso_week_str(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[str]:
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    df = pd.read_csv(UNIVERSE_FILE)
    return [s.strip() + ".NS" for s in df["symbol"].tolist()]


def fetch_batch(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Download OHLCV for a batch. Returns a long-format DataFrame."""
    raw = yf.download(
        tickers,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        progress=False,
        group_by="ticker",
        threads=False,
    )
    if raw.empty:
        return pd.DataFrame()

    frames = []
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy()
            if df.empty or df["Close"].isna().all():
                continue
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            df["symbol"] = ticker.replace(".NS", "")
            df = df.rename(columns={"adj close": "adj_close"})
            df = df[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]]
            df = df.dropna(subset=["close"])
            frames.append(df)
        except KeyError:
            continue

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_all(tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, list[str]]:
    all_frames = []
    empty_tickers = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        df = fetch_batch(batch, start, end)

        batch_syms = {t.replace(".NS", "") for t in batch}
        fetched_syms = set(df["symbol"].unique()) if not df.empty else set()
        missing = batch_syms - fetched_syms
        empty_tickers.extend(missing)

        if not df.empty:
            all_frames.append(df)

        if i + BATCH_SIZE < len(tickers):
            sleep_s = random.uniform(3, 10)
            time.sleep(sleep_s)

    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    return combined, empty_tickers


def write_weekly_snapshot(df: pd.DataFrame, week_str: str):
    out = PRICES_DIR / f"{week_str}.csv"
    if out.exists():
        print(f"  Weekly snapshot {out.name} already exists — skipping overwrite.")
        return
    df.to_csv(out, index=False)
    print(f"  Weekly snapshot written: {out.name} ({len(df)} rows)")


def update_daily_file(df: pd.DataFrame):
    adj = df[["symbol", "date", "adj_close"]].copy()
    adj["date"] = pd.to_datetime(adj["date"]).dt.date

    pivot = adj.pivot_table(index="date", columns="symbol", values="adj_close")
    pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.sort_index()

    if DAILY_FILE.exists():
        existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        last_date = existing.index.max()
        new_rows = pivot[pivot.index > last_date]
        if new_rows.empty:
            print(f"  daily_adj_close.csv already up to date (last: {last_date.date()}).")
            return
        combined = pd.concat([existing, new_rows])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()
        combined.to_csv(DAILY_FILE)
        print(f"  Appended {len(new_rows)} new rows to daily_adj_close.csv (total: {len(combined)} rows)")
    else:
        pivot.to_csv(DAILY_FILE)
        print(f"  Created daily_adj_close.csv ({len(pivot)} rows, {len(pivot.columns)} symbols)")


def main():
    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    tickers = load_symbols()
    total = len(tickers)
    print(f"Universe: {total} symbols")

    today = date.today()
    week_str = iso_week_str(today)

    weekly_out = PRICES_DIR / f"{week_str}.csv"
    if weekly_out.exists():
        existing_start = pd.read_csv(weekly_out)["date"].min() if not pd.read_csv(weekly_out).empty else None
        print(f"Weekly snapshot {week_str}.csv already exists.")
        weekly_exists = True
    else:
        weekly_exists = False

    if DAILY_FILE.exists():
        existing_daily = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        last_daily_date = existing_daily.index.max().date()
        start = last_daily_date + timedelta(days=1)
        print(f"Daily file exists. Fetching from {start} to {today}.")
    else:
        start = today - timedelta(weeks=HISTORY_WEEKS)
        print(f"First run. Fetching {HISTORY_WEEKS} weeks of history from {start} to {today}.")

    if start > today:
        print("Already up to date.")
        return

    print(f"Fetching data for {total} symbols in batches of {BATCH_SIZE}...")
    df, empty = fetch_all(tickers, start, today)

    if df.empty:
        print("ERROR: No data returned for any symbol. Check yfinance connectivity.")
        sys.exit(1)

    empty_pct = len(empty) / total
    if empty_pct > FAILURE_THRESHOLD_PCT:
        print(f"ERROR: {len(empty)}/{total} ({empty_pct:.1%}) symbols returned no data — above {FAILURE_THRESHOLD_PCT:.0%} threshold.")
        print(f"Empty symbols: {sorted(empty)[:20]}...")
        sys.exit(1)

    if empty:
        print(f"WARNING: {len(empty)} symbols returned no data: {sorted(empty)}")

    if not weekly_exists:
        week_start = today - timedelta(days=today.weekday())
        week_df = df[pd.to_datetime(df["date"]).dt.date >= week_start]
        if not week_df.empty:
            write_weekly_snapshot(week_df, week_str)
        else:
            print(f"WARNING: No data for current week {week_str} — snapshot not written.")

    update_daily_file(df)
    print(f"Done. Fetched {len(df['symbol'].unique())} symbols.")


if __name__ == "__main__":
    main()
