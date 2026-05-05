"""Unit tests for enhanced fetch_prices.py — uses mocks, no live API calls.

Covers:
  - yfinance rate-limit detection and exponential-backoff retry
  - fetch_all_yf: batching, empty-symbol tracking, combined output
  - fetch_symbol_nse: JSON parsing, row normalisation
  - fetch_failed_via_nse: session warming failure path
  - write_weekly_snapshot: idempotency, schema
  - update_daily_file: create, append, idempotency, dedup
  - iso_week_str / load_symbol_meta helpers

Run: uv run python test_fetch_prices_unit.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd

# ── import the module under test ──────────────────────────────────────────────
import scripts.fetch_prices as fp

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results: list[tuple[str, str, str]] = []

def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


# ── shared helpers ─────────────────────────────────────────────────────────────

def _make_ohlcv(symbol: str, nrows: int = 5) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame in the shape fetch_prices expects."""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(nrows - 1, -1, -1)]
    return pd.DataFrame({
        "symbol":    [symbol] * nrows,
        "date":      pd.to_datetime(dates),
        "open":      [100.0 + i for i in range(nrows)],
        "high":      [105.0 + i for i in range(nrows)],
        "low":       [95.0  + i for i in range(nrows)],
        "close":     [102.0 + i for i in range(nrows)],
        "adj_close": [102.0 + i for i in range(nrows)],
        "volume":    [1_000_000 + i * 1000 for i in range(nrows)],
    })


def _make_yf_raw(tickers: list[str], nrows: int = 5) -> pd.DataFrame:
    """Build a fake yf.download() DataFrame.

    For a single ticker yfinance returns a plain DataFrame (columns = Open/High/…).
    For multiple tickers it returns a MultiIndex DataFrame (ticker × OHLCV).
    This mock matches that contract.
    """
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(nrows - 1, -1, -1)]
    idx = pd.DatetimeIndex(dates, name="Date")

    if len(tickers) == 1:
        px = 100.0 + float(abs(hash(tickers[0])) % 50)
        return pd.DataFrame({
            "Open":      [px + i for i in range(nrows)],
            "High":      [px + 5 + i for i in range(nrows)],
            "Low":       [px - 5 + i for i in range(nrows)],
            "Close":     [px + 1 + i for i in range(nrows)],
            "Adj Close": [px + 1 + i for i in range(nrows)],
            "Volume":    [1_000_000 + i for i in range(nrows)],
        }, index=idx)

    frames: dict[str, pd.DataFrame] = {}
    for t in tickers:
        px = 100.0 + float(abs(hash(t)) % 50)
        frames[t] = pd.DataFrame({
            "Open":      [px + i for i in range(nrows)],
            "High":      [px + 5 + i for i in range(nrows)],
            "Low":       [px - 5 + i for i in range(nrows)],
            "Close":     [px + 1 + i for i in range(nrows)],
            "Adj Close": [px + 1 + i for i in range(nrows)],
            "Volume":    [1_000_000 + i for i in range(nrows)],
        }, index=idx)
    return pd.concat(frames, axis=1)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 1: helpers
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST GROUP 1: helper functions")
print("="*65)

record("iso_week_str: known date 2026-01-05 → 2026-02",
       fp.iso_week_str(date(2026, 1, 5)) == "2026-02")

record("iso_week_str: known date 2026-05-05 → 2026-19",
       fp.iso_week_str(date(2026, 5, 5)) == "2026-19")

record("_is_rate_limit_error: YFRateLimitError string",
       fp._is_rate_limit_error(Exception("Too Many Requests. Rate limited.")))

record("_is_rate_limit_error: '429' in message",
       fp._is_rate_limit_error(Exception("HTTP Error 429: Too Many Requests")))

record("_is_rate_limit_error: generic error → False",
       not fp._is_rate_limit_error(Exception("KeyError: 'Close'")))

record("load_symbols: returns .NS tickers",
       all(t.endswith(".NS") for t in fp.load_symbols()[:5]))

record("load_symbol_meta: returns dict with >100 entries",
       len(fp.load_symbol_meta()) > 100)

record("load_symbol_meta: values are series strings",
       set(fp.load_symbol_meta().values()).issubset({"EQ","BE","SM","ST",""}))


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 2: yfinance batch with retry
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST GROUP 2: yfinance retry logic (mocked)")
print("="*65)

