"""Paper trading process (Phase 6 step 16; design v2.1 §11 "paper=on", §4 K). One instance (lock), a
heartbeat, a STOP file, dependence on the collector heartbeat; one tick every `--interval` minutes:

  1. settle: every open paper order is matched against the quotes that arrived after it (partial fills,
     `max_wait`); unmatched after the wait -> canceled (paper venue: nothing is ever "uncertain" except on
     a crash between journal and projection, which the ledger surfaces at the next reconcile);
  2. decide: for every strategy in lifecycle state Validated / Paper (validator.db, read-only) whose
     holdout verdict passed, build the panel from the mid-price bars, compile the spec, take the latest
     complete decision's target weights, size them with the paper capital, check the risk policy
     (`risk.limits`, signed policy or refuse everything), submit idempotent orders;
  3. reconcile positions; write the heartbeat; record divergence inputs for the measurement report.

Strategy specs are read from research.db (the submitted S3 trial's experiment spec_json) read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import polars as pl
import structlog

from quant_research.backtest.market import prepare
from quant_research.backtest.panel import PanelConfig
from quant_research.backtest.run import unblob
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.run import Health
from quant_research.config import Settings, load_settings
from quant_research.log import configure_logging
from quant_research.paper.engine import plan_portfolio, simulate_fills, slippage_bps
from quant_research.paper.feed import (
    FeedWindow,
    LakeFeed,
    MarketFeed,
    bar_mid,
    latest_mid,
    panel_from_bars,
    quotes_after,
)
from quant_research.paper.ledger import OrderIntent, PaperLedger
from quant_research.paper.monitor import (
    StrategyFill,
    check_paper_health,
    daily_returns_from_fills,
    promote_validated,
)
from quant_research.risk.limits import Exposure, Limits, approve_in_order
from quant_research.risk.policy import (
    FakeVerifier,
    PolicyError,
    PolicyStore,
    RefuseAllVerifier,
    Verifier,
    parse_policy,
)
from quant_research.strategy.compile import compile_spec
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec
from quant_research.validator.lifecycle import LifecycleLedger
from quant_research.validator.store import ValidatorStore, open_readonly

log = structlog.get_logger(__name__)
STOP_FILE = "STOP"
HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
MINUTE_NS = 60 * 10**9
FILL_PAD_NS = 5 * 60 * 10**9  # quotes this long before the earliest arrival (reference mid for slippage)
DEFAULT_LIMITS = Limits(
    max_gross_notional=20_000.0,
    max_symbol_notional=8_000.0,
    max_leverage=2.0,
    max_daily_loss=500.0,
    max_weekly_loss=1_500.0,
    max_total_loss=3_000.0,
    max_orders_per_minute=30,
    max_order_notional=5_000.0,
)


@dataclass(frozen=True, slots=True)
class PaperConfig:
    run_id: str
    capital: float = 10_000.0
    latency_ns: int = 60 * 10**9  # decision -> arrival (one minute: the feed's resolution)
    max_wait_ns: int = 10 * 60 * 10**9
    lookback_days: int = 45
    min_notional: float = 10.0
    venue: str = "binance"


@dataclass(frozen=True, slots=True)
class Strategy:
    spec_hash: str
    spec: StrategySpec
    state: str
    expected: tuple[float, float] | None = None  # (mean, sd) of the confirmation-split daily returns (S3)


def approved_strategies(validator_db: Path, research_db: Path) -> list[Strategy]:
    """Strategies research may paper-trade: passed verdict + lifecycle Validated / Paper (both read-only)."""
    if not validator_db.exists():
        return []
    v = open_readonly(validator_db)
    r = sqlite3.connect(f"file:{research_db.as_posix()}?mode=ro", uri=True)
    try:
        states = {
            str(h): str(s)
            for h, s in v.execute("select spec_hash, state from strategy_state where state in ('Validated', 'Paper')")
        }
        passed = {
            str(row[0]): str(row[1])
            for row in v.execute(
                "select s.spec_hash, s.experiment_key from verdict x join submission s using (submission_id) "
                "where x.passed = 1"
            )
        }
        out: list[Strategy] = []
        for h, state in sorted(states.items()):
            if h not in passed:
                continue
            row = r.execute("select spec_json from experiment where spec_hash = ? limit 1", (h,)).fetchone()
            if row is None:
                continue
            spec = StrategySpec.model_validate_json(str(row[0]))
            out.append(Strategy(h, spec, state, _expected(r, passed[h])))
        return out
    finally:
        v.close()
        r.close()


def _expected(research: sqlite3.Connection, experiment_key: str) -> tuple[float, float] | None:
    """Mean and sd of the submitted experiment's S3 (confirmation) daily returns, the degradation baseline."""
    row = research.execute(
        "select b.pnl_daily from backtest_run b join trial t using (trial_id) where t.experiment_key = ? "
        "and t.stage = 'S3' and t.status = 'done' order by t.trial_id desc limit 1",
        (experiment_key,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    daily = unblob(bytes(row[0]))
    if daily.shape[0] < 2:
        return None
    return float(np.mean(daily)), float(np.std(daily, ddof=1))


def daily_closes(bars: pl.DataFrame, *, before_ns: int) -> dict[int, dict[str, float]]:
    """UTC day start -> symbol -> the day's last mid close, for complete days before `before_ns`."""
    day = pl.col("event_ns") // DAY_NS * DAY_NS
    last = (
        bars.filter(pl.col("event_ns") < before_ns // DAY_NS * DAY_NS)
        .with_columns(day.alias("day"))
        .sort(["symbol", "event_ns"])
        .group_by("day", "symbol", maintain_order=True)
        .agg(pl.col("close").last())
    )
    out: dict[int, dict[str, float]] = {}
    for d, s, c in last.select("day", "symbol", "close").iter_rows():
        out.setdefault(int(d), {})[str(s)] = float(c)
    return out


def paper_health(
    ledger: PaperLedger,
    lifecycle: LifecycleLedger,
    bars: pl.DataFrame,
    strategies: list[Strategy],
    cfg: PaperConfig,
    *,
    now_ns: int,
) -> dict[str, str]:
    """The daily degradation check of every Paper strategy (review finding 11); returns the moved ones."""
    closes = daily_closes(bars, before_ns=now_ns)
    capital_each = cfg.capital / max(1, len(strategies))
    moved: dict[str, str] = {}
    for st in strategies:
        fills = [StrategyFill(*f) for f in ledger.strategy_fills(st.spec_hash)]
        returns = daily_returns_from_fills(fills, closes, capital=capital_each)
        new = check_paper_health(
            lifecycle, st.spec_hash, returns, expected=st.expected, evidence_ref=f"paper:{cfg.run_id}"
        )
        if new is not None:
            moved[st.spec_hash] = new
    return moved


def health_due(feed: MarketFeed, *, now_ns: int, last_day: int | None) -> bool:
    """Once per UTC day, and only when the lake covers the whole previous day (it lags about an hour; a truncated
    last day could trip an irreversible Degraded move, review 3)."""
    today = now_ns // DAY_NS
    if last_day == today:
        return False
    midnight = today * DAY_NS
    return feed.covers(FeedWindow(midnight - HOUR_NS, midnight - 1))


def paper_verifier(data_root: Path) -> Verifier:
    """HMAC with <data_root>/paper/policy.key; without a (non-empty) key nothing verifies (fail closed)."""
    key = data_root / "paper" / "policy.key"
    secret = key.read_bytes() if key.exists() else b""
    if not secret:
        log.error("paper.policy_key_missing", path=str(key))
        return RefuseAllVerifier()
    return FakeVerifier(secret)


def load_policy(data_root: Path, store: PolicyStore, *, now_ns: int) -> None:
    """Paper policy = <data_root>/paper/policy.json + policy.sig (HMAC with <data_root>/paper/policy.key)."""
    folder = data_root / "paper"
    body, sig = folder / "policy.json", folder / "policy.sig"
    if body.exists() and sig.exists():
        message = body.read_bytes()
        if store.current is not None and parse_policy(message) == store.current:
            return  # the policy in force, re-read every tick: nothing to apply (review finding 1)
        store.apply(message, bytes.fromhex(sig.read_text(encoding="utf-8").strip()), now_ns=now_ns)


def target_weights(spec: StrategySpec, frame: pl.DataFrame, config: PanelConfig) -> dict[str, float]:
    """Latest complete decision's signed target weights per symbol (0 when the spec has no view)."""
    compiled = compile_spec(spec, frame)
    market, targets = prepare(
        spec, compiled, frame, _empty_funding(), fidelity="L1", config_latency_ns=config.latency_ns
    )
    if targets.weight.shape[0] == 0:
        return {}
    last = targets.weight[-1]
    return {s: float(w) for s, w in zip(market.symbols, last, strict=True) if math.isfinite(float(w)) and w != 0.0}


def _empty_funding() -> pl.DataFrame:
    return pl.DataFrame(schema={"symbol": pl.String, "event_ns": pl.Int64, "rate": pl.Float64, "mark": pl.Float64})


def tick(
    settings: Settings,
    cfg: PaperConfig,
    ledger: PaperLedger,
    policy: PolicyStore,
    *,
    now_ns: int,
    symbols: list[str],
    strategies: list[Strategy],
    feed: MarketFeed | None = None,
) -> dict[str, object]:
    """One paper step; pure apart from reading the feed and writing the ledger. Returns a summary.

    Raw quotes are read only for the fill window of open orders (earliest arrival - 5 min .. now); the
    decision panel comes from minute bars, which the feed builds one lake file at a time (analysis A-1)."""
    feed = feed if feed is not None else LakeFeed(settings.data_root)
    open_orders = ledger.open_orders()
    summary: dict[str, object] = {"quotes": 0, "settled": 0, "canceled": 0, "submitted": 0, "refused": 0}
    # 1. settle open orders against the quotes that arrived after them: one short window per order
    #    [arrival - pad, min(now, arrival + max_wait)] (simulate_fills never uses quotes after the wait).
    #    Arrival = the later of submission and decision, plus the latency: an order submitted at 10:05 for the
    #    09:00 decision is never filled against 09:01 quotes (review finding 6: that made paper a backtest).
    #    An order is settled ONCE, when the feed covers its whole fill window (the lake lags about an hour;
    #    canceling on the wall clock canceled every order before its quotes were readable, review 2 finding 1).
    for o in open_orders:
        row = ledger.conn.execute(
            "select decision_ns, run_id, spec_hash, venue, symbol, side, qty, limit_price, reduce_only, created_ns "
            "from order_ where client_id = ?",
            (o.client_id,),
        ).fetchone()
        decision_ns = int(row[0])
        arrive = max(decision_ns, int(row[9])) + cfg.latency_ns
        end = arrive + cfg.max_wait_ns
        if o.state == "PENDING_SUBMIT":
            ledger.ack(o.client_id, f"paper-{o.client_id[:8]}")
        if not feed.covers(FeedWindow(arrive - FILL_PAD_NS, end)):
            summary["waiting"] = int(cast(int, summary.get("waiting", 0))) + 1
            continue
        quotes = feed.quotes([o.symbol], FeedWindow(arrive - FILL_PAD_NS, end))
        summary["quotes"] = int(cast(int, summary["quotes"])) + quotes.height
        stream = quotes_after(quotes, o.symbol, arrive, until_ns=end)

        intent = OrderIntent(
            o.client_id,
            str(row[1]),
            str(row[2]),
            str(row[3]),
            o.symbol,
            o.side,
            o.qty - o.filled_qty,
            o.limit_price,
            bool(row[8]),
            decision_ns,
        )
        fills = simulate_fills(intent, stream, arrive_ns=arrive, max_wait_ns=cfg.max_wait_ns)
        for f in fills:
            ledger.fill(o.client_id, f.qty, f.price, fee=f.qty * f.price * 0.0005, liquidity=f.liquidity, at_ns=f.at_ns)
            summary["settled"] = int(cast(int, summary["settled"])) + 1
        still = ledger.order(o.client_id)
        if still is not None and still.state in ("ACKED", "PARTIALLY_FILLED"):  # the whole window was simulated
            ledger.cancel(o.client_id, "paper: no fill within max_wait")
            summary["canceled"] = int(cast(int, summary["canceled"])) + 1
        ref = latest_mid(quotes, o.symbol, arrive)
        if fills and ref:
            summary.setdefault("slippage_bps", []).append(slippage_bps(fills, ref, o.side))  # type: ignore[union-attr]
        elif fills:  # no quote within the pad before arrival (a collection gap): say so instead of dropping it
            summary["slippage_ref_missing"] = int(cast(int, summary.get("slippage_ref_missing", 0))) + 1
    problems = ledger.reconcile()
    if problems:
        summary["reconcile"] = problems
        log.error("paper.reconcile_mismatch", problems=problems)
        return summary  # §11: stop on a mismatch, no new orders
    # 2. decide for each approved strategy at the latest complete decision
    try:
        active = policy.active(now_ns=now_ns)
    except PolicyError as e:
        summary["policy"] = str(e)
        return summary
    config = PanelConfig(HOUR_NS, decision_delay_ns=0, latency_ns=0)
    last_decision = (now_ns // HOUR_NS) * HOUR_NS - HOUR_NS  # the last fully elapsed hour
    first_decision = last_decision - (cfg.lookback_days - 1) * DAY_NS
    hourly = [st for st in strategies if TIMEFRAME_NS[st.spec.timeframe] == HOUR_NS]  # the paper feed is hourly
    other = {st.spec_hash for st in strategies} - {st.spec_hash for st in hourly}
    # each strategy's own position + its open orders' remainder (an order not yet settled is not planned
    # again, review 2 finding 4); approved non-hourly strategies are left alone (not decided, not flattened)
    filled = {k: q for k, q in ledger.strategy_positions().items() if k[0] not in other}
    held = dict(filled)
    for k, q in ledger.strategy_open_qty().items():
        if k[0] not in other:
            held[k] = held.get(k, 0.0) + q
    if not hourly and not held:
        summary["bars"] = 0
        return summary
    bars = feed.bars(symbols, FeedWindow(now_ns - cfg.lookback_days * DAY_NS, now_ns))
    summary["bars"] = bars.height
    # decide only on bars that reach the decision hour (review 2 finding 5: the lake lags ~1 h); a stale
    # symbol gets no price, so nothing is traded in it, and with every symbol stale nothing is decided
    latest = dict(bars.group_by("symbol").agg(pl.col("available_ns").max()).iter_rows())
    fresh = [s for s in symbols if int(latest.get(s) or 0) >= last_decision - MINUTE_NS]
    stale = [s for s in symbols if s not in fresh]
    if stale:
        summary["stale"] = stale
    if not fresh:
        return summary
    panel = panel_from_bars(bars, symbols, config, first_decision_ns=first_decision, last_decision_ns=last_decision)
    prices = {s: m for s in fresh if (m := bar_mid(bars, s, last_decision)) is not None}
    capital_each = cfg.capital / max(1, len(hourly))  # equal capital per approved strategy
    targets = {
        st.spec_hash: {s: w * capital_each for s, w in target_weights(st.spec, panel.frame, config).items()}
        for st in hourly
    }
    intents = plan_portfolio(
        run_id=cfg.run_id,
        venue=cfg.venue,
        decision_ns=last_decision,
        targets=targets,
        positions=held,
        prices=prices,
        min_notional=cfg.min_notional,
        filled=filled,
    )
    intents = [i for i in intents if ledger.order(i.client_id) is None]  # an existing id is not re-checked
    exposure = account_exposure(ledger, prices, capital=cfg.capital, now_ns=now_ns)
    signed = [(i.symbol, i.qty * prices.get(i.symbol, 0.0) * (1 if i.side == "buy" else -1)) for i in intents]
    for intent, violations in zip(intents, approve_in_order(signed, exposure, active.limits), strict=True):
        if violations:
            summary["refused"] = int(cast(int, summary["refused"])) + 1
            log.warning("paper.order_refused", client_id=intent.client_id, violations=violations)
            continue
        before = ledger.order(intent.client_id)
        ledger.submit(intent)
        if before is None:
            summary["submitted"] = int(cast(int, summary["submitted"])) + 1
    return summary


def account_exposure(ledger: PaperLedger, prices: dict[str, float], *, capital: float, now_ns: int) -> Exposure:
    """The account as the risk limits see it (review finding 5): positions AND open orders' remaining notional;
    loss windows = realized since the window start + the current unrealized P&L (conservative: the whole
    unrealized loss counts in every window); the order rate counts the orders created in the last minute."""
    positions = ledger.positions()
    per = {s: p.qty * prices.get(s, p.avg_price) for s, p in positions.items()}
    for o in ledger.open_orders():
        remaining = (o.qty - o.filled_qty) * prices.get(o.symbol, 0.0) * (1 if o.side == "buy" else -1)
        per[o.symbol] = per.get(o.symbol, 0.0) + remaining
    unrealized = sum(p.qty * (prices.get(s, p.avg_price) - p.avg_price) for s, p in positions.items())
    total = sum(p.realized_pnl - p.fees for p in positions.values()) + unrealized
    return Exposure(
        equity=capital + total,
        gross_notional=sum(abs(v) for v in per.values()),
        per_symbol=per,
        daily_pnl=ledger.realized_since(now_ns - DAY_NS) + unrealized,
        weekly_pnl=ledger.realized_since(now_ns - 7 * DAY_NS) + unrealized,
        total_pnl=total,
        orders_last_minute=ledger.orders_since(now_ns - MINUTE_NS),
    )


async def loop(
    settings: Settings, cfg: PaperConfig, *, interval_s: float, duration_s: float | None, symbols: list[str]
) -> int:
    run_dir = settings.data_root / "run"
    health = Health("paper", run_dir / "paper.heartbeat.json")
    ledger = PaperLedger.open(settings.data_root / "meta" / "ledger.db")
    policy = PolicyStore(paper_verifier(settings.data_root))
    feed = LakeFeed(settings.data_root)  # one per process: the per-file bar cache lives across ticks
    validator_db = settings.data_root / "meta" / "validator.db"
    vstore = ValidatorStore.open(validator_db) if validator_db.exists() else None  # lifecycle moves (signer paper)
    lifecycle = LifecycleLedger(vstore.conn) if vstore is not None else None
    health_day: int | None = None
    started = time.monotonic()
    try:
        while True:
            if (run_dir / STOP_FILE).exists():
                log.info("paper.stop_file")
                break
            now_ns = time.time_ns()
            rest_age = _heartbeat_age_s(run_dir / "rest.heartbeat.json", now_ns)
            if rest_age is None or rest_age > 120:
                health.fail("collector", RuntimeError(f"rest heartbeat age {rest_age}"))
                log.warning("paper.collector_stale", age_s=rest_age)
            else:
                try:
                    load_policy(settings.data_root, policy, now_ns=now_ns)
                    strategies = approved_strategies(validator_db, settings.data_root / "meta" / "research.db")
                    traded = [s for s in strategies if TIMEFRAME_NS[s.spec.timeframe] == HOUR_NS]  # = tick's
                    if lifecycle is not None:
                        ref = f"paper:{cfg.run_id}"
                        promote_validated(lifecycle, [s.spec_hash for s in traded], evidence_ref=ref)
                        if health_due(feed, now_ns=now_ns, last_day=health_day):
                            bars = feed.bars(symbols, FeedWindow(now_ns - cfg.lookback_days * DAY_NS, now_ns))
                            moved = paper_health(ledger, lifecycle, bars, traded, cfg, now_ns=now_ns)
                            if moved:
                                log.warning("paper.degraded", moved=moved)
                            health_day = now_ns // DAY_NS
                    summary = tick(
                        settings, cfg, ledger, policy, now_ns=now_ns, symbols=symbols, strategies=strategies, feed=feed
                    )
                    health.ok["tick"] = health.ok.get("tick", 0) + 1
                    log.info("paper.tick", **{k: v for k, v in summary.items() if k != "slippage_bps"})
                except Exception as e:  # the job runner boundary: record and keep the loop alive
                    health.fail("tick", e)
                    log.exception("paper.tick_failed")
            health.write()
            if duration_s is not None and time.monotonic() - started >= duration_s:
                break
            await asyncio.sleep(interval_s)
    finally:
        ledger.close()
        if vstore is not None:
            vstore.close()
    return 0


def _heartbeat_age_s(path: Path, now_ns: int) -> float | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    at = body.get("at_ns")
    return (now_ns - int(at)) / 1e9 if isinstance(at, int) else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="quant_research.paper.run")
    ap.add_argument("--interval-minutes", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--run-id", default="paper-v1")
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,LINKUSDT")
    a = ap.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level, json=True)
    try:
        with InstanceLock(settings.data_root / "run" / "paper.lock"):
            code = asyncio.run(
                loop(
                    settings,
                    PaperConfig(a.run_id, capital=a.capital),
                    interval_s=a.interval_minutes * 60,
                    duration_s=a.duration,
                    symbols=[s for s in a.symbols.split(",") if s],
                )
            )
            raise SystemExit(code)
    except AlreadyRunningError as e:
        log.error("paper.already_running", error=str(e))
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
