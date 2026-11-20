"""v2.2 B8 (validator side): judge the pending validation requests on the sealed holdouts.

    uv run python tools/run_validator.py

Reads research.db read-only (the outbox), writes only validator.db (submissions, access log, budget, verdicts,
acks) and validator-trials.db (its own trial ledger of the holdout runs). Holdout panels are built through the
panel store in the VALIDATOR role: H_hist [2023-01, 2024-12) and H_fwd [2026-07, 2026-11), each with the same
feature warm-up research gives (up to 1,200 bars of earlier rows, from 2020-01 at the earliest), the universe = the
point-in-time top-100 members, the spec's timeframe, and the stressed latencies the rule needs. The query budget is
the rule's k. Holds run/validator.lock (one validator at a time). Prints one line per request with its ack bit
(null when there is no verdict) - exactly what research can read; refusal reasons stay in validator.db."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

EARLIEST_NS = 1_577_836_800 * 10**9  # 2020-01-01: the first month of the lake history


def month_ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def month_floor(ns: int) -> int:
    d = datetime.fromtimestamp(ns // 10**9, UTC)
    return month_ns(d.year, d.month)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    import structlog

    from quant_research.backtest.panel import Panel, PanelConfig, slice_panel
    from quant_research.backtest.panel_store import PanelStore, Window, load_universe_panel
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.capture.run import git_sha, repo_root
    from quant_research.catalog.store import Catalog
    from quant_research.catalog.universe_load import PIT_RULE
    from quant_research.config import load_settings
    from quant_research.data.access import PointInTime
    from quant_research.research.ledger import TrialLedger
    from quant_research.research.store import ResearchStore
    from quant_research.search.campaign_runner import MAX_WARMUP_BARS
    from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec
    from quant_research.validator.process import H_FWD, H_HIST, HoldoutRole, process_pending
    from quant_research.validator.store import ValidatorStore, open_readonly
    from quant_research.validator.verdict import DecisionRule

    log = structlog.get_logger("run_validator")
    root = load_settings().data_root
    try:
        lock = InstanceLock(root / "run" / "validator.lock").__enter__()
    except AlreadyRunningError as e:
        print(json.dumps({"refused": f"another validator holds run/validator.lock: {e}"}))
        return 2
    catalog = Catalog.open(root / "meta" / "catalog.db")
    store = ValidatorStore.open(root / "meta" / "validator.db")
    trials = ResearchStore.open(root / "meta" / "validator-trials.db")
    research_ro = open_readonly(root / "meta" / "research.db")
    panels = PanelStore(root / "cache" / "panels")
    pit = PointInTime(catalog, role="validator")
    cache: dict[tuple[str, str, int], Panel] = {}

    def holdout(spec: StrategySpec, role: HoldoutRole, latency_ns: int) -> Panel:
        key = (spec.timeframe, role, latency_ns)
        if key not in cache:
            target = H_HIST if role == "hist" else H_FWD
            tf = TIMEFRAME_NS[spec.timeframe]
            start = max(EARLIEST_NS, month_floor(target.first_ns - MAX_WARMUP_BARS * tf))  # research's warm-up
            window = Window(start, target.end_ns)
            universe = load_universe_panel(
                pit, root, panels, window, PanelConfig(tf, latency_ns=latency_ns), top_m=None, rule=PIT_RULE
            )
            if universe.missing:  # holdout-side information: the validator's log, not stdout (ack bits only)
                log.warning("validator.members_without_klines", holdout=role, members=len(universe.missing))
            cache[key] = slice_panel(
                universe.panel, target.first_ns, target.end_ns - tf, warmup_ns=target.first_ns - window.first_ns
            )
        return cache[key]

    try:
        ledger = TrialLedger(trials, env_id="ENV-VALIDATOR", code_sha=git_sha(repo_root()))
        outcomes = process_pending(
            research_ro, store=store, ledger=ledger, catalog=catalog, holdout=holdout, rule=DecisionRule()
        )
        k, used = store.budget(2)
    finally:
        research_ro.close()
        trials.close()
        store.close()
        catalog.close()
        lock.__exit__(None, None, None)
    for o in outcomes:  # the ack bit only (null: no verdict); "error" requests are retried on the next run
        print(json.dumps({"request_id": o.request_id, "ack": o.passed, "retry": o.status == "error"}))
    print(json.dumps({"processed": len(outcomes), "epoch": 2, "k": k, "used": used}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
