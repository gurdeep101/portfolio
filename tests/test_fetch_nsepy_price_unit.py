"""Unit tests for fetch_nsepy_price.py — uses mocks, no live API calls.

Covers:
  - _is_nse_retryable: transient error detection
  - _normalise_nselib_df: column mapping, zero-volume filtering
  - fetch_symbol_nselib: success, empty, retry on transient error
  - fetch_all_nselib: circuit breaker, progress, combined output
  - _normalise_jugaad_df: column mapping
  - fetch_failed_via_jugaad: partial recovery, full recovery
  - fetch_failed_via_yfinance: symbol format conversion, delegation
  - write_weekly_snapshot / update_daily_file: imported writer verification

Run: uv run python tests/test_fetch_nsepy_price_unit.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import MagicMock, patch

import pandas as pd

import scripts.fetch.fetch_nsepy_price as fnp

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


# ── shared helpers ───────────────────────────────────────────────────────────


def _make_ohlcv(symbol: str, nrows: int = 5) -> pd.DataFrame:
    """Return a minimal OHLCV DataFrame in the 8-column standard shape."""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(nrows - 1, -1, -1)]
    return pd.DataFrame({
        "symbol": [symbol] * nrows,
        "date": pd.to_datetime(dates),
        "open": [100.0 + i for i in range(nrows)],
        "high": [105.0 + i for i in range(nrows)],
        "low": [95.0 + i for i in range(nrows)],
        "close": [102.0 + i for i in range(nrows)],
        "adj_close": [102.0 + i for i in range(nrows)],
        "volume": [1_000_000 + i * 1000 for i in range(nrows)],
    })


def _make_nselib_raw(symbol: str, nrows: int = 5) -> pd.DataFrame:
    """Build a fake nselib price_volume_data DataFrame."""
    today = date.today()
    dates = [(today - timedelta(days=i)).strftime("%d-%b-%Y") for i in range(nrows - 1, -1, -1)]
    return pd.DataFrame({
        "Symbol": [symbol] * nrows,
        "Series": ["EQ"] * nrows,
        "Date": dates,
        "PrevClose": [99.0 + i for i in range(nrows)],
        "OpenPrice": [100.0 + i for i in range(nrows)],
        "HighPrice": [105.0 + i for i in range(nrows)],
        "LowPrice": [95.0 + i for i in range(nrows)],
        "LastPrice": [101.0 + i for i in range(nrows)],
        "ClosePrice": [102.0 + i for i in range(nrows)],
        "AveragePrice": [100.5 + i for i in range(nrows)],
        "TotalTradedQuantity": [1_000_000 + i * 1000 for i in range(nrows)],
        "Turnover": [100_000_000.0 + i for i in range(nrows)],
        "No.ofTrades": [50_000 + i for i in range(nrows)],
    })


def _make_jugaad_raw(symbol: str, nrows: int = 5) -> pd.DataFrame:
    """Build a fake jugaad-data stock_df DataFrame."""
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(nrows - 1, -1, -1)]
    return pd.DataFrame({
        "DATE": pd.to_datetime(dates),
        "SERIES": ["EQ"] * nrows,
        "OPEN": [100.0 + i for i in range(nrows)],
        "HIGH": [105.0 + i for i in range(nrows)],
        "LOW": [95.0 + i for i in range(nrows)],
        "PREV. CLOSE": [99.0 + i for i in range(nrows)],
        "LTP": [101.0 + i for i in range(nrows)],
        "CLOSE": [102.0 + i for i in range(nrows)],
        "VWAP": [100.5 + i for i in range(nrows)],
        "VOLUME": [1_000_000 + i * 1000 for i in range(nrows)],
        "VALUE": [100_000_000.0 + i for i in range(nrows)],
        "NO OF TRADES": [50_000 + i for i in range(nrows)],
        "DELIVERY QTY": [500_000 + i for i in range(nrows)],
        "DELIVERY %": [50.0 + i * 0.1 for i in range(nrows)],
        "SYMBOL": [symbol] * nrows,
    })


START = date.today() - timedelta(days=14)
END = date.today()


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 1: helpers
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 1: helper functions")
print("=" * 65)

record(
    "_is_nse_retryable: 403 error",
    fnp._is_nse_retryable(Exception("HTTP 403 Forbidden")),
)

record(
    "_is_nse_retryable: 429 error",
    fnp._is_nse_retryable(Exception("HTTP Error 429: Too Many Requests")),
)

record(
    "_is_nse_retryable: timeout error",
    fnp._is_nse_retryable(Exception("Connection timeout occurred")),
)

record(
    "_is_nse_retryable: resource not available",
    fnp._is_nse_retryable(Exception("Resource not available MSG: error")),
)

record(
    "_is_nse_retryable: generic error → False",
    not fnp._is_nse_retryable(Exception("KeyError: 'Close'")),
)

record(
    "_is_nse_retryable: connection error",
    fnp._is_nse_retryable(Exception("ConnectionError: failed to connect")),
)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 2: nselib normalisation and fetching (mocked)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 2: nselib normalisation and fetching (mocked)")
print("=" * 65)

# 2a: _normalise_nselib_df — correct column mapping
raw_nselib = _make_nselib_raw("RELIANCE", 5)
normed = fnp._normalise_nselib_df(raw_nselib, "RELIANCE")

record(
    "nselib normalise: 5 rows returned",
    len(normed) == 5,
    f"{len(normed)} rows",
)
record(
    "nselib normalise: all 8 columns present",
    set(normed.columns) == {"symbol", "date", "open", "high", "low", "close", "adj_close", "volume"},
)
record(
    "nselib normalise: symbol set correctly",
    (normed["symbol"] == "RELIANCE").all(),
)
record(
    "nselib normalise: close = 102.0 for first row",
    abs(normed.iloc[0]["close"] - 102.0) < 0.01,
    f"close={normed.iloc[0]['close']}",
)
record(
    "nselib normalise: adj_close equals close",
    (normed["adj_close"] == normed["close"]).all(),
)
record(
    "nselib normalise: date is datetime",
    pd.api.types.is_datetime64_any_dtype(normed["date"]),
)

# 2b: _normalise_nselib_df — zero-volume filtering
raw_zv = _make_nselib_raw("TCS", 3)
raw_zv.loc[1, "TotalTradedQuantity"] = 0
raw_zv.loc[1, "ClosePrice"] = raw_zv.loc[0, "ClosePrice"]
normed_zv = fnp._normalise_nselib_df(raw_zv, "TCS")
record(
    "nselib normalise: zero-volume carry-forward row dropped",
    len(normed_zv) == 2,
    f"{len(normed_zv)} rows",
)

# 2c: _normalise_nselib_df — empty input
record(
    "nselib normalise: empty input → empty output",
    fnp._normalise_nselib_df(pd.DataFrame(), "X").empty,
)

# 2d: _normalise_nselib_df — missing columns
bad_df = pd.DataFrame({"Foo": [1], "Bar": [2]})
record(
    "nselib normalise: missing columns → empty output",
    fnp._normalise_nselib_df(bad_df, "X").empty,
)

# 2e: fetch_symbol_nselib — success path
mock_cm = MagicMock()
mock_cm.price_volume_data.return_value = _make_nselib_raw("RELIANCE", 5)

with (
    patch.dict("sys.modules", {"nselib": MagicMock(), "nselib.capital_market": mock_cm}),
    patch("scripts.fetch.fetch_nsepy_price.time.sleep"),
    patch("nselib.capital_market", mock_cm),
):
    df_ok = fnp.fetch_symbol_nselib("RELIANCE", START, END)

record(
    "fetch_symbol_nselib: success returns 5 rows",
    len(df_ok) == 5,
    f"{len(df_ok)} rows",
)

# 2f: fetch_symbol_nselib — empty response
mock_cm2 = MagicMock()
mock_cm2.price_volume_data.return_value = pd.DataFrame()

with (
    patch.dict("sys.modules", {"nselib": MagicMock(), "nselib.capital_market": mock_cm2}),
    patch("scripts.fetch.fetch_nsepy_price.time.sleep"),
    patch("nselib.capital_market", mock_cm2),
):
    df_empty = fnp.fetch_symbol_nselib("RELIANCE", START, END)

record("fetch_symbol_nselib: empty response → empty DataFrame", df_empty.empty)

# 2g: fetch_symbol_nselib — retry on transient error then succeed
call_count = [0]


def _nselib_retry_mock(*a, **kw):
    call_count[0] += 1
    if call_count[0] == 1:
        raise Exception("Resource not available MSG: timeout")
    return _make_nselib_raw("RELIANCE", 3)


mock_cm3 = MagicMock()
mock_cm3.price_volume_data.side_effect = _nselib_retry_mock

with patch.dict("sys.modules", {"nselib": MagicMock(), "nselib.capital_market": mock_cm3}), \
     patch("scripts.fetch.fetch_nsepy_price.time.sleep"):
    call_count[0] = 0
    with patch("nselib.capital_market", mock_cm3):
        df_retry = fnp.fetch_symbol_nselib("RELIANCE", START, END)

record(
    "fetch_symbol_nselib: retried on transient error and succeeded",
    len(df_retry) == 3 and call_count[0] == 2,
    f"rows={len(df_retry)} calls={call_count[0]}",
)

# 2h: fetch_all_nselib — circuit breaker and combined output
def _mock_nselib_fetch(sym, *a, **kw):
    if sym.startswith("FAIL"):
        return pd.DataFrame()
    return _make_ohlcv(sym, 3)

with patch("scripts.fetch.fetch_nsepy_price.fetch_symbol_nselib", side_effect=_mock_nselib_fetch), \
     patch("scripts.fetch.fetch_nsepy_price.time.sleep"):
    syms = ["OK1", "OK2", "FAIL1", "FAIL2", "FAIL3", "FAIL4", "FAIL5", "OK3"]
    df_all, failed_all = fnp.fetch_all_nselib(syms, START, END)

record(
    "fetch_all_nselib: 3 ok + 5 failed",
    len(df_all["symbol"].unique()) == 3 and len(failed_all) == 5,
    f"ok={len(df_all['symbol'].unique())} failed={len(failed_all)}",
)
record(
    "fetch_all_nselib: failed list correct",
    set(failed_all) == {"FAIL1", "FAIL2", "FAIL3", "FAIL4", "FAIL5"},
)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 3: jugaad-data normalisation and fallback (mocked)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 3: jugaad-data normalisation and fallback (mocked)")
print("=" * 65)

# 3a: _normalise_jugaad_df — correct column mapping
raw_jugaad = _make_jugaad_raw("TCS", 5)
normed_j = fnp._normalise_jugaad_df(raw_jugaad, "TCS")

record(
    "jugaad normalise: 5 rows returned",
    len(normed_j) == 5,
    f"{len(normed_j)} rows",
)
record(
    "jugaad normalise: all 8 columns present",
    set(normed_j.columns) == {"symbol", "date", "open", "high", "low", "close", "adj_close", "volume"},
)
record(
    "jugaad normalise: symbol set correctly",
    (normed_j["symbol"] == "TCS").all(),
)
record(
    "jugaad normalise: adj_close equals close",
    (normed_j["adj_close"] == normed_j["close"]).all(),
)
record(
    "jugaad normalise: empty input → empty output",
    fnp._normalise_jugaad_df(pd.DataFrame(), "X").empty,
)

# 3b: fetch_failed_via_jugaad — partial recovery
def _mock_jugaad_partial(sym, *a, **kw):
    return _make_ohlcv(sym, 5) if sym != "PROBLEM" else pd.DataFrame()

with patch("scripts.fetch.fetch_nsepy_price.fetch_symbol_jugaad", side_effect=_mock_jugaad_partial), \
     patch("scripts.fetch.fetch_nsepy_price.time.sleep"):
    df_fb, still = fnp.fetch_failed_via_jugaad(
        ["RELIANCE", "PROBLEM"], START, END, {"RELIANCE": "EQ", "PROBLEM": "EQ"}
    )

record(
    "jugaad fallback: 1 recovered + 1 still missing",
    len(df_fb["symbol"].unique()) == 1 and still == ["PROBLEM"],
    f"recovered={len(df_fb['symbol'].unique())} still={still}",
)

# 3c: fetch_failed_via_jugaad — all recovered
def _mock_jugaad_all(sym, *a, **kw):
    return _make_ohlcv(sym, 5)

with patch("scripts.fetch.fetch_nsepy_price.fetch_symbol_jugaad", side_effect=_mock_jugaad_all), \
     patch("scripts.fetch.fetch_nsepy_price.time.sleep"):
    df_fb2, still2 = fnp.fetch_failed_via_jugaad(
        ["A", "B", "C"], START, END, {"A": "EQ", "B": "EQ", "C": "EQ"}
    )

record(
    "jugaad fallback: all 3 recovered",
    len(df_fb2["symbol"].unique()) == 3 and still2 == [],
    f"{len(df_fb2['symbol'].unique())} symbols",
)

# 3d: fetch_failed_via_jugaad — empty input
df_fb3, still3 = fnp.fetch_failed_via_jugaad([], START, END, {})
record("jugaad fallback: empty input → empty output", df_fb3.empty and still3 == [])


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 4: yfinance fallback (mocked)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 4: yfinance fallback (mocked)")
print("=" * 65)

# 4a: fetch_failed_via_yfinance — per-symbol Ticker.history() with .NS suffix
def _make_ticker_history_df(sym: str, nrows: int = 5) -> pd.DataFrame:
    """Build a fake Ticker.history() DataFrame: DatetimeIndex, OHLCV + Adj Close columns."""
    today = date.today()
    idx = pd.DatetimeIndex(
        [today - timedelta(days=i) for i in range(nrows - 1, -1, -1)],
        name="Date",
    )
    return pd.DataFrame({
        "Open":      [100.0 + i for i in range(nrows)],
        "High":      [105.0 + i for i in range(nrows)],
        "Low":       [95.0  + i for i in range(nrows)],
        "Close":     [102.0 + i for i in range(nrows)],
        "Adj Close": [102.0 + i for i in range(nrows)],
        "Volume":    [1_000_000 + i * 1000 for i in range(nrows)],
    }, index=idx)


class _MockTicker:
    """Minimal yf.Ticker stand-in that returns fake history data."""
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
    def history(self, **kw: object) -> pd.DataFrame:
        sym = self.ticker.replace(".NS", "")
        return _make_ticker_history_df(sym, 5)


with (
    patch("scripts.fetch.fetch_nsepy_price.yf.Ticker", side_effect=_MockTicker),
    patch("scripts.fetch.fetch_nsepy_price.time.sleep"),
):
    df_yf, yf_missing = fnp.fetch_failed_via_yfinance(["RELIANCE", "TCS"], START, END)

record(
    "yfinance fallback: 2 symbols returned",
    len(df_yf["symbol"].unique()) == 2 and yf_missing == [],
    f"{len(df_yf['symbol'].unique())} symbols",
)
record(
    "yfinance fallback: all 8 columns present",
    all(c in df_yf.columns for c in ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]),
)

# 4b: fetch_failed_via_yfinance — empty input
df_yf2, yf_m2 = fnp.fetch_failed_via_yfinance([], START, END)
record("yfinance fallback: empty input → empty output", df_yf2.empty and yf_m2 == [])


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 5: file writers (temp dir)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 5: file writers (temp dir)")
print("=" * 65)

# Monkeypatch the module-level path constants in fetch_nsepy_price itself
tmp = Path(tempfile.mkdtemp(prefix="fnp_unit_"))
orig_prices_dir = fnp.PRICES_DIR
orig_daily      = fnp.DAILY_FILE
fnp.PRICES_DIR  = tmp
fnp.DAILY_FILE  = tmp / "daily_adj_close.csv"

df_write = pd.concat(
    [_make_ohlcv(s, 10) for s in ["RELIANCE", "TCS", "INFY"]],
    ignore_index=True,
)

week_str  = fnp.iso_week_str(date.today())
today     = date.today()
week_start = today - timedelta(days=today.weekday())
week_df   = df_write[pd.to_datetime(df_write["date"]).dt.date >= week_start]
if not week_df.empty:
    fnp.write_weekly_snapshot(week_df, week_str)

snap_path = tmp / f"{week_str}.csv"
record("write_weekly_snapshot: file created", snap_path.exists())

if snap_path.exists():
    snap_df = pd.read_csv(snap_path)
    record(
        "write_weekly_snapshot: 8-column schema",
        set(snap_df.columns) == {"symbol", "date", "open", "high", "low", "close", "adj_close", "volume"},
    )

fnp.update_daily_file(df_write)
record("update_daily_file: file created", fnp.DAILY_FILE.exists())

if fnp.DAILY_FILE.exists():
    daily = pd.read_csv(fnp.DAILY_FILE, index_col=0, parse_dates=True)
    record("update_daily_file: 3 symbol columns", len(daily.columns) == 3, f"{list(daily.columns)}")

shutil.rmtree(tmp, ignore_errors=True)
fnp.PRICES_DIR = orig_prices_dir
fnp.DAILY_FILE = orig_daily


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 6: --force behavior
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 6: --force behavior")
print("=" * 65)

# 6a: update_daily_file(force=True) replaces overlapping dates but keeps others
tmp_force = Path(tempfile.mkdtemp(prefix="fnp_force_"))
orig_prices_dir = fnp.PRICES_DIR
orig_daily = fnp.DAILY_FILE
fnp.PRICES_DIR = tmp_force
fnp.DAILY_FILE = tmp_force / "daily_adj_close.csv"

existing_idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
existing_daily = pd.DataFrame(
    {"RELIANCE": [100.0, 101.0, 102.0], "TCS": [200.0, 201.0, 202.0]},
    index=existing_idx,
)
existing_daily.to_csv(fnp.DAILY_FILE)

df_force_write = pd.DataFrame({
    "symbol": ["RELIANCE", "TCS", "RELIANCE", "TCS"],
    "date": pd.to_datetime(["2026-01-02", "2026-01-02", "2026-01-04", "2026-01-04"]),
    "open": [0.0, 0.0, 0.0, 0.0],
    "high": [0.0, 0.0, 0.0, 0.0],
    "low": [0.0, 0.0, 0.0, 0.0],
    "close": [0.0, 0.0, 0.0, 0.0],
    "adj_close": [999.0, 888.0, 111.0, 222.0],
    "volume": [1, 1, 1, 1],
})

fnp.update_daily_file(df_force_write, force=True)
after_force = pd.read_csv(fnp.DAILY_FILE, index_col=0, parse_dates=True)

record(
    "update_daily_file force: overlap date replaced",
    abs(after_force.loc[pd.Timestamp("2026-01-02"), "RELIANCE"] - 999.0) < 1e-9
    and abs(after_force.loc[pd.Timestamp("2026-01-02"), "TCS"] - 888.0) < 1e-9,
)
record(
    "update_daily_file force: non-overlap old date preserved",
    abs(after_force.loc[pd.Timestamp("2026-01-01"), "RELIANCE"] - 100.0) < 1e-9
    and abs(after_force.loc[pd.Timestamp("2026-01-03"), "TCS"] - 202.0) < 1e-9,
)
record(
    "update_daily_file force: new date added",
    pd.Timestamp("2026-01-04") in after_force.index
    and abs(after_force.loc[pd.Timestamp("2026-01-04"), "RELIANCE"] - 111.0) < 1e-9,
)

shutil.rmtree(tmp_force, ignore_errors=True)
fnp.PRICES_DIR = orig_prices_dir
fnp.DAILY_FILE = orig_daily

# 6b: main --force uses HISTORY_WEEKS window when --months omitted
captured_ranges: list[tuple[date, date]] = []

def _mock_run_cascade(symbols, meta, start, end):
    captured_ranges.append((start, end))
    return _make_ohlcv(symbols[0], 3), []

with (
    patch("scripts.fetch.fetch_nsepy_price.load_symbols", return_value=["RELIANCE"]),
    patch("scripts.fetch.fetch_nsepy_price.load_symbol_meta", return_value={"RELIANCE": "EQ"}),
    patch("scripts.fetch.fetch_nsepy_price.check_snapshot_sync", return_value=set()),
    patch("scripts.fetch.fetch_nsepy_price._run_cascade", side_effect=_mock_run_cascade),
    patch("scripts.fetch.fetch_nsepy_price.write_historical_snapshots"),
    patch("scripts.fetch.fetch_nsepy_price.update_daily_file"),
    patch("sys.argv", ["fetch_nsepy_price.py", "--force"]),
):
    captured_ranges.clear()
    fnp.main()

expected_start = date.today() - timedelta(weeks=fnp.HISTORY_WEEKS)
record(
    "main force: full HISTORY_WEEKS range used",
    len(captured_ranges) == 1
    and captured_ranges[0][0] == expected_start
    and captured_ranges[0][1] == date.today(),
    f"got={captured_ranges[0] if captured_ranges else None}",
)

# 6c: main --force --months N uses requested month-based start
with (
    patch("scripts.fetch.fetch_nsepy_price.load_symbols", return_value=["RELIANCE"]),
    patch("scripts.fetch.fetch_nsepy_price.load_symbol_meta", return_value={"RELIANCE": "EQ"}),
    patch("scripts.fetch.fetch_nsepy_price.check_snapshot_sync", return_value=set()),
    patch("scripts.fetch.fetch_nsepy_price._run_cascade", side_effect=_mock_run_cascade),
    patch("scripts.fetch.fetch_nsepy_price.write_historical_snapshots"),
    patch("scripts.fetch.fetch_nsepy_price.update_daily_file"),
    patch("sys.argv", ["fetch_nsepy_price.py", "--force", "--months", "3"]),
):
    captured_ranges.clear()
    fnp.main()

expected_month_start = fnp.subtract_months(date.today(), 3)
record(
    "main force months: month-based start used",
    len(captured_ranges) == 1
    and captured_ranges[0][0] == expected_month_start
    and captured_ranges[0][1] == date.today(),
    f"got={captured_ranges[0] if captured_ranges else None}",
)


# ═══════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("SUMMARY")
print("=" * 65)
passed = sum(1 for _, s, _ in results if s == PASS)
failed = sum(1 for _, s, _ in results if s == FAIL)

for name, status, detail in results:
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))

print(f"\n  {passed}/{passed + failed} tests passed")
if failed:
    failing_names = [n for n, s, _ in results if s == FAIL]
    print("  Failures:")
    for n in failing_names:
        print(f"    • {n}")
    sys.exit(1)
else:
    print("  All unit tests passed ✅")
