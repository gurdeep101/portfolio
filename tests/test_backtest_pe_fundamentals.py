from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backtest.backtest as bt


def test_select_fundamentals_for_week_exact_match() -> None:
    exact = {"AAA": {"pe_ratio": 12.0, "sector": "Tech"}}
    weekly = {
        "2026-01": exact,
        "2026-03": {"AAA": {"pe_ratio": 18.0, "sector": "Tech"}},
    }

    assert bt.select_fundamentals_for_week(weekly, date(2026, 1, 2)) is exact


def test_select_fundamentals_for_week_missing_uses_latest_prior() -> None:
    prior = {"AAA": {"pe_ratio": 12.0, "sector": "Tech"}}
    weekly = {
        "2026-01": prior,
        "2026-03": {"AAA": {"pe_ratio": 18.0, "sector": "Tech"}},
    }

    assert bt.select_fundamentals_for_week(weekly, date(2026, 1, 9)) is prior


def test_select_fundamentals_for_week_before_first_returns_none() -> None:
    weekly = {"2026-01": {"AAA": {"pe_ratio": 12.0, "sector": "Tech"}}}

    assert bt.select_fundamentals_for_week(weekly, date(2025, 12, 26)) is None


def test_rankings_ignore_stale_non_pe_fundamental_fields() -> None:
    dates = pd.bdate_range("2025-01-01", periods=270)
    daily = pd.DataFrame(
        {
            "AAA": np.linspace(100.0, 200.0, len(dates)),
            "BBB": np.linspace(100.0, 150.0, len(dates)),
        },
        index=dates,
    )
    fundamentals = {
        "AAA": {
            "pe_ratio": 10.0,
            "sector": "Tech",
            "fetch_date": "2026-01-02",
            "source": "nselib",
            "roe": -999.0,
            "pb_ratio": 999.0,
            "market_cap_cr": 1.0,
        },
        "BBB": {
            "pe_ratio": 20.0,
            "sector": "Tech",
            "fetch_date": "2026-01-02",
            "source": "nselib",
            "roe": 999.0,
            "pb_ratio": 0.01,
            "market_cap_cr": 999999.0,
        },
    }

    rankings, excluded = bt.compute_rankings(fundamentals, daily)  # type: ignore[arg-type]

    assert excluded == []
    assert not rankings.empty
    assert {"roe", "pb_ratio", "market_cap_cr"}.isdisjoint(rankings.columns)
    assert "value" in rankings.columns
    assert "earnings_yield" in rankings.columns


if __name__ == "__main__":
    tests = [
        test_select_fundamentals_for_week_exact_match,
        test_select_fundamentals_for_week_missing_uses_latest_prior,
        test_select_fundamentals_for_week_before_first_returns_none,
        test_rankings_ignore_stale_non_pe_fundamental_fields,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)}/{len(tests)} tests passed")
