"""Fetch weekly OHLCV and cumulative daily adj_close for all Nifty 250 symbols.

Writes:
  data/prices/YYYY-WW.csv          — weekly OHLCV snapshot (immutable once written)
  data/prices/daily_adj_close.csv  — cumulative append-only daily adj_close (wide format)

On first run, pulls HISTORY_WEEKS of history for the daily file.
On subsequent runs, appends only rows newer than the last date already present.
The daily file is never trimmed — scripts that read it filter by date window.

Data sources (tried in order):
  1. PRIMARY  : yfinance batch download (50 symbols/call) with exponential backoff
  2. SECONDARY: NSE equity historical API via Playwright browser session
                (same approach as fetch_universe.py — bypasses Akamai)
  3. FINAL    : symbols that fail both sources are logged; session aborts only
                if failures exceed FAILURE_THRESHOLD_PCT of the universe.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# Suppress yfinance / pandas deprecation chatter that isn't actionable
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*utcnow.*")

print(f"yfinance version: {yf.__version__}")

DATA_DIR = Path(__file__).parent.parent / "data"
PRICES_DIR = DATA_DIR / "prices"
UNIVERSE_FILE = DATA_DIR / "universe.csv"
DAILY_FILE = PRICES_DIR / "daily_adj_close.csv"

BATCH_SIZE = 50               # symbols per yf.download call
FAILURE_THRESHOLD_PCT = 0.08  # abort if > this fraction return no data from ANY source
HISTORY_WEEKS = 52            # weeks of history pulled on first run
# Weekly/incremental: short sleep is fine (few calls per session)
SLEEP_MIN_S = 3.0             # min random sleep between yfinance batches
SLEEP_MAX_S = 10.0            # max random sleep between yfinance batches
# First-run / large history: use longer sleeps to avoid rate limits across 5 chunks
SLEEP_HIST_MIN_S = 15.0       # min sleep between batches in a multi-chunk run
SLEEP_HIST_MAX_S = 30.0       # max sleep between batches in a multi-chunk run
SLEEP_INTER_CHUNK_S = 60.0    # sleep between date chunks (fixed, not random)

# For first-run (large history), split into chunks to avoid rate-limits.
# Yahoo Finance rate-limits aggressively on 52-week single requests.
YF_CHUNK_WEEKS = 12           # max weeks per single yf.download call

# Retry config for yfinance rate-limit errors.
# Only retry once (15s) to handle a brief transient throttle.
# If v7 is truly blocked, one fast retry reveals that and the _yf_v7_blocked
# flag flips, routing all remaining batches directly to Ticker.history().
YF_MAX_RETRIES = 1
YF_RETRY_BASE_S = 15.0        # retry once after 15s; if still blocked → v8 fallback

NSE_MARKET_URL = "https://www.nseindia.com/market-data/live-equity-market"
NSE_HISTORY_URL = "https://www.nseindia.com/api/historical/cm/equity"
NSE_BATCH_SLEEP_S = 0.8       # sleep between NSE API calls (per symbol)


# ── helpers ──────────────────────────────────────────────────────────────────

def iso_week_str(d: date) -> str:
    """Return an ISO year-week string (e.g. '2026-18') for date *d*."""
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def load_symbols() -> list[str]:
    """Read data/universe.csv and return (nse_tickers, symbol_list).

    nse_tickers: list of '.NS' tickers for yfinance
    Returns the plain symbol list.
    Exits with code 1 if the universe file is missing.
    """
    if not UNIVERSE_FILE.exists():
        print("ERROR: data/universe.csv not found. Run fetch_universe.py first.")
        sys.exit(1)
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return [s.strip() + ".NS" for s in df["symbol"].tolist()]
    except (OSError, pd.errors.ParserError, KeyError) as e:
        print(f"ERROR: Could not read universe.csv: {e}")
        sys.exit(1)


def load_symbol_meta() -> dict[str, str]:
    """Return {symbol: series} from universe.csv (used for NSE API calls)."""
    try:
        df = pd.read_csv(UNIVERSE_FILE)
        return dict(zip(df["symbol"].str.strip(), df["series"].str.strip()))
    except Exception:
        return {}


# ── SOURCE 1: yfinance ────────────────────────────────────────────────────────

def _is_rate_limit_error(exc: Exception) -> bool:
    """Return True if *exc* is a Yahoo Finance rate-limit error."""
    msg = str(exc).lower()
    return "ratelimit" in msg or "too many requests" in msg or "429" in msg


def _normalise_ticker_df(raw_df: pd.DataFrame, sym: str) -> pd.DataFrame:
    """Normalise a single-symbol OHLCV DataFrame (from either download or history).

    Handles both yf.download() output (columns: Open/High/… or Adj Close) and
    Ticker.history() output (columns: Open/High/… Capital-case, no Adj Close).

    Returns a cleaned DataFrame with 8 standard columns, or empty on failure.
    """
    df = raw_df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]

    # Ticker.history() uses 'dividends'/'stock splits'; also no 'adj close'.
    # Map 'close' → 'adj_close' when 'adj close' is absent (history() path).
    if "adj close" in df.columns:
        df = df.rename(columns={"adj close": "adj_close"})
    elif "adj_close" not in df.columns:
        df["adj_close"] = df["close"]   # history() — use unadjusted close

    # Normalise timezone-aware DatetimeIndex produced by history()
    if "date" in df.columns and pd.api.types.is_datetime64tz_dtype(df["date"]):
        df["date"] = df["date"].dt.tz_convert(None)

    df["symbol"] = sym

    needed = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        return pd.DataFrame()

    df = df[needed].dropna(subset=["close"])

    # Drop zero-volume carry-forward rows (Yahoo repeats prior close on NSE holidays).
    zero_vol = (df["volume"] == 0) & (df["close"] == df["close"].shift(1))
    if zero_vol.any():
        dates_dropped = df.loc[zero_vol, "date"].dt.date.tolist()
        print(f"  [{sym}] dropping {int(zero_vol.sum())} zero-volume "
              f"carry-forward row(s): {dates_dropped}")
        df = df[~zero_vol]

    return df


def _fetch_ticker_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Fetch a single ticker via yf.Ticker.history() (v8/chart endpoint).

    This endpoint has an independent rate-limit quota from yf.download (v7).
    Falls back to this when the batch download is rate-limited.
    """
    sym = ticker.replace(".NS", "")
    try:
        raw = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
        )
        if raw is None or raw.empty or raw["Close"].isna().all():
            return pd.DataFrame()
        return _normalise_ticker_df(raw, sym)
    except Exception:
        return pd.DataFrame()


