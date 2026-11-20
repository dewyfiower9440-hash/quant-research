"""Backtest runner (Phase 2 step 8): every evaluation goes through the trial ledger (design v2.1 §8) and
leaves a `backtest_run` row with everything needed to reproduce it (§9-5 fidelity, data version, lag
versions, seal epochs, cost settings, environment and code version via the trial).

Refusals are trials too (status `rejected` with the reason): S0 violations, L2 fidelity, path-dependent
rules on the vector engine, possible liquidations the vector engine cannot model, and average holding
below one hour (L2 needed, §9-5). Results carry flags for what is NOT modelled (e.g. funding without data).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import zlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

import numpy as np
import polars as pl

from quant_research.backtest.costs import CostModel
from quant_research.backtest.engines import EngineResult, EngineUnsupported, run_event, run_vector
from quant_research.backtest.market import F64, Fidelity, FidelityUnavailable, Targets, prepare
from quant_research.backtest.panel import Panel, select_top_liquidity
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import Stage, TrialHandle, TrialLedger, TrialOrigin
from quant_research.search.archive import exposure_metrics, probe_vector
from quant_research.strategy.canonical import spec_hash
from quant_research.strategy.compile import compile_spec
from quant_research.strategy.ops import OPS
from quant_research.strategy.s0 import check
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec

Engine = Literal["vector", "event"]
HOUR_NS = 3_600 * 10**9


@dataclass(frozen=True, slots=True)
class RunConfig:
    engine: Engine
    fidelity: Fidelity = "L1"
    cost: CostModel = field(default_factory=CostModel)
    capital: float = 10_000.0
    seed: int = 0


@dataclass
class RunOutcome:
    trial_id: int
    status: Literal["done", "rejected"]
    reason: str | None
    metrics: dict[str, object]
    result: EngineResult | None


def data_version(files: list[tuple[str, int]]) -> str:
    text = "\n".join(f"{path}:{size}" for path, size in sorted(files))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_datasets(spec: StrategySpec) -> list[str]:
    """Datasets whose lag policies the spec depends on (data operators plus the price data of the engine)."""
    found = {"binance.um.bulk.klines_1m", "binance.um.bulk.mark_klines_1m"}
    text = spec.model_dump_json()
    for name, op in OPS.items():
        if op.kind == "data" and f'"op":"{name}"' in text:
            found.update(op.datasets)
    return sorted(found)


def policy_versions(catalog: Catalog, datasets: list[str]) -> tuple[dict[str, int], dict[str, int]]:
    lags: dict[str, int] = {}
    seals: dict[str, int] = {}
    for d in datasets:
        lag = catalog.conn.execute("select max(version) from lag_policy where dataset_id = ?", (d,)).fetchone()[0]
        seal = catalog.conn.execute("select max(epoch) from seal_policy where dataset_id = ?", (d,)).fetchone()[0]
        if lag is not None:
            lags[d] = int(lag)
        if seal is not None:
            seals[d] = int(seal)
    return lags, seals


def daily_returns(decisions: np.ndarray, equity: F64, capital: float) -> F64:
    """Return per UTC day from the equity at each day's last decision (first day against the capital)."""
    days = decisions // (86_400 * 10**9)
    last = np.r_[np.nonzero(np.diff(days))[0], len(days) - 1]
    closes = equity[last]
    prev = np.r_[capital, closes[:-1]]
    return np.asarray(closes / prev - 1.0, dtype=np.float64)


def summary(decisions: np.ndarray, res: EngineResult, capital: float, timeframe_ns: int) -> dict[str, object]:
    daily = daily_returns(decisions, res.equity, capital)
    sd = float(np.std(daily, ddof=1)) if len(daily) > 1 else math.nan
    sr_day = float(np.mean(daily)) / sd if sd > 0 else math.nan
    peak = np.maximum.accumulate(res.equity)
    holding_h = (res.position_bars / res.entries) * timeframe_ns / HOUR_NS if res.entries else math.nan
    return {
        "final_equity": float(res.equity[-1]),
        "total_return": float(res.equity[-1] / capital - 1.0),
        "days": len(daily),
        "sr_daily": sr_day,  # period units (DSR inputs stay in period units, §8)
        "sr_annualised": sr_day * math.sqrt(365) if not math.isnan(sr_day) else math.nan,
        "max_drawdown": float(np.max(1.0 - res.equity / peak)),
        "costs": res.costs,
        "funding": res.funding,
        "trades": res.trades,
        "turnover": res.traded_notional / capital,
        "entries": res.entries,
        "avg_holding_hours": holding_h,
        "liquidations": res.liquidations,
        "stops": res.stops,
        "worst_case_total_return": float((res.equity[-1] - float(np.sum(res.gap_loss))) / capital - 1.0),
    }


