"""v2.2 B8 (research side): submit a campaign's candidates to the validator outbox.

    uv run python tools/submit_candidates.py --run-id c1 [--dry-run]

Reads qr-data/run/campaign-<run>.json, takes every batch whose cause is `candidate` (its S3 trial id) and writes one
`validation_request` per trial into research.db (holding run/research.lock: research.db has one writer). A trial
already submitted is skipped. The validator process (tools/run_validator.py) judges them; research learns one bit."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Mapping
from typing import cast


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", a.run_id):
        ap.error(f"run id must be 1-40 of [A-Za-z0-9_-], got {a.run_id!r}")
    from quant_research.capture.lock import AlreadyRunningError, InstanceLock
    from quant_research.config import load_settings
    from quant_research.research.store import ResearchStore
    from quant_research.search.campaign import CampaignState
    from quant_research.validator.process import submit_candidate
    from quant_research.validator.store import ValidatorError
    from quant_research.validator.verdict import DecisionRule

    root = load_settings().data_root
    saved = json.loads((root / "run" / f"campaign-{a.run_id}.json").read_text(encoding="utf-8"))
    state = CampaignState.from_json(saved["state"])
    trials: list[int] = []
    for h in state.history:
        summary = h.get("summary")
        if h.get("cause") == "candidate" and isinstance(summary, Mapping):
            trial = cast(Mapping[str, object], summary).get("s3_trial_id")
            if isinstance(trial, int):
                trials.append(trial)
    submitted: list[dict[str, int]] = []
    skipped: dict[str, str] = {}
    if a.dry_run or not trials:
        print(json.dumps({"run_id": a.run_id, "candidates": trials, "dry_run": a.dry_run}))
        return 0
    try:
        lock = InstanceLock(root / "run" / "research.lock").__enter__()
    except AlreadyRunningError as e:
        print(json.dumps({"refused": f"another research process holds run/research.lock: {e}"}))
        return 2
    research = ResearchStore.open(root / "meta" / "research.db")
    try:
        for trial in trials:
            try:
                rid = submit_candidate(research, trial, rule=DecisionRule(), now_ns=time.time_ns())
                submitted.append({"trial_id": trial, "request_id": rid})
            except ValidatorError as e:
                skipped[str(trial)] = str(e)
    finally:
        research.close()
        lock.__exit__(None, None, None)
    print(json.dumps({"run_id": a.run_id, "candidates": trials, "submitted": submitted, "skipped": skipped}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
