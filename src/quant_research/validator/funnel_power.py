"""ADR-056: recovery of a planted alpha through the whole campaign funnel. Pure part: which conditions are
measured, how one batch is reduced to stage flags, the recovery table and the pre-registered branch.

The I/O (synthetic markets, the campaign runner, the validator rule on a synthetic holdout) is in
`tools/measure_funnel_power.py`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from quant_research.validator.null_pipeline import wilson

Verdict = Literal["sufficient", "low", "null_stop", "incomplete"]

REPS = 43  # decision 6: Wilson 95 % half-width <= 0.15 at a recovery of 0.5
REQUIRED_Z: tuple[float, ...] = (6.0, 4.0)  # decision 5: this order
OPTIONAL_Z: tuple[float, ...] = (2.0,)
# decision 4 as amended before the measurement (addenda 1 and 2): trend@1d is infeasible in space v3 (ADR-051), and
# the one-bar mean reversion cannot survive the validator's one-bar lag stress on daily bars, so the mechanism
# alpha is the slow one. alpha -> (a family that can express it, one that cannot)
PAIRS: dict[str, tuple[str, str]] = {
    "random": ("breakout", "xs_reversal"),
    "slow_reversion": ("mean_reversion", "momentum"),
    "cross": ("low_vol", "momentum"),
}
MODEL_CELLS: tuple[str, ...] = ("combo", "ridge")
# decision 8: the branch looks at these alphas' expressible cells only (the drift alpha is a control)
CRITERION_ALPHAS: tuple[str, ...] = ("slow_reversion", "cross")
THRESHOLDS: dict[float, float] = {4.0: 0.5, 6.0: 0.8}
NULL_UPPER = 0.05  # decision 9
STAGES: tuple[str, ...] = (
    "s1",
    "s2",
    "exposed",
    "s2_positive",
    "s3_positive",
    "cross_positive",
    "candidate",
    "validated",
)
# causes of a batch that did not measure anything: the run failed or the cell could not run
FAILED_CAUSES: tuple[str, ...] = ("error", "data_missing", "infeasible")


@dataclass(frozen=True, slots=True)
class Condition:
    alpha: str  # none / random / slow_reversion / cross
    z: float  # oracle z on the synthetic holdout (0 for the null)
    family: str
    role: str  # expressible / not_expressible / combo / ridge / null

    @property
    def key(self) -> tuple[str, float, str, str]:
        return self.alpha, self.z, self.role, self.family


def conditions(zs: Sequence[float] = REQUIRED_Z) -> list[Condition]:
    """Planted conditions, in running order: every alpha's four cells at the first z, then the next z."""
    out: list[Condition] = []
    for z in zs:
        for alpha, (can, cannot) in PAIRS.items():
            out.append(Condition(alpha, z, can, "expressible"))
            out.append(Condition(alpha, z, cannot, "not_expressible"))
            out.extend(Condition(alpha, z, model, model) for model in MODEL_CELLS)
    return out


def null_conditions() -> list[Condition]:
    """The null market, once per family that appears in a planted condition."""
    families = list(dict.fromkeys([f for pair in PAIRS.values() for f in pair] + list(MODEL_CELLS)))
    return [Condition("none", 0.0, f, "null") for f in families]


def _positive(value: object) -> bool:
    return isinstance(value, int | float) and float(value) > 0


def stages(summary: Mapping[str, object], cause: str, *, validated: bool | None) -> dict[str, bool]:
    """Where one batch got to. Cumulative: a stage holds only if every earlier one does, so the counts of a
    condition never rise from one stage to the next (the table shows where a planted alpha is lost).
    `s2_positive` is the selection-split SR of the best exposed point: the runner still runs an S3 for a batch
    whose selection SR is not positive, and without this stage such a batch would read as lost at the cross
    confirmation."""
    reached = [
        _positive(summary.get("s1_finite")),
        _positive(summary.get("s2_done")),
        _positive(summary.get("s2_exposed")),
        _positive(summary.get("best_sr_net")),
        _positive(summary.get("s3_sr")),
        _positive(summary.get("cross_sr")),
        cause == "candidate",
        validated is True,
    ]
    out: dict[str, bool] = {}
    alive = True
    for name, ok in zip(STAGES, reached, strict=True):
        alive = alive and ok
        out[name] = alive
    return out


