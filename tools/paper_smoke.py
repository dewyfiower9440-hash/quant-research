"""v2.2 B11: paper-trading infrastructure check on the real lake with a dummy strategy (no validated strategy needed).

    uv run python tools/paper_smoke.py [--hours 12] [--out docs/measurements/b11-paper-smoke.md]

Runs `paper.run.tick` once per hour over the newest `--hours` hours the lake covers, with the v1 trend template
(equal weights, top-5 by liquidity: a stand-in, not a candidate), an EPHEMERAL HMAC policy key (never the real one)
and a fresh ledger under qr-data/paper/smoke-<UTC time>. It checks what a live paper run depends on: orders are
planned and settled from lake quotes, positions reconcile with fills, the ledger's hash chain verifies, and the
scheduled-task side (heartbeat, STOP file) is left to tools/register_paper_task.ps1 (user-run). Nothing is written
to qr-data/run, so the monitor does not start expecting a paper heartbeat."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    os.environ.setdefault("POLARS_MAX_THREADS", "2")
    from quant_research.capture.run import repo_root
    from quant_research.config import load_settings
    from quant_research.paper.feed import LakeFeed
    from quant_research.paper.ledger import PaperLedger
    from quant_research.paper.run import PaperConfig, Strategy, tick
    from quant_research.risk.limits import Limits
    from quant_research.risk.policy import FakeVerifier, Policy, PolicyStore, policy_bytes
    from quant_research.strategy.canonical import spec_hash
    from quant_research.strategy.spec import StrategySpec
    from quant_research.strategy.templates import build

    settings = load_settings()
    root = settings.data_root
    feed = LakeFeed(root)
    top = feed.coverage_ns()
    if top is None:
        print(json.dumps({"refused": "the lake has no compacted quotes"}))
        return 1
    last = top // HOUR_NS * HOUR_NS
    first = last - a.hours * HOUR_NS
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    folder = root / "paper" / f"smoke-{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    clock = {"t": first}
    ledger = PaperLedger.open(folder / "ledger.db", now_ns=lambda: clock["t"])
    verifier = FakeVerifier(os.urandom(32))  # ephemeral: this check never uses the real policy key
    policy = PolicyStore(verifier)
    limits = Limits(50_000.0, 20_000.0, 3.0, 5_000.0, 5_000.0, 5_000.0, 100, 20_000.0)
    message = policy_bytes(Policy(1, limits, first - HOUR_NS, last + DAY_NS))
    policy.apply(message, verifier.sign(message), now_ns=first)
    body = build("trend", {"fast": 24, "slow": 96}, n=5).model_dump(mode="json")
    body["sizing"] = {**body["sizing"], "method": "equal"}
    spec = StrategySpec.model_validate(body)
    strategy = Strategy(spec_hash(spec), spec, "Paper")
    cfg = PaperConfig(f"smoke-{stamp}", lookback_days=10)
    ticks: list[dict[str, object]] = []
    try:
        for now in range(first, last + 1, HOUR_NS):
            clock["t"] = now + 5 * 60 * 10**9  # five minutes into the hour, like the scheduled task
            summary = tick(
                settings, cfg, ledger, policy, now_ns=clock["t"], symbols=SYMBOLS, strategies=[strategy], feed=feed
            )
            ticks.append({"now": datetime.fromtimestamp(clock["t"] / 1e9, UTC).isoformat(), **summary})
        chain_ok, chain_why = ledger.verify_chain()
        report: dict[str, object] = {
            "ledger": str(folder / "ledger.db"),
            "window": [
                datetime.fromtimestamp(first / 1e9, UTC).isoformat(),
                datetime.fromtimestamp(last / 1e9, UTC).isoformat(),
            ],
            "ticks": len(ticks),
            "submitted": sum(int(str(t.get("submitted", 0))) for t in ticks),
            "open_orders_left": len(ledger.open_orders()),
            "positions": {f"{k[0][:8]}:{k[1]}": q for k, q in ledger.strategy_positions().items()},
            "reconcile": ledger.reconcile(),
            "hash_chain_ok": chain_ok,
            "hash_chain_detail": chain_why,
            "feed_files_read": feed.files_read,
            "feed_max_file_rows": feed.max_file_rows,
        }
    finally:
        ledger.close()
    out = a.out or repo_root() / "docs" / "measurements" / "b11-paper-smoke.md"
    lines = [
        "# B11 모의매매 인프라 점검 (tools/paper_smoke.py)",
        "",
        "> 실제 lake, 더미 전략(v1 trend, 동일 가중, top-5), 일회용 정책 키, 새 원장. 성과가 아닌 경로 점검.",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        *(f"| {k} | {json.dumps(v, ensure_ascii=False, default=str)} |" for k, v in report.items()),
        "",
        "## tick",
        "",
        *(f"- {json.dumps(t, ensure_ascii=False, default=str)}" for t in ticks),
        "",
    ]
    out.write_bytes("\n".join(lines).encode("utf-8"))
    print(json.dumps({k: report[k] for k in ("ticks", "submitted", "open_orders_left", "reconcile", "hash_chain_ok")}))
    return 0 if report["reconcile"] == [] and chain_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
