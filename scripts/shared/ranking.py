"""Stock ranking engine — factor computation and composite scoring.

This module is strategy-neutral infrastructure: it exposes the individual
factor functions and the composite ranking function used by both the live
metrics pipeline and the backtesting engine. It has no dependencies on
other project modules (only pandas and stdlib).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

MA_SHORT = 50           # short moving average window (days) for near-term momentum
MA_LONG = 200           # long moving average window (days) for near-term momentum
MOMENTUM_LOOKBACK_WEEKS = 52   # total lookback for long-term momentum
MOMENTUM_SKIP_WEEKS = 4        # skip the most recent N weeks (12-1 month calculation)
MIN_HISTORY_DAYS = 200         # minimum daily price history required for MA calculation

# Factor weights must sum to 1.0.
WEIGHTS: dict[str, float] = {
    "lt_momentum": 0.15,   # long-term momentum (12-1 month return)
    "nt_momentum": 0.30,   # near-term momentum (50-DMA vs 200-DMA ratio)
    "cross_speed": 0.30,   # golden cross speed (1 / days from death cross to golden cross)
    "cross_peak": 0.25,    # golden cross peak (1 / days from price-200DMA touch to golden cross)
}


def normalise_series(s: pd.Series[Any]) -> pd.Series[Any]:
    """Min-max normalise a pandas Series to [0, 1].

    Returns a constant 0.5 series when min == max (avoids division by zero).
    """
    mn, mx = s.min(), s.max()
    if mx == mn:
        return pd.Series(0.5, index=s.index)
    result: pd.Series[Any] = (s - mn) / (mx - mn)
    return result


def compute_lt_momentum(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute 12-1 month long-term momentum for each symbol.

    Formula: (52-week return) − (4-week return) — removes the short-term
    reversal component to isolate true trend momentum.

    Returns an empty Series if *daily* is None or has insufficient history.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    approx_days_per_week = 5  # trading days

    for sym in daily.columns:
        col = daily[sym].dropna()
        n = len(col)
        lookback_idx = MOMENTUM_LOOKBACK_WEEKS * approx_days_per_week
        skip_idx = MOMENTUM_SKIP_WEEKS * approx_days_per_week

        if n <= lookback_idx or n <= skip_idx:
            continue

        price_now = col.iloc[-1]
        price_4w_ago = col.iloc[-skip_idx]
        price_52w_ago = col.iloc[-lookback_idx]

        if price_52w_ago > 0 and price_4w_ago > 0:
            lt_ret = (price_now / price_52w_ago) - 1
            st_ret = (price_now / price_4w_ago) - 1
            result[sym] = lt_ret - st_ret

    return pd.Series(result)


def compute_nt_momentum(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute near-term momentum as (50-DMA − 200-DMA) / 200-DMA for each symbol.

    Positive values indicate the short MA is above the long MA (bullish trend);
    negative values indicate a bearish trend.

    Excludes symbols with fewer than MIN_HISTORY_DAYS of price history.

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean().iloc[-1]
        ma200 = col.rolling(MA_LONG).mean().iloc[-1]
        if pd.notna(ma50) and pd.notna(ma200) and ma200 > 0:
            result[sym] = (ma50 - ma200) / ma200

    return pd.Series(result)


_SENTINEL = float("inf")  # marks "no prior cross found" — replaced with 75th pct after normalise


def compute_cross_speed(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute Golden Cross speed for each symbol.

    Score = 1 / calendar_days_between(last Death Cross, most recent Golden Cross).
    Fewer days = faster moving-average cycle = higher score.

    Special cases:
      - Currently in Death Cross (50-DMA < 200-DMA): raw = 0.0
      - Golden Cross found but no prior Death Cross in history: raw = _SENTINEL
        (replaced with the 75th percentile of the universe after normalisation)

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean()
        ma200 = col.rolling(MA_LONG).mean()
        valid = ma50.notna() & ma200.notna()
        if not valid.any():
            continue

        above = (ma50 > ma200).astype(int)
        above = above[valid]

        if above.iloc[-1] == 0:
            result[sym] = 0.0
            continue

        transitions = above.diff()
        golden_crosses = transitions.index[transitions == 1]
        if len(golden_crosses) == 0:
            result[sym] = _SENTINEL
            continue

        last_golden = golden_crosses[-1]
        death_crosses_before = transitions.index[
            (transitions == -1) & (transitions.index < last_golden)
        ]
        if len(death_crosses_before) == 0:
            result[sym] = _SENTINEL
            continue

        last_death = death_crosses_before[-1]
        days = (last_golden - last_death).days
        result[sym] = 1.0 / days if days > 0 else 1.0

    return pd.Series(result)


def compute_cross_peak(daily: pd.DataFrame | None) -> pd.Series[Any]:
    """Compute Golden Cross peak for each symbol.

    Score = 1 / calendar_days_between(last day price <= 200-DMA, most recent Golden Cross).
    Fewer days = price breakout and MA confirmation were tightly coupled = higher score.

    Special cases:
      - Currently in Death Cross: raw = 0.0
      - Golden Cross found but price never touched 200-DMA before it: raw = _SENTINEL
        (replaced with the 75th percentile of the universe after normalisation)

    Returns an empty Series if *daily* is None.
    """
    if daily is None or daily.empty:
        return pd.Series(dtype=float)

    result: dict[str, float] = {}
    for sym in daily.columns:
        col = daily[sym].dropna()
        if len(col) < MIN_HISTORY_DAYS:
            continue
        ma50 = col.rolling(MA_SHORT).mean()
        ma200 = col.rolling(MA_LONG).mean()
        valid = ma50.notna() & ma200.notna()
        if not valid.any():
            continue

        above = (ma50 > ma200).astype(int)
        above = above[valid]
        col_valid = col[valid]
        ma200_valid = ma200[valid]

        if above.iloc[-1] == 0:
            result[sym] = 0.0
            continue

        transitions = above.diff()
        golden_crosses = transitions.index[transitions == 1]
        if len(golden_crosses) == 0:
            result[sym] = _SENTINEL
            continue

        last_golden = golden_crosses[-1]

        # Find the most recent day BEFORE the Golden Cross when price <= 200-DMA.
        before_cross = col_valid.index < last_golden
        price_before = col_valid[before_cross]
        ma200_before = ma200_valid[before_cross]
        touched = price_before.index[price_before <= ma200_before]

        if len(touched) == 0:
            result[sym] = _SENTINEL
            continue

        last_touch = touched[-1]
        days = (last_golden - last_touch).days
        result[sym] = 1.0 / days if days > 0 else 1.0

    return pd.Series(result)


def _apply_sentinel_substitution(
    raw: pd.Series[Any], normalised: pd.Series[Any]
) -> pd.Series[Any]:
    """Replace _SENTINEL entries in *normalised* with the 75th percentile of non-sentinel values."""
    sentinel_mask = raw == _SENTINEL
    if not sentinel_mask.any():
        return normalised
    non_sentinel_norm = normalised[~sentinel_mask]
    p75 = float(non_sentinel_norm.quantile(0.75)) if not non_sentinel_norm.empty else 0.75
    result = normalised.copy()
    result[sentinel_mask] = p75
    return result


def compute_rankings(
    daily: pd.DataFrame | None,
) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Compute composite factor rankings for all eligible symbols.

    Symbols are excluded from ranking (and cannot be bought) if there is
    insufficient price history for MA calculation (< MIN_HISTORY_DAYS days).

    Args:
        daily: Wide-format daily adj_close DataFrame (columns=symbols).

    Returns:
        A tuple of:
          - ranked DataFrame (index=symbol) with factor scores, composite score,
            rank, and MA signal columns
          - list of (symbol, reason) tuples for excluded symbols
    """
    lt_mom = compute_lt_momentum(daily)
    nt_mom = compute_nt_momentum(daily)
    cross_speed = compute_cross_speed(daily)
    cross_peak = compute_cross_peak(daily)

    eligible = lt_mom.index.intersection(nt_mom.index).intersection(
        cross_speed.index
    ).intersection(cross_peak.index)

    excluded: list[tuple[str, str]] = []
    if daily is not None:
        for sym in daily.columns:
            if sym not in eligible:
                excluded.append((sym, "insufficient_price_history"))

    if len(eligible) == 0:
        return pd.DataFrame(), excluded

    df = pd.DataFrame({
        "lt_momentum_raw": lt_mom[eligible],
        "nt_momentum_raw": nt_mom[eligible],
        "cross_speed_raw": cross_speed[eligible],
        "cross_peak_raw": cross_peak[eligible],
    })
    df.index.name = "symbol"

    df["lt_momentum"] = normalise_series(df["lt_momentum_raw"])
    df["nt_momentum"] = normalise_series(df["nt_momentum_raw"])

    def _normalise_with_sentinel(raw_col: pd.Series[Any]) -> pd.Series[Any]:
        """Normalise a column that may contain _SENTINEL values.

        Sentinel entries are temporarily excluded from normalisation, then
        replaced with the 75th percentile of the normalised non-sentinel values.
        """
        sentinel_mask = raw_col == _SENTINEL
        non_sentinel = raw_col[~sentinel_mask]
        norm = normalise_series(non_sentinel)
        # Extend to full index with 0.0 for sentinels, then apply substitution.
        full_norm = norm.reindex(raw_col.index, fill_value=0.0)
        return _apply_sentinel_substitution(raw_col, full_norm)

    df["cross_speed"] = _normalise_with_sentinel(df["cross_speed_raw"])
    df["cross_peak"] = _normalise_with_sentinel(df["cross_peak_raw"])

    df["composite"] = (
        df["lt_momentum"] * WEIGHTS["lt_momentum"]
        + df["nt_momentum"] * WEIGHTS["nt_momentum"]
        + df["cross_speed"] * WEIGHTS["cross_speed"]
        + df["cross_peak"] * WEIGHTS["cross_peak"]
    )
    df = df.sort_values("composite", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    df["ma_signal"] = df["nt_momentum_raw"].apply(
        lambda x: "ABOVE" if x > 0 else "BELOW"
    )

    return df, excluded
