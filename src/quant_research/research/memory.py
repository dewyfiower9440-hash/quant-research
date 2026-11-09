"""Research memory (design v2.1 §4 module I, §10): hypotheses, plans, findings with evidence, failures with
cause codes, cooldowns, and the anonymized summary the Director shows the LLM.

Rules:
- a finding without evidence references is refused (SQL check and here); a newer finding supersedes an old
  one by a one-time link, nothing is rewritten;
- any write error propagates: the loop must stop rather than continue without memory (§4 I);
- the summary contains research-split results only (every trial is research-split by construction: the
  point-in-time API refuses the holdout) and no real symbols or dates (aliases only, see anonymize.py);
- with `memory=off` (ablation, §9-9) the summary is empty and cooldowns are not applied.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from quant_research.research.store import ResearchStore

CauseCode = Literal[
    "weak_signal",
    "cost_erosion",
    "regime_dependent",
    "capacity",
    "missing_data",
    "overfit",
    "infeasible",
    "prior_knowledge",
]
CAUSE_CODES: tuple[CauseCode, ...] = (
    "weak_signal",
    "cost_erosion",
    "regime_dependent",
    "capacity",
    "missing_data",
    "overfit",
    "infeasible",
    "prior_knowledge",
)
MAX_TEXT = 2_000


class MemoryRuleError(ValueError):
    """A memory write that violates the memory rules (e.g. a finding without evidence)."""


def struct_key(family: str, template: str, required_data: Sequence[str], required_features: Sequence[str]) -> str:
    """Identity of a hypothesis by its structured fields (§10: duplicates are judged on structure, not prose)."""
    body = json.dumps(
        {"family": family, "template": template, "data": sorted(required_data), "features": sorted(required_features)},
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class FamilyStats:
    family: str
    cycles: int
    confirmed: int  # cycles whose confirmation-split run passed the Phase 3 bar (not a validation, §9)
    causes: dict[str, int]


@dataclass(frozen=True, slots=True)
class Summary:
    """What the LLM may see: aggregate, anonymized, research split only."""

    families: list[FamilyStats] = field(default_factory=lambda: list[FamilyStats]())
    findings: list[str] = field(default_factory=lambda: list[str]())  # untrusted text, quoted as data
    cooled_down: list[str] = field(default_factory=lambda: list[str]())  # struct keys not to repeat
    data_blocked: list[str] = field(default_factory=lambda: list[str]())  # families waiting for data

    def as_payload(self) -> dict[str, object]:
        return {
            "families": [
                {"family": f.family, "cycles": f.cycles, "confirmed": f.confirmed, "failure_causes": f.causes}
                for f in self.families
            ],
            "findings": self.findings,
            "cooled_down_hypotheses": self.cooled_down,
            "families_waiting_for_data": self.data_blocked,
        }


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    """Strip control characters and cap the length of untrusted text before it is stored or re-shown."""
    kept = "".join(ch for ch in text if ch == "\n" or (ch.isprintable() and ch != "\x7f"))
    return kept[:limit]


class MemoryStore:
    def __init__(self, store: ResearchStore, *, enabled: bool = True, cooldown_cycles: int = 5) -> None:
        self.store = store
        self.enabled = enabled
        self.cooldown_cycles = cooldown_cycles

    # ------------------------------------------------------------------ writes
    def record_finding(
        self, cycle_id: int, family: str, statement: str, evidence_refs: Sequence[str], *, hypothesis_id: int | None
    ) -> int:
        refs = [r for r in evidence_refs if r]
        if not refs:
            raise MemoryRuleError("a finding needs at least one evidence reference (design §4 I)")
        cur = self.store.conn.execute(
            "insert into finding(cycle_id, hypothesis_id, family, statement, evidence_refs_json, created_ns) "
            "values (?,?,?,?,?,?)",
            (cycle_id, hypothesis_id, family, clean_text(statement), json.dumps(refs), time.time_ns()),
        )
        return int(cur.lastrowid or 0)

    def supersede(self, old_id: int, new_id: int) -> None:
        if old_id == new_id:
            raise MemoryRuleError("a finding cannot supersede itself")
        cur = self.store.conn.execute(
            "update finding set superseded_by = ? where finding_id = ? and superseded_by is null", (new_id, old_id)
        )
        if cur.rowcount != 1:
            raise MemoryRuleError(f"finding {old_id} does not exist or is already superseded")

    def record_failure(
        self,
        cycle_id: int,
        family: str,
        cause: CauseCode,
        detail: str,
        evidence: Mapping[str, object],
        *,
        hypothesis_id: int | None,
    ) -> int:
        if cause not in CAUSE_CODES:
            raise MemoryRuleError(f"unknown cause code {cause!r}")
        cur = self.store.conn.execute(
            "insert into failure(cycle_id, hypothesis_id, family, cause_code, detail, evidence_json, created_ns) "
            "values (?,?,?,?,?,?,?)",
            (
                cycle_id,
                hypothesis_id,
                family,
                cause,
                clean_text(detail),
                json.dumps(evidence, sort_keys=True),
                time.time_ns(),
            ),
        )
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ reads
    def active_findings(self, family: str | None = None) -> list[tuple[int, str, str, list[str]]]:
        rows = self.store.conn.execute(
            "select finding_id, family, statement, evidence_refs_json from finding where superseded_by is null "
            "and (? is null or family = ?) order by finding_id",
            (family, family),
        ).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2]), cast(list[str], json.loads(r[3]))) for r in rows]

    def cooled_down(self, current_seq: int, run_id: str, causes: Sequence[str] | None = None) -> set[str]:
        """Struct keys of hypotheses that failed (optionally: with one of `causes`) within the last
        `cooldown_cycles` cycles of this run."""
        if not self.enabled:
            return set()
        rows = self.store.conn.execute(
            "select distinct h.struct_key, f.cause_code from hypothesis h join cycle c using (cycle_id) "
            "join failure f on f.cycle_id = h.cycle_id "
            "where c.run_id = ? and c.seq >= ? and c.seq < ?",
            (run_id, current_seq - self.cooldown_cycles, current_seq),
        ).fetchall()
        return {str(r[0]) for r in rows if causes is None or str(r[1]) in causes}

    def data_blocked(self) -> set[str]:
        rows = self.store.conn.execute(
            "select distinct family from failure where cause_code = 'missing_data' "
            "and family not in (select family from finding where statement like 'data available:%')"
        ).fetchall()
        return {str(r[0]) for r in rows}

    def family_stats(self, run_id: str) -> list[FamilyStats]:
        cycles = self.store.conn.execute(
            "select h.family, count(*), sum(case when exists (select 1 from transition t where t.cycle_id = h.cycle_id "
            "and t.to_state = 'VALIDATE') then 1 else 0 end) from hypothesis h join cycle c using (cycle_id) "
            "where c.run_id = ? group by h.family order by h.family",
            (run_id,),
        ).fetchall()
        causes = self.store.conn.execute(
            "select f.family, f.cause_code, count(*) from failure f join cycle c using (cycle_id) where c.run_id = ? "
            "group by 1, 2",
            (run_id,),
        ).fetchall()
        by_family: dict[str, dict[str, int]] = {}
        for fam, cause, n in causes:
            by_family.setdefault(str(fam), {})[str(cause)] = int(n)
        return [FamilyStats(str(f), int(n), int(ok or 0), by_family.get(str(f), {})) for f, n, ok in cycles]

    def summary(self, run_id: str, current_seq: int, *, max_findings: int = 10) -> Summary:
        if not self.enabled:
            return Summary()
        findings = [s for _i, _f, s, _e in self.active_findings()][-max_findings:]
        return Summary(
            families=self.family_stats(run_id),
            findings=findings,
            cooled_down=sorted(self.cooled_down(current_seq, run_id)),
            data_blocked=sorted(self.data_blocked()),
        )
