"""Diagnostic: fetch prices for first 100 universe symbols and identify failures.

Tests each symbol individually (not in batches) so we can see exactly which ones fail
with yfinance and test alternate sources for the failures.
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

DATA_DIR = Path(__file__).parent / "data"
UNIVERSE_FILE = DATA_DIR / "universe.csv"

# Short window: last 10 trading days (2 weeks)
END   = date.today()
START = END - timedelta(days=14)
LIMIT = 100   # only test first N symbols

SLEEP_BETWEEN = 0.5  # seconds between individual symbol fetches


# ── helpers ─────────────────────────────────────────────────────────────────

def symbols_to_test() -> list[str]:
    df = pd.read_csv(UNIVERSE_FILE)
    return df["symbol"].tolist()[:LIMIT]


# ── Source 1: yfinance ───────────────────────────────────────────────────────

def yf_fetch_single(symbol: str) -> pd.DataFrame:
    """Fetch a single symbol via yfinance."""
    try:
        raw = yf.download(
            symbol + ".NS",
            start=START.isoformat(),
            end=(END + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty or raw["Close"].isna().all():
            return pd.DataFrame()
        raw = raw.reset_index()
        raw.columns = [str(c).lower() for c in raw.columns]
        raw["symbol"] = symbol
        raw = raw.rename(columns={"adj close": "adj_close"})
        return raw[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].dropna(subset=["close"])
    except Exception as e:
        return pd.DataFrame()


# ── Source 2: NSE via Playwright session ────────────────────────────────────

NSE_COOKIES: dict[str, str] = {}

def _warm_nse_session() -> None:
    """Open NSE market page via Playwright to get real Akamai cookies."""
    global NSE_COOKIES
    if NSE_COOKIES:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = ctx.new_page()
            page.goto(
                "https://www.nseindia.com/market-data/live-equity-market",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            page.wait_for_timeout(3000)
            NSE_COOKIES = {c["name"]: c["value"] for c in ctx.cookies()}
            browser.close()
            print(f"  NSE session warmed ({len(NSE_COOKIES)} cookies)")
    except Exception as e:
        print(f"  WARNING: Could not warm NSE session: {e}")


def nse_fetch_single(symbol: str) -> pd.DataFrame:
    """Fetch historical OHLCV from NSE equityHistoricalData API."""
    _warm_nse_session()
    series = "EQ"
    fmt = "%d-%m-%Y"
    from_dt = START.strftime(fmt)
    to_dt   = END.strftime(fmt)
    url = (
        "https://www.nseindia.com/api/historical/cm/equity"
        f"?symbol={symbol}&series=[%22{series}%22]"
        f"&from={from_dt}&to={to_dt}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, cookies=NSE_COOKIES, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return pd.DataFrame()
        rows = []
        for d in data:
            rows.append({
                "symbol":    symbol,
                "date":      pd.to_datetime(d.get("CH_TIMESTAMP") or d.get("mTIMESTAMP", ""), dayfirst=True),
                "open":      float(d.get("CH_OPENING_PRICE", 0) or 0),
                "high":      float(d.get("CH_TRADE_HIGH_PRICE", 0) or 0),
                "low":       float(d.get("CH_TRADE_LOW_PRICE", 0) or 0),
                "close":     float(d.get("CH_CLOSING_PRICE", 0) or 0),
                "adj_close": float(d.get("CH_CLOSING_PRICE", 0) or 0),
                "volume":    int(d.get("CH_TOT_TRADED_QTY", 0) or 0),
            })
        df = pd.DataFrame(rows).dropna(subset=["close"])
        return df[df["close"] > 0]
    except Exception:
        return pd.DataFrame()


# ── Source 3: BSE via requests ──────────────────────────────────────────────

BSE_ISIN_MAP: dict[str, str] = {}

def _load_isin_map() -> None:
    global BSE_ISIN_MAP
    if BSE_ISIN_MAP:
        return
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        BSE_ISIN_MAP = dict(zip(df["symbol"], df["isin_code"]))
    except Exception:
        pass


def bse_fetch_single(symbol: str) -> pd.DataFrame:
    """Fetch historical data from BSE bhav-copy style API."""
    _load_isin_map()
    isin = BSE_ISIN_MAP.get(symbol, "")
    if not isin:
        return pd.DataFrame()
    url = f"https://api.bseindia.com/BseIndiaAPI/api/StockReachGraph/w?scripcode={isin}&flag=1&fromdate={START.strftime('%Y%m%d')}&todate={END.strftime('%Y%m%d')}&seriesid="
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer":    "https://www.bseindia.com",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append({
                    "symbol":    symbol,
                    "date":      pd.to_datetime(item.get("dDt", ""), dayfirst=True),
                    "open":      float(item.get("OPEN", 0) or 0),
                    "high":      float(item.get("HIGH", 0) or 0),
                    "low":       float(item.get("LOW", 0) or 0),
                    "close":     float(item.get("CLOSE", 0) or 0),
                    "adj_close": float(item.get("CLOSE", 0) or 0),
                    "volume":    int(item.get("QTY", 0) or 0),
                })
        df = pd.DataFrame(rows).dropna(subset=["close"])
        return df[df["close"] > 0]
    except Exception:
        return pd.DataFrame()


# ── main diagnostic ──────────────────────────────────────────────────────────

def main() -> None:
    symbols = symbols_to_test()
    print(f"\n{'='*65}")
    print(f" PRICE FETCH DIAGNOSTIC — first {LIMIT} symbols")
    print(f" Window: {START} → {END}")
    print(f"{'='*65}\n")

    results: list[dict] = []

    print(f"{'SYM':12}  {'yfinance':10}  {'NSE API':10}  {'BSE API':10}  rows_yf")
    print("-"*60)

    nse_warmed = False

    for sym in symbols:
        # --- yfinance ---
        df_yf = yf_fetch_single(sym)
        yf_ok  = not df_yf.empty
        yf_rows = len(df_yf)

        # --- NSE API (only try if yfinance failed, to be efficient) ---
        nse_ok = False
        if not yf_ok:
            if not nse_warmed:
                print("\n  Warming NSE session for fallback test…")
                _warm_nse_session()
                nse_warmed = True
            df_nse = nse_fetch_single(sym)
            nse_ok  = not df_nse.empty

        # --- BSE API (only try if both yf and NSE failed) ---
        bse_ok = False
        if not yf_ok and not nse_ok:
            df_bse = bse_fetch_single(sym)
            bse_ok = not df_bse.empty

        status_yf  = "✅" if yf_ok  else "❌"
        status_nse = "✅" if nse_ok else ("❌" if not yf_ok else "skip")
        status_bse = "✅" if bse_ok else ("❌" if (not yf_ok and not nse_ok) else "skip")

        results.append({
            "symbol":   sym,
            "yf_ok":    yf_ok,
            "yf_rows":  yf_rows,
            "nse_ok":   nse_ok,
            "bse_ok":   bse_ok,
            "any_ok":   yf_ok or nse_ok or bse_ok,
        })

        print(f"{sym:12}  {status_yf:10}  {status_nse:10}  {status_bse:10}  {yf_rows}")
        time.sleep(SLEEP_BETWEEN)

    # ── summary ──
    yf_success   = sum(r["yf_ok"] for r in results)
    nse_success  = sum(r["nse_ok"] for r in results)
    bse_success  = sum(r["bse_ok"] for r in results)
    total_any    = sum(r["any_ok"] for r in results)
    total        = len(results)
    yf_fail_syms = [r["symbol"] for r in results if not r["yf_ok"]]

    print(f"\n{'='*65}")
    print(" SUMMARY")
    print(f"{'='*65}")
    print(f"  Total symbols tested    : {total}")
    print(f"  yfinance ✅              : {yf_success}/{total} ({yf_success/total:.1%})")
    print(f"  yfinance ❌ (failures)   : {len(yf_fail_syms)}")
    print(f"  NSE API recovered       : {nse_success}")
    print(f"  BSE API recovered       : {bse_success}")
    print(f"  Any source succeeded    : {total_any}/{total} ({total_any/total:.1%})")
    print(f"  Still missing           : {total - total_any}")
    print()

    if yf_fail_syms:
        print(f"  yfinance FAILURES ({len(yf_fail_syms)}):")
        for s in yf_fail_syms:
            r = next(x for x in results if x["symbol"] == s)
            recovered = "NSE✅" if r["nse_ok"] else ("BSE✅" if r["bse_ok"] else "NONE❌")
            print(f"    {s:15}  recovery: {recovered}")

    print()


if __name__ == "__main__":
    main()
