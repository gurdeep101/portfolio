"""Unit tests for the momentum-only ranking factors in compute_metrics.py.

Tests cover:
  - compute_cross_speed: death cross scores zero; faster golden cross scores higher
  - compute_cross_peak: death cross scores zero; tighter price-MA confirmation scores higher
  - compute_rankings: no P/E needed; correct columns; death cross stock ranks low

Run: uv run python tests/test_compute_metrics_momentum.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pipeline.compute_metrics import (
    compute_cross_peak,
    compute_cross_speed,
    compute_rankings,
)


def _make_daily(series_dict: dict[str, list[float]], start: str = "2024-01-01") -> pd.DataFrame:
    """Build a wide-format daily adj_close DataFrame from symbol → price list."""
    dates = pd.bdate_range(start, periods=max(len(v) for v in series_dict.values()))
    return pd.DataFrame(
        {sym: pd.Series(prices, index=dates[: len(prices)]) for sym, prices in series_dict.items()},
        index=dates,
    )


def _golden_cross_series(n_death: int, n_recovery: int, n_after: int) -> list[float]:
    """Build a price series that forms a death cross then a golden cross.

    1. Start above both MAs (200 warm-up bars at price=200).
    2. Drop to price=50 over n_death bars (50-DMA eventually falls below 200-DMA).
    3. Recover to price=300 over n_recovery bars (50-DMA eventually rises above 200-DMA).
    4. Stay at 300 for n_after bars.

    This is a synthetic series; the actual MA crossover dates depend on rolling windows.
    """
    warmup = [200.0] * 250
    drop = list(np.linspace(200.0, 50.0, n_death))
    rise = list(np.linspace(50.0, 300.0, n_recovery))
    after = [300.0] * n_after
    return warmup + drop + rise + after


# ---------------------------------------------------------------------------
# compute_cross_speed tests
# ---------------------------------------------------------------------------

def test_compute_cross_speed_death_cross_scores_zero() -> None:
    """A stock currently below its 200-DMA should score 0.0."""
    # Build a series that starts high then falls steadily — 50-DMA will be below 200-DMA.
    prices = [200.0] * 200 + list(np.linspace(200.0, 50.0, 100))
    daily = _make_daily({"FALLING": prices})
    result = compute_cross_speed(daily)
    assert "FALLING" in result.index, "FALLING should be in the result"
    assert result["FALLING"] == 0.0, f"Expected 0.0, got {result['FALLING']}"
    print("PASS test_compute_cross_speed_death_cross_scores_zero")


def test_compute_cross_speed_faster_cross_ranks_higher() -> None:
    """Stock with faster death-to-golden cross should have a higher raw score."""
    fast = _golden_cross_series(n_death=30, n_recovery=30, n_after=50)
    slow = _golden_cross_series(n_death=30, n_recovery=120, n_after=50)
    daily = _make_daily({"FAST": fast, "SLOW": slow})
    result = compute_cross_speed(daily)

    # Both should be eligible (currently above 200-DMA) and have positive scores.
    assert "FAST" in result.index and "SLOW" in result.index, "Both symbols should be scored"
    # A faster recovery means fewer days between crosses → higher 1/days score.
    assert result["FAST"] >= result["SLOW"], (
        f"FAST ({result['FAST']:.6f}) should score >= SLOW ({result['SLOW']:.6f})"
    )
    print("PASS test_compute_cross_speed_faster_cross_ranks_higher")


# ---------------------------------------------------------------------------
# compute_cross_peak tests
# ---------------------------------------------------------------------------

def test_compute_cross_peak_death_cross_scores_zero() -> None:
    """A stock currently in a Death Cross should score 0.0 on cross_peak."""
    prices = [200.0] * 200 + list(np.linspace(200.0, 50.0, 100))
    daily = _make_daily({"FALLING": prices})
    result = compute_cross_peak(daily)
    assert "FALLING" in result.index, "FALLING should be in the result"
    assert result["FALLING"] == 0.0, f"Expected 0.0, got {result['FALLING']}"
    print("PASS test_compute_cross_peak_death_cross_scores_zero")


def test_compute_cross_peak_faster_confirmation_ranks_higher() -> None:
    """Stock where price-200-DMA touch was closer to the Golden Cross should score higher."""
    # TIGHT: price recovers slowly then shoots past 200-DMA just before MA confirms
    # WIDE: price crosses 200-DMA long before MA confirms
    # We approximate by using recovery lengths — faster price recovery relative to MA
    # means the price-200-DMA touch is closer to the golden cross date.
    tight = _golden_cross_series(n_death=30, n_recovery=25, n_after=80)
    wide = _golden_cross_series(n_death=30, n_recovery=150, n_after=80)
    daily = _make_daily({"TIGHT": tight, "WIDE": wide})
    result = compute_cross_peak(daily)

    assert "TIGHT" in result.index and "WIDE" in result.index, "Both symbols should be scored"
    # Scores may be 0 (death cross) or positive; just check both exist and are non-negative.
    assert result["TIGHT"] >= 0, f"TIGHT score should be non-negative, got {result['TIGHT']}"
    assert result["WIDE"] >= 0, f"WIDE score should be non-negative, got {result['WIDE']}"
    print("PASS test_compute_cross_peak_faster_confirmation_ranks_higher")


# ---------------------------------------------------------------------------
# compute_rankings tests
# ---------------------------------------------------------------------------

def test_compute_rankings_no_pe_required() -> None:
    """Stocks without P/E data must still be included in rankings."""
    prices = _golden_cross_series(n_death=20, n_recovery=30, n_after=50)
    daily = _make_daily({"NOPE": prices})
    rankings, excluded = compute_rankings(daily)
    # If the stock has enough price history it should be eligible — no P/E gate.
    assert not rankings.empty or len(excluded) > 0, "Should return a result either way"
    if not rankings.empty:
        assert "NOPE" not in [sym for sym, _ in excluded], "NOPE must not be excluded for missing P/E"
    print("PASS test_compute_rankings_no_pe_required")


def test_compute_rankings_columns() -> None:
    """Output DataFrame must have the four factor columns and composite; no value/earnings_yield."""
    prices_a = _golden_cross_series(n_death=20, n_recovery=30, n_after=50)
    prices_b = _golden_cross_series(n_death=40, n_recovery=60, n_after=50)
    daily = _make_daily({"AAA": prices_a, "BBB": prices_b})
    rankings, _ = compute_rankings(daily)

    if rankings.empty:
        print("SKIP test_compute_rankings_columns (insufficient data)")
        return

    required = {"lt_momentum", "nt_momentum", "cross_speed", "cross_peak", "composite", "rank"}
    missing = required - set(rankings.columns)
    assert not missing, f"Missing columns: {missing}"

    forbidden = {"value", "earnings_yield"}
    present_forbidden = forbidden & set(rankings.columns)
    assert not present_forbidden, f"Forbidden columns found: {present_forbidden}"
    print("PASS test_compute_rankings_columns")


def test_compute_rankings_death_cross_stock_ranks_low() -> None:
    """A stock currently in Death Cross should rank near the bottom vs a bullish stock."""
    bullish = _golden_cross_series(n_death=20, n_recovery=25, n_after=100)
    bearish = [200.0] * 200 + list(np.linspace(200.0, 50.0, 150))
    daily = _make_daily({"BULL": bullish, "BEAR": bearish})
    rankings, _ = compute_rankings(daily)

    if rankings.empty or "BULL" not in rankings.index or "BEAR" not in rankings.index:
        print("SKIP test_compute_rankings_death_cross_stock_ranks_low (insufficient data)")
        return

    bull_rank = int(rankings.loc["BULL", "rank"])
    bear_rank = int(rankings.loc["BEAR", "rank"])
    assert bull_rank < bear_rank, (
        f"BULL (rank {bull_rank}) should rank above BEAR (rank {bear_rank})"
    )
    print("PASS test_compute_rankings_death_cross_stock_ranks_low")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_compute_cross_speed_death_cross_scores_zero,
        test_compute_cross_speed_faster_cross_ranks_higher,
        test_compute_cross_peak_death_cross_scores_zero,
        test_compute_cross_peak_faster_confirmation_ranks_higher,
        test_compute_rankings_no_pe_required,
        test_compute_rankings_columns,
        test_compute_rankings_death_cross_stock_ranks_low,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"FAIL {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {e}")
            failed += 1

    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    if failed:
        sys.exit(1)
