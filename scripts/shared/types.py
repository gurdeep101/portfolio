"""Shared TypedDict definitions for all JSON schemas used in nifty_agent.

Import from this module instead of using dict[str, Any] throughout the scripts.
Every JSON structure that crosses script boundaries has a TypedDict here.
"""

from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# portfolio.json
# ---------------------------------------------------------------------------

class PortfolioHolding(TypedDict):
    """A single equity position held in the portfolio."""

    symbol: str
    shares: float
    avg_cost: float        # weighted average purchase price per share
    current_price: float
    current_value: float   # shares * current_price
    weight: float          # current_value / total_nav
    date_bought: str       # ISO date of first purchase (YYYY-MM-DD)


class NavHistoryEntry(TypedDict):
    """One row of the portfolio NAV history, appended each session."""

    date: str              # ISO date (YYYY-MM-DD)
    nav: float             # total portfolio value in INR
    benchmark_tri: float | None  # Nifty 250 TRI level on this date; None if unavailable


class TransactionLogEntry(TypedDict, total=False):
    """A single executed trade recorded in the transaction log.

    ``proceeds`` is present on SELL trades; ``spend`` is present on BUY trades.
    """

    date: str
    action: str    # "BUY" or "SELL"
    symbol: str
    shares: float
    price: float
    proceeds: float   # gross proceeds (SELL only)
    spend: float      # gross spend    (BUY  only)
    cost: float       # transaction cost (0.1% of gross value)
    net: float        # net cash movement after cost


class Portfolio(TypedDict):
    """Root structure of data/portfolio/portfolio.json."""

    inception_date: str | None   # ISO date of first session
    initial_capital: float       # INR 25,000
    current_cash: float          # uninvested cash
    holdings: list[PortfolioHolding]
    nav_history: list[NavHistoryEntry]
    total_nav: float             # current_cash + sum(holding.current_value)
    transaction_log: list[TransactionLogEntry]


# ---------------------------------------------------------------------------
# data/decisions/YYYY-MM-DD.json
# ---------------------------------------------------------------------------

class TradeDecision(TypedDict, total=False):
    """A single trade instruction written by the agent each session."""

    action: str          # "BUY" or "SELL"
    symbol: str
    target_weight: float # target fraction of NAV for BUY trades (e.g. 0.08 = 8%)
    quantity: str        # "ALL" for full SELL; omitted on BUY
    reason: str          # one-sentence rationale


class DecisionsFile(TypedDict):
    """Root structure of data/decisions/YYYY-MM-DD.json."""

    session_date: str              # ISO date (YYYY-MM-DD)
    trades: list[TradeDecision]
    notes: str                     # agent observations for this session


# ---------------------------------------------------------------------------
# return type of metrics/compute_metrics.compute_performance()
# ---------------------------------------------------------------------------

class PerformanceResult(TypedDict, total=False):
    """All performance metrics computed each session.

    Fields are optional (total=False) because many are unavailable on the
    first session or when the benchmark source is down.
    """

    nav: float
    weekly_return_pct: float | None
    inception_return_pct: float | None
    inception_return_inr: float | None  # absolute INR gain/loss since inception
    cagr_pct: float | None              # compound annual growth rate
    bm_level: float | None             # benchmark TRI or price index level
    bm_weekly_return_pct: float | None
    bm_inception_return_pct: float | None
    bm_cagr_pct: float | None
    active_weekly: float | None         # portfolio weekly − benchmark weekly
    active_inception: float | None
    active_cagr: float | None
    bm_source: str | None               # "TRI" | "price_index_nse" | "price_index_yfinance"
    weeks_since_inception: int          # number of sessions completed


# ---------------------------------------------------------------------------
# data/backtest/backtest_YYYYMMDD_Nmo.csv  (one row per weekly step)
# ---------------------------------------------------------------------------

class BacktestWeekResult(TypedDict, total=False):
    """One row of backtest output, written per simulated weekly rebalance."""

    week_date: str                           # ISO date of the weekly rebalance (Friday)
    portfolio_nav: float
    weekly_return_pct: float | None          # None for the first week (no prior NAV)
    cumulative_return_pct: float             # (nav / initial_capital - 1) * 100
    benchmark_level: float | None            # ^CNX250 close on or before week_date
    benchmark_weekly_return_pct: float | None
    benchmark_cumulative_return_pct: float | None
    active_return_weekly_pct: float | None   # portfolio weekly - benchmark weekly
    active_return_cumulative_pct: float | None
    num_holdings: int
    cash_pct: float                          # cash / nav * 100
    num_buys: int
    num_sells: int
    turnover_pct: float                      # (gross_buys + gross_sells) / nav_before * 100
    transaction_cost_inr: float              # total cost deducted this week


# ---------------------------------------------------------------------------
# data/backtest/backtest_YYYYMMDD_Nmo_trades.csv  (one row per trade)
# ---------------------------------------------------------------------------

class BacktestTradeEntry(TypedDict):
    """One executed trade recorded during the backtest simulation."""

    week_date: str          # ISO date of the signal week (Friday)
    action: str             # "BUY" | "SELL" | "TRIM"
    symbol: str
    shares: float
    execution_price: float  # next-day high (BUY) or next-day low (SELL / TRIM)
    gross_value: float      # shares * execution_price
    transaction_cost: float # gross_value * TRANSACTION_COST_PCT
    avg_cost_before: float  # cost basis per share before this trade
    realized_pnl: float     # (execution_price - avg_cost_before) * shares; 0 for buys


# ---------------------------------------------------------------------------
# data/backtest/backtest_YYYYMMDD_Nmo_tax.csv  (one row per calendar year)
# ---------------------------------------------------------------------------

class BacktestAnnualTax(TypedDict):
    """Tax liability for one calendar year, based on realized gains only.

    Losses are carried forward and offset future years' gains.
    Does not affect NAV or return calculations.
    """

    year: int
    gross_realized_gain_inr: float      # sum of realized P&L from all sells/trims this year
    prior_loss_carryforward_inr: float  # unrelieved loss accumulated from prior years
    net_taxable_gain_inr: float         # max(0, gross_realized_gain - prior_loss_carryforward)
    tax_rate_pct: float                 # flat rate (e.g. 30.0)
    tax_liability_inr: float            # net_taxable_gain * tax_rate_pct / 100
    loss_to_carryforward_inr: float     # unrelieved loss rolled into next year (0 if net gain)