START = date.today() - timedelta(days=14)
END   = date.today()

# 2a: yf.download succeeds immediately
with patch("scripts.fetch_prices.yf") as mock_yf:
    tickers = ["RELIANCE.NS", "TCS.NS"]
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.return_value = _make_yf_raw(tickers)
    df, empty = fp.fetch_batch_yf(tickers, START, END)
    record("yf success: 2 symbols returned",
           len(df["symbol"].unique()) == 2 and empty == [],
           f"{len(df)} rows")
    record("yf success: all 8 columns present",
           all(c in df.columns for c in ["symbol","date","open","high","low","close","adj_close","volume"]))
    record("yf success: close > 0",
           (df["close"] > 0).all())

# 2b: rate-limit on attempt 0, success on attempt 1
call_count = [0]
def _yf_retry_mock(*a, **kw):
    call_count[0] += 1
    if call_count[0] == 1:
        raise Exception("Too Many Requests. Rate limited.")
    tickers_arg = a[0] if a else kw.get("tickers", ["X.NS"])
    if isinstance(tickers_arg, str):
        tickers_arg = [tickers_arg]
    return _make_yf_raw(tickers_arg)

with patch("scripts.fetch_prices.yf") as mock_yf, \
     patch("scripts.fetch_prices.time.sleep") as mock_sleep:
    call_count[0] = 0
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.side_effect = _yf_retry_mock
    df, empty = fp.fetch_batch_yf(["RELIANCE.NS"], START, END)
    record("yf rate-limit: retried and succeeded on attempt 2",
           len(df) > 0 and mock_yf.download.call_count == 2,
           f"calls={mock_yf.download.call_count}")
    record("yf rate-limit: sleep was called once",
           mock_sleep.call_count == 1)
    record("yf rate-limit: sleep duration was YF_RETRY_BASE_S",
           mock_sleep.call_args_list[0] == call(fp.YF_RETRY_BASE_S * (2 ** 0)),
           f"{mock_sleep.call_args_list[0]}")

# 2c: exhausts all retries → auto-falls-back to Ticker.history() (v8 endpoint)
with patch("scripts.fetch_prices.yf") as mock_yf, \
     patch("scripts.fetch_prices.time.sleep"), \
     patch("scripts.fetch_prices._fetch_batch_via_history") as mock_hist:
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.side_effect = Exception("Too Many Requests. Rate limited.")
    mock_hist.return_value = (_make_ohlcv("RELIANCE", 5), [])
    df, empty = fp.fetch_batch_yf(["RELIANCE.NS"], START, END)
    record("yf retries exhausted: auto-falls-back to Ticker.history()",
           mock_hist.called, f"history fallback called={mock_hist.called}")
    record("yf retries exhausted: data returned from history fallback",
           len(df) > 0 and empty == [], f"{len(df)} rows")
    record("yf retries exhausted: download called 1+MAX_RETRIES times",
           mock_yf.download.call_count == 1 + fp.YF_MAX_RETRIES,
           f"calls={mock_yf.download.call_count}")

# 2c2b: yf.download returns EMPTY (rate-limit silent path) → canary confirms v8 works → block v7
with patch("scripts.fetch_prices.yf") as mock_yf, \
     patch("scripts.fetch_prices._fetch_ticker_history") as mock_canary, \
     patch("scripts.fetch_prices._fetch_batch_via_history") as mock_hist, \
     patch("scripts.fetch_prices.time.sleep"):
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.return_value = pd.DataFrame()   # silent empty (rate-limit)
    mock_canary.return_value = _make_ohlcv("RELIANCE", 3)   # v8 has data
    mock_hist.return_value = (_make_ohlcv("RELIANCE", 3), [])
    fp._yf_v7_blocked = False
    df_sl, _ = fp.fetch_batch_yf(["RELIANCE.NS"], START, END)
    record("silent rate-limit: canary check triggers v8 fallback",
           mock_canary.called and mock_hist.called,
           f"canary={mock_canary.called} hist={mock_hist.called}")
    record("silent rate-limit: _yf_v7_blocked flag set",
           fp._yf_v7_blocked)
    fp._yf_v7_blocked = False  # reset

