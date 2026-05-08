"""Apply rebalance decisions to data/portfolio/portfolio.json.

Usage:
  uv run python scripts/update_portfolio.py --decisions data/decisions/YYYY-MM-DD.json
  uv run python scripts/update_portfolio.py --decisions data/decisions/YYYY-MM-DD.json --dry-run
  uv run python scripts/update_portfolio.py --init   # initialise all-cash portfolio.json

Execution price: uses adj_close from the most recent weekly prices CSV.
Transaction cost: TRANSACTION_COST_PCT per trade side (deducted from notional cash).
Fractional shares are allowed (paper trading — no lot-size constraints).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# Make portfolio_types importable when running as `uv run python scripts/foo.py`
sys.path.insert(0, str(Path(__file__).parent))
from portfolio_types import DecisionsFile, Portfolio, PortfolioHolding, TransactionLogEntry

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "market" / "prices"
PORTFOLIO_FILE = DATA_DIR / "portfolio" / "portfolio.json"

INITIAL_CAPITAL: float = 25000.0       # INR notional capital
TRANSACTION_COST_PCT: float = 0.001    # 0.1% per trade side
MIN_BUY_VALUE: float = 500.0           # minimum INR trade size; smaller buys are skipped


def load_portfolio() -> Portfolio:
    """Load data/portfolio/portfolio.json.

    Exits with code 1 if the file is missing (prompt user to run --init)
    or if it cannot be parsed.
    """
    if not PORTFOLIO_FILE.exists():
        print("ERROR: portfolio.json not found. Run --init to create it.")
        sys.exit(1)
    try:
        with open(PORTFOLIO_FILE) as f:
            data: Portfolio = json.load(f)
        return data
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Could not read portfolio.json: {e}")
        sys.exit(1)


def save_portfolio(portfolio: Portfolio, dry_run: bool) -> None:
    """Write the portfolio to data/portfolio/portfolio.json, or print a preview in dry-run mode."""
    if dry_run:
        print("\n[DRY RUN] portfolio.json NOT written. Resulting state would be:")
        print(json.dumps(portfolio, indent=2))
        return
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump(portfolio, f, indent=2)
        print("\nportfolio.json updated.")
    except OSError as e:
        print(f"ERROR: Could not write portfolio.json: {e}")
        sys.exit(1)


def latest_prices() -> dict[str, float]:
    """Return a symbol → latest adj_close price map from the most recent weekly CSV.

    Returns an empty dict if no valid prices file is found (caller handles missing prices).
    """
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    for f in files:
        try:
            df = pd.read_csv(f)
            if df.empty:
                continue
            price_map: dict[str, float] = {}
            for _, row in df.iterrows():
                sym = str(row["symbol"])
                if sym not in price_map:
                    # Prefer adj_close; fall back to close if adj_close is NaN.
                    price = (
                        row["adj_close"]
                        if pd.notna(row.get("adj_close"))
                        else row["close"]
                    )
                    price_map[sym] = float(price)
            return price_map
        except (OSError, pd.errors.ParserError, KeyError) as e:
            print(f"  WARNING: Could not read prices file {f.name}: {e}")
            continue
    return {}


def compute_nav(portfolio: Portfolio, prices: dict[str, float]) -> float:
    """Compute total portfolio NAV using current market prices.

    Falls back to avg_cost for any holding not present in *prices*.
    """
    cash = portfolio.get("current_cash", 0.0)
    holdings_value = sum(
        h["shares"] * prices.get(h["symbol"], h.get("current_price", h["avg_cost"]))
        for h in portfolio.get("holdings", [])
    )
    return cash + holdings_value


def apply_sell(
    portfolio: Portfolio,
    symbol: str,
    prices: dict[str, float],
    today: str,
) -> float:
    """Execute a full sell of *symbol* and update the portfolio in-place.

    Args:
        portfolio: Portfolio dict to mutate.
        symbol:    NSE symbol to sell.
        prices:    Symbol → price map for execution prices.
        today:     ISO date string for the transaction log.

    Returns:
        Net proceeds (after transaction cost), or 0.0 if the symbol is not held.
    """
    holdings = portfolio["holdings"]
    idx = next((i for i, h in enumerate(holdings) if h["symbol"] == symbol), None)
    if idx is None:
        print(f"  SKIP SELL {symbol}: not in current holdings")
        return 0.0

    h = holdings[idx]
    price = prices.get(symbol, h.get("current_price", h["avg_cost"]))
    proceeds = h["shares"] * price
    cost = proceeds * TRANSACTION_COST_PCT
    net = proceeds - cost

    portfolio["holdings"].pop(idx)
    portfolio["current_cash"] += net
    portfolio["transaction_log"].append(
        TransactionLogEntry(
            date=today, action="SELL", symbol=symbol,
            shares=h["shares"], price=price,
            proceeds=round(proceeds, 4), cost=round(cost, 4), net=round(net, 4),
        )
    )
    print(
        f"  SELL {symbol}: {h['shares']:.4f} sh @ {price:.2f} "
        f"= INR {proceeds:.2f} (txn cost {cost:.2f})"
    )
    return net


def apply_buy(
    portfolio: Portfolio,
    symbol: str,
    target_weight: float,
    prices: dict[str, float],
    today: str,
    total_nav: float,
) -> float:
    """Execute a buy of *symbol* up to *target_weight* of *total_nav*.

    The buy value is capped at available cash. Skipped if the resulting trade
    is below MIN_BUY_VALUE.

    Args:
        portfolio:     Portfolio dict to mutate.
        symbol:        NSE symbol to buy.
        target_weight: Target fraction of total_nav (e.g. 0.08 for 8%).
        prices:        Symbol → price map for execution prices.
        today:         ISO date string for the transaction log.
        total_nav:     Total portfolio NAV used to compute target INR value.

    Returns:
        Net spend (including transaction cost), or 0.0 if the trade was skipped.
    """
    price = prices.get(symbol)
    if price is None or price <= 0:
        print(f"  SKIP BUY {symbol}: no price available")
        return 0.0

    target_value = total_nav * target_weight

    # Deduct the current holding's market value to avoid over-buying.
    current_holding = next(
        (h for h in portfolio["holdings"] if h["symbol"] == symbol), None
    )
    current_value = current_holding["shares"] * price if current_holding else 0.0
    buy_value = target_value - current_value

    if buy_value <= 0:
        print(f"  SKIP BUY {symbol}: already at or above target weight")
        return 0.0

    # Cap at available cash.
    available_cash = portfolio["current_cash"]
    if buy_value > available_cash:
        buy_value = available_cash
        print(f"  INFO: {symbol} buy capped at available cash INR {buy_value:.2f}")

    if buy_value < MIN_BUY_VALUE:
        print(
            f"  SKIP BUY {symbol}: trade value INR {buy_value:.2f} "
            f"< minimum INR {MIN_BUY_VALUE:.0f}"
        )
        return 0.0

    cost = buy_value * TRANSACTION_COST_PCT
    net_spend = buy_value + cost
    shares_bought = buy_value / price

    portfolio["current_cash"] -= net_spend

    if current_holding is not None:
        # Average-down / average-up the existing position.
        total_shares = current_holding["shares"] + shares_bought
        total_cost_basis = (
            current_holding["avg_cost"] * current_holding["shares"]
            + price * shares_bought
        )
        current_holding["shares"] = total_shares
        current_holding["avg_cost"] = total_cost_basis / total_shares
    else:
        portfolio["holdings"].append(
            PortfolioHolding(
                symbol=symbol,
                shares=shares_bought,
                avg_cost=price,
                current_price=price,
                current_value=shares_bought * price,
                weight=target_weight,
                date_bought=today,
            )
        )

    portfolio["transaction_log"].append(
        TransactionLogEntry(
            date=today, action="BUY", symbol=symbol,
            shares=round(shares_bought, 4), price=price,
            spend=round(buy_value, 4), cost=round(cost, 4), net=round(net_spend, 4),
        )
    )
    print(
        f"  BUY  {symbol}: {shares_bought:.4f} sh @ {price:.2f} "
        f"= INR {buy_value:.2f} (txn cost {cost:.2f})"
    )
    return net_spend


def refresh_holdings(portfolio: Portfolio, prices: dict[str, float]) -> None:
    """Update current_price, current_value, and weight on all holdings in-place.

    Also recalculates portfolio total_nav.
    """
    for h in portfolio["holdings"]:
        p = prices.get(h["symbol"], h.get("current_price", h["avg_cost"]))
        h["current_price"] = p
        h["current_value"] = h["shares"] * p

    total_nav = portfolio["current_cash"] + sum(
        h["current_value"] for h in portfolio["holdings"]
    )
    for h in portfolio["holdings"]:
        h["weight"] = h["current_value"] / total_nav if total_nav > 0 else 0.0
    portfolio["total_nav"] = round(total_nav, 4)


def main() -> None:
    """Parse arguments and either initialise portfolio.json or apply trade decisions."""
    parser = argparse.ArgumentParser(
        description="Apply rebalance decisions to data/portfolio/portfolio.json"
    )
    parser.add_argument("--decisions", help="Path to decisions JSON file")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and print trades without writing portfolio.json"
    )
    parser.add_argument(
        "--init", action="store_true",
        help="Initialise a fresh all-cash portfolio.json (first run only)"
    )
    args = parser.parse_args()

    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # Initialisation mode
    # -------------------------------------------------------------------------
    if args.init:
        if PORTFOLIO_FILE.exists() and not args.dry_run:
            print(
                "ERROR: portfolio.json already exists. "
                "Delete it manually before re-initialising."
            )
            return
        today = date.today().isoformat()
        portfolio = Portfolio(
            inception_date=today,
            initial_capital=INITIAL_CAPITAL,
            current_cash=INITIAL_CAPITAL,
            holdings=[],
            nav_history=[{"date": today, "nav": INITIAL_CAPITAL, "benchmark_tri": None}],
            total_nav=INITIAL_CAPITAL,
            transaction_log=[],
        )
        if not args.dry_run:
            try:
                with open(PORTFOLIO_FILE, "w") as f:
                    json.dump(portfolio, f, indent=2)
                print(
                    f"OK: portfolio.json initialised with "
                    f"INR {INITIAL_CAPITAL:,.0f} all-cash on {today}"
                )
            except OSError as e:
                print(f"ERROR: Could not write portfolio.json: {e}")
                sys.exit(1)
        else:
            print("[DRY RUN] Would initialise portfolio.json:")
            print(json.dumps(portfolio, indent=2))
        return

    if not args.decisions:
        parser.error("--decisions PATH is required (or use --init for first run)")

    # -------------------------------------------------------------------------
    # Rebalance mode
    # -------------------------------------------------------------------------
    decisions_file = Path(args.decisions)
    if not decisions_file.exists():
        print(f"ERROR: Decisions file not found: {decisions_file}")
        sys.exit(1)

    try:
        with open(decisions_file) as f:
            decisions: DecisionsFile = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: Could not read decisions file {decisions_file}: {e}")
        sys.exit(1)

    portfolio = load_portfolio()
    prices = latest_prices()
    today = decisions.get("session_date", date.today().isoformat())

    nav_before = compute_nav(portfolio, prices)
    print(f"NAV before rebalance: INR {nav_before:,.2f}")
    print(f"Cash before:          INR {portfolio['current_cash']:,.2f}")
    print(f"Holdings before:      {len(portfolio['holdings'])} stocks")
    print()
    print("Executing trades:")

    # Execute sells first to free up cash for buys.
    sells = [t for t in decisions.get("trades", []) if t.get("action") == "SELL"]
    buys = [t for t in decisions.get("trades", []) if t.get("action") == "BUY"]

    for trade in sells:
        apply_sell(portfolio, trade["symbol"], prices, today)

    # Use post-sell NAV as the basis for computing buy sizes.
    nav_after_sells = compute_nav(portfolio, prices)

    # Execute largest target weights first to prioritise high-conviction buys
    # if cash runs out.
    buys_sorted = sorted(buys, key=lambda t: t.get("target_weight", 0.0), reverse=True)
    for trade in buys_sorted:
        apply_buy(
            portfolio, trade["symbol"],
            float(trade.get("target_weight", 0.05)),
            prices, today, nav_after_sells,
        )

    refresh_holdings(portfolio, prices)
    nav_after = portfolio["total_nav"]
    portfolio["nav_history"].append(
        {"date": today, "nav": nav_after, "benchmark_tri": None}
    )

    print()
    print(f"NAV after rebalance:  INR {nav_after:,.2f}")
    print(f"Cash after:           INR {portfolio['current_cash']:,.2f}")
    print(f"Holdings after:       {len(portfolio['holdings'])} stocks")

    notes = decisions.get("notes", "")
    if notes:
        print(f"Notes: {notes}")

    save_portfolio(portfolio, args.dry_run)


if __name__ == "__main__":
    main()
