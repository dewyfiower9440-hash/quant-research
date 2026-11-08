"""Raw capture record -> typed rows (plan step 7). Pure functions, one per dataset.

Every row carries `event_ns` (exchange time, ns), `received_ns` (local receive time), `offset_ms` and
`clock_suspect` (segment format v2). Prices/quantities arrive as decimal strings and become float64;
an empty string means "not provided" and becomes None, never 0. Payload shapes were taken from the
real capture of 2026-11-08 (tests/normalize/test_rows.py).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

MS = 1_000_000
Row = dict[str, object]


@dataclass(frozen=True, slots=True)
class RawRecord:
    recv_ns: int
    offset_ms: float | None
    clock_suspect: bool
    payload: object


def _f(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(cast(str | float | int, value))


def _base(rec: RawRecord, event_ms: object) -> Row:
    return {
        "event_ns": int(cast(int, event_ms)) * MS,
        "received_ns": rec.recv_ns,
        "offset_ms": rec.offset_ms,
        "clock_suspect": rec.clock_suspect,
    }


def _book_ticker(rec: RawRecord) -> list[Row]:
    out: list[Row] = []
    for r in cast(list[dict[str, Any]], rec.payload):
        row = {"symbol": str(r["symbol"]), **_base(rec, r["time"])}
        row.update(
            bid_price=_f(r["bidPrice"]),
            bid_qty=_f(r["bidQty"]),
            ask_price=_f(r["askPrice"]),
            ask_qty=_f(r["askQty"]),
            update_id=int(r["lastUpdateId"]),
        )
        out.append(row)
    return out


def _force_order(rec: RawRecord) -> list[Row]:
    data = cast(dict[str, Any], cast(dict[str, Any], rec.payload)["data"])
    o = cast(dict[str, Any], data["o"])
    row = {"symbol": str(o["s"]), **_base(rec, o["T"])}
    row.update(
        stream_event_ns=int(data["E"]) * MS,
        side=str(o["S"]),
        order_type=str(o["o"]),
        time_in_force=str(o["f"]),
        status=str(o["X"]),
        price=_f(o["p"]),
        avg_price=_f(o["ap"]),
        orig_qty=_f(o["q"]),
        last_filled_qty=_f(o["l"]),
        filled_qty=_f(o["z"]),
    )
    return [row]


MetricMap = Mapping[str, str]


def _metric(columns: MetricMap, key: str = "symbol") -> Callable[[RawRecord], list[Row]]:
    def normalize_metric(rec: RawRecord) -> list[Row]:
        payload = cast(dict[str, Any], rec.payload)
        params = cast(dict[str, Any], payload.get("params") or {})
        out: list[Row] = []
        for r in cast(list[dict[str, Any]], payload["data"]):
            symbol = r.get(key) or r.get("symbol") or params.get(key)
            row: Row = {"symbol": str(symbol), **_base(rec, r["timestamp"])}
            for source, target in columns.items():
                row[target] = _f(r.get(source))
            out.append(row)
        return out

    return normalize_metric


_RATIO = {"longAccount": "long_account", "shortAccount": "short_account", "longShortRatio": "long_short_ratio"}


def _upbit_trades(rec: RawRecord) -> list[Row]:
    out: list[Row] = []
    for r in cast(list[dict[str, Any]], cast(dict[str, Any], rec.payload)["rows"]):
        row = {"market": str(r["market"]), **_base(rec, r["timestamp"])}
        row.update(
            sequential_id=int(r["sequential_id"]),
            trade_price=_f(r["trade_price"]),
            trade_volume=_f(r["trade_volume"]),
            ask_bid=str(r["ask_bid"]),
            prev_closing_price=_f(r.get("prev_closing_price")),
        )
        out.append(row)
    return out


def _upbit_orderbook(rec: RawRecord) -> list[Row]:
    out: list[Row] = []
    for r in cast(list[dict[str, Any]], rec.payload):
        units = cast(list[dict[str, Any]], r.get("orderbook_units") or [])
        if not units:
            raise ValueError(f"orderbook_units empty for {r.get('market')} at {r.get('timestamp')}")
        u = units[0]
        row = {"market": str(r["market"]), **_base(rec, r["timestamp"])}
        row.update(
            bid_price=_f(u["bid_price"]),
            bid_size=_f(u["bid_size"]),
            ask_price=_f(u["ask_price"]),
            ask_size=_f(u["ask_size"]),
            total_bid_size=_f(r.get("total_bid_size")),
            total_ask_size=_f(r.get("total_ask_size")),
        )
        out.append(row)
    return out


def _server_time(rec: RawRecord) -> list[Row]:
    p = cast(dict[str, Any], rec.payload)
    return [
        {
            "event_ns": rec.recv_ns,
            "received_ns": rec.recv_ns,
            "offset_ms": rec.offset_ms,
            "clock_suspect": rec.clock_suspect,
            "clock_offset_ms": _f(p.get("offset_ms")),
            "rtt_ms": int(cast(int, p["rtt_ms"])) if p.get("rtt_ms") is not None else None,
        }
    ]


NORMALIZERS: dict[str, Callable[[RawRecord], list[Row]]] = {
    "binance.um.book_ticker_1s": _book_ticker,
    "binance.um.force_order": _force_order,
    "binance.um.oi_hist": _metric(
        {"sumOpenInterest": "sum_open_interest", "sumOpenInterestValue": "sum_open_interest_value"}
    ),
    "binance.um.top_ls_account": _metric(_RATIO),
    "binance.um.top_ls_position": _metric(_RATIO),
    "binance.um.global_ls_account": _metric(_RATIO),
    "binance.um.taker_ls": _metric({"buySellRatio": "buy_sell_ratio", "buyVol": "buy_vol", "sellVol": "sell_vol"}),
    "binance.um.basis": _metric(
        {
            "indexPrice": "index_price",
            "futuresPrice": "futures_price",
            "basis": "basis",
            "basisRate": "basis_rate",
            "annualizedBasisRate": "annualized_basis_rate",
        },
        key="pair",
    ),
    "binance.server_time": _server_time,
    "upbit.trades": _upbit_trades,
    "upbit.orderbook_l1_1s": _upbit_orderbook,
}

# Dedupe/version keys used by the lake reader (bitemporal: identical repeats collapse to the first
# receipt; a changed value under the same key is kept as a later version).
VERSION_KEYS: dict[str, tuple[str, ...]] = {
    "binance.um.oi_hist": ("symbol", "event_ns"),
    "binance.um.top_ls_account": ("symbol", "event_ns"),
    "binance.um.top_ls_position": ("symbol", "event_ns"),
    "binance.um.global_ls_account": ("symbol", "event_ns"),
    "binance.um.taker_ls": ("symbol", "event_ns"),
    "binance.um.basis": ("symbol", "event_ns"),
    "upbit.trades": ("market", "sequential_id"),
}


def normalize(dataset_id: str, rec: RawRecord) -> list[Row]:
    fn = NORMALIZERS.get(dataset_id)
    if fn is None:
        raise KeyError(f"no normalizer for dataset {dataset_id!r}")
    return fn(rec)