def fetch_batch_yf(
    tickers: list[str], start: date, end: date, attempt: int = 0
) -> tuple[pd.DataFrame, list[str]]:
    """Download OHLCV for a batch of Yahoo tickers.

    Strategy (in order):
      1. yf.download() — single call for the whole batch (fast, v7 endpoint).
         Skipped immediately if _yf_v7_blocked is set (already confirmed blocked).
      2. On YFRateLimitError: retry up to YF_MAX_RETRIES with exponential backoff.
      3. After retries exhausted: set _yf_v7_blocked=True (skips v7 for all future
         batches this session) then fall back to yf.Ticker.history() per symbol
         (v8/chart endpoint — independent quota, not affected by v7 rate limits).

    Args:
        tickers: List of Yahoo Finance ticker strings (e.g. ['RELIANCE.NS', ...]).
        start:   First date to fetch (inclusive).
        end:     Last date to fetch (inclusive; +1 day added internally).
        attempt: Current retry count (0 = first try).

    Returns:
        (DataFrame with OHLCV rows, list of symbols with no data from this batch).
    """
    global _yf_v7_blocked

    # Skip v7 entirely if a previous batch already confirmed it's blocked.
    if _yf_v7_blocked:
        return _fetch_batch_via_history(tickers, start, end)

    try:
        raw = yf.download(
            tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=False,
        )
    except Exception as e:
        if _is_rate_limit_error(e):
            if attempt < YF_MAX_RETRIES:
                wait_s = YF_RETRY_BASE_S * (2 ** attempt)
                print(
                    f"  yfinance v7 rate-limited (batch {tickers[0].replace('.NS','')}…). "
                    f"Retry {attempt+1}/{YF_MAX_RETRIES} in {wait_s:.0f}s…"
                )
                time.sleep(wait_s)
                return fetch_batch_yf(tickers, start, end, attempt + 1)
            else:
                # Retries exhausted — block v7 for this entire session, then
                # fall back to per-symbol Ticker.history() (v8/chart endpoint).
                _yf_v7_blocked = True
                print(
                    f"  yfinance v7 retries exhausted. Switching ALL remaining batches "
                    f"to Ticker.history() (v8 endpoint) for this session."
                )
                return _fetch_batch_via_history(tickers, start, end)
        print(f"  WARNING: yf.download failed: {e}")
        return pd.DataFrame(), [t.replace(".NS", "") for t in tickers]

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        # yf.download silently returns empty on rate-limit (no exception raised).
        # Distinguish "truly no data" (weekend/holiday) from rate-limit by checking
        # one symbol via Ticker.history() (v8 endpoint, independent quota).
        canary = tickers[0]
        canary_df = _fetch_ticker_history(canary, start, end)
        if not canary_df.empty:
            # v8 has data → v7 is rate-limited. Block v7 and switch all batches to v8.
            _yf_v7_blocked = True
            print(
                f"  yfinance v7 returned empty (rate-limited). "
                f"Confirmed via Ticker.history(). Switching ALL batches to v8 endpoint."
            )
            return _fetch_batch_via_history(tickers, start, end)
        # v8 also empty → genuinely no data (weekend/holiday window)
        return pd.DataFrame(), [t.replace(".NS", "") for t in tickers]

    frames: list[pd.DataFrame] = []
    empty: list[str] = []

    for ticker in tickers:
        sym = ticker.replace(".NS", "")
        try:
            chunk = raw[ticker].copy() if len(tickers) > 1 else raw.copy()
            if chunk.empty or chunk["Close"].isna().all():
                empty.append(sym)
                continue
            df = _normalise_ticker_df(chunk, sym)
            if df.empty:
                empty.append(sym)
            else:
                frames.append(df)
        except KeyError:
            empty.append(sym)

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, empty


