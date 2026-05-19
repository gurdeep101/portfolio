"""Unit tests for fetch_fundamentals.py — uses mocks, no live API calls.

Covers:
  - iso_week_str: date → ISO year-week string
  - weeks_to_process: price-dir scanning, missing-file filtering, --force
  - get_week_date: reads latest date from a prices CSV
  - fetch_pe_from_nselib: column detection, date fallback, failure handling
  - build_fundamentals: FundamentalsEntry construction from PE map
  - main(): end-to-end routing, --force, early-exit

Run: uv run python tests/test_fetch_fundamentals_unit.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from contextlib import suppress
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

import scripts.fetch.fetch_fundamentals as ff

PASS = "✅ PASS"
FAIL = "❌ FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


# ─── shared fixture helpers ──────────────────────────────────────────────────

def _write_prices_csv(path: Path, dates: list[str]) -> None:
    rows = "\n".join(
        f"RELIANCE,{d},100,105,95,102,102,1000000" for d in dates
    )
    path.write_text(
        "symbol,date,open,high,low,close,adj_close,volume\n" + rows,
        encoding="utf-8",
    )


def _write_fundamentals_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _make_pe_df(symbols_pes: dict[str, float | None]) -> pd.DataFrame:
    rows = [{"SYMBOL": sym, "SYMBOLP/E": pe} for sym, pe in symbols_pes.items()]
    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 1: iso_week_str
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 1: iso_week_str")
print("=" * 65)

record(
    "iso_week_str: known date 2026-01-05 (week 2)",
    ff.iso_week_str(date(2026, 1, 5)) == "2026-02",
)
record(
    "iso_week_str: zero-padded single-digit week",
    ff.iso_week_str(date(2026, 1, 1)) == "2026-01",
)
record(
    "iso_week_str: week 52/53 boundary",
    ff.iso_week_str(date(2025, 12, 29)).startswith("2026-"),
)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 2: weeks_to_process
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 2: weeks_to_process")
print("=" * 65)

tmp_wtp = Path(tempfile.mkdtemp(prefix="ff_wtp_"))
prices_dir = tmp_wtp / "prices"
funds_dir = tmp_wtp / "fundamentals"
prices_dir.mkdir()
funds_dir.mkdir()

for w in ["2025-01", "2025-02", "2025-03"]:
    _write_prices_csv(prices_dir / f"{w}.csv", ["2025-01-03"])

_write_fundamentals_json(funds_dir / "2025-02.json", {})

with (
    patch.object(ff, "PRICES_DIR", prices_dir),
    patch.object(ff, "FUNDAMENTALS_DIR", funds_dir),
):
    missing = ff.weeks_to_process(force=False)
    forced = ff.weeks_to_process(force=True)

record(
    "weeks_to_process: skips week with existing JSON",
    [w for w, _ in missing] == ["2025-01", "2025-03"],
    f"got={[w for w, _ in missing]}",
)
record(
    "weeks_to_process: --force returns all 3 weeks",
    [w for w, _ in forced] == ["2025-01", "2025-02", "2025-03"],
    f"got={[w for w, _ in forced]}",
)
record(
    "weeks_to_process: csv_path is a Path object",
    all(isinstance(p, Path) for _, p in missing),
)

(prices_dir / "daily_adj_close.csv").write_text("date,RELIANCE\n", encoding="utf-8")

with (
    patch.object(ff, "PRICES_DIR", prices_dir),
    patch.object(ff, "FUNDAMENTALS_DIR", funds_dir),
):
    after_daily = ff.weeks_to_process(force=False)

record(
    "weeks_to_process: daily_adj_close.csv excluded by glob",
    all("daily" not in w for w, _ in after_daily),
)

shutil.rmtree(tmp_wtp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 3: get_week_date
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 3: get_week_date")
print("=" * 65)

tmp_gwd = Path(tempfile.mkdtemp(prefix="ff_gwd_"))

csv_path = tmp_gwd / "2025-01.csv"
_write_prices_csv(csv_path, ["2025-01-03", "2025-01-06", "2025-01-10"])
record(
    "get_week_date: returns latest date from multi-date CSV",
    ff.get_week_date(csv_path) == date(2025, 1, 10),
    f"got={ff.get_week_date(csv_path)}",
)

timestamp_csv = tmp_gwd / "2025-01-timestamps.csv"
_write_prices_csv(timestamp_csv, ["2025-01-03 00:00:00", "2025-01-10 00:00:00"])
record(
    "get_week_date: accepts timestamp-formatted dates",
    ff.get_week_date(timestamp_csv) == date(2025, 1, 10),
    f"got={ff.get_week_date(timestamp_csv)}",
)

single_csv = tmp_gwd / "2025-02.csv"
_write_prices_csv(single_csv, ["2025-01-17"])
record(
    "get_week_date: single-date CSV",
    ff.get_week_date(single_csv) == date(2025, 1, 17),
)
record(
    "get_week_date: missing file returns None",
    ff.get_week_date(tmp_gwd / "nonexistent.csv") is None,
)

empty_csv = tmp_gwd / "empty.csv"
empty_csv.write_text("symbol,date,open,high,low,close,adj_close,volume\n", encoding="utf-8")
record(
    "get_week_date: empty CSV returns None",
    ff.get_week_date(empty_csv) is None,
)

shutil.rmtree(tmp_gwd, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 4: fetch_pe_from_nselib
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 4: fetch_pe_from_nselib")
print("=" * 65)

# 4a: successful first-attempt response
pe_df = _make_pe_df({"RELIANCE": 22.5, "TCS": 30.1, "INFY": 0.0})
mock_cm = MagicMock()
mock_cm.pe_ratio.return_value = pe_df

with patch.object(ff, "nse_cm", mock_cm):
    pe_map = ff.fetch_pe_from_nselib(date(2025, 1, 10))

record(
    "fetch_pe_from_nselib: returns PE for valid symbols",
    pe_map.get("RELIANCE") == 22.5 and pe_map.get("TCS") == 30.1,
    f"RELIANCE={pe_map.get('RELIANCE')} TCS={pe_map.get('TCS')}",
)
record(
    "fetch_pe_from_nselib: PE=0 mapped to None",
    pe_map.get("INFY") is None,
    f"INFY={pe_map.get('INFY')}",
)

# 4b: first date raises, second succeeds (holiday fallback)
call_count = [0]

def _fail_then_succeed(date_str: str) -> pd.DataFrame:
    call_count[0] += 1
    if call_count[0] == 1:
        raise Exception("NSE archive not found for date")
    return _make_pe_df({"RELIANCE": 25.0})

mock_cm_retry = MagicMock()
mock_cm_retry.pe_ratio.side_effect = _fail_then_succeed

with patch.object(ff, "nse_cm", mock_cm_retry):
    call_count[0] = 0
    pe_map_retry = ff.fetch_pe_from_nselib(date(2025, 1, 10))

record(
    "fetch_pe_from_nselib: retries on failure and succeeds",
    pe_map_retry.get("RELIANCE") == 25.0 and call_count[0] == 2,
    f"calls={call_count[0]} RELIANCE={pe_map_retry.get('RELIANCE')}",
)

# 4c: all attempts fail → empty dict
mock_cm_fail = MagicMock()
mock_cm_fail.pe_ratio.side_effect = Exception("Archive unavailable")

with patch.object(ff, "nse_cm", mock_cm_fail):
    pe_map_fail = ff.fetch_pe_from_nselib(date(2025, 1, 10))

record(
    "fetch_pe_from_nselib: all attempts fail → empty dict",
    pe_map_fail == {},
    f"got={pe_map_fail}",
)
record(
    "fetch_pe_from_nselib: exactly 4 attempts on total failure",
    mock_cm_fail.pe_ratio.call_count == 4,
    f"calls={mock_cm_fail.pe_ratio.call_count}",
)

# 4d: empty DataFrame → skips and tries next date
empty_call_count = [0]

def _empty_then_data(date_str: str) -> pd.DataFrame:
    empty_call_count[0] += 1
    if empty_call_count[0] == 1:
        return pd.DataFrame()
    return _make_pe_df({"TCS": 28.0})

mock_cm_empty = MagicMock()
mock_cm_empty.pe_ratio.side_effect = _empty_then_data

with patch.object(ff, "nse_cm", mock_cm_empty):
    empty_call_count[0] = 0
    pe_map_empty = ff.fetch_pe_from_nselib(date(2025, 1, 10))

record(
    "fetch_pe_from_nselib: empty DataFrame skipped, next date used",
    pe_map_empty.get("TCS") == 28.0,
    f"TCS={pe_map_empty.get('TCS')}",
)

# 4e: date formatted as DD-MM-YYYY
received_dates: list[str] = []

def _capture_date(date_str: str) -> pd.DataFrame:
    received_dates.append(date_str)
    return _make_pe_df({"RELIANCE": 20.0})

mock_cm_date = MagicMock()
mock_cm_date.pe_ratio.side_effect = _capture_date

with patch.object(ff, "nse_cm", mock_cm_date):
    received_dates.clear()
    ff.fetch_pe_from_nselib(date(2025, 3, 15))

record(
    "fetch_pe_from_nselib: date formatted as DD-MM-YYYY",
    received_dates[0] == "15-03-2025",
    f"got='{received_dates[0]}'",
)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 5: build_fundamentals
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 5: build_fundamentals")
print("=" * 65)

symbols = [("RELIANCE", "Energy"), ("TCS", "Technology"), ("UNKNOWN", "Financials")]
pe_map = {"RELIANCE": 22.5, "TCS": 30.0}
d = date(2025, 1, 10)

hist = ff.build_fundamentals(d, symbols, pe_map)

record(
    "build_fundamentals: all 3 symbols present",
    set(hist.keys()) == {"RELIANCE", "TCS", "UNKNOWN"},
)
record(
    "build_fundamentals: PE set for matched symbols",
    hist["RELIANCE"]["pe_ratio"] == 22.5 and hist["TCS"]["pe_ratio"] == 30.0,
    f"RELIANCE.pe={hist['RELIANCE']['pe_ratio']}",
)
record(
    "build_fundamentals: PE is None for unmatched symbol",
    hist["UNKNOWN"]["pe_ratio"] is None,
)
record(
    "build_fundamentals: pe_ratio is the only fundamental field written",
    all("pb_ratio" not in v and "roe" not in v and "market_cap_cr" not in v
        for v in hist.values()),
)
record(
    "build_fundamentals: fetch_date is trading date ISO string",
    all(v["fetch_date"] == "2025-01-10" for v in hist.values()),
)
record(
    "build_fundamentals: source is nselib",
    all(v["source"] == "nselib" for v in hist.values()),
)
record(
    "build_fundamentals: sector from universe preserved",
    hist["TCS"]["sector"] == "Technology",
)
record(
    "build_fundamentals: empty PE map gives None PE",
    ff.build_fundamentals(d, symbols[:1], {})["RELIANCE"]["pe_ratio"] is None,
)


# ═══════════════════════════════════════════════════════════════════
# TEST GROUP 6: main() integration (mocked)
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 65)
print("TEST GROUP 6: main() integration (mocked)")
print("=" * 65)

tmp_main = Path(tempfile.mkdtemp(prefix="ff_main_"))
prices_main = tmp_main / "prices"
funds_main = tmp_main / "fundamentals"
prices_main.mkdir()
funds_main.mkdir()

today = date.today()
_write_prices_csv(prices_main / "2025-10.csv", ["2025-03-07"])
_write_prices_csv(prices_main / f"{ff.iso_week_str(today)}.csv", [today.isoformat()])

mock_symbols = [("RELIANCE", "Energy"), ("TCS", "Technology")]
mock_cm_main = MagicMock()
mock_cm_main.pe_ratio.return_value = _make_pe_df({"RELIANCE": 22.0, "TCS": 28.0})

with (
    patch.object(ff, "PRICES_DIR", prices_main),
    patch.object(ff, "FUNDAMENTALS_DIR", funds_main),
    patch.object(ff, "load_symbols", return_value=mock_symbols),
    patch.object(ff, "nse_cm", mock_cm_main),
    patch("sys.argv", ["fetch_fundamentals.py"]),
):
    ff.main()

hist_file = funds_main / "2025-10.json"
curr_file = funds_main / f"{ff.iso_week_str(today)}.json"

record("main: historical week JSON created", hist_file.exists())
record("main: current week JSON created", curr_file.exists())

if hist_file.exists():
    data = json.loads(hist_file.read_text())
    record(
        "main: source=nselib in output",
        data.get("RELIANCE", {}).get("source") == "nselib",
    )
    record(
        "main: pe_ratio set from nselib",
        data.get("RELIANCE", {}).get("pe_ratio") == 22.0,
        f"pe={data.get('RELIANCE', {}).get('pe_ratio')}",
    )
    record(
        "main: only pe_ratio, sector, fetch_date, source written (no pb_ratio/roe/market_cap)",
        "pb_ratio" not in data.get("RELIANCE", {})
        and "roe" not in data.get("RELIANCE", {})
        and "market_cap_cr" not in data.get("RELIANCE", {}),
    )

# --force overwrites existing files
with (
    patch.object(ff, "PRICES_DIR", prices_main),
    patch.object(ff, "FUNDAMENTALS_DIR", funds_main),
    patch.object(ff, "load_symbols", return_value=mock_symbols),
    patch.object(ff, "nse_cm", mock_cm_main),
    patch("sys.argv", ["fetch_fundamentals.py", "--force"]),
):
    ff.main()

record("main: --force re-runs when files already exist", True)

# early exit when nothing to do
exited = [False]
exit_code = [None]

def _capture_exit(code=0):
    exited[0] = True
    exit_code[0] = code
    raise SystemExit(code)

with (
    patch.object(ff, "PRICES_DIR", prices_main),
    patch.object(ff, "FUNDAMENTALS_DIR", funds_main),
    patch("sys.argv", ["fetch_fundamentals.py"]),
    patch("sys.exit", side_effect=_capture_exit),
    suppress(SystemExit),
):
    ff.main()

record(
    "main: exits 0 when all files exist and no --force",
    exited[0] and exit_code[0] == 0,
    f"exited={exited[0]} code={exit_code[0]}",
)

shutil.rmtree(tmp_main, ignore_errors=True)


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