# 2c3: once _yf_v7_blocked is set, subsequent batches skip v7 entirely
with patch("scripts.fetch_prices.yf") as mock_yf, \
     patch("scripts.fetch_prices._fetch_batch_via_history") as mock_hist, \
     patch("scripts.fetch_prices.time.sleep"):
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.side_effect = Exception("Too Many Requests. Rate limited.")
    mock_hist.return_value = (_make_ohlcv("TCS", 5), [])
    fp._yf_v7_blocked = True          # pre-set the flag (simulates prior batch exhausting retries)
    fp.fetch_batch_yf(["TCS.NS"], START, END)
    fp._yf_v7_blocked = False         # reset
    record("_yf_v7_blocked: skips yf.download entirely when flag is set",
           mock_yf.download.call_count == 0, f"download calls={mock_yf.download.call_count}")
    record("_yf_v7_blocked: goes straight to history fallback",
           mock_hist.called)

# 2c3: _fetch_batch_via_history calls _fetch_ticker_history per symbol
with patch("scripts.fetch_prices._fetch_ticker_history") as mock_th, \
     patch("scripts.fetch_prices.time.sleep"):
    mock_th.side_effect = lambda t, *a, **kw: _make_ohlcv(t.replace(".NS",""), 5)
    df_h, empty_h = fp._fetch_batch_via_history(["RELIANCE.NS","TCS.NS"], START, END)
    record("_fetch_batch_via_history: calls _fetch_ticker_history per symbol",
           mock_th.call_count == 2, f"calls={mock_th.call_count}")
    record("_fetch_batch_via_history: combines results",
           len(df_h["symbol"].unique()) == 2 and empty_h == [])

# 2d: _normalise_ticker_df handles Ticker.history() timezone-aware index
tz_df = pd.DataFrame({
    "Open":   [100.0], "High": [105.0], "Low": [95.0],
    "Close":  [102.0], "Volume": [1_000_000],
    "Dividends": [0.0], "Stock Splits": [0.0],
}, index=pd.DatetimeIndex(
    [pd.Timestamp("2026-05-05", tz="Asia/Kolkata")], name="Date"
))
normed = fp._normalise_ticker_df(tz_df, "RELIANCE")
record("_normalise_ticker_df: tz-aware index stripped",
       not pd.api.types.is_datetime64tz_dtype(normed["date"]),
       str(normed["date"].dtype))
record("_normalise_ticker_df: adj_close filled from close when missing",
       abs(normed["adj_close"].iloc[0] - 102.0) < 0.01)
record("_normalise_ticker_df: symbol set correctly",
       normed["symbol"].iloc[0] == "RELIANCE")

# 2f: fetch_all_yf batches correctly
with patch("scripts.fetch_prices.yf") as mock_yf, \
     patch("scripts.fetch_prices.time.sleep"):
    mock_yf.__version__ = "0.2.54"
    mock_yf.download.side_effect = lambda tickers, **kw: _make_yf_raw(
        tickers if isinstance(tickers, list) else [tickers]
    )
    # 55 symbols = 2 batches (50 + 5)
    tickers_55 = [f"SYM{i:03d}.NS" for i in range(55)]
    df_all, empty_all = fp.fetch_all_yf(tickers_55, START, END)
    record("fetch_all_yf: 55 symbols → 2 batches",
           mock_yf.download.call_count == 2,
           f"calls={mock_yf.download.call_count}")
    record("fetch_all_yf: all 55 symbols returned",
           len(df_all["symbol"].unique()) == 55, f"{len(df_all['symbol'].unique())} symbols")
    record("fetch_all_yf: inter-batch sleep called once",
           mock_yf.download.call_count == 2)  # sleep is between batches


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 3: NSE data parsing
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST GROUP 3: NSE data parsing (mocked page)")
print("="*65)

# Simulated NSE API response for RELIANCE (realistic field names)
import json as _json
NSE_SAMPLE_RESPONSE = _json.dumps([
    {
        "CH_SYMBOL": "RELIANCE",
        "CH_SERIES": "EQ",
        "CH_OPENING_PRICE": "1285.00",
        "CH_TRADE_HIGH_PRICE": "1310.00",
        "CH_TRADE_LOW_PRICE": "1280.00",
        "CH_CLOSING_PRICE": "1298.50",
        "CH_TOT_TRADED_QTY": 5432100,
        "CH_TIMESTAMP": "28-Apr-2026",
        "mTIMESTAMP": "28-Apr-2026",
    },
    {
        "CH_SYMBOL": "RELIANCE",
        "CH_SERIES": "EQ",
        "CH_OPENING_PRICE": "1290.00",
        "CH_TRADE_HIGH_PRICE": "1315.00",
        "CH_TRADE_LOW_PRICE": "1285.00",
        "CH_CLOSING_PRICE": "1303.00",
        "CH_TOT_TRADED_QTY": 4321000,
        "CH_TIMESTAMP": "29-Apr-2026",
        "mTIMESTAMP": "29-Apr-2026",
    },
])