def _fetch_batch_via_history(
    tickers: list[str], start: date, end: date
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch each ticker individually via Ticker.history() (v8/chart endpoint).

    Slower than batch download but uses an independent rate-limit quota.
    Called automatically when yf.download() (v7) is rate-limited.
    """
    frames: list[pd.DataFrame] = []
    empty:  list[str] = []

    for ticker in tickers:
        sym = ticker.replace(".NS", "")
        df = _fetch_ticker_history(ticker, start, end)
        if df.empty:
            empty.append(sym)
        else:
            frames.append(df)
        # Small sleep to respect v8 rate limits too
        time.sleep(0.5)

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, empty


def fetch_all_yf(
    tickers: list[str], start: date, end: date
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch all tickers for [start, end], splitting into YF_CHUNK_WEEKS-sized date windows.

    Yahoo Finance rate-limits aggressively on large date ranges (e.g. 52 weeks).
    Splitting into ~12-week chunks keeps each request below the trigger threshold.
    Symbols are only marked as 'empty' if they returned no data across ALL chunks.

    Returns:
        tuple[combined_df, empty_symbols] where empty_symbols is the list of
        symbols that returned no data from yfinance across the entire window.
    """
    # Build date chunks: [start, start+chunk), [start+chunk, start+2*chunk), …
    chunks: list[tuple[date, date]] = []
    cursor = start
    chunk_delta = timedelta(weeks=YF_CHUNK_WEEKS)
    while cursor <= end:
        chunk_end = min(cursor + chunk_delta - timedelta(days=1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

    multi_chunk = len(chunks) > 1
    if multi_chunk:
        print(f"  Date range spans {(end - start).days} days → split into "
              f"{len(chunks)} chunks of ≤{YF_CHUNK_WEEKS} weeks "
              f"(inter-batch sleep {SLEEP_HIST_MIN_S:.0f}–{SLEEP_HIST_MAX_S:.0f}s, "
              f"inter-chunk sleep {SLEEP_INTER_CHUNK_S:.0f}s).")

    all_frames:     list[pd.DataFrame] = []
    symbols_seen:   set[str] = set()
    all_tickers_set = {t.replace(".NS", "") for t in tickers}

    for chunk_idx, (c_start, c_end) in enumerate(chunks, 1):
        if multi_chunk:
            print(f"  Chunk {chunk_idx}/{len(chunks)}: {c_start} → {c_end}")

        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i : i + BATCH_SIZE]
            df, _empty = fetch_batch_yf(batch, c_start, c_end)

            if not df.empty:
                all_frames.append(df)
                symbols_seen.update(df["symbol"].unique())

            if i + BATCH_SIZE < len(tickers):
                # Use longer sleep for multi-chunk (large history) runs to stay under rate limits.
                if multi_chunk:
                    sleep_s = random.uniform(SLEEP_HIST_MIN_S, SLEEP_HIST_MAX_S)
                else:
                    sleep_s = random.uniform(SLEEP_MIN_S, SLEEP_MAX_S)
                time.sleep(sleep_s)

        # Fixed inter-chunk sleep — gives Yahoo Finance time to reset the rate counter.
        if chunk_idx < len(chunks):
            print(f"  Waiting {SLEEP_INTER_CHUNK_S:.0f}s before next chunk…")
            time.sleep(SLEEP_INTER_CHUNK_S)

    all_empty = sorted(all_tickers_set - symbols_seen)
    combined = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()
    return combined, all_empty


# ── SOURCE 2: NSE via Playwright session ──────────────────────────────────────

_nse_page: object | None = None          # playwright Page, kept open for batch use
_nse_browser: object | None = None

# Once yf.download (v7) is confirmed rate-limited this session, flip this flag so
# all subsequent batches skip straight to Ticker.history() without waiting.
_yf_v7_blocked: bool = False


def _start_nse_session() -> bool:
    """Open a Playwright Chromium browser session on NSE to acquire Akamai cookies.

    The browser stays open so we can make multiple /api calls without re-launching.
    Returns True on success, False if Playwright is not installed or navigation fails.
    """
    global _nse_page, _nse_browser

    try:
        from playwright.sync_api import sync_playwright as _sync_playwright  # noqa: F401
    except ImportError:
        print("  NSE fallback unavailable: playwright not installed.")
        return False

    from playwright.sync_api import sync_playwright

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                # Force HTTP/1.1 — NSE Akamai sometimes resets HTTP/2 connections.
                "--disable-http2",
            ],
        )
        ctx = browser.new_context(
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
            },
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()

        print("  Opening NSE session for fallback fetches (this may take ~30s)…")
        # NSE / Akamai can abort connections — retry up to 5 times with backoff.
        max_nav_attempts = 5
        for attempt in range(max_nav_attempts):
            try:
                page.goto(NSE_MARKET_URL, wait_until="domcontentloaded", timeout=90_000)
                break   # success
            except Exception:
                try:
                    page.goto(NSE_MARKET_URL, wait_until="load", timeout=90_000)
                    break   # success on fallback wait_until
                except Exception as nav_err:
                    if attempt < max_nav_attempts - 1:
                        wait_s = 15 * (attempt + 1)   # 15s, 30s, 45s, 60s
                        print(f"  NSE navigation failed (attempt {attempt+1}/{max_nav_attempts}), "
                              f"retrying in {wait_s}s…")
                        time.sleep(wait_s)
                    else:
                        raise nav_err
        page.wait_for_timeout(3000)

        _nse_page = page
        _nse_browser = browser
        print("  NSE session ready.")
        return True
    except Exception as e:
        print(f"  WARNING: Could not start NSE Playwright session: {e}")
        return False