def _blob(values: F64) -> bytes:
    return zlib.compress(np.asarray(values, dtype="<f8").tobytes())


def unblob(data: bytes) -> F64:
    return np.frombuffer(zlib.decompress(data), dtype="<f8").astype(np.float64)


def run_backtest(
    spec: StrategySpec,
    panel: Panel,
    catalog: Catalog,
    ledger: TrialLedger,
    config: RunConfig,
    *,
    stage: Stage = "smoke",
    fold: str | None = None,
    origin: TrialOrigin | None = None,
    tags: Mapping[str, object] | None = None,
) -> RunOutcome:
    """One trial per call. `tags` are stored with the trial metrics (e.g. the S1 -> S2 promotion kind)."""
    h, run_config = experiment_config(spec, panel, catalog, config)
    frame = panel.frame
    key = ledger.register_experiment(h, spec.model_dump_json(), run_config)
    with ledger.evaluate(key, h, stage, fold=fold, origin=origin) as trial:
        out = _evaluate(spec, panel, frame, catalog, ledger, config, trial, run_config)
        if tags:
            trial.metrics.update(tags)
            out.metrics.update(tags)
        return out


def experiment_config(
    spec: StrategySpec, panel: Panel, catalog: Catalog, config: RunConfig
) -> tuple[str, dict[str, object]]:
    """(spec hash, configuration) that identify an experiment (design §10 experiment key)."""
    h = spec_hash(spec)
    lags, seals = policy_versions(catalog, spec_datasets(spec))
    frame = panel.frame
    decisions = sorted(set(frame["decision_ns"].to_list()))
    run_config: dict[str, object] = {
        "data_version": data_version(panel.files),
        "lag_versions": lags,
        "seal_epochs": seals,
        "engine": config.engine,
        "fidelity": config.fidelity,
        "cost": asdict(config.cost),
        "capital": config.capital,
        "seed": config.seed,
        "panel": {
            "timeframe_ns": panel.config.timeframe_ns,
            "decision_delay_ns": panel.config.decision_delay_ns,
            "latency_ns": panel.config.latency_ns,
        },
        "window": [decisions[0], decisions[-1]] if decisions else [],
        "symbols": sorted(set(frame["symbol"].to_list())),
        "evaluate_from_ns": panel.evaluate_from_ns,
    }
    return h, run_config


