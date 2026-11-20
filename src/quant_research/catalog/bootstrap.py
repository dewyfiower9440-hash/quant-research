"""Seed the catalog with the T0 datasets, initial lag priors and the pre-registered T0 seal epoch (ADR-011).

Idempotent: datasets/policies that already exist are left untouched (rows are immutable by design).

    uv run python -m quant_research.catalog.bootstrap   # uses Settings.data_root/meta/catalog.db
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from quant_research.catalog.epochs import register_epoch2
from quant_research.catalog.store import Catalog, CatalogError, Dataset, LagPolicy, SealEpoch
from quant_research.config import load_settings

DAY_NS = 86_400 * 10**9
FIVE_MIN_MS = 300_000

# ADR-011: first T0 capture (supervisor start of the 1-hour live run, rest heartbeat started_ns).
T0_START_NS = 1_794_133_310_767_502_700
T0_RESEARCH_DAYS = 60
T0_HOLDOUT_DAYS = 30

T0_DATASETS: tuple[Dataset, ...] = (
    Dataset(
        "binance.um.book_ticker_1s", "binance", "event", None, "REST bookTicker 1 s sample, top-50 USDT perps (ADR-003)"
    ),
    Dataset(
        "binance.um.force_order", "binance", "event", None, "!forceOrder@arr; max 1 per symbol per second (undercount)"
    ),
    Dataset("binance.um.oi_hist", "binance", "unknown_interval", FIVE_MIN_MS, "openInterestHist 5m, 30-day API window"),
    Dataset("binance.um.top_ls_account", "binance", "unknown_interval", FIVE_MIN_MS, "topLongShortAccountRatio 5m"),
    Dataset("binance.um.top_ls_position", "binance", "unknown_interval", FIVE_MIN_MS, "topLongShortPositionRatio 5m"),
    Dataset(
        "binance.um.global_ls_account", "binance", "unknown_interval", FIVE_MIN_MS, "globalLongShortAccountRatio 5m"
    ),
    Dataset("binance.um.taker_ls", "binance", "unknown_interval", FIVE_MIN_MS, "takerlongshortRatio 5m"),
    Dataset("binance.um.basis", "binance", "unknown_interval", FIVE_MIN_MS, "futures basis 5m (not all symbols)"),
    Dataset("binance.um.exchange_info", "binance", "snapshot", None, "daily exchangeInfo (filters have no history)"),
    Dataset("binance.spot.exchange_info", "binance", "snapshot", None, "daily exchangeInfo spot"),
    Dataset("binance.um.ticker_24h", "binance", "snapshot", None, "daily 24h tickers futures"),
    Dataset("binance.spot.ticker_24h", "binance", "snapshot", None, "daily 24h tickers spot"),
    Dataset("binance.server_time", "collector", "event", None, "clock offset bursts (ADR-005)"),
    Dataset("upbit.orderbook_l1_1s", "upbit", "event", None, "orderbook count=1, 1 s sample, top-20 KRW"),
    Dataset("upbit.trades", "upbit", "event", None, "trades cursor catch-up (7-day API window)"),
    Dataset("upbit.markets", "upbit", "snapshot", None, "daily market list with warning/caution flags"),
)


ONE_MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
# ADR-013: bulk history seal. Research until 2026-07-01 (after the June 2026 knowledge cutoff of the
# research LLM, design v2.1 §9-6), sealed holdout 2026-07-01 .. 2026-11-01.
BULK_RESEARCH_UNTIL_NS = 1_782_864_000 * 10**9  # 2026-07-01T00:00:00Z
BULK_HOLDOUT_UNTIL_NS = 1_793_491_200 * 10**9  # 2026-11-01T00:00:00Z
BULK_DATASETS: tuple[Dataset, ...] = (
    *(
        Dataset(
            f"binance.um.bulk.{name}_1m",
            "binance",
            "interval_start",
            ONE_MIN_MS,
            f"data.binance.vision monthly {kind} 1m",
        )
        for name, kind in (
            ("klines", "klines"),
            ("mark_klines", "markPriceKlines"),
            ("index_klines", "indexPriceKlines"),
            ("premium_klines", "premiumIndexKlines"),
        )
    ),
    # plan step 9: REST backfill; a minute without a candle is `no_trade` (gap cause), not missing data
    Dataset("upbit.candles_1m", "upbit", "interval_start", ONE_MIN_MS, "Upbit REST 1m candles backfill, KRW top-N"),
    # phase-1 completion step 4: T1 extras (bulk/extras.py). Metrics stamps are of unknown meaning (Phase 0).
    Dataset("binance.um.bulk.funding", "binance", "event", None, "data.binance.vision monthly fundingRate"),
    Dataset(
        "binance.um.bulk.metrics_5m", "binance", "unknown_interval", FIVE_MIN_MS, "data.binance.vision daily metrics"
    ),
    Dataset("binance.um.bulk.book_depth", "binance", "event", None, "data.binance.vision daily bookDepth snapshots"),
    Dataset(
        "binance.spot.bulk.klines_1m", "binance", "interval_start", ONE_MIN_MS, "data.binance.vision spot monthly 1m"
    ),
    # v2.2 B4 tier T5: monthly aggTrades aggregated to 1m taker flow (bulk/flow.py); raw trades are not stored
    Dataset(
        "binance.um.bulk.agg_flow_1m",
        "binance",
        "interval_start",
        ONE_MIN_MS,
        "data.binance.vision monthly aggTrades -> 1m taker buy/sell qty and quote, trades, large (>=100k) quote",
    ),
    # v2.2 B4 tier T7: key-less external sources (bulk/external.py, docs/measurements/b4-t7-sources.md)
    Dataset("alternative.fng_1d", "external", "event", None, "Alternative.me Crypto Fear & Greed, daily 00:00 UTC"),
    Dataset("coinmetrics.asset_1d", "external", "interval_start", DAY_MS, "Coin Metrics Community 1d asset metrics"),
    Dataset("deribit.dvol_1h", "external", "interval_start", HOUR_MS, "Deribit DVOL volatility index, 1h candles"),
)
# Publication times of the external daily values are not documented: conservative lags (v2.1 §5.4 priors).
# Fear & Greed: the value stamped D 00:00 is taken as known at D+1 01:00; Coin Metrics: day D (interval start)
# known at D+2 00:00 (interval + one more day); DVOL: an hourly candle known 5 min after it closes.
BULK_LAG_NS: dict[str, int] = {
    "alternative.fng_1d": (DAY_MS + HOUR_MS) * 1_000_000,
    "coinmetrics.asset_1d": DAY_MS * 1_000_000,
    "deribit.dvol_1h": 5 * ONE_MIN_MS * 1_000_000,
}


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    datasets_added: int
    lag_policies_added: int
    seal_epochs_added: int


def initial_lag_ns(d: Dataset) -> int:
    """v2.1 §5.4 priors. Live-observed event data uses received_at (lag 0 on top). Interval data whose
    timestamp meaning is unknown is treated as interval START (the API adds the interval) plus one
    more interval as a conservative publication lag. Snapshots are known when received."""
    if d.time_semantics in ("unknown_interval", "interval_start", "interval_end") and d.interval_ms:
        return d.interval_ms * 1_000_000
    return 0


def tolerate_race(action: Callable[[], object], *, exists: Callable[[], bool]) -> bool:
    """Run one registration insert; True if it ran. Two tools may bootstrap a new dataset at once: the later
    insert fails although the row it wanted exists now, which is accepted (False); any other failure re-raises."""
    try:
        action()
    except (sqlite3.IntegrityError, CatalogError):
        if exists():
            return False
        raise
    return True


def bootstrap(c: Catalog) -> BootstrapResult:
    datasets = lags = seals = 0
    for d in T0_DATASETS:
        try:
            c.register_dataset(d)
            datasets += 1
        except sqlite3.IntegrityError:
            pass  # already registered; rows are not rewritten
        if c.conn.execute("select 1 from lag_policy where dataset_id = ?", (d.dataset_id,)).fetchone() is None:
            c.add_lag_policy(LagPolicy(d.dataset_id, initial_lag_ns(d), "v2.1 §5.4 prior (bootstrap)"))
            lags += 1
        if c.conn.execute("select 1 from seal_policy where dataset_id = ?", (d.dataset_id,)).fetchone() is None:
            c.add_seal_epoch(
                SealEpoch(
                    d.dataset_id,
                    1,
                    research_until_ns=T0_START_NS + T0_RESEARCH_DAYS * DAY_NS,
                    holdout_until_ns=T0_START_NS + (T0_RESEARCH_DAYS + T0_HOLDOUT_DAYS) * DAY_NS,
                    rationale="ADR-011 T0 pre-registration: first 60 days research, next 30 days sealed holdout",
                )
            )
            seals += 1
    for d in BULK_DATASETS:
        try:
            c.register_dataset(d)
            datasets += 1
        except sqlite3.IntegrityError:
            pass

        def lag_exists(ds: str = d.dataset_id) -> bool:
            return c.conn.execute("select 1 from lag_policy where dataset_id = ?", (ds,)).fetchone() is not None

        def seal_exists(ds: str = d.dataset_id) -> bool:
            return c.conn.execute("select 1 from seal_policy where dataset_id = ?", (ds,)).fetchone() is not None

        if not lag_exists():
            lag = BULK_LAG_NS.get(d.dataset_id, ONE_MIN_MS * 1_000_000)
            why = (
                "v2.2 B4 prior: undocumented publication (conservative)"
                if d.dataset_id in BULK_LAG_NS
                else ("v2.1 §5.4 prior: one decision bar (1m)")
            )
            policy = LagPolicy(d.dataset_id, lag, why)
            lags += tolerate_race(lambda p=policy: c.add_lag_policy(p), exists=lag_exists)
        if not seal_exists():
            epoch1 = SealEpoch(
                d.dataset_id,
                1,
                research_until_ns=BULK_RESEARCH_UNTIL_NS,
                holdout_until_ns=BULK_HOLDOUT_UNTIL_NS,
                rationale="ADR-013: research < 2026-07-01 (after LLM cutoff 2026-06), holdout to 2026-11-01",
            )
            seals += tolerate_race(lambda e=epoch1: c.add_seal_epoch(e), exists=seal_exists)
    # v2.2 B4 (ADR-040): a bulk dataset registered here starts on the ADR-013 epoch 1, under which research could
    # read the historical holdout; move it to epoch 2 at once (no-op for datasets already there)
    seals += len(register_epoch2(c, apply=True))
    return BootstrapResult(datasets, lags, seals)


def main() -> None:
    settings = load_settings()
    c = Catalog.open(settings.data_root / "meta" / "catalog.db")
    try:
        result = bootstrap(c)
        gaps = c.import_gaps(settings.data_root / "raw")
        print(f"bootstrap: {result}; gaps imported={gaps.imported} skipped_lines={gaps.skipped_lines}")  # CLI output
    finally:
        c.close()


if __name__ == "__main__":
    main()