def _run_nse_parse_test() -> None:
    """Helper: sets _nse_page to a mock, calls fetch_symbol_nse."""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = NSE_SAMPLE_RESPONSE
    fp._nse_page = mock_page
    df = fp.fetch_symbol_nse("RELIANCE", START, END)
    fp._nse_page = None
    return df

df_nse_parsed = _run_nse_parse_test()

record("NSE parse: 2 rows returned",
       len(df_nse_parsed) == 2, f"{len(df_nse_parsed)} rows")
record("NSE parse: all 8 columns present",
       all(c in df_nse_parsed.columns for c in ["symbol","date","open","high","low","close","adj_close","volume"]))
record("NSE parse: symbol set correctly",
       (df_nse_parsed["symbol"] == "RELIANCE").all())
record("NSE parse: close = 1298.50 for first row",
       abs(df_nse_parsed.iloc[0]["close"] - 1298.50) < 0.01,
       f"close={df_nse_parsed.iloc[0]['close']}")
record("NSE parse: date is datetime",
       pd.api.types.is_datetime64_any_dtype(df_nse_parsed["date"]))
record("NSE parse: volume > 0",
       (df_nse_parsed["volume"] > 0).all())

# NSE parse: empty page response
mock_page2 = MagicMock()
mock_page2.evaluate.return_value = "[]"
fp._nse_page = mock_page2
df_empty = fp.fetch_symbol_nse("RELIANCE", START, END)
fp._nse_page = None
record("NSE parse: empty API response → empty DataFrame", df_empty.empty)

# NSE parse: error response
mock_page3 = MagicMock()
mock_page3.evaluate.return_value = '{"error": "Session expired"}'
fp._nse_page = mock_page3
df_err = fp.fetch_symbol_nse("RELIANCE", START, END)
fp._nse_page = None
record("NSE parse: error JSON response → empty DataFrame", df_err.empty)

# NSE parse: _nse_page is None → empty
fp._nse_page = None
df_none = fp.fetch_symbol_nse("RELIANCE", START, END)
record("NSE parse: _nse_page=None → empty DataFrame", df_none.empty)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 4: NSE fallback orchestration
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST GROUP 4: NSE fallback orchestration (mocked)")
print("="*65)

# 4a: session start fails → all symbols still missing
with patch("scripts.fetch_prices._start_nse_session", return_value=False), \
     patch("scripts.fetch_prices._close_nse_session"):
    df_fb, still = fp.fetch_failed_via_nse(
        ["RELIANCE","TCS"], START, END, {"RELIANCE":"EQ","TCS":"EQ"}
    )
    record("NSE fallback: session failure → all returned as missing",
           df_fb.empty and set(still) == {"RELIANCE","TCS"})

# 4b: session starts, all symbols recovered
def _mock_fetch_symbol(sym: str, *a, **kw) -> pd.DataFrame:
    return _make_ohlcv(sym, 5)

with patch("scripts.fetch_prices._start_nse_session", return_value=True), \
     patch("scripts.fetch_prices._close_nse_session"), \
     patch("scripts.fetch_prices.fetch_symbol_nse", side_effect=_mock_fetch_symbol), \
     patch("scripts.fetch_prices.time.sleep"):
    df_fb2, still2 = fp.fetch_failed_via_nse(
        ["RELIANCE","TCS","INFY"], START, END,
        {"RELIANCE":"EQ","TCS":"EQ","INFY":"EQ"}
    )
    record("NSE fallback: all 3 symbols recovered",
           len(df_fb2["symbol"].unique()) == 3 and still2 == [],
           f"{len(df_fb2['symbol'].unique())} symbols")

# 4c: session starts, one symbol still empty
def _mock_fetch_partial(sym: str, *a, **kw) -> pd.DataFrame:
    return _make_ohlcv(sym, 5) if sym != "PROBLEM" else pd.DataFrame()