def _evaluate(
    spec: StrategySpec,
    panel: Panel,
    frame: pl.DataFrame,
    catalog: Catalog,
    ledger: TrialLedger,
    config: RunConfig,
    trial: TrialHandle,
    run_config: dict[str, object],
) -> RunOutcome:
    violations = check(spec)
    if violations:
        trial.reject("S0: " + "; ".join(violations))
        return RunOutcome(trial.trial_id, "rejected", trial.rejected_reason, {}, None)
    if TIMEFRAME_NS[spec.timeframe] != panel.config.timeframe_ns:
        trial.reject(
            f"timeframe: the spec is {spec.timeframe} but the panel bars are {panel.config.timeframe_ns // 10**9} s"
        )
        return RunOutcome(trial.trial_id, "rejected", trial.rejected_reason, {}, None)
    if spec.universe.selector == "pit_top_liquidity":
        frame = select_top_liquidity(frame, spec.universe.n)
    stateless = spec.exit.time_stop_bars is None and spec.exit.stop_loss_atr is None
    try:
        compiled = compile_spec(spec, frame)
        market, targets = prepare(
            spec,
            compiled,
            frame,
            panel.funding,
            fidelity=config.fidelity,
            config_latency_ns=panel.config.latency_ns,
        )
        if config.engine == "vector":
            res = run_vector(
                market,
                targets,
                config.cost,
                capital=config.capital,
                fidelity=config.fidelity,
                n_universe=spec.universe.n,
                stateless=stateless,
                rebalance_bars=spec.execution.rebalance_bars,
                min_trade_weight=spec.execution.min_trade_weight,
            )
        else:
            res = run_event(
                market,
                targets,
                config.cost,
                capital=config.capital,
                fidelity=config.fidelity,
                n_universe=spec.universe.n,
                time_stop_bars=spec.exit.time_stop_bars,
                stop_loss_atr=spec.exit.stop_loss_atr,
                rebalance_bars=spec.execution.rebalance_bars,
                min_trade_weight=spec.execution.min_trade_weight,
            )
    except (FidelityUnavailable, EngineUnsupported) as e:
        trial.reject(str(e))
        return RunOutcome(trial.trial_id, "rejected", trial.rejected_reason, {}, None)
    decisions = market.decisions
    if panel.evaluate_from_ns is not None:  # warm-up rows: features only, no orders, not in the metrics
        start = int(np.searchsorted(decisions, panel.evaluate_from_ns, side="left"))
        res = replace(res, equity=res.equity[start:], gap_loss=res.gap_loss[start:])
        decisions = decisions[start:]
        targets = Targets(targets.weight[start:], targets.desire[start:])
        if abs(float(res.equity[0]) - config.capital) > 1e-6:
            raise RuntimeError(f"equity moved during warm-up ({float(res.equity[0])} != {config.capital})")
    metrics = summary(decisions, res, config.capital, TIMEFRAME_NS[spec.timeframe])
    metrics.update(exposure_metrics(targets.weight))  # MAP-Elites descriptors (search.archive)
    metrics["probe"] = probe_vector(targets.weight)
    flags: list[str] = []
    if config.fidelity == "L1" and spec.market == "usdm_futures" and not market.funding_known:
        flags.append("funding_not_modelled: no funding data loaded (OPS-003/006)")
    if config.fidelity == "L0":
        flags.append("L0: close fills without costs, research upper bound only")
    flags += res.notes
    metrics["flags"] = flags
    trial.metrics = metrics
    _record_run(ledger, trial.trial_id, config, run_config, res, decisions)
    if res.liquidation_unmodelled:
        trial.reject("vector engine: a liquidation was possible; evaluate with the event engine")
    else:
        holding = metrics["avg_holding_hours"]
        if isinstance(holding, float) and holding < 1.0:
            trial.reject(f"average holding {holding:.2f} h < 1 h needs L2 fidelity (aggTrades not collected)")
    status: Literal["done", "rejected"] = "rejected" if trial.rejected_reason else "done"
    return RunOutcome(trial.trial_id, status, trial.rejected_reason, metrics, res)


def _record_run(
    ledger: TrialLedger,
    trial_id: int,
    config: RunConfig,
    run_config: dict[str, object],
    res: EngineResult,
    decisions: np.ndarray,
) -> None:
    daily = daily_returns(decisions, res.equity, config.capital)
    worst_equity = res.equity - np.cumsum(res.gap_loss)
    worst = daily_returns(decisions, worst_equity, config.capital)
    lo, hi = int(decisions[0]), int(decisions[-1])
    ledger.store.conn.execute(
        "insert into backtest_run(trial_id, engine, fidelity, data_version, lag_versions_json, seal_epochs_json, "
        "cost_json, window_from_ns, window_to_ns, pnl_daily, worst_case_pnl_daily, created_ns) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trial_id,
            config.engine,
            config.fidelity,
            str(run_config["data_version"]),
            json.dumps(run_config["lag_versions"], sort_keys=True),
            json.dumps(run_config["seal_epochs"], sort_keys=True),
            json.dumps(run_config["cost"], sort_keys=True),
            int(lo),
            int(hi),
            _blob(daily),
            _blob(worst),
            time.time_ns(),
        ),
    )


def utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, UTC).strftime("%Y-%m-%d %H:%MZ")
