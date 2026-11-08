"""Step 7: raw record -> typed rows, using the real payload shapes captured on 2026-11-08."""

import pytest

from quant_research.normalize.rows import NORMALIZERS, RawRecord, normalize

MS = 1_000_000


def _rec(payload: object, recv: int = 1_794_171_600_000 * MS) -> RawRecord:
    return RawRecord(recv_ns=recv, offset_ms=-8.0, clock_suspect=False, payload=payload)


def test_book_ticker_rows_keep_event_and_receive_time() -> None:
    p = [
        {
            "symbol": "BTCUSDT",
            "bidPrice": "75715.80",
            "bidQty": "8.986",
            "askPrice": "75715.90",
            "askQty": "1.749",
            "time": 1794171598280,
            "lastUpdateId": 12050650710966,
        }
    ]
    rows = normalize("binance.um.book_ticker_1s", _rec(p))
    assert rows == [
        {
            "symbol": "BTCUSDT",
            "event_ns": 1794171598280 * MS,
            "received_ns": 1_794_171_600_000 * MS,
            "offset_ms": -8.0,
            "clock_suspect": False,
            "bid_price": 75715.8,
            "bid_qty": 8.986,
            "ask_price": 75715.9,
            "ask_qty": 1.749,
            "update_id": 12050650710966,
        }
    ]


def test_force_order_uses_the_trade_time_as_event_time() -> None:
    p = {
        "stream": "!forceOrder@arr",
        "data": {
            "e": "forceOrder",
            "E": 1794171600181,
            "o": {
                "s": "AIOUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1533",
                "p": "0.0502200",
                "ap": "0.0493691",
                "X": "FILLED",
                "l": "13",
                "z": "1533",
                "T": 1794171599176,
            },
        },
    }
    (row,) = normalize("binance.um.force_order", _rec(p))
    assert row["symbol"] == "AIOUSDT" and row["side"] == "BUY" and row["status"] == "FILLED"
    assert row["event_ns"] == 1794171599176 * MS and row["stream_event_ns"] == 1794171600181 * MS
    assert row["avg_price"] == pytest.approx(0.0493691) and row["filled_qty"] == 1533.0


def test_metrics_rows_take_symbol_from_params_when_the_row_lacks_it() -> None:
    p = {
        "params": {"symbol": "1000PEPEUSDT", "period": "5m"},
        "data": [
            {
                "buySellRatio": "0.5928",
                "sellVol": "207967249.0000",
                "buyVol": "123283480.0000",
                "timestamp": 1794022500000,
            }
        ],
    }
    (row,) = normalize("binance.um.taker_ls", _rec(p))
    assert row["symbol"] == "1000PEPEUSDT" and row["event_ns"] == 1794022500000 * MS
    assert row["buy_sell_ratio"] == pytest.approx(0.5928) and row["buy_vol"] == 123283480.0


def test_basis_empty_strings_become_null_not_zero() -> None:
    p = {
        "params": {"pair": "BTCUSDT", "contractType": "PERPETUAL"},
        "data": [
            {
                "indexPrice": "1",
                "contractType": "PERPETUAL",
                "basisRate": "0.0003",
                "futuresPrice": "1.0003",
                "annualizedBasisRate": "",
                "basis": "0.0003",
                "pair": "BTCUSDT",
                "timestamp": 5,
            }
        ],
    }
    (row,) = normalize("binance.um.basis", _rec(p))
    assert row["annualized_basis_rate"] is None and row["basis_rate"] == pytest.approx(0.0003)


@pytest.mark.parametrize(
    ("dataset", "row", "column", "value"),
    [
        (
            "binance.um.oi_hist",
            {"symbol": "X", "sumOpenInterest": "2", "sumOpenInterestValue": "3", "timestamp": 1},
            "sum_open_interest",
            2.0,
        ),
        (
            "binance.um.top_ls_account",
            {"symbol": "X", "longAccount": "0.7", "longShortRatio": "2.4", "shortAccount": "0.3", "timestamp": 1},
            "long_short_ratio",
            2.4,
        ),
        (
            "binance.um.top_ls_position",
            {"symbol": "X", "longAccount": "0.6", "longShortRatio": "1.5", "shortAccount": "0.4", "timestamp": 1},
            "long_account",
            0.6,
        ),
        (
            "binance.um.global_ls_account",
            {"symbol": "X", "longAccount": "0.5", "longShortRatio": "1.0", "shortAccount": "0.5", "timestamp": 1},
            "short_account",
            0.5,
        ),
    ],
)
def test_every_metric_endpoint_has_a_normalizer(
    dataset: str, row: dict[str, str | int], column: str, value: float
) -> None:
    (out,) = normalize(dataset, _rec({"params": {"symbol": "X"}, "data": [row]}))
    assert out[column] == pytest.approx(value) and out["event_ns"] == 1 * MS


def test_upbit_trades_rows_keep_sequential_id_and_side() -> None:
    p = {
        "market": "KRW-XRP",
        "days_ago": 0,
        "rows": [
            {
                "market": "KRW-XRP",
                "trade_date_utc": "2026-11-08",
                "trade_time_utc": "17:00:02",
                "timestamp": 1794157202733,
                "trade_price": 1579.0,
                "trade_volume": 12.37020723,
                "prev_closing_price": 1592.0,
                "change_price": -13.0,
                "ask_bid": "BID",
                "sequential_id": 17941572027330005,
            }
        ],
    }
    (row,) = normalize("upbit.trades", _rec(p))
    assert row["market"] == "KRW-XRP" and row["sequential_id"] == 17941572027330005
    assert row["ask_bid"] == "BID" and row["event_ns"] == 1794157202733 * MS and row["trade_price"] == 1579.0


def test_upbit_orderbook_level_one() -> None:
    p = [
        {
            "market": "KRW-XRP",
            "timestamp": 1794166045012,
            "total_ask_size": 1.0,
            "total_bid_size": 2.0,
            "orderbook_units": [{"bid_price": 1578.0, "bid_size": 35.5, "ask_price": 1579.0, "ask_size": 387.6}],
            "level": 0,
        }
    ]
    (row,) = normalize("upbit.orderbook_l1_1s", _rec(p))
    assert (row["bid_price"], row["ask_price"], row["event_ns"]) == (1578.0, 1579.0, 1794166045012 * MS)


def test_empty_upbit_orderbook_units_are_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="orderbook_units"):
        empty_units: list[dict[str, float]] = []
        normalize("upbit.orderbook_l1_1s", _rec([{"market": "KRW-X", "timestamp": 1, "orderbook_units": empty_units}]))


def test_server_time_rows_carry_the_offset() -> None:
    (row,) = normalize(
        "binance.server_time", _rec({"samples": [[21.0, 78], [4.0, 44]], "offset_ms": 4.0, "rtt_ms": 44})
    )
    assert (row["clock_offset_ms"], row["rtt_ms"]) == (4.0, 44) and row["event_ns"] == row["received_ns"]


def test_unknown_dataset_is_an_error_and_registry_covers_all_t0_event_and_interval_sets() -> None:
    with pytest.raises(KeyError, match="no normalizer"):
        normalize("binance.um.unknown", _rec({}))
    from quant_research.catalog.bootstrap import T0_DATASETS

    expected = {d.dataset_id for d in T0_DATASETS if d.time_semantics != "snapshot"}
    assert expected <= set(NORMALIZERS)
