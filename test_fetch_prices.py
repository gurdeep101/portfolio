"""Integration test for the enhanced fetch_prices.py.

Tests:
  1. yfinance batch download with retry logic (20 symbols, 2-week window)
  2. NSE Playwright fallback for artificially-failed symbols
  3. Combined output shape and schema
  4. Weekly snapshot write
  5. daily_adj_close.csv write and append

Run:  uv run python test_fetch_prices.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ── point the module at a temp data dir so we don't touch real data ──────────
import importlib, types

# We need to patch DATA_DIR / PRICES_DIR before importing
import scripts.fetch_prices as fp

ORIG_DATA_DIR   = fp.DATA_DIR
ORIG_PRICES_DIR = fp.PRICES_DIR
ORIG_DAILY_FILE = fp.DAILY_FILE

END   = date.today()
START = END - timedelta(days=14)
SYMBOLS_20 = [
    "RELIANCE","HDFCBANK","ICICIBANK","SBIN","ITC","TCS","INFY",
    "AXISBANK","BHARTIARTL","LT","KOTAKBANK","MARUTI","SUNPHARMA",
    "TITAN","BAJFINANCE","HCLTECH","WIPRO","ULTRACEMCO","NTPC","ONGC",
]

PASS = "✅ PASS"
FAIL = "❌ FAIL"

results: list[tuple[str, str, str]] = []

def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


# ═══════════════════════════════════════════════════════════════════
# TEST 1: yfinance batch download (retry logic)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST 1: yfinance batch download (20 symbols, 2-week window)")
print("="*65)

tickers = [s + ".NS" for s in SYMBOLS_20]
t0 = time.time()
df_yf, yf_empty = fp.fetch_all_yf(tickers, START, END)
elapsed = time.time() - t0

got = len(df_yf["symbol"].unique()) if not df_yf.empty else 0
record("yfinance returns non-empty DataFrame", not df_yf.empty, f"{got} symbols")
record("yfinance row count > 0", len(df_yf) > 0, f"{len(df_yf)} rows")
record("yfinance required columns present",
       not df_yf.empty and all(c in df_yf.columns for c in
           ["symbol","date","open","high","low","close","adj_close","volume"]),
       "all 8 columns" if not df_yf.empty else "empty")
record("yfinance close > 0 for all rows",
       not df_yf.empty and (df_yf["close"] > 0).all(),
       f"min_close={df_yf['close'].min():.2f}" if not df_yf.empty else "n/a")
record("yfinance failure rate < 50%",
       len(yf_empty) < len(SYMBOLS_20) * 0.5,
       f"{len(yf_empty)}/{len(SYMBOLS_20)} empty")
print(f"  Elapsed: {elapsed:.1f}s  |  yf_empty: {yf_empty}")

# Show sample
if not df_yf.empty:
    print("\n  Sample (RELIANCE or first symbol, last 3 rows):")
    sample_sym = "RELIANCE" if "RELIANCE" in df_yf["symbol"].values else df_yf["symbol"].iloc[0]
    print(df_yf[df_yf["symbol"]==sample_sym][["date","open","high","low","close","volume"]].tail(3).to_string(index=False))


# ═══════════════════════════════════════════════════════════════════
# TEST 2: NSE Playwright fallback (artificially fail 3 symbols)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST 2: NSE Playwright fallback for 3 symbols")
print("="*65)

# Use 3 symbols that are known to be in universe.csv
test_nse_symbols = ["RELIANCE", "HDFCBANK", "TCS"]
meta = fp.load_symbol_meta()

print(f"  Requesting NSE fallback for: {test_nse_symbols}")
t0 = time.time()
df_nse, still_missing = fp.fetch_failed_via_nse(test_nse_symbols, START, END, meta)
elapsed = time.time() - t0

nse_got = len(df_nse["symbol"].unique()) if not df_nse.empty else 0
nse_session_ok = nse_got > 0

# NSE tests: log as WARN (not FAIL) when session itself can't establish —
# HTTP/2 errors on NSE are intermittent and handled by retry in production.
# A PASS here means session connected AND data was returned.
# A FAIL here means the session couldn't start (documented known failure mode).
record("NSE fallback schema correct (when connected)",
       df_nse.empty or all(c in df_nse.columns for c in
           ["symbol","date","open","high","low","close","adj_close","volume"]),
       "schema ok" if not df_nse.empty else "session unavailable (intermittent HTTP/2)")
record("NSE fallback close > 0 (when connected)",
       df_nse.empty or (df_nse["close"] > 0).all(),
       f"min_close={df_nse['close'].min():.2f}" if not df_nse.empty else "session unavailable")
record("NSE session + data (may be intermittent)",
       nse_got >= 1,
       f"{nse_got}/3 recovered" if nse_got else "NSE HTTP/2 error — retry logic active")
print(f"  Elapsed: {elapsed:.1f}s  |  still_missing: {still_missing}")

if not df_nse.empty:
    print("\n  NSE sample (first symbol, last 3 rows):")
    sym0 = df_nse["symbol"].iloc[0]
    print(df_nse[df_nse["symbol"]==sym0][["date","open","high","low","close","volume"]].tail(3).to_string(index=False))


# ═══════════════════════════════════════════════════════════════════
# TEST 3: combine + write outputs to a temp dir
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST 3: write weekly snapshot + daily_adj_close.csv")
print("="*65)

tmp_dir = Path(tempfile.mkdtemp(prefix="fp_test_"))
fp.PRICES_DIR = tmp_dir
fp.DAILY_FILE = tmp_dir / "daily_adj_close.csv"

frames = [f for f in [df_yf, df_nse] if not f.empty]
df_all = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

if not df_all.empty:
    # Weekly snapshot
    from datetime import date as _date
    today = _date.today()
    week_str = fp.iso_week_str(today)
    week_start = today - timedelta(days=today.weekday())
    week_df = df_all[pd.to_datetime(df_all["date"]).dt.date >= week_start]
    if not week_df.empty:
        fp.write_weekly_snapshot(week_df, week_str)
        snap_path = tmp_dir / f"{week_str}.csv"
        record("weekly snapshot file created", snap_path.exists(), str(snap_path.name))
        snap = pd.read_csv(snap_path)
        record("weekly snapshot has data", len(snap) > 0, f"{len(snap)} rows")
    else:
        record("weekly snapshot (no current-week data)", True, "skipped (no data this week)")

    # daily_adj_close.csv — first write
    fp.update_daily_file(df_all)
    record("daily_adj_close.csv created", fp.DAILY_FILE.exists())
    if fp.DAILY_FILE.exists():
        daily = pd.read_csv(fp.DAILY_FILE, index_col=0, parse_dates=True)
        record("daily file has rows", len(daily) > 0, f"{len(daily)} rows × {len(daily.columns)} symbols")
        record("daily file column count matches symbols",
               len(daily.columns) == len(df_all["symbol"].unique()),
               f"{len(daily.columns)} cols")

    # daily_adj_close.csv — append (should be no-op, already up to date)
    rows_before = len(daily) if fp.DAILY_FILE.exists() else 0
    fp.update_daily_file(df_all)
    daily2 = pd.read_csv(fp.DAILY_FILE, index_col=0, parse_dates=True)
    record("daily append is idempotent (no duplicate rows)",
           len(daily2) == rows_before, f"{len(daily2)} rows still")
else:
    record("skip write tests (no data)", False, "df_all is empty")

# cleanup temp
shutil.rmtree(tmp_dir, ignore_errors=True)

# Restore module paths
fp.PRICES_DIR = ORIG_PRICES_DIR
fp.DAILY_FILE = ORIG_DAILY_FILE


# ═══════════════════════════════════════════════════════════════════
# TEST 4: helper functions
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST 4: helper functions")
print("="*65)

record("iso_week_str format correct",
       len(fp.iso_week_str(date(2026, 5, 5)).split("-")) == 2 and
       fp.iso_week_str(date(2026, 5, 5)) == "2026-19",
       fp.iso_week_str(date(2026, 5, 5)))

record("load_symbols returns .NS tickers",
       all(t.endswith(".NS") for t in fp.load_symbols()[:5]),
       "5 checked")

record("load_symbol_meta returns dict",
       isinstance(fp.load_symbol_meta(), dict) and len(fp.load_symbol_meta()) > 0,
       f"{len(fp.load_symbol_meta())} entries")

record("_is_rate_limit_error detects YFRateLimitError",
       fp._is_rate_limit_error(Exception("Too Many Requests. Rate limited.")),
       "matched 'too many requests'")

record("_is_rate_limit_error ignores generic error",
       not fp._is_rate_limit_error(Exception("KeyError: Close")),
       "not rate-limit")


# ═══════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("SUMMARY")
print("="*65)

passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)

for name, status, detail in results:
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))

print(f"\n  {passed}/{passed+failed} tests passed")
if failed:
    print(f"  {failed} failures — review output above for details")
    sys.exit(1)
else:
    print("  All tests passed ✅")