def row_of(
    cond: Condition,
    rep: int,
    cause: str,
    summary: Mapping[str, object],
    *,
    validated: bool | None,
    insufficient: bool = False,
    reasons: Sequence[str] = (),
    seconds: float | None = None,
) -> dict[str, object]:
    """One line of the measurement's state file. `validated` is None when the batch was no candidate;
    `insufficient`: the holdout gave no verdict (ADR-042 preconditions or an incomplete run), which is neither a
    pass nor a statistical failure."""
    return {
        "alpha": cond.alpha,
        "z": cond.z,
        "family": cond.family,
        "role": cond.role,
        "rep": rep,
        "cause": cause,
        "stages": stages(summary, cause, validated=validated),
        "best_sr_net": summary.get("best_sr_net"),
        "s3_sr": summary.get("s3_sr"),
        "cross_sr": summary.get("cross_sr"),
        "pooled_sr": summary.get("pooled_sr"),
        "pooled_threshold": summary.get("pooled_threshold"),
        "validated": validated,
        "insufficient": insufficient,
        "lag_stress_failed": any("stress lag" in r for r in reasons),
        "reasons": list(reasons),
        "error": summary.get("error", ""),
        "seconds": seconds,
    }


def _key(row: Mapping[str, object]) -> tuple[str, float, str, str]:
    return str(row["alpha"]), float(cast(float, row["z"])), str(row["role"]), str(row["family"])


@dataclass(frozen=True, slots=True)
class Cell:
    n: int
    recovered: int
    low: float
    high: float
    passed: dict[str, int]  # batches that reached each stage
    failed: int = 0  # batches that measured nothing (FAILED_CAUSES)
    insufficient: int = 0  # candidates the holdout gave no verdict on
    lag_stress_failed: int = 0  # candidates rejected (also) by a lag stress

    @property
    def rate(self) -> float:
        return self.recovered / self.n if self.n else 0.0


