"""Unit tests for validate_data.py completeness check functions.

Tests check_price_completeness() and _benchmark_missing_days() using
small in-memory DataFrames — no file I/O, no network calls.

Run: uv run python tests/test_validate_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.metrics.validate_data import (
    _benchmark_missing_days,
    check_price_completeness,
)

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))


# ── shared test fixtures ──────────────────────────────────────────────────────

# Monday–Friday trading days: mirrors daily_adj_close.csv which strips weekends/holidays
MON = pd.Timestamp("2024-01-08")
TUE = pd.Timestamp("2024-01-09")
WED = pd.Timestamp("2024-01-10")
THU = pd.Timestamp("2024-01-11")
FRI = pd.Timestamp("2024-01-12")
TRADING_DAYS = [MON, TUE, WED, THU, FRI]
SAT = pd.Timestamp("2024-01-13")
SUN = pd.Timestamp("2024-01-14")


def make_daily_df(data: dict[str, list]) -> pd.DataFrame:
    """Build a wide-format daily_adj_close DataFrame over TRADING_DAYS."""
    return pd.DataFrame(data, index=pd.DatetimeIndex(TRADING_DAYS))


# ── check_price_completeness ──────────────────────────────────────────────────

def test_no_gaps_no_warnings() -> None:
    """Symbol with clean data every day → no warnings."""
    df = make_daily_df({"SYM_A": [100.0, 101.0, 102.0, 103.0, 104.0]})
    warns = check_price_completeness(df, {"SYM_A"}, verbose=False)
    record("no_gaps_no_warnings", len(warns) == 0, f"warnings={warns}")


def test_gap_in_middle_flagged() -> None:
    """Symbol with a NaN mid-window → one warning about data gaps."""
    df = make_daily_df({"SYM_B": [100.0, None, 102.0, None, 104.0]})
    warns = check_price_completeness(df, {"SYM_B"}, verbose=False)
    ok = len(warns) == 1 and "gap" in warns[0].lower()
    record("gap_in_middle_flagged", ok, f"warnings={warns}")


def test_gap_count_in_warning() -> None:
    """Warning text mentions the correct number of gap days."""
    df = make_daily_df({"SYM_C": [100.0, None, None, 103.0, 104.0]})
    warns = check_price_completeness(df, {"SYM_C"}, verbose=False)
    ok = len(warns) == 1 and "2 total gap-days" in warns[0]
    record("gap_count_in_warning", ok, f"warnings={warns}")


def test_trailing_nan_not_a_gap() -> None:
    """NaN after the last valid price is outside the active window → not a gap."""
    df = make_daily_df({"SYM_D": [100.0, 101.0, 102.0, 103.0, None]})
    warns = check_price_completeness(df, {"SYM_D"}, verbose=False)
    record("trailing_nan_not_a_gap", len(warns) == 0, f"warnings={warns}")


def test_leading_nan_not_a_gap() -> None:
    """NaN before the first valid price is outside the active window → not a gap."""
    df = make_daily_df({"SYM_E": [None, 101.0, 102.0, 103.0, 104.0]})
    warns = check_price_completeness(df, {"SYM_E"}, verbose=False)
    record("leading_nan_not_a_gap", len(warns) == 0, f"warnings={warns}")


def test_symbol_absent_from_file() -> None:
    """Symbol in universe but absent from daily file → 'no data' warning."""
    df = make_daily_df({"OTHER": [1.0, 2.0, 3.0, 4.0, 5.0]})
    warns = check_price_completeness(df, {"MISSING_SYM"}, verbose=False)
    ok = len(warns) == 1 and "no data" in warns[0].lower()
    record("symbol_absent_from_file", ok, f"warnings={warns}")


def test_symbol_all_nan_treated_as_no_data() -> None:
    """Column that is entirely NaN → treated as 'no data', not as gaps."""
    df = make_daily_df({"SYM_F": [None, None, None, None, None]})
    warns = check_price_completeness(df, {"SYM_F"}, verbose=False)
    ok = len(warns) == 1 and "no data" in warns[0].lower()
    record("symbol_all_nan_as_no_data", ok, f"warnings={warns}")


def test_weekends_never_flagged() -> None:
    """Sat/Sun are absent from the DataFrame index and cannot appear in warnings."""
    df = make_daily_df({"SYM_G": [100.0, 101.0, 102.0, 103.0, 104.0]})
    warns = check_price_completeness(df, {"SYM_G"}, verbose=False)
    all_text = " ".join(warns)
    ok = (
        len(warns) == 0
        and str(SAT.date()) not in all_text
        and str(SUN.date()) not in all_text
    )
    record("weekends_never_flagged", ok, f"warnings={warns}")


def test_verbose_mode_no_exception() -> None:
    """verbose=True with a gap does not raise an exception."""
    df = make_daily_df({"SYM_H": [100.0, None, 102.0, 103.0, 104.0]})
    try:
        check_price_completeness(df, {"SYM_H"}, verbose=True)
        record("verbose_mode_no_exception", True)
    except Exception as exc:
        record("verbose_mode_no_exception", False, str(exc))


def test_multiple_symbols_mixed() -> None:
    """Clean + gapped + absent symbols produce the correct two warnings."""
    df = make_daily_df({
        "CLEAN":  [100.0, 101.0, 102.0, 103.0, 104.0],
        "GAPPED": [100.0, None,  102.0, 103.0, 104.0],
    })
    warns = check_price_completeness(df, {"CLEAN", "GAPPED", "ABSENT"}, verbose=False)
    has_no_data = any("no data" in w.lower() for w in warns)
    has_gap     = any("gap" in w.lower() for w in warns)
    record("multiple_symbols_mixed", has_no_data and has_gap, f"warnings={warns}")


# ── _benchmark_missing_days ───────────────────────────────────────────────────

def _bm_df(covered: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame({
        "date": covered,
        "price_index": [100.0] * len(covered),
    })


def test_benchmark_fully_covered() -> None:
    """All trading days in benchmark → empty missing list."""
    daily_df = make_daily_df({"SYM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    missing = _benchmark_missing_days(daily_df, _bm_df(TRADING_DAYS))
    record("benchmark_fully_covered", len(missing) == 0, f"missing={missing}")


def test_benchmark_missing_three_days() -> None:
    """Benchmark covers only Mon and Fri → 3 missing days (Tue, Wed, Thu)."""
    daily_df = make_daily_df({"SYM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    missing = _benchmark_missing_days(daily_df, _bm_df([MON, FRI]))
    ok = len(missing) == 3 and TUE in missing and WED in missing and THU in missing
    record("benchmark_missing_three_days", ok, f"missing={missing}")


def test_benchmark_empty_df() -> None:
    """Empty benchmark DataFrame → all trading days are missing."""
    daily_df = make_daily_df({"SYM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    empty_bm = pd.DataFrame({"date": [], "price_index": []})
    missing = _benchmark_missing_days(daily_df, empty_bm)
    record("benchmark_empty_df", len(missing) == 5, f"missing={missing}")


def test_benchmark_nan_price_not_counted_as_covered() -> None:
    """Benchmark row with NaN price_index is treated as missing, not covered."""
    daily_df = make_daily_df({"SYM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    bm_with_nan = pd.DataFrame({
        "date": TRADING_DAYS,
        "price_index": [100.0, None, 102.0, None, 104.0],
    })
    missing = _benchmark_missing_days(daily_df, bm_with_nan)
    ok = len(missing) == 2 and TUE in missing and THU in missing
    record("benchmark_nan_price_not_covered", ok, f"missing={missing}")


def test_benchmark_extra_weekend_rows_ignored() -> None:
    """Extra Sat/Sun rows in benchmark do not affect the missing-day count."""
    daily_df = make_daily_df({"SYM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    bm_with_weekend = pd.DataFrame({
        "date": TRADING_DAYS + [SAT, SUN],
        "price_index": [100.0] * 7,
    })
    missing = _benchmark_missing_days(daily_df, bm_with_weekend)
    record("benchmark_extra_weekend_rows_ignored", len(missing) == 0, f"missing={missing}")


# ── runner ────────────────────────────────────────────────────────────────────

def run_all() -> None:
    tests = [
        test_no_gaps_no_warnings,
        test_gap_in_middle_flagged,
        test_gap_count_in_warning,
        test_trailing_nan_not_a_gap,
        test_leading_nan_not_a_gap,
        test_symbol_absent_from_file,
        test_symbol_all_nan_treated_as_no_data,
        test_weekends_never_flagged,
        test_verbose_mode_no_exception,
        test_multiple_symbols_mixed,
        test_benchmark_fully_covered,
        test_benchmark_missing_three_days,
        test_benchmark_empty_df,
        test_benchmark_nan_price_not_counted_as_covered,
        test_benchmark_extra_weekend_rows_ignored,
    ]

    for t in tests:
        try:
            t()
        except Exception as exc:
            record(t.__name__, False, f"EXCEPTION: {exc}")

    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)

    print(f"\n{'=' * 60}")
    print(f"validate_data unit tests: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    for name, status, detail in results:
        mark = "✓" if status == PASS else "✗"
        line = f"  {mark}  {name}"
        if detail and status == FAIL:
            line += f"\n       {detail}"
        print(line)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
