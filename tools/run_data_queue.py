"""v2.3 step 5 (ADR-049 decision 5): consume the campaign's data request queue, one action per scheduled tick.

    uv run python tools/run_data_queue.py [--max-hours 2] [--universe C:/dev/qr-data/bulk/universe-YYYYMMDD.json]
        [--log-file F] [--dry-run]

Reads `run/data-queue.json` (written by tools/run_campaign.py) and keeps the per-tier state in
`run/data-queue-state.json` (the campaign rewrites the queue file, so the state lives apart). The first unfinished
tier gets one step (bulk/tiers.py):
- pilot: tools/bulk_extras.py --kinds K --top 2 --limit-months 1 [--since]; the lake bytes it added (that
  dataset's lake folder before/after) per symbol-month are projected to top x months, then checked against the
  200 GB lake cap and the 50 GB disk reserve. Over the cap -> `skipped_cap` (a user decision), else `piloted`.
- full: tools/bulk_extras.py --kinds K [--top N] [--since] (resumable from the catalog) or tools/fetch_external.py,
  killed at --max-hours (the next tick resumes it). New datasets are sealed in epoch 2 by the catalog bootstrap
  inside those tools before research can read them (B4).
Exit codes: 0 a step ran or nothing to do, 1 error, 2 another data queue tick holds run/data-queue.lock."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from quant_research.adapters.jobhost import Log, write_json
from quant_research.bulk.tiers import (
    TIERS,
    Action,
    TierState,
    after_exit,
    decide,
    next_action,
    parse_state,
    project,
    tier_for,
)
from quant_research.capture.lock import AlreadyRunningError, InstanceLock
from quant_research.config import load_settings

REPO = Path(__file__).resolve().parents[1]
KLINES_DATASET = "binance.um.bulk.klines_1m"
PILOT_TOP = 2


async def run_child(argv: list[str], timeout_s: float) -> int:
    """The child's exit code; -9 when it was killed at the tick budget (the next tick resumes from the catalog)."""
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(REPO))
    try:
        return await asyncio.wait_for(proc.wait(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -9


def symbol_months(root: Path, dataset_id: str, since: str | None = None) -> int:
    """Distinct (symbol, month) files the catalog holds for the dataset, from month `since` on (read-only)."""
    uri = f"file:{(root / 'meta' / 'catalog.db').as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        row = conn.execute(
            "select count(*) from (select distinct symbol, month from bulk_file where dataset_id = ? and month >= ?)",
            (dataset_id, since or ""),
        ).fetchone()
    return int(row[0])


def pause_reason(root: Path) -> str:
    """The pause reason tools/bulk_extras.py wrote to its progress file (empty when there is none)."""
    path = root / "run" / "bulk-extras.progress.json"
    if not path.exists():
        return ""
    note = cast(Mapping[str, object], json.loads(path.read_text(encoding="utf-8"))).get("note", "")
    return str(note)


def lake_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*.parquet")) if path.exists() else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hours", type=float, default=2.0)
    ap.add_argument("--universe", type=Path, default=None, help="default: the newest bulk/universe-*.json")
    ap.add_argument("--log-file", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true", help="print the next action and its command only")
    a = ap.parse_args()
    if a.max_hours <= 0:
        ap.error("--max-hours must be > 0")

    root = load_settings().data_root
    log = Log(a.log_file)
    queue_path, state_path = root / "run" / "data-queue.json", root / "run" / "data-queue-state.json"
    if not queue_path.exists():
        log("NOTHING QUEUED (run/data-queue.json does not exist yet)")
        log.close()
        return 0
    queue = [str(q) for q in json.loads(queue_path.read_text(encoding="utf-8")).get("next_data", [])]
    states = load_states(state_path)
    known = [q for q in queue if _known(q)]
    for q in queue:
        if q not in known:
            log(f"SKIP unknown request {q!r}")
    step = next_action(known, states)
    if step is None:
        log(f"ALL TIERS FINISHED {json.dumps({k: v.status for k, v in states.items()})}")
        log.close()
        return 0
    request, action = step
    tier = tier_for(request)
    universe = a.universe or max((root / "bulk").glob("universe-*.json"))
    report = json.loads(universe.read_text(encoding="utf-8"))
    if tier.source == "external":
        cmd = [sys.executable, str(REPO / "tools" / "fetch_external.py")]
    else:
        cmd = [sys.executable, str(REPO / "tools" / "bulk_extras.py"), "--universe", str(universe)]
        cmd += ["--kinds", str(tier.bulk_kind)]
        if action == "pilot":
            cmd += ["--top", str(PILOT_TOP), "--limit-months", "1"]
        elif tier.top is not None:
            cmd += ["--top", str(tier.top)]
        if tier.since is not None:
            cmd += ["--since", tier.since]
        if a.log_file is not None:
            cmd += ["--log-file", str(a.log_file)]
    log(f"STEP {request} {action}: {' '.join(cmd)}")
    if a.dry_run:
        log.close()
        return 0
    try:  # one queue tick at a time (a session run beside the scheduled task would lose state updates)
        lock = InstanceLock(root / "run" / "data-queue.lock").__enter__()
    except AlreadyRunningError as e:
        log(f"REFUSED another data queue tick is running: {e}")
        log.close()
        return 2
    try:
        return run_step(a, root, log, states, state_path, request, action, cmd, report)
    finally:
        lock.__exit__(None, None, None)


def run_step(
    a: argparse.Namespace,
    root: Path,
    log: Log,
    states: dict[str, TierState],
    state_path: Path,
    request: str,
    action: Action,
    cmd: list[str],
    report: Mapping[str, object],
) -> int:
    tier, state = tier_for(request), states.get(request, TierState())
    # the entry exists before the first file does: a campaign tick that starts now sees the tier as unready
    states[request] = state
    write_json(state_path, {k: asdict(v) for k, v in states.items()})
    started = time.monotonic()
    files_before = symbol_months(root, request) if tier.source == "bulk" else 0
    code = asyncio.run(run_child(cmd, a.max_hours * 3_600))
    progressed = tier.source == "bulk" and symbol_months(root, request) > files_before
    new = after_exit(state, action, code, progressed=progressed, gate_reason=pause_reason(root) if code == 3 else "")
    if action == "pilot" and code == 0 and tier.source == "bulk":
        # bytes per symbol-month from everything this dataset holds (an earlier pilot's files count too: a pilot
        # that found its month already there adds 0 bytes and must not project 0), scaled to what is still missing
        have = symbol_months(root, request)
        months = [m for m in cast(list[str], report["months"]) if tier.since is None or m >= tier.since]
        # a whole-universe tier downloads each symbol's member months: as many (symbol, month) files as the klines
        total = symbol_months(root, KLINES_DATASET, tier.since) if tier.top is None else tier.top * len(months)
        if have == 0:
            new = after_exit(state, action, 1, progressed=False)  # the pilot wrote nothing: a failed attempt
        else:
            projected = project(
                pilot_bytes=lake_bytes(root / "lake" / request), pilot_units=have, total_units=max(0, total - have)
            )
            # the cap counts the extras folders, like the download tool's own guard (tools/bulk_extras.py)
            extras = sum(lake_bytes(root / "lake" / d) for d in TIERS)
            ok, why = decide(projected=projected, lake_bytes=extras, free_disk=shutil.disk_usage(root).free)
            new = TierState("piloted" if ok else "skipped_cap", projected_bytes=projected, note=why)
    states[request] = new
    write_json(state_path, {k: asdict(v) for k, v in states.items()})
    write_json(
        root / "run" / "data-queue.progress.json",
        {
            "updated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": request,
            "action": action,
            "exit": code,
            "s": round(time.monotonic() - started, 1),
            "state": asdict(new),
        },
    )
    log(f"DONE {request} {action} exit={code} -> {new.status} ({new.note})")
    log.close()
    return 0


def _known(request: str) -> bool:
    try:
        tier_for(request)
    except ValueError:
        return False
    return True


def load_states(path: Path) -> dict[str, TierState]:
    """The per-tier state file (boundary: each entry parsed into a TierState; a bad file fails loudly)."""
    if not path.exists():
        return {}
    raw = cast(Mapping[str, Mapping[str, object]], json.loads(path.read_text(encoding="utf-8")))
    return {str(request): parse_state(str(request), entry) for request, entry in raw.items()}


if __name__ == "__main__":
    raise SystemExit(main())
