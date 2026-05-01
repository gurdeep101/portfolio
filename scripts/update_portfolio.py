"""Apply rebalance decisions to data/portfolio.json.

Usage:
  python scripts/update_portfolio.py --decisions decisions/YYYY-MM-DD.json
  python scripts/update_portfolio.py --decisions decisions/YYYY-MM-DD.json --dry-run
  python scripts/update_portfolio.py --init   # initialise all-cash portfolio.json

Execution price: uses adj_close from the latest weekly prices CSV.
Transaction cost: 0.1% per trade side.
Fractional shares are allowed (paper trading).
"""

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "prices"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"

INITIAL_CAPITAL = 25000.0
TRANSACTION_COST_PCT = 0.001


def load_portfolio() -> dict:
    with open(PORTFOLIO_FILE) as f:
        return json.load(f)


def save_portfolio(portfolio: dict, dry_run: bool):
    if dry_run:
        print("\n[DRY RUN] portfolio.json NOT written. Would have been:")
        print(json.dumps(portfolio, indent=2))
        return
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)
    print(f"\nportfolio.json updated.")


def latest_prices() -> dict:
    files = sorted(PRICES_DIR.glob("????-??.csv"), reverse=True)
    for f in files:
        try:
            df = pd.read_csv(f)
            if df.empty:
                continue
            price_map = {}
            for _, row in df.iterrows():
                sym = row["symbol"]
                if sym not in price_map:
                    price = row["adj_close"] if pd.notna(row.get("adj_close")) else row["close"]
                    price_map[sym] = float(price)
            return price_map
        except Exception:
            continue
    return {}


def compute_nav(portfolio: dict, prices: dict) -> float:
    cash = portfolio.get("current_cash", 0)
    holdings_value = sum(
        h["shares"] * prices.get(h["symbol"], h.get("current_price", h["avg_cost"]))
        for h in portfolio.get("holdings", [])
    )
    return cash + holdings_value


def apply_sell(portfolio: dict, symbol: str, prices: dict, today: str) -> float:
    holdings = portfolio["holdings"]
    idx = next((i for i, h in enumerate(holdings) if h["symbol"] == symbol), None)
    if idx is None:
        print(f"  SKIP SELL {symbol}: not in holdings")
        return 0.0

    h = holdings[idx]
    price = prices.get(symbol, h.get("current_price", h["avg_cost"]))
    proceeds = h["shares"] * price
    cost = proceeds * TRANSACTION_COST_PCT
    net = proceeds - cost

    portfolio["holdings"].pop(idx)
    portfolio["current_cash"] += net
    portfolio["transaction_log"].append({
        "date": today, "action": "SELL", "symbol": symbol,
        "shares": h["shares"], "price": price, "proceeds": round(proceeds, 4),
        "cost": round(cost, 4), "net": round(net, 4),
    })
    print(f"  SELL {symbol}: {h['shares']:.4f} shares @ {price:.2f} = INR {proceeds:.2f} (cost {cost:.2f})")
    return net


def apply_buy(portfolio: dict, symbol: str, target_weight: float, prices: dict, today: str, total_nav: float) -> float:
    price = prices.get(symbol)
    if price is None or price <= 0:
        print(f"  SKIP BUY {symbol}: no price available")
        return 0.0

    target_value = total_nav * target_weight
    current_holding = next((h for h in portfolio["holdings"] if h["symbol"] == symbol), None)
    current_value = current_holding["shares"] * price if current_holding else 0.0
    buy_value = target_value - current_value

    if buy_value <= 0:
        print(f"  SKIP BUY {symbol}: already at or above target weight")
        return 0.0

    available_cash = portfolio["current_cash"]
    if buy_value > available_cash:
        buy_value = available_cash
        print(f"  WARNING: {symbol} buy capped at available cash INR {buy_value:.2f}")

    if buy_value < 500:
        print(f"  SKIP BUY {symbol}: buy value INR {buy_value:.2f} below minimum INR 500")
        return 0.0

    cost = buy_value * TRANSACTION_COST_PCT
    net_spend = buy_value + cost
    shares_bought = buy_value / price

    portfolio["current_cash"] -= net_spend

    if current_holding:
        total_shares = current_holding["shares"] + shares_bought
        total_cost = current_holding["avg_cost"] * current_holding["shares"] + price * shares_bought
        current_holding["shares"] = total_shares
        current_holding["avg_cost"] = total_cost / total_shares
    else:
        portfolio["holdings"].append({
            "symbol": symbol,
            "shares": shares_bought,
            "avg_cost": price,
            "current_price": price,
            "current_value": shares_bought * price,
            "weight": target_weight,
            "date_bought": today,
        })

    portfolio["transaction_log"].append({
        "date": today, "action": "BUY", "symbol": symbol,
        "shares": round(shares_bought, 4), "price": price,
        "spend": round(buy_value, 4), "cost": round(cost, 4), "net": round(net_spend, 4),
    })
    print(f"  BUY  {symbol}: {shares_bought:.4f} shares @ {price:.2f} = INR {buy_value:.2f} (cost {cost:.2f})")
    return net_spend


