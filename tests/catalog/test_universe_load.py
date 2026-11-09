"""Plan step 4b (phase-1 completion): instrument lineage rows and point-in-time universe membership."""

from pathlib import Path

import pytest

from quant_research.catalog.store import Catalog
from quant_research.catalog.universe_load import (
    binance_um_instrument,
    collector_spans,
    load_universe,
    month_start_ns,
    split_multiplier,
)

JAN_2026 = 1_767_225_600 * 10**9


@pytest.mark.parametrize(
    ("asset", "expected"),
    [
        ("BTC", ("BTC", 1.0)),
        ("1INCH", ("1INCH", 1.0)),  # a token name that starts with a digit
        ("1000SHIB", ("SHIB", 1000.0)),
        ("1000000MOG", ("MOG", 1_000_000.0)),
        ("1MBABYDOGE", ("BABYDOGE", 1_000_000.0)),
    ],
)
def test_split_multiplier_reads_the_contract_size_prefix(asset: str, expected: tuple[str, float]) -> None:
    assert split_multiplier(asset) == expected


def test_month_start_is_utc_midnight_of_the_first_day() -> None:
    assert month_start_ns("2026-01") == JAN_2026


def test_binance_instrument_maps_contract_type_dates_and_delisting() -> None:
    perp = binance_um_instrument(
        {
            "symbol": "1000SHIBUSDT",
            "baseAsset": "1000SHIB",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": 1_620_000_000_000,
            "deliveryDate": 4_133_404_800_000,
            "status": "TRADING",
        }
    )
    assert (perp.base, perp.contract_multiplier, perp.market_type) == ("SHIB", 1000.0, "usdm_perp")
    assert perp.onboard_ns == 1_620_000_000_000 * 10**6 and perp.delist_ns is None
    settled = binance_um_instrument(
        {
            "symbol": "XUSDT",
            "baseAsset": "X",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": 1_600_000_000_000,
            "deliveryDate": 1_700_000_000_000,
            "status": "SETTLING",
        }
    )
    assert settled.delist_ns == 1_700_000_000_000 * 10**6
    quarter = binance_um_instrument(
        {
            "symbol": "BTCUSDT_261225",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "contractType": "CURRENT_QUARTER",
            "onboardDate": 1,
            "deliveryDate": 2,
            "status": "TRADING",
        }
    )
    assert quarter.market_type == "usdm_future" and quarter.delist_ns is None  # trading: not delisted


def test_collector_spans_follow_each_restart_universe() -> None:
    records = [(10, ["A", "B"]), (20, ["B", "C"]), (30, ["A", "B", "C"])]
    assert collector_spans(records) == {"A": [(10, 20), (30, None)], "B": [(10, None)], "C": [(20, None)]}


def test_load_universe_is_idempotent_and_answers_point_in_time_queries(tmp_path: Path) -> None:
    catalog = Catalog.open(tmp_path / "c.db")
    info = [
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "contractType": "PERPETUAL",
            "onboardDate": 1_567_965_300_000,
            "deliveryDate": 4_133_404_800_000,
            "status": "TRADING",
        }
    ]
    spans: dict[str, list[list[str | None]]] = {"BTCUSDT": [["2025-12", None]], "GONEUSDT": [["2025-11", "2026-01"]]}
    upbit = [{"market": "KRW-BTC"}, {"market": "BTC-ETH"}]  # only KRW markets are loaded
    collector = [(JAN_2026, {"binance_um": ["BTCUSDT"], "upbit_krw": ["KRW-BTC"]})]
    try:
        first = load_universe(catalog, info, spans, upbit, collector)
        again = load_universe(catalog, info, spans, upbit, collector)
        assert (first.instruments, first.memberships) == (3, 4)
        assert (again.instruments, again.memberships) == (0, 0)
        gone = catalog.conn.execute(
            "select instrument_id, base, onboard_ns from instrument where venue_symbol = 'GONEUSDT'"
        ).fetchone()
        assert gone[1] == "GONE" and gone[2] is None  # delisted, not in exchangeInfo: base from the symbol
        rule = "binance.um.pit_top100.v1"
        assert catalog.members(rule, at_ns=month_start_ns("2025-12")) == sorted(
            [
                int(gone[0]),
                int(
                    catalog.conn.execute(
                        "select instrument_id from instrument where venue_symbol='BTCUSDT'"
                    ).fetchone()[0]
                ),
            ]
        )
        assert catalog.members(rule, at_ns=month_start_ns("2026-01")) != []
        assert int(gone[0]) not in catalog.members(rule, at_ns=month_start_ns("2026-01"))  # end is exclusive
        assert len(catalog.members("upbit.collector_krw.v1", at_ns=JAN_2026)) == 1
    finally:
        catalog.close()
