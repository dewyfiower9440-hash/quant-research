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

import polars as pl
import structlog

from quant_research.backtest.market import prepare
from quant_research.backtest.panel import PanelConfig
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.capture.run import Health
from quant_research.config import Settings, load_settings
from quant_research.log import configure_logging
from quant_research.paper.engine import plan_orders, simulate_fills, slippage_bps
from quant_research.paper.feed import FeedWindow, latest_mid, load_quotes, panel_from_quotes, quotes_after
from quant_research.paper.ledger import OrderIntent, PaperLedger
from quant_research.risk.limits import Exposure, Limits, check_order
from quant_research.risk.policy import FakeVerifier, PolicyError, PolicyStore
from quant_research.strategy.compile import compile_spec
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec
from quant_research.validator.store import open_readonly

log = structlog.get_logger(__name__)
STOP_FILE = "STOP"
HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
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
            str(row[0])
            for row in v.execute(
                "select s.spec_hash from verdict x join submission s using (submission_id) where x.passed = 1"
            )
        }
        out: list[Strategy] = []
        for h, state in sorted(states.items()):
            if h not in passed:
                continue
            row = r.execute("select spec_json from experiment where spec_hash = ? limit 1", (h,)).fetchone()
            if row is None:
                continue
            out.append(Strategy(h, StrategySpec.model_validate_json(str(row[0])), state))
        return out
    finally:
        v.close()
        r.close()


def load_policy(data_root: Path, store: PolicyStore, *, now_ns: int) -> None:
    """Paper policy = <data_root>/paper/policy.json + policy.sig (HMAC with <data_root>/paper/policy.key)."""
    folder = data_root / "paper"
    body, sig = folder / "policy.json", folder / "policy.sig"
    if body.exists() and sig.exists():
        store.apply(body.read_bytes(), bytes.fromhex(sig.read_text(encoding="utf-8").strip()), now_ns=now_ns)


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
) -> dict[str, object]:
    """One paper step; pure apart from reading the lake and writing the ledger. Returns a summary."""
    window = FeedWindow(now_ns - cfg.lookback_days * DAY_NS, now_ns)
    quotes = load_quotes(settings.data_root, symbols, window)
    summary: dict[str, object] = {"quotes": quotes.height, "settled": 0, "canceled": 0, "submitted": 0, "refused": 0}
    # 1. settle open orders against the quotes that arrived after them
    for o in ledger.open_orders():
        row = ledger.conn.execute(
            "select decision_ns, run_id, spec_hash, venue, symbol, side, qty, limit_price, reduce_only "
            "from order_ where client_id = ?",
            (o.client_id,),
        ).fetchone()
        decision_ns = int(row[0])
        arrive = decision_ns + cfg.latency_ns
        stream = quotes_after(quotes, o.symbol, arrive, until_ns=now_ns)
        if o.state == "PENDING_SUBMIT":
            ledger.ack(o.client_id, f"paper-{o.client_id[:8]}")

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
        if still is not None and still.state in ("ACKED", "PARTIALLY_FILLED") and now_ns > arrive + cfg.max_wait_ns:
            ledger.cancel(o.client_id, "paper: no fill within max_wait")
            summary["canceled"] = int(cast(int, summary["canceled"])) + 1
        ref = latest_mid(quotes, o.symbol, arrive)
        if fills and ref:
            summary.setdefault("slippage_bps", []).append(slippage_bps(fills, ref, o.side))  # type: ignore[union-attr]
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
    positions = {s: p.qty for s, p in ledger.positions().items()}
    for strat in strategies:
        if TIMEFRAME_NS[strat.spec.timeframe] != HOUR_NS:
            continue  # the paper feed is hourly for now
        panel = panel_from_quotes(
            quotes, symbols, config, first_decision_ns=first_decision, last_decision_ns=last_decision
        )
        if panel.frame.height == 0:
            continue
        weights = target_weights(strat.spec, panel.frame, config)
        prices = {s: m for s in symbols if (m := latest_mid(quotes, s, last_decision)) is not None}
        targets = {s: w * cfg.capital for s, w in weights.items()}
        intents = plan_orders(
            run_id=cfg.run_id,
            spec_hash=strat.spec_hash,
            venue=cfg.venue,
            decision_ns=last_decision,
            targets=targets,
            positions=positions,
            prices=prices,
            min_notional=cfg.min_notional,
        )
        gross = sum(abs(q * prices.get(s, 0.0)) for s, q in positions.items())
        per_symbol = {s: q * prices.get(s, 0.0) for s, q in positions.items()}
        realized = sum(p.realized_pnl - p.fees for p in ledger.positions().values())
        exposure = Exposure(cfg.capital + realized, gross, per_symbol, realized, realized, realized, 0)
        for intent in intents:
            notional = intent.qty * prices.get(intent.symbol, 0.0) * (1 if intent.side == "buy" else -1)
            violations = check_order(intent.symbol, notional, exposure, active.limits)
            if violations:
                summary["refused"] = int(cast(int, summary["refused"])) + 1
                log.warning("paper.order_refused", client_id=intent.client_id, violations=violations)
                continue
            before = ledger.order(intent.client_id)
            ledger.submit(intent)
            if before is None:
                summary["submitted"] = int(cast(int, summary["submitted"])) + 1
    return summary


async def loop(
    settings: Settings, cfg: PaperConfig, *, interval_s: float, duration_s: float | None, symbols: list[str]
) -> int:
    run_dir = settings.data_root / "run"
    health = Health("paper", run_dir / "paper.heartbeat.json")
    ledger = PaperLedger.open(settings.data_root / "meta" / "ledger.db")
    policy = PolicyStore(
        FakeVerifier(
            (settings.data_root / "paper" / "policy.key").read_bytes()
            if (settings.data_root / "paper" / "policy.key").exists()
            else b""
        )
    )
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
                    strategies = approved_strategies(
                        settings.data_root / "meta" / "validator.db", settings.data_root / "meta" / "research.db"
                    )
                    summary = tick(settings, cfg, ledger, policy, now_ns=now_ns, symbols=symbols, strategies=strategies)
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
