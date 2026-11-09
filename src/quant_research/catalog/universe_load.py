"""Instrument rows and point-in-time universe membership (design v2.1 §5.3, §5.4; phase-1 completion 4b).

Sources, all already captured:
- Binance UM exchangeInfo snapshot -> instrument rows (contract-size prefix such as 1000SHIB becomes
  base SHIB with multiplier 1000; settled contracts get delist_ns). Universe symbols that are no longer
  listed get a row whose base comes from the symbol (onboard unknown).
- bulk universe report spans -> rule `binance.um.pit_top100.v1` (month starts, end exclusive).
- collector.universe records (one per collector start) -> rules `binance.um.collector_top.v1` and
  `upbit.collector_krw.v1`: what the live collectors actually covered, as of each start.
Lineage links (MATIC -> POL) need a curated source and are not created here.
Idempotent: instruments are found by (venue, symbol, market type); memberships by their primary key.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from quant_research.catalog.store import Catalog, Instrument, MarketType

PERPETUAL_DELIVERY_MS = 4_000_000_000_000  # perpetuals carry a far-future deliveryDate (year 2100)
_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)([A-Z].*)$")
_MULTIPLIER = {"1000000": 1e6, "100000": 1e5, "10000": 1e4, "1000": 1e3, "1M": 1e6}
PIT_RULE = "binance.um.pit_top100.v1"
COLLECTOR_BINANCE_RULE = "binance.um.collector_top.v1"
COLLECTOR_UPBIT_RULE = "upbit.collector_krw.v1"

Span = tuple[int, int | None]


def split_multiplier(asset: str) -> tuple[str, float]:
    m = _PREFIX.match(asset)
    return (m.group(2), _MULTIPLIER[m.group(1)]) if m else (asset, 1.0)


def month_start_ns(month: str) -> int:
    y, mo = (int(x) for x in month.split("-"))
    return int(datetime(y, mo, 1, tzinfo=UTC).timestamp()) * 10**9


def binance_um_instrument(s: Mapping[str, object]) -> Instrument:
    base, mult = split_multiplier(str(s["baseAsset"]))
    contract = str(s["contractType"])
    market: MarketType = "usdm_perp" if contract.endswith("PERPETUAL") else "usdm_future"
    delivery = int(str(s["deliveryDate"]))
    delisted = s.get("status") != "TRADING" and delivery < PERPETUAL_DELIVERY_MS
    return Instrument(
        "binance",
        str(s["symbol"]),
        base,
        str(s["quoteAsset"]),
        market,
        onboard_ns=int(str(s["onboardDate"])) * 10**6,
        delist_ns=delivery * 10**6 if delisted else None,
        contract_multiplier=mult,
    )


def collector_spans(records: Sequence[tuple[int, Sequence[str]]]) -> dict[str, list[Span]]:
    """Membership spans from successive universe snapshots (each valid until the next one)."""
    spans: dict[str, list[Span]] = {}
    open_from: dict[str, int] = {}
    for at, members in sorted(records, key=lambda r: r[0]):
        now = set(members)
        for sym in [s for s in open_from if s not in now]:
            spans.setdefault(sym, []).append((open_from.pop(sym), at))
        for sym in sorted(now - set(open_from)):
            open_from[sym] = at
    for sym, start in open_from.items():
        spans.setdefault(sym, []).append((start, None))
    return {k: sorted(v) for k, v in sorted(spans.items())}


@dataclass(frozen=True, slots=True)
class LoadResult:
    instruments: int
    memberships: int


def load_universe(
    catalog: Catalog,
    exchange_symbols: Sequence[Mapping[str, object]],
    pit_spans: Mapping[str, Sequence[Sequence[str | None]]],  # universe report "spans": [start, end|null] months
    upbit_markets: Sequence[Mapping[str, object]],
    collector_universes: Sequence[tuple[int, Mapping[str, Sequence[str]]]],
) -> LoadResult:
    added = members = 0
    ids: dict[tuple[str, str], int] = {
        (str(r[0]), str(r[1])): int(r[2])
        for r in catalog.conn.execute("select venue, venue_symbol, instrument_id from instrument")
    }

    def ensure(i: Instrument) -> int:
        nonlocal added
        key = (i.venue, i.venue_symbol)
        if key not in ids:
            ids[key] = catalog.add_instrument(i)
            added += 1
        return ids[key]

    def member(instrument_id: int, rule: str, span: Span) -> None:
        nonlocal members
        try:
            catalog.add_membership(instrument_id, rule, from_ns=span[0], to_ns=span[1])
            members += 1
        except sqlite3.IntegrityError:
            pass  # already loaded (primary key instrument, rule, from)

    with catalog.transaction():
        for s in exchange_symbols:
            ensure(binance_um_instrument(s))
        for symbol, sym_spans in sorted(pit_spans.items()):
            iid = ensure(Instrument("binance", symbol, symbol.removesuffix("USDT"), "USDT", "usdm_perp"))
            for start, end in sym_spans:
                member(iid, PIT_RULE, (month_start_ns(str(start)), None if end is None else month_start_ns(str(end))))
        for m in upbit_markets:
            quote, _, base = str(m["market"]).partition("-")
            if quote == "KRW":
                ensure(Instrument("upbit", str(m["market"]), base, "KRW", "spot"))
        for rule, venue, field in (
            (COLLECTOR_BINANCE_RULE, "binance", "binance_um"),
            (COLLECTOR_UPBIT_RULE, "upbit", "upbit_krw"),
        ):
            for symbol, sym_spans in collector_spans([(at, u.get(field, [])) for at, u in collector_universes]).items():
                if (venue, symbol) not in ids:
                    continue  # not in the reference snapshots (should not happen; counted by the caller)
                for span in sym_spans:
                    member(ids[(venue, symbol)], rule, span)
    return LoadResult(added, members)