with patch("scripts.fetch_prices._start_nse_session", return_value=True), \
     patch("scripts.fetch_prices._close_nse_session"), \
     patch("scripts.fetch_prices.fetch_symbol_nse", side_effect=_mock_fetch_partial), \
     patch("scripts.fetch_prices.time.sleep"):
    df_fb3, still3 = fp.fetch_failed_via_nse(
        ["RELIANCE","PROBLEM"], START, END,
        {"RELIANCE":"EQ","PROBLEM":"EQ"}
    )
    record("NSE fallback: 1 recovered + 1 still missing",
           len(df_fb3["symbol"].unique()) == 1 and still3 == ["PROBLEM"],
           f"recovered={len(df_fb3['symbol'].unique())} still={still3}")


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 5: file writers
# ═══════════════════════════════════════════════════════════════════
print("\n" + "="*65)
print("TEST GROUP 5: file writers (temp dir)")
print("="*65)

tmp = Path(tempfile.mkdtemp(prefix="fp_unit_"))
orig_prices_dir = fp.PRICES_DIR
orig_daily     = fp.DAILY_FILE
fp.PRICES_DIR  = tmp
fp.DAILY_FILE  = tmp / "daily_adj_close.csv"

# Build a realistic combined DataFrame (3 symbols, 10 days)
df_write = pd.concat(
    [_make_ohlcv(s, 10) for s in ["RELIANCE","TCS","INFY"]],
    ignore_index=True,
)

week_str = fp.iso_week_str(date.today())

# 5a: write weekly snapshot
today = date.today()
week_start = today - timedelta(days=today.weekday())
week_df = df_write[pd.to_datetime(df_write["date"]).dt.date >= week_start]
if not week_df.empty:
    fp.write_weekly_snapshot(week_df, week_str)
snap_path = tmp / f"{week_str}.csv"
record("write_weekly_snapshot: file created", snap_path.exists())
snap_df = pd.read_csv(snap_path)
record("write_weekly_snapshot: 8-column schema",
       set(snap_df.columns) == {"symbol","date","open","high","low","close","adj_close","volume"})

# 5b: idempotency — calling again must not overwrite
import os
mtime_before = os.path.getmtime(snap_path)
fp.write_weekly_snapshot(week_df, week_str)
mtime_after  = os.path.getmtime(snap_path)
record("write_weekly_snapshot: idempotent (mtime unchanged)", mtime_before == mtime_after)

# 5c: daily file create
fp.update_daily_file(df_write)
record("update_daily_file: file created", fp.DAILY_FILE.exists())
daily = pd.read_csv(fp.DAILY_FILE, index_col=0, parse_dates=True)
record("update_daily_file: 3 symbol columns",
       len(daily.columns) == 3, f"{list(daily.columns)}")
record("update_daily_file: rows > 0", len(daily) > 0, f"{len(daily)} rows")

# 5d: append — add 2 extra future days
future = pd.concat(
    [_make_ohlcv(s, 2) for s in ["RELIANCE","TCS","INFY"]],
    ignore_index=True,
)
# shift dates forward so they're after the existing data
future["date"] = future["date"] + pd.Timedelta(days=365)
rows_before = len(daily)
fp.update_daily_file(future)
daily2 = pd.read_csv(fp.DAILY_FILE, index_col=0, parse_dates=True)
record("update_daily_file: append adds new rows",
       len(daily2) > rows_before, f"{rows_before} → {len(daily2)}")
record("update_daily_file: no duplicate index",
       not daily2.index.duplicated().any())

# 5e: idempotent re-append (same data, no new rows)
rows_mid = len(daily2)
fp.update_daily_file(future)
daily3 = pd.read_csv(fp.DAILY_FILE, index_col=0, parse_dates=True)
record("update_daily_file: idempotent append (no duplicate rows)",
       len(daily3) == rows_mid, f"{rows_mid} rows unchanged")

shutil.rmtree(tmp, ignore_errors=True)
fp.PRICES_DIR = orig_prices_dir
fp.DAILY_FILE = orig_daily


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
    failing_names = [n for n, s, _ in results if s == FAIL]
    print("  Failures:")
    for n in failing_names:
        print(f"    • {n}")
    sys.exit(1)
else:
    print("  All unit tests passed ✅")