def refresh_holdings(portfolio: dict, prices: dict):
    for h in portfolio["holdings"]:
        p = prices.get(h["symbol"], h.get("current_price", h["avg_cost"]))
        h["current_price"] = p
        h["current_value"] = h["shares"] * p

    total_nav = portfolio["current_cash"] + sum(h["current_value"] for h in portfolio["holdings"])
    for h in portfolio["holdings"]:
        h["weight"] = h["current_value"] / total_nav if total_nav > 0 else 0
    portfolio["total_nav"] = round(total_nav, 4)


def main():
    parser = argparse.ArgumentParser(description="Apply rebalance decisions to portfolio.json")
    parser.add_argument("--decisions", help="Path to decisions JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing portfolio.json")
    parser.add_argument("--init", action="store_true", help="Initialise a fresh all-cash portfolio.json")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.init:
        if PORTFOLIO_FILE.exists() and not args.dry_run:
            print("ERROR: portfolio.json already exists. Delete it manually to reinitialise.")
            return
        today = date.today().isoformat()
        portfolio = {
            "inception_date": today,
            "initial_capital": INITIAL_CAPITAL,
            "current_cash": INITIAL_CAPITAL,
            "holdings": [],
            "nav_history": [{"date": today, "nav": INITIAL_CAPITAL, "benchmark_tri": None}],
            "total_nav": INITIAL_CAPITAL,
            "transaction_log": [],
        }
        if not args.dry_run:
            with open(PORTFOLIO_FILE, "w") as f:
                json.dump(portfolio, f, indent=2)
            print(f"OK: portfolio.json initialised with INR {INITIAL_CAPITAL:,.0f} all-cash on {today}")
        else:
            print("[DRY RUN] Would initialise portfolio.json:")
            print(json.dumps(portfolio, indent=2))
        return

    if not args.decisions:
        parser.error("--decisions is required (or use --init)")

    decisions_file = Path(args.decisions)
    if not decisions_file.exists():
        print(f"ERROR: Decisions file not found: {decisions_file}")
        return

    with open(decisions_file) as f:
        decisions = json.load(f)

    portfolio = load_portfolio()
    prices = latest_prices()
    today = decisions.get("session_date", date.today().isoformat())

    nav_before = compute_nav(portfolio, prices)
    print(f"NAV before rebalance: INR {nav_before:,.2f}")
    print(f"Cash before: INR {portfolio['current_cash']:,.2f}")
    print(f"Holdings before: {len(portfolio['holdings'])} stocks")
    print()
    print("Executing trades:")

    sells = [t for t in decisions.get("trades", []) if t["action"] == "SELL"]
    buys = [t for t in decisions.get("trades", []) if t["action"] == "BUY"]

    for trade in sells:
        apply_sell(portfolio, trade["symbol"], prices, today)

    nav_after_sells = compute_nav(portfolio, prices)

    buys_sorted = sorted(buys, key=lambda t: t.get("target_weight", 0), reverse=True)
    for trade in buys_sorted:
        apply_buy(portfolio, trade["symbol"], trade.get("target_weight", 0.05), prices, today, nav_after_sells)

    refresh_holdings(portfolio, prices)
    nav_after = portfolio["total_nav"]

    portfolio["nav_history"].append({"date": today, "nav": nav_after, "benchmark_tri": None})

    print()
    print(f"NAV after rebalance:  INR {nav_after:,.2f}")
    print(f"Cash after:  INR {portfolio['current_cash']:,.2f}")
    print(f"Holdings after: {len(portfolio['holdings'])} stocks")

    notes = decisions.get("notes", "")
    if notes:
        print(f"Notes: {notes}")

    save_portfolio(portfolio, args.dry_run)


if __name__ == "__main__":
    main()
