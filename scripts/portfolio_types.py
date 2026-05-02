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
    """Root structure of data/portfolio.json."""

    inception_date: str | None   # ISO date of first session
    initial_capital: float       # INR 25,000
    current_cash: float          # uninvested cash
    holdings: list[PortfolioHolding]
    nav_history: list[NavHistoryEntry]
    total_nav: float             # current_cash + sum(holding.current_value)
    transaction_log: list[TransactionLogEntry]


# ---------------------------------------------------------------------------
# decisions/YYYY-MM-DD.json
# ---------------------------------------------------------------------------

class TradeDecision(TypedDict, total=False):
    """A single trade instruction written by the agent each session."""

    action: str          # "BUY" or "SELL"
    symbol: str
    target_weight: float # target fraction of NAV for BUY trades (e.g. 0.08 = 8%)
    quantity: str        # "ALL" for full SELL; omitted on BUY
    reason: str          # one-sentence rationale


class DecisionsFile(TypedDict):
    """Root structure of decisions/YYYY-MM-DD.json."""

    session_date: str              # ISO date (YYYY-MM-DD)
    trades: list[TradeDecision]
    notes: str                     # agent observations for this session


# ---------------------------------------------------------------------------
# data/fundamentals/YYYY-WW.json  (per-symbol entries)
# ---------------------------------------------------------------------------

class FundamentalsEntry(TypedDict, total=False):
    """Fundamental data for one symbol, sourced from yfinance .info.

    All financial fields are optional because yfinance returns None for
    many Indian stocks (~20–30% null rate is expected).
    """

    pe_ratio: float | None
    pb_ratio: float | None
    roe: float | None           # return on equity as a decimal (e.g. 0.15 = 15%)
    market_cap_cr: float | None # market cap in INR crore
    sector: str
    fetch_date: str             # ISO date when this entry was last fetched
    source: str                 # "yfinance"
    error: str                  # set to "no_data" if yfinance returned nothing


# ---------------------------------------------------------------------------
# return type of compute_metrics.compute_performance()
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
