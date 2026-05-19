"""Unit tests for fetch_benchmark.py - uses mocks, no live API calls.

Run: uv run python tests/test_fetch_benchmark.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

import scripts.fetch.fetch_benchmark as fb

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    status = PASS if ok else FAIL
    results.append((name, status, detail))
    print(f"  {status}  {name}" + (f"  [{detail}]" if detail else ""))


def _mock_response(payload: dict[str, object]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


def _run_with_temp_benchmark(test_func) -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        market_dir = tmp / "market"
        market_dir.mkdir(parents=True)
        with patch.object(fb, "BENCHMARK_FILE", market_dir / "benchmark.csv"):
            test_func()
    finally:
        shutil.rmtree(tmp)


print("\n" + "=" * 65)
print("TEST GROUP 1: source fetchers")
print("=" * 65)

with patch(
    "scripts.fetch.fetch_benchmark.requests.get",
    return_value=_mock_response(
        {"data": [{"indexName": "NIFTY 250", "current": "15300.5", "triValue": "24500.75"}]}
    ),
):
    price, tri = fb.fetch_tri_from_nse()

record(
    "fetch_tri_from_nse: TRI + price success",
    price == 15300.5 and tri == 24500.75,
    f"price={price} tri={tri}",
)

retry_calls = [0]


def _retry_then_success(*_args, **_kwargs):
    retry_calls[0] += 1
    if retry_calls[0] == 1:
        raise requests.RequestException("HTTP 429 Too Many Requests")
    return _mock_response(
        {"data": [{"indexName": "NIFTY 250", "current": "100.0", "triValue": "200.0"}]}
    )


with (
    patch("scripts.fetch.fetch_benchmark.requests.get", side_effect=_retry_then_success),
    patch("scripts.fetch.fetch_benchmark.time.sleep"),
):
    price, tri = fb.fetch_tri_from_nse()

record(
    "fetch_tri_from_nse: retry then success",
    retry_calls[0] == 2 and price == 100.0 and tri == 200.0,
    f"calls={retry_calls[0]} price={price} tri={tri}",
)

with (
    patch(
        "scripts.fetch.fetch_benchmark.requests.get",
        side_effect=requests.RequestException("Connection timeout"),
    ),
    patch("scripts.fetch.fetch_benchmark.time.sleep"),
):
    price, tri = fb.fetch_tri_from_nse()

record(
    "fetch_tri_from_nse: all retries fail",
    price is None and tri is None,
    f"price={price} tri={tri}",
)

with patch(
    "scripts.fetch.fetch_benchmark.requests.get",
    return_value=_mock_response({"data": [{"indexName": "NIFTY 250", "current": "123.45"}]}),
):
    price, tri = fb.fetch_tri_from_nse()

record(
    "fetch_tri_from_nse: price only",
    price == 123.45 and tri is None,
    f"price={price} tri={tri}",
)

nselib_df = pd.DataFrame({"index": ["NIFTY 50", "NIFTY 250"], "last": ["24000", "15001.25"]})
record(
    "nselib normalise: NIFTY 250 row",
    fb._normalise_nselib_index_df(nselib_df) == 15001.25,
)

record(
    "nselib normalise: no matching row",
    fb._normalise_nselib_index_df(pd.DataFrame({"index": ["NIFTY 50"], "last": ["1"]})) is None,
)

mock_indices = MagicMock()
mock_indices.live_index_performances.return_value = nselib_df
with (
    patch.dict("sys.modules", {"nselib": MagicMock(), "nselib.indices": mock_indices}),
    patch("nselib.indices", mock_indices),
):
    nselib_price = fb.fetch_price_index_from_nselib()

record(
    "fetch_price_index_from_nselib: success",
    nselib_price == 15001.25,
    f"price={nselib_price}",
)

mock_indices_fail = MagicMock()
mock_indices_fail.live_index_performances.side_effect = Exception("Resource not available")
with (
    patch.dict("sys.modules", {"nselib": MagicMock(), "nselib.indices": mock_indices_fail}),
    patch("nselib.indices", mock_indices_fail),
):
    nselib_price = fb.fetch_price_index_from_nselib()

record("fetch_price_index_from_nselib: exception -> None", nselib_price is None)

yf_hist = pd.DataFrame({"Close": [1.0, 2.0, 3.0]})
mock_ticker = MagicMock()
mock_ticker.history.return_value = yf_hist
with patch("scripts.fetch.fetch_benchmark.yf.Ticker", return_value=mock_ticker):
    yf_price = fb.fetch_price_index_from_yfinance()

record("fetch_price_index_from_yfinance: success", yf_price == 3.0, f"price={yf_price}")

mock_ticker_empty = MagicMock()
mock_ticker_empty.history.return_value = pd.DataFrame()
with patch("scripts.fetch.fetch_benchmark.yf.Ticker", return_value=mock_ticker_empty):
    yf_price = fb.fetch_price_index_from_yfinance()

record("fetch_price_index_from_yfinance: empty -> None", yf_price is None)


print("\n" + "=" * 65)
print("TEST GROUP 2: loading and main output")
print("=" * 65)


def test_load_existing_no_file() -> None:
    df = fb.load_existing()
    record(
        "load_existing: missing file -> empty expected columns",
        df.empty and list(df.columns) == ["date", "price_index", "tri_level", "source"],
    )


_run_with_temp_benchmark(test_load_existing_no_file)


def test_load_existing_valid() -> None:
    fb.BENCHMARK_FILE.write_text(
        "date,price_index,tri_level,source\n2026-01-01,100,200,TRI\n",
        encoding="utf-8",
    )
    df = fb.load_existing()
    record(
        "load_existing: valid CSV",
        len(df) == 1 and float(df.iloc[0]["tri_level"]) == 200.0,
    )


_run_with_temp_benchmark(test_load_existing_valid)


def test_main_tri_source() -> None:
    with (
        patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(100.0, 200.0)),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_nselib") as nselib_mock,
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_yfinance") as yf_mock,
    ):
        fb.main()
        df = pd.read_csv(fb.BENCHMARK_FILE)
        ok = (
            len(df) == 1
            and df.iloc[0]["date"] == date.today().isoformat()
            and float(df.iloc[0]["price_index"]) == 100.0
            and float(df.iloc[0]["tri_level"]) == 200.0
            and df.iloc[0]["source"] == "TRI"
            and not nselib_mock.called
            and not yf_mock.called
        )
        record("main: TRI source writes unchanged row shape", ok)


_run_with_temp_benchmark(test_main_tri_source)


def test_main_nse_price_source() -> None:
    with (
        patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(101.0, None)),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_nselib") as nselib_mock,
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_yfinance") as yf_mock,
    ):
        fb.main()
        df = pd.read_csv(fb.BENCHMARK_FILE)
        ok = (
            len(df) == 1
            and float(df.iloc[0]["price_index"]) == 101.0
            and pd.isna(df.iloc[0]["tri_level"])
            and df.iloc[0]["source"] == "price_index_nse"
            and not nselib_mock.called
            and not yf_mock.called
        )
        record("main: NSE JSON price-only source", ok)


_run_with_temp_benchmark(test_main_nse_price_source)


def test_main_nselib_fallback() -> None:
    with (
        patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(None, None)),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_nselib", return_value=15001.25),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_yfinance") as yf_mock,
    ):
        fb.main()
        df = pd.read_csv(fb.BENCHMARK_FILE)
        ok = (
            len(df) == 1
            and float(df.iloc[0]["price_index"]) == 15001.25
            and pd.isna(df.iloc[0]["tri_level"])
            and df.iloc[0]["source"] == "price_index_nse"
            and not yf_mock.called
        )
        record("main: nselib fallback writes NSE price-index source", ok)


_run_with_temp_benchmark(test_main_nselib_fallback)


def test_main_yfinance_fallback() -> None:
    with (
        patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(None, None)),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_nselib", return_value=None),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_yfinance", return_value=14000.0),
    ):
        fb.main()
        df = pd.read_csv(fb.BENCHMARK_FILE)
        ok = (
            len(df) == 1
            and float(df.iloc[0]["price_index"]) == 14000.0
            and pd.isna(df.iloc[0]["tri_level"])
            and df.iloc[0]["source"] == "price_index_yfinance"
        )
        record("main: yfinance fallback source unchanged", ok)


_run_with_temp_benchmark(test_main_yfinance_fallback)


def test_main_all_fail() -> None:
    with (
        patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(None, None)),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_nselib", return_value=None),
        patch("scripts.fetch.fetch_benchmark.fetch_price_index_from_yfinance", return_value=None),
    ):
        try:
            fb.main()
            ok = False
        except SystemExit as exc:
            ok = exc.code == 1
        record("main: all sources fail -> exit 1", ok)


_run_with_temp_benchmark(test_main_all_fail)


def test_main_deduplication() -> None:
    fb.BENCHMARK_FILE.write_text(
        "date,price_index,tri_level,source\n"
        "2026-01-01,90,,price_index_nse\n"
        f"{date.today().isoformat()},100,200,TRI\n",
        encoding="utf-8",
    )
    with patch("scripts.fetch.fetch_benchmark.fetch_tri_from_nse", return_value=(101.0, 201.0)):
        fb.main()
    df = pd.read_csv(fb.BENCHMARK_FILE)
    today_rows = df[df["date"] == date.today().isoformat()]
    ok = (
        len(df) == 2
        and len(today_rows) == 1
        and float(today_rows.iloc[0]["price_index"]) == 101.0
        and list(df["date"]) == sorted(df["date"])
    )
    record("main: deduplicates same-date row and sorts", ok)


_run_with_temp_benchmark(test_main_deduplication)


print("\n" + "=" * 65)
passed = sum(1 for _, status, _ in results if status == PASS)
failed = len(results) - passed
print(f"RESULTS: {passed} passed, {failed} failed")
print("=" * 65)

if failed:
    sys.exit(1)