def recovery(rows: Sequence[Mapping[str, object]]) -> dict[tuple[str, float, str, str], Cell]:
    """Per condition: batches, recovered ones (validated) with the Wilson 95 % interval, and the stage counts."""
    grouped: dict[tuple[str, float, str, str], list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(_key(row), []).append(row)
    out: dict[tuple[str, float, str, str], Cell] = {}
    for key, batch in grouped.items():
        flags = [cast(Mapping[str, bool], r["stages"]) for r in batch]
        passed = {stage: sum(1 for f in flags if f.get(stage)) for stage in STAGES}
        n, hit = len(batch), passed["validated"]
        low, high = wilson(hit / n, n)
        out[key] = Cell(
            n,
            hit,
            low,
            high,
            passed,
            failed=sum(1 for r in batch if r.get("cause") in FAILED_CAUSES),
            insufficient=sum(1 for r in batch if r.get("insufficient") is True),
            lag_stress_failed=sum(1 for r in batch if r.get("lag_stress_failed") is True),
        )
    return out


def null_replications(rows: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    """(null replications, those with at least one candidate). The seven null cells of a replication share its
    markets, so the replication is the independent unit; the registered stop reads batches (decision 9)."""
    by_rep: dict[int, bool] = {}
    for row in rows:
        if row.get("alpha") == "none":
            rep = int(cast(int, row["rep"]))
            by_rep[rep] = by_rep.get(rep, False) or row.get("cause") == "candidate"
    return len(by_rep), sum(1 for hit in by_rep.values() if hit)


@dataclass(frozen=True, slots=True)
class Branch:
    verdict: Verdict
    detail: str


def branch(rows: Sequence[Mapping[str, object]], *, reps: int = REPS) -> Branch:
    """ADR-056 decisions 8 and 9, as registered before the measurement. `incomplete` until every condition the
    rule reads has `reps` batches (the criterion cells and the whole null) and none of them failed to run: a batch
    that measured nothing would lower a recovery, or widen the null's denominator, without being evidence."""
    table = recovery(rows)
    read: list[tuple[str, Cell]] = []
    for cond in null_conditions():
        cell = table.get(cond.key)
        if cell is None or cell.n < reps:
            return Branch("incomplete", f"null {cond.family}: {0 if cell is None else cell.n} of {reps} batches")
        read.append((f"null {cond.family}", cell))
    needed: list[tuple[str, float, Cell]] = []
    for alpha in CRITERION_ALPHAS:
        for z in THRESHOLDS:
            cond = Condition(alpha, z, PAIRS[alpha][0], "expressible")
            cell = table.get(cond.key)
            if cell is None or cell.n < reps:
                return Branch("incomplete", f"{alpha} z={z:g}: {0 if cell is None else cell.n} of {reps} batches")
            needed.append((alpha, z, cell))
            read.append((f"{alpha} z={z:g}", cell))
    broken = [f"{name}: {cell.failed}" for name, cell in read if cell.failed]
    if broken:
        return Branch(
            "incomplete", "batches that did not run (error / data_missing / infeasible): " + "; ".join(broken)
        )
    null_n = sum(cell.n for name, cell in read if name.startswith("null "))
    null_hits = sum(cell.passed["candidate"] for name, cell in read if name.startswith("null "))
    upper = wilson(null_hits / null_n, null_n)[1]
    if upper > NULL_UPPER:
        return Branch(
            "null_stop",
            f"null candidates {null_hits} of {null_n}: Wilson upper {upper:.3f} > {NULL_UPPER:.2f} (no branch)",
        )
    misses = [f"{a} z={z:g}: {c.rate:.3f} < {THRESHOLDS[z]}" for a, z, c in needed if c.rate < THRESHOLDS[z]]
    if misses:
        return Branch("low", "; ".join(misses))
    return Branch("sufficient", "; ".join(f"{a} z={z:g}: {c.rate:.3f}" for a, z, c in needed))


def render(
    rows: Sequence[Mapping[str, object]],
    *,
    start_n: Mapping[str, int],
    strengths: Mapping[str, float],
    seconds_per_batch: float | None,
) -> str:
    """The measurement table (Korean report text; a measurement, not a performance claim)."""
    table = recovery(rows)
    decided = branch(rows)
    total_n = sum(start_n.values())
    null_reps, null_rep_hits = null_replications(rows)
    rep_upper = wilson(null_rep_hits / null_reps, null_reps)[1] if null_reps else float("nan")
    lines = [
        "# 깔때기 전체의 심은 알파 회수율 (ADR-056, v2.5 단계 6)",
        "",
        "> c2 러너(공간 v3, S1 60, S2 5 + 5%, 노출 5% 자격, S3, 교차 확인, 결합 기준)에 알파를 심은 합성 일봉",
        "> 시장을 넣고, 후보가 합성 holdout에서 검증기 규칙(ADR-042)까지 통과한 비율을 잰 측정이다.",
        "> 성과 주장이 아니다. 기준은 측정 전에 ADR-056과 그 보강으로 등록했다.",
        "",
        f"- 시작 N(측정 시작 시점의 누적 S3 판정 수, 고정): N {total_n} ({dict(start_n)})",
        f"- 알파 강도(오라클 z에 맞춘 값): {dict(strengths)}",
        f"- 배치당 시간: {'미측정' if seconds_per_batch is None else f'{seconds_per_batch:.1f}초'}",
        f"- 배치 수: {len(rows)}",
        f"- 분기 판정(ADR-056 결정 8·9): **{decided.verdict}** — {decided.detail}",
        f"- 귀무의 반복 단위: 후보가 하나라도 나온 반복 {null_rep_hits} / {null_reps}"
        + ("" if not null_reps else f" (Wilson 95% 상한 {rep_upper:.3f})"),
        "",
        "## 회수율과 단계별 통과 수",
        "",
        "| 알파 | z | 역할 | 계열 | 배치 | 회수 | 회수율 | Wilson 95% | "
        + " | ".join(STAGES)
        + " | 실행 실패 | 판정 불가 | 지연 스트레스 탈락 |",
        "|---|---|---|---|---|---|---|---|" + "---|" * (len(STAGES) + 3),
    ]
    for key in sorted(table, key=lambda k: (k[0] == "none", k[0], -k[1], k[2], k[3])):
        alpha, z, role, family = key
        cell = table[key]
        counts = " | ".join(str(cell.passed[s]) for s in STAGES)
        lines.append(
            f"| {alpha} | {z:g} | {role} | {family} | {cell.n} | {cell.recovered} | {cell.rate:.3f} | "
            f"{cell.low:.3f}~{cell.high:.3f} | {counts} | {cell.failed} | {cell.insufficient} | "
            f"{cell.lag_stress_failed} |"
        )
    lines += [
        "",
        "## 읽는 법",
        "- 단계는 누적이다. 한 단계를 통과하지 못한 배치는 그 뒤 단계에서도 세지 않는다.",
        "- `s2_positive`는 선택 구간에서 노출 자격을 넘은 최선의 순 SR이 양수인 배치다.",
        "- `candidate`는 결합 기준까지 넘은 배치다.",
        "- `validated`는 그 spec이 합성 holdout에서 검증기 규칙을 통과한 배치다.",
        "- 판정 불가는 holdout이 판정을 내리지 않은 후보다(진입 수·활성 일수·노출의 사전 조건 미달, 실행 미완료).",
        "- 지연 스트레스 탈락은 체결을 한 봉 또는 두 봉 늦췄을 때 SR이 0 이하가 된 후보다(일봉에서는 하루, 이틀).",
        "- 귀무(`none`) 행의 `candidate`가 거짓 후보다.",
        "  분기 판정은 배치 단위 비율의 Wilson 상한이 0.05 이하일 때만 한다.",
        "  한 반복의 귀무 7셀은 같은 시장을 쓰므로 독립 단위는 반복이다. 반복 단위 값을 위에 함께 적는다.",
        "- 드리프트형(`random`)은 분기 기준에 쓰지 않는다(대조군).",
        "",
    ]
    return "\n".join(lines)
