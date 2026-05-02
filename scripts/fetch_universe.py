"""Fetch Nifty LargeMidcap 250 constituent list from NSE and write to data/universe.csv.

NSE's archive CSV URL is defunct (blocked by Akamai bot protection). This script
uses a headless Playwright browser to establish a real browser session, then calls
the NSE equity-stockIndices JSON API from within that session.

Self-throttles: skips if universe.csv is less than MAX_AGE_DAYS old.
Use --force to override. Exits with code 1 on download failure without touching
the existing file.

First-time setup: after `uv sync`, run once:
    uv run playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
OUT_FILE = DATA_DIR / "universe.csv"

MAX_AGE_DAYS = 90           # refresh cadence — matches NSE semi-annual reindex
MIN_ROWS = 240              # sanity bound: fewer rows → partial download
MAX_ROWS = 260              # sanity bound: more rows → unexpected format change
REINDEX_WARN_THRESHOLD = 5  # flag if symbol count changes by more than this

NSE_INDEX = "NIFTY LARGEMIDCAP 250"
NSE_MARKET_URL = "https://www.nseindia.com/market-data/live-equity-market"


def is_fresh(path: Path, max_age_days: int) -> bool:
    """Return True if *path* exists and was last modified within *max_age_days* days."""
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return datetime.now() - mtime < timedelta(days=max_age_days)


def load_existing_symbols(path: Path) -> set[str]:
    """Return the set of symbol strings from an existing universe CSV.

    Returns an empty set if the file is missing, unreadable, or malformed.
    """
    if not path.exists():
        return set()
    try:
        df = pd.read_csv(path)
        return set(df["symbol"].tolist())
    except (OSError, pd.errors.ParserError, KeyError, ValueError):
        return set()


def fetch_via_browser() -> list[dict]:  # type: ignore[type-arg]
    """Use a headless Playwright Chromium browser to bypass Akamai and call the NSE API.

    Opens the NSE market page to acquire Akamai session cookies, then calls the
    equity-stockIndices JSON endpoint from within that browser session.

    Returns:
        List of raw stock dicts from the NSE API response.

    Raises:
        RuntimeError: if the API returns an error payload.
        SystemExit: if playwright is not installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed. Run: uv run playwright install chromium")
        sys.exit(1)

    index_encoded = NSE_INDEX.replace(" ", "%20")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-Ch-Ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        )
        # Mask navigator.webdriver to avoid triggering Akamai bot detection.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        print("  Opening NSE session (this may take ~30s)...")
        try:
            page.goto(NSE_MARKET_URL, wait_until="domcontentloaded", timeout=90_000)
        except Exception:
            # Akamai may abort HTTP/2 — retry waiting for full load event.
            page.goto(NSE_MARKET_URL, wait_until="load", timeout=90_000)
        page.wait_for_timeout(3000)  # let Akamai JS cookies settle

        print(f"  Fetching {NSE_INDEX} constituent list via API...")
        raw = page.evaluate(f"""() => {{
            return fetch('/api/equity-stockIndices?index={index_encoded}', {{
                credentials: 'include',
                headers: {{'Accept': 'application/json, text/plain, */*'}}
            }})
            .then(r => r.json())
            .then(d => JSON.stringify(d.data || []))
            .catch(e => JSON.stringify({{error: String(e)}}));
        }}""")

        browser.close()

    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "error" in parsed:
        raise RuntimeError(parsed["error"])

    # The first element is an index-level summary row — skip it.
    stocks: list[dict] = [s for s in parsed if s.get("symbol") != NSE_INDEX]  # type: ignore[type-arg]
    return stocks


def build_dataframe(stocks: list[dict]) -> pd.DataFrame:  # type: ignore[type-arg]
    """Convert the raw NSE API stock list into a normalised DataFrame.

    Args:
        stocks: Raw stock dicts from the NSE equity-stockIndices API.

    Returns:
        DataFrame with columns [symbol, company_name, series, isin_code, sector].
        Rows with empty symbol strings are dropped.
    """
    rows = []
    for s in stocks:
        meta = s.get("meta") or {}
        rows.append({
            "symbol": s.get("symbol", "").strip(),
            "company_name": meta.get("companyName", s.get("symbol", "")),
            "series": s.get("series") or (meta.get("activeSeries") or ["EQ"])[0],
            "isin_code": meta.get("isin", ""),
            "sector": meta.get("industry", ""),
        })
    df = pd.DataFrame(rows)
    return df[df["symbol"].str.len() > 0]


def main() -> None:
    """Fetch the Nifty LargeMidcap 250 constituent list and write to data/universe.csv.

    Skips the download if universe.csv is less than MAX_AGE_DAYS old.
    Exits with code 1 without overwriting the existing file on any failure.
    """
    parser = argparse.ArgumentParser(
        description="Fetch Nifty LargeMidcap 250 universe from NSE"
    )
    parser.add_argument(
        "--force", action="store_true", help="Ignore age check and force re-download"
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # --- Age check -----------------------------------------------------------
    if not args.force and is_fresh(OUT_FILE, MAX_AGE_DAYS):
        age_days = (
            datetime.now() - datetime.fromtimestamp(OUT_FILE.stat().st_mtime)
        ).days
        print(
            f"universe.csv is {age_days} days old (< {MAX_AGE_DAYS}). "
            "Skipping. Use --force to override."
        )
        sys.exit(0)

    prev_symbols = load_existing_symbols(OUT_FILE)

    # --- Fetch via Playwright browser session --------------------------------
    print(f"Downloading {NSE_INDEX} list from NSE...")
    try:
        stocks = fetch_via_browser()
    except Exception as e:
        print(f"ERROR: Download failed: {e}")
        print("Keeping existing universe.csv unchanged.")
        sys.exit(1)

    if not stocks:
        print("ERROR: API returned an empty stock list.")
        print("Keeping existing universe.csv unchanged.")
        sys.exit(1)

    df = build_dataframe(stocks)

    # --- Row count sanity check ----------------------------------------------
    row_count = len(df)
    if not (MIN_ROWS <= row_count <= MAX_ROWS):
        print(
            f"ERROR: Row count {row_count} is outside expected range "
            f"{MIN_ROWS}–{MAX_ROWS}. Aborting to avoid overwriting good data."
        )
        sys.exit(1)

    # --- Reindex change detection --------------------------------------------
    new_symbols = set(df["symbol"].tolist())
    if prev_symbols:
        added = new_symbols - prev_symbols
        removed = prev_symbols - new_symbols
        delta = len(added) + len(removed)
        if delta > REINDEX_WARN_THRESHOLD:
            print("WARNING: Large index change detected — possible reindex event!")
            print(f"  Added   ({len(added)}): {sorted(added)}")
            print(f"  Removed ({len(removed)}): {sorted(removed)}")
            print("  Verify this change is intentional before proceeding.")
        elif added or removed:
            print(f"  Minor index change: +{sorted(added)} -{sorted(removed)}")

    # --- Write ---------------------------------------------------------------
    try:
        df.to_csv(OUT_FILE, index=False)
    except OSError as e:
        print(f"ERROR: Could not write {OUT_FILE}: {e}")
        sys.exit(1)

    print(f"OK: {row_count} stocks written to {OUT_FILE}")


if __name__ == "__main__":
    main()