def _close_nse_session() -> None:
    """Close the Playwright browser if it is open."""
    global _nse_page, _nse_browser
    try:
        if _nse_browser is not None:
            _nse_browser.close()   # type: ignore[union-attr]
    except Exception:
        pass
    _nse_page = None
    _nse_browser = None


def fetch_symbol_nse(symbol: str, start: date, end: date, series: str = "EQ") -> pd.DataFrame:
    """Fetch historical OHLCV for a single symbol via the NSE equity history API.

    Requires an active NSE Playwright session (_nse_page must be set).
    The API returns OHLCV rows for all trading days in [start, end].

    Args:
        symbol: NSE symbol string (e.g. 'RELIANCE').
        start:  First date (inclusive).
        end:    Last date (inclusive).
        series: Market series, almost always 'EQ'.

    Returns:
        DataFrame with columns [symbol, date, open, high, low, close, adj_close, volume],
        or empty DataFrame on failure.
    """
    if _nse_page is None:
        return pd.DataFrame()

    from_dt = start.strftime("%d-%m-%Y")
    to_dt   = end.strftime("%d-%m-%Y")
    series_enc = series.replace('"', '\\"')

    js = f"""() => {{
        return fetch('/api/historical/cm/equity?symbol={symbol}&series=[%22{series_enc}%22]&from={from_dt}&to={to_dt}', {{
            credentials: 'include',
            headers: {{'Accept': 'application/json, text/plain, */*'}}
        }})
        .then(r => r.json())
        .then(d => JSON.stringify(d.data || []))
        .catch(e => JSON.stringify({{error: String(e)}}));
    }}"""

    try:
        raw = _nse_page.evaluate(js)  # type: ignore[union-attr]
        parsed = json.loads(raw)
    except Exception as e:
        return pd.DataFrame()

    if isinstance(parsed, dict) and "error" in parsed:
        return pd.DataFrame()

    rows = []
    for d in parsed:
        try:
            rows.append({
                "symbol":    symbol,
                "date":      pd.to_datetime(
                    d.get("CH_TIMESTAMP") or d.get("mTIMESTAMP", ""),
                    dayfirst=True,
                ),
                "open":      float(d.get("CH_OPENING_PRICE") or 0),
                "high":      float(d.get("CH_TRADE_HIGH_PRICE") or 0),
                "low":       float(d.get("CH_TRADE_LOW_PRICE") or 0),
                "close":     float(d.get("CH_CLOSING_PRICE") or 0),
                "adj_close": float(d.get("CH_CLOSING_PRICE") or 0),  # NSE doesn't adjust
                "volume":    int(d.get("CH_TOT_TRADED_QTY") or 0),
            })
        except (TypeError, ValueError):
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_failed_via_nse(
    symbols: list[str], start: date, end: date, meta: dict[str, str]
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch a list of symbols that failed yfinance via the NSE historical API.

    Args:
        symbols: Plain NSE symbols (without .NS) that need recovery.
        start:   Fetch window start.
        end:     Fetch window end.
        meta:    Dict {symbol: series} from universe.csv.

    Returns:
        (recovered_df, still_missing_symbols)
    """
    if not symbols:
        return pd.DataFrame(), []

    print(f"\n  NSE fallback: recovering {len(symbols)} symbols…")
    if not _start_nse_session():
        return pd.DataFrame(), symbols

    frames: list[pd.DataFrame] = []
    still_missing: list[str] = []

    for i, sym in enumerate(symbols, 1):
        series = meta.get(sym, "EQ")
        df = fetch_symbol_nse(sym, start, end, series)
        if df.empty:
            still_missing.append(sym)
        else:
            frames.append(df)
        if i % 10 == 0:
            print(f"    NSE fallback progress: {i}/{len(symbols)} ({len(frames)} recovered)")
        time.sleep(NSE_BATCH_SLEEP_S)

    _close_nse_session()

    print(f"  NSE fallback complete: recovered {len(frames)} symbols, "
          f"{len(still_missing)} still missing.")

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return combined, still_missing


# ── output writers ────────────────────────────────────────────────────────────

def write_weekly_snapshot(df: pd.DataFrame, week_str: str) -> None:
    """Write the current-week OHLCV snapshot CSV.

    No-ops if the file already exists to preserve immutability.
    Exits with code 1 on write failure.
    """
    out = PRICES_DIR / f"{week_str}.csv"
    if out.exists():
        print(f"  Weekly snapshot {out.name} already exists — skipping overwrite.")
        return
    try:
        df.to_csv(out, index=False)
        print(f"  Weekly snapshot written: {out.name} ({len(df)} rows)")
    except OSError as e:
        print(f"ERROR: Could not write weekly snapshot {out.name}: {e}")
        sys.exit(1)


def update_daily_file(df: pd.DataFrame) -> None:
    """Append new rows to the cumulative daily adj_close wide-format CSV.

    The file uses dates as the row index and symbols as columns.
    Append-only — historical rows are never removed.
    Exits with code 1 on read or write failure (the daily file is critical).
    """
    adj = df[["symbol", "date", "adj_close"]].copy()
    adj["date"] = pd.to_datetime(adj["date"]).dt.date
    pivot = adj.pivot_table(index="date", columns="symbol", values="adj_close")
    pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.sort_index()

    if DAILY_FILE.exists():
        try:
            existing = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)

        last_date = existing.index.max()
        new_rows = pivot[pivot.index > last_date]

        if new_rows.empty:
            print(f"  daily_adj_close.csv already up to date (last: {last_date.date()}).")
            return

        combined = pd.concat([existing, new_rows])
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = combined.sort_index()

        try:
            combined.to_csv(DAILY_FILE)
            print(
                f"  Appended {len(new_rows)} new rows to daily_adj_close.csv "
                f"(total: {len(combined)} rows)"
            )
        except OSError as e:
            print(f"ERROR: Could not write daily_adj_close.csv: {e}")
            sys.exit(1)
    else:
        try:
            pivot.to_csv(DAILY_FILE)
            print(
                f"  Created daily_adj_close.csv "
                f"({len(pivot)} rows, {len(pivot.columns)} symbols)"
            )
        except OSError as e:
            print(f"ERROR: Could not create daily_adj_close.csv: {e}")
            sys.exit(1)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Fetch prices for all Nifty 250 symbols and write OHLCV + daily adj_close files.

    On first run pulls HISTORY_WEEKS of daily history. On subsequent runs appends
    only rows newer than the last date in daily_adj_close.csv.
    Exits with code 1 if more than FAILURE_THRESHOLD_PCT of symbols return no data
    from any source.

    Flags:
      --limit N    Only fetch the first N symbols (for testing).
      --dry-run    Fetch and report but do not write any files.
    """
    parser = argparse.ArgumentParser(description="Fetch NSE prices for Nifty 250")
    parser.add_argument("--limit",   type=int, default=None,
                        help="Only process the first N symbols (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch but do not write output files")
    args = parser.parse_args()

    PRICES_DIR.mkdir(parents=True, exist_ok=True)

    tickers = load_symbols()
    meta    = load_symbol_meta()

    if args.limit:
        tickers = tickers[: args.limit]
        meta    = {k: v for k, v in meta.items() if k + ".NS" in tickers or k in [t.replace(".NS","") for t in tickers]}
        print(f"[--limit {args.limit}] Testing with first {len(tickers)} symbols only.")

    total   = len(tickers)
    print(f"Universe: {total} symbols")

    today    = date.today()
    week_str = iso_week_str(today)

    # ── determine fetch window ────────────────────────────────────────────────
    if DAILY_FILE.exists():
        try:
            existing_daily = pd.read_csv(DAILY_FILE, index_col=0, parse_dates=True)
            last_daily_date = existing_daily.index.max().date()
            start = last_daily_date + timedelta(days=1)
            print(f"Daily file exists. Fetching from {start} to {today}.")
        except (OSError, pd.errors.ParserError) as e:
            print(f"ERROR: Could not read existing daily_adj_close.csv: {e}")
            sys.exit(1)
    else:
        start = today - timedelta(weeks=HISTORY_WEEKS)
        print(f"First run. Fetching {HISTORY_WEEKS} weeks of history from {start} to {today}.")

    if start > today:
        print("Already up to date.")
        return

    # ── SOURCE 1: yfinance ────────────────────────────────────────────────────
    print(f"\nFetching prices via yfinance (batches of {BATCH_SIZE})…")
    df_yf, yf_empty = fetch_all_yf(tickers, start, today)

    yf_got     = len(df_yf["symbol"].unique()) if not df_yf.empty else 0
    yf_missing = len(yf_empty)
    print(f"yfinance: {yf_got} succeeded, {yf_missing} failed")
    if yf_empty:
        print(f"  yfinance failures: {sorted(yf_empty)[:20]}"
              + (" …" if len(yf_empty) > 20 else ""))

    # ── SOURCE 2: NSE fallback for yfinance failures ──────────────────────────
    df_nse = pd.DataFrame()
    nse_still_missing: list[str] = []

    if yf_empty:
        df_nse, nse_still_missing = fetch_failed_via_nse(yf_empty, start, today, meta)
        nse_got = len(df_nse["symbol"].unique()) if not df_nse.empty else 0
        print(f"NSE fallback: {nse_got} recovered, {len(nse_still_missing)} still missing")

    # ── combine results ───────────────────────────────────────────────────────
    frames = [f for f in [df_yf, df_nse] if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    all_missing = nse_still_missing if yf_empty else []

    if df.empty:
        print("ERROR: No data returned for any symbol from any source. "
              "Check network connectivity.")
        sys.exit(1)

    # ── failure threshold check ───────────────────────────────────────────────
    missing_pct = len(all_missing) / total
    if missing_pct > FAILURE_THRESHOLD_PCT:
        print(
            f"ERROR: {len(all_missing)}/{total} ({missing_pct:.1%}) symbols returned no data "
            f"from any source — above the {FAILURE_THRESHOLD_PCT:.0%} abort threshold."
        )
        print(f"  First 20 missing: {sorted(all_missing)[:20]}")
        sys.exit(1)

    if all_missing:
        print(f"WARNING: {len(all_missing)} symbols missing from all sources: "
              f"{sorted(all_missing)[:20]}" + (" …" if len(all_missing) > 20 else ""))

    # ── write outputs ─────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n[--dry-run] Skipping file writes.")
        print(f"  Would write: {PRICES_DIR}/{week_str}.csv  ({len(df)} rows for current week)")
        print(f"  Would update: {DAILY_FILE}")
    else:
        weekly_out = PRICES_DIR / f"{week_str}.csv"
        if not weekly_out.exists():
            week_start = today - timedelta(days=today.weekday())
            week_df = df[pd.to_datetime(df["date"]).dt.date >= week_start]
            if not week_df.empty:
                write_weekly_snapshot(week_df, week_str)
            else:
                print(f"WARNING: No data for current week {week_str} — snapshot not written.")
        else:
            print(f"Weekly snapshot {week_str}.csv already exists — skipping.")

        update_daily_file(df)

    # ── summary ───────────────────────────────────────────────────────────────
    symbols_fetched = len(df["symbol"].unique())
    yf_symbols  = set(df_yf["symbol"].unique())  if not df_yf.empty  else set()
    nse_symbols = set(df_nse["symbol"].unique()) if not df_nse.empty else set()
    dry_tag = " [dry-run, no files written]" if args.dry_run else ""
    print(
        f"\nDone{dry_tag}. {symbols_fetched}/{total} symbols fetched "
        f"(yfinance: {len(yf_symbols)}, NSE fallback: {len(nse_symbols)}, "
        f"missing: {len(all_missing)})"
    )
    if all_missing:
        print(f"Missing symbols: {sorted(all_missing)}")


if __name__ == "__main__":
    main()
