"""ADR-056: recovery of a planted alpha through the whole campaign funnel. Pure part: which conditions are
measured, how one batch is reduced to stage flags, the recovery table and the pre-registered branch.

The I/O (synthetic markets, the campaign runner, the validator rule on a synthetic holdout) is in
`tools/measure_funnel_power.py`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
import numpy.typing as npt

from quant_research.search.multiplicity import psr, sharpe_stats
from quant_research.validator.null_pipeline import Alpha, oracle_pnl, wilson
from quant_research.validator.verdict import DecisionRule, sr_test

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
    variant: Mapping[str, object] | None = None,
    sizing: str | None = None,
    oracle: bool | None = None,
) -> dict[str, object]:
    """One line of the measurement's state file. `validated` is None when the batch was no candidate;
    `insufficient`: the holdout gave no verdict (ADR-042 preconditions or an incomplete run), which is neither a
    pass nor a statistical failure. `variant` (ADR-060): the run's space version and N rule, written on every
    row with the raw-N bar, the sizing of the batch's S3 point and whether the replication's oracle passed."""
    out: dict[str, object] = {
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
    if variant is not None:  # ADR-060: rows of the first measurement (ADR-056) keep their shape
        out |= {
            **variant,
            "pooled_threshold_raw": summary.get("pooled_threshold_raw"),
            "sizing": sizing,
            "oracle_pass": oracle,
        }
    return out


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


def branch(
    rows: Sequence[Mapping[str, object]],
    *,
    reps: int = REPS,
    null_reps: int | None = None,
    null_unit: Literal["batch", "replication"] = "batch",
) -> Branch:
    """ADR-056 decisions 8 and 9, as registered before the measurement. `incomplete` until every condition the
    rule reads has `reps` batches (the criterion cells and the whole null) and none of them failed to run: a batch
    that measured nothing would lower a recovery, or widen the null's denominator, without being evidence.
    ADR-060: the null has its own number of replications (`null_reps`) and its condition is read per replication
    (`null_unit`): the share of replications with at least one candidate among their seven cells."""
    table = recovery(rows)
    read: list[tuple[str, Cell]] = []
    wanted_null = reps if null_reps is None else null_reps
    for cond in null_conditions():
        cell = table.get(cond.key)
        if cell is None or cell.n < wanted_null:
            return Branch("incomplete", f"null {cond.family}: {0 if cell is None else cell.n} of {wanted_null} batches")
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
    if null_unit == "replication":
        null_n, null_hits = null_replications(rows)
        unit = "null replications with a candidate"
    else:
        null_n = sum(cell.n for name, cell in read if name.startswith("null "))
        null_hits = sum(cell.passed["candidate"] for name, cell in read if name.startswith("null "))
        unit = "null candidates"
    upper = wilson(null_hits / null_n, null_n)[1]
    if upper > NULL_UPPER:
        return Branch(
            "null_stop",
            f"{unit} {null_hits} of {null_n}: Wilson upper {upper:.3f} > {NULL_UPPER:.2f} (no branch)",
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


# ---- ADR-060: the re-measurement after step 8a (space v4, the cell-level N), beside the criterion of ADR-056

NULL_REPS_REMEASURE = 111  # decision 2: one null replication with a candidate still reads below 0.05, two do not
F64 = npt.NDArray[np.float64]


def criterion_conditions(zs: Sequence[float] = REQUIRED_Z) -> list[Condition]:
    """Decision 3: the cells the branch reads, in running order (the "before" run measures only these)."""
    return [Condition(alpha, z, PAIRS[alpha][0], "expressible") for z in zs for alpha in CRITERION_ALPHAS]


def oracle_passes(in_sample: F64, holdout: F64, *, bar: float, rule: DecisionRule) -> bool:
    """Decision 6: the oracle's pnl clears the pooled bar on the joined confirmation splits (daily SR above `bar`)
    and, on the synthetic holdout, the significance part of the validator rule (H0 rejected and the PSR above
    1 - alpha / k). No preconditions, no stress runs, no floor against the in-sample SR: a ceiling, not a strategy."""
    sd = float(np.std(in_sample, ddof=1)) if in_sample.shape[0] > 1 else 0.0
    if sd <= 0 or float(np.mean(in_sample)) / sd <= bar:
        return False
    test = sr_test(holdout, rule)
    if not test.reject:
        return False
    stats = sharpe_stats(holdout)
    return psr(stats.sr, 0.0, stats.skew, stats.kurt, stats.t) > 1 - rule.alpha_per_query


def oracle_rates(rows: Sequence[Mapping[str, object]]) -> dict[tuple[str, float], tuple[int, int]]:
    """(alpha, z) -> (replications with an oracle result, those where the oracle passed). The cells of a
    replication carry the same value: it is counted once."""
    seen: dict[tuple[str, float, int], bool] = {}
    for row in rows:
        value = row.get("oracle_pass")
        if isinstance(value, bool):
            seen[(str(row["alpha"]), float(cast(float, row["z"])), int(cast(int, row["rep"])))] = value
    out: dict[tuple[str, float], tuple[int, int]] = {}
    for (alpha, z, _rep), ok in seen.items():
        n, hit = out.get((alpha, z), (0, 0))
        out[(alpha, z)] = (n + 1, hit + ok)
    return out


def _over_raw_bar(row: Mapping[str, object]) -> bool:
    raw, pooled = row.get("pooled_threshold_raw"), row.get("pooled_sr")
    if raw is None:  # a run under the raw rule: its bar is the raw one
        return True
    return isinstance(raw, int | float) and isinstance(pooled, int | float) and float(pooled) > float(raw)


def as_raw_rule(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Decision 5: the same rows judged by the raw-N bar (the bar of ADR-049 as it was). A candidate of the cell
    rule that does not clear the raw bar is no candidate here, and so not validated either; its cause reads
    `below_bar`. The raw bar is never below the cell bar, so no batch becomes a candidate that was not one."""
    out: list[dict[str, object]] = []
    for row in rows:
        new = dict(row)
        if not _over_raw_bar(row):
            stages_ = dict(cast(Mapping[str, bool], row["stages"]))
            if stages_.get("candidate"):
                stages_["candidate"] = False
                stages_["validated"] = False
                new["stages"] = stages_
                new["cause"] = "below_bar"
        out.append(new)
    return out


def paired(
    before: Sequence[Mapping[str, object]], after: Sequence[Mapping[str, object]]
) -> dict[tuple[str, float, str], tuple[int, int, int, int]]:
    """Decision 7: per criterion cell (alpha, z, family), the replications both runs have, split into (recovered
    in both, before only, after only, neither). The same (alpha, z, replication) is the same market."""

    def recovered(rows: Sequence[Mapping[str, object]]) -> dict[tuple[str, float, str, int], bool]:
        return {
            (
                str(r["alpha"]),
                float(cast(float, r["z"])),
                str(r["family"]),
                int(cast(int, r["rep"])),
            ): cast(Mapping[str, bool], r["stages"]).get("validated") is True
            for r in rows
        }

    b, a = recovered(before), recovered(after)
    out: dict[tuple[str, float, str], tuple[int, int, int, int]] = {}
    for cond in criterion_conditions():
        both = only_b = only_a = neither = 0
        for key in sorted(set(b) & set(a)):
            if key[:3] != (cond.alpha, cond.z, cond.family):
                continue
            both += b[key] and a[key]
            only_b += b[key] and not a[key]
            only_a += a[key] and not b[key]
            neither += not b[key] and not a[key]
        out[(cond.alpha, cond.z, cond.family)] = (both, only_b, only_a, neither)
    return out


def _table(rows: Sequence[Mapping[str, object]], *, raw_column: bool) -> list[str]:
    table = recovery(rows)
    raw_table = recovery(as_raw_rule(rows))
    equal: dict[tuple[str, float, str, str], int] = {}
    for row in rows:
        if row.get("sizing") == "equal":
            equal[_key(row)] = equal.get(_key(row), 0) + 1
    head = "| 알파 | z | 역할 | 계열 | 배치 | 회수 | 회수율 | Wilson 95% | " + " | ".join(STAGES)
    head += " | 실행 실패 | 판정 불가 | 지연 스트레스 탈락 |"
    extra = 0
    if raw_column:
        head += " 원시 N 기준 후보 | 원시 N 기준 회수 | S3 점이 equal 비중 |"
        extra = 3
    lines = [head, "|---|---|---|---|---|---|---|---|" + "---|" * (len(STAGES) + 3 + extra)]
    for key in sorted(table, key=lambda k: (k[0] == "none", k[0], -k[1], k[2], k[3])):
        alpha, z, role, family = key
        cell = table[key]
        counts = " | ".join(str(cell.passed[st]) for st in STAGES)
        line = (
            f"| {alpha} | {z:g} | {role} | {family} | {cell.n} | {cell.recovered} | {cell.rate:.3f} | "
            f"{cell.low:.3f}~{cell.high:.3f} | {counts} | {cell.failed} | {cell.insufficient} | "
            f"{cell.lag_stress_failed} |"
        )
        if raw_column:
            raw = raw_table[key]
            line += f" {raw.passed['candidate']} | {raw.recovered} | {equal.get(key, 0)} |"
        lines.append(line)
    return lines


def render_remeasure(
    after: Sequence[Mapping[str, object]],
    before: Sequence[Mapping[str, object]],
    *,
    start_n: Mapping[str, int],
    start_pairs: int,
    strengths: Mapping[str, float],
    seconds_per_batch: float | None,
) -> str:
    """The table of the re-measurement (Korean report text; a measurement, not a performance claim)."""
    main = branch(after, null_reps=NULL_REPS_REMEASURE, null_unit="replication")
    raw = branch(as_raw_rule(after), null_reps=NULL_REPS_REMEASURE, null_unit="replication")
    null_reps, null_rep_hits = null_replications(after)
    rep_upper = wilson(null_rep_hits / null_reps, null_reps)[1] if null_reps else float("nan")
    null_rows = [r for r in after if r.get("alpha") == "none"]
    null_hits = sum(1 for r in null_rows if r.get("cause") == "candidate")
    batch_upper = wilson(null_hits / len(null_rows), len(null_rows))[1] if null_rows else float("nan")
    lines = [
        "# 8a 수정 뒤의 깔때기 회수율 재측정 (ADR-060, v2.5 단계 8a)",
        "",
        "> 공간 v4(포트폴리오 단위 변동성 목표, ADR-059)와 셀 단위 N(ADR-058 재검토)을 넣은 러너에 알파를 심은",
        "> 합성 일봉 시장을 넣고, 후보가 합성 holdout에서 검증기 규칙(ADR-042)까지 통과한 비율을 잰 측정이다.",
        "> 성과 주장이 아니다. 기준과 읽는 법은 측정 전에 ADR-060으로 등록했다. 주 판정은 ADR-056의 기준 그대로다.",
        "",
        f"- 시작 N(고정): 쌍의 수 {start_pairs}(이 측정의 기준), "
        f"원시 판정 수 {sum(start_n.values())} ({dict(start_n)})",
        f"- 알파 강도(오라클 z에 맞춘 값): {dict(strengths)}",
        f"- 배치당 시간: {'미측정' if seconds_per_batch is None else f'{seconds_per_batch:.1f}초'}",
        f"- 배치 수: 수정 뒤 {len(after)}, 수정 전 {len(before)}",
        f"- 주 판정(ADR-056 결정 8, 귀무는 ADR-060 결정 8): **{main.verdict}** — {main.detail}",
        f"- 원시 N 기준으로 읽은 같은 판정(병행 보고): **{raw.verdict}** — {raw.detail}",
        f"- 귀무, 반복 단위(주 기준): 후보가 하나라도 나온 반복 {null_rep_hits} / {null_reps}"
        + ("" if not null_reps else f" (Wilson 95% 상한 {rep_upper:.3f})"),
        f"- 귀무, 배치 단위(병기): 후보 {null_hits} / {len(null_rows)}"
        + ("" if not null_rows else f" (Wilson 95% 상한 {batch_upper:.3f})"),
        "",
        "## 수정 뒤: 회수율과 단계별 통과 수",
        "",
        *_table(after, raw_column=True),
        "",
        "## 보조 1: 오라클 대비 회수율 (ADR-060 결정 6, 판정에 쓰지 않는다)",
        "",
        "| 알파 | z | 반복 | 오라클 통과 | 통과율 | Wilson 95% | 셀별 회수율 ÷ 오라클 통과율 |",
        "|---|---|---|---|---|---|---|",
    ]
    table = recovery(after)
    for (alpha, z), (n, hit) in sorted(oracle_rates(after).items(), key=lambda kv: (kv[0][0], -kv[0][1])):
        low, high = wilson(hit / n, n)
        cells = [(key, cell) for key, cell in table.items() if key[:2] == (alpha, z)]
        ratios = (
            "오라클 통과 0: 비율 없음"
            if hit == 0
            else ", ".join(f"{key[3]} {cell.rate / (hit / n):.2f}" for key, cell in sorted(cells))
        )
        lines.append(f"| {alpha} | {z:g} | {n} | {hit} | {hit / n:.3f} | {low:.3f}~{high:.3f} | {ratios} |")
    lines += [
        "",
        "## 보조 2: 수정 전과의 비교 (ADR-060 결정 7, 판정에 쓰지 않는다)",
        "",
        "같은 (알파, z, 반복 번호)는 같은 시장이다. 수정 전은 공간 v3과 원시 N 규칙이다.",
        "",
        "| 알파 | z | 계열 | 둘 다 회수 | 전만 | 후만 | 둘 다 아님 |",
        "|---|---|---|---|---|---|---|",
    ]
    for (alpha, z, family), (both, only_b, only_a, neither) in paired(before, after).items():
        lines.append(f"| {alpha} | {z:g} | {family} | {both} | {only_b} | {only_a} | {neither} |")
    lines += ["", "### 수정 전: 회수율과 단계별 통과 수", "", *_table(before, raw_column=False)]
    lines += [
        "",
        "## 읽는 법",
        "- 단계는 누적이다. 한 단계를 통과하지 못한 배치는 그 뒤 단계에서도 세지 않는다.",
        "- `candidate`는 그 실행의 결합 기준(수정 뒤는 셀 단위 N)을 넘은 배치다.",
        "- `validated`는 검증기 규칙까지 통과한 배치다.",
        "- 원시 N 기준 열은 같은 배치를 옛 기준값(원시 판정 수의 N)으로 다시 읽은 수다.",
        "- 오라클은 비용이 없는 상한이다(전 종목 동일 가중, 결합 기준과 holdout의 H0·PSR만 적용).",
        "- 귀무(`none`) 행의 `candidate`가 거짓 후보다. 한 반복의 귀무 7셀은 같은 시장을 쓴다.",
        "- 수정의 효과는 보조 1과 보조 2로 읽는다. 결과를 본 뒤 기준을 바꾸지 않는다(ADR-060).",
        "- 드리프트형(`random`)은 분기 기준에 쓰지 않는다(대조군).",
        "",
    ]
    return "\n".join(lines)


# ---- the run rules of tools/measure_funnel_power.py (review of step 8a): which runs exist, when one is complete,
# and what a run may print. ADR-060 decision 9: nothing about pass counts leaves a run of the re-measurement before
# the whole registered design is there.

RunKind = Literal["first", "after", "before", "trial"]
Key = tuple[str, float, str, int]  # (alpha, z, family, replication)
FIRST_SEED = 56  # ADR-056
REMEASURE_SEED = 57  # ADR-060 decision 1
VARIANTS: dict[RunKind, tuple[int, str]] = {"after": (4, "cell"), "before": (3, "raw")}  # (space version, N rule)


def run_kind(
    *,
    space_version: int,
    n_rule: str,
    only_criterion: bool,
    seed: int,
    reps: int | None,
    null_reps: int | None,
    zs: Sequence[float],
    setup_given: bool,
    trial: bool,
    null: bool = False,
) -> RunKind:
    """Which registered run a command line is. `first`: the measurement of ADR-056 as it was. `after` / `before`:
    the two runs of ADR-060 (space v4 with the cell rule; the criterion cells on space v3 with the raw rule), with
    the registered seed and replications only. Anything else is refused (ValueError) unless it is an explicit
    `trial`, which never prints pass counts and never writes a report: a forgotten flag must not start an
    unregistered measurement on the registered markets."""
    if trial:
        return "trial"
    variant = (space_version, n_rule)
    kind: RunKind | None = None
    if variant == VARIANTS["after"] and not only_criterion:
        kind = "after"
    elif variant == VARIANTS["before"] and only_criterion:
        kind = "before"
    elif variant == VARIANTS["before"] and not setup_given and null_reps is None and seed == FIRST_SEED:
        return "first"
    if kind is None:
        raise ValueError(
            f"not a registered run: space version {space_version}, N rule {n_rule}, only_criterion {only_criterion}, "
            f"seed {seed} (ADR-056: v3 / raw / seed {FIRST_SEED}; ADR-060 after: v4 / cell; before: v3 / raw / "
            "--only-criterion). Use --trial for anything else"
        )
    problems: list[str] = []
    if seed != REMEASURE_SEED:
        problems.append(f"seed {seed} (registered: {REMEASURE_SEED})")
    if reps not in (None, REPS):
        problems.append(f"--reps {reps} (registered: {REPS})")
    if null_reps not in (None, NULL_REPS_REMEASURE):
        problems.append(f"--null-reps {null_reps} (registered: {NULL_REPS_REMEASURE})")
    if not set(zs) <= set(REQUIRED_Z):
        problems.append(f"--z {list(zs)} (registered: {list(REQUIRED_Z)})")
    if kind == "before" and not setup_given:
        problems.append("the before run reads the N and the strengths of the after run's setup file: give --setup")
    if kind == "before" and null:
        problems.append("--null (the before run measures the four criterion cells only)")
    if problems:
        raise ValueError(f"the {kind} run of ADR-060 is registered otherwise: " + "; ".join(problems))
    return kind


def key_of(row: Mapping[str, object]) -> Key:
    return str(row["alpha"]), float(cast(float, row["z"])), str(row["family"]), int(cast(int, row["rep"]))


def design_keys(kind: RunKind, *, zs: Sequence[float] = REQUIRED_Z, null: bool = True) -> list[Key]:
    """Every batch of a registered run of ADR-060 (the whole design by default; `zs` / `null` narrow it to what one
    call asks for)."""
    if kind == "before":
        return [(c.alpha, c.z, c.family, rep) for c in criterion_conditions(zs) for rep in range(REPS)]
    if kind != "after":
        raise ValueError(f"{kind}: only the runs of ADR-060 have a registered design here")
    out: list[Key] = []
    if null:
        out += [(c.alpha, c.z, c.family, rep) for c in null_conditions() for rep in range(NULL_REPS_REMEASURE)]
    out += [(c.alpha, c.z, c.family, rep) for c in conditions(zs) for rep in range(REPS)]
    return out


def variant_problem(rows: Sequence[Mapping[str, object]], kind: RunKind, variant: Mapping[str, object]) -> str | None:
    """Why this run may not append to a state file with these rows (None: it may). Rows of the first measurement
    carry no variant; a run of ADR-060 or a trial appends only to rows of exactly its own space version and N
    rule (decision 5)."""
    carrying = [r for r in rows if "space_version" in r or "n_rule" in r]
    if kind == "first":
        return f"{len(carrying)} rows of a re-measurement or a trial are in this state file" if carrying else None
    plain = len(rows) - len(carrying)
    mine = {**variant, "trial": kind == "trial"}  # a trial never appends to a registered run's file, nor back
    other = sum(1 for r in carrying if any(r.get(k, False) != v for k, v in mine.items()))
    if plain or other:
        return (
            f"{plain} rows without a variant and {other} rows of another space version, N rule or kind of run "
            f"than {mine} are in this state file"
        )
    return None


def report_problems(
    after: Sequence[Mapping[str, object]], before: Sequence[Mapping[str, object]], *, seed: int
) -> list[str]:
    """Why the report of ADR-060 may not be written yet (empty: it may). Both runs must hold their whole
    registered design exactly once, each in its registered variant, on the registered seed, and nothing else.
    Only batch counts are named."""
    out: list[str] = []
    if seed != REMEASURE_SEED:
        out.append(f"the setup was fixed with seed {seed}, not the registered {REMEASURE_SEED}")
    for kind, rows in (("after", after), ("before", before)):
        version, rule = VARIANTS[cast(RunKind, kind)]
        wrong = sum(1 for r in rows if (r.get("space_version"), r.get("n_rule")) != (version, rule))
        if wrong:
            out.append(f"{kind}: {wrong} rows are not space version {version} / N rule {rule}")
        if trials := sum(1 for r in rows if r.get("trial") is True):
            out.append(f"{kind}: {trials} rows are of a trial run")
        wanted = set(design_keys(cast(RunKind, kind)))
        have = {key_of(r) for r in rows}
        if doubled := len(rows) - len(have):  # two processes on one state file: every rate would be off
            out.append(f"{kind}: {doubled} batches are there more than once")
        if missing := len(wanted - have):
            out.append(f"{kind}: {missing} of {len(wanted)} batches are missing")
        if extra := len(have - wanted):
            out.append(f"{kind}: {extra} batches are outside the registered design")
    return out


def closing_line(
    kind: RunKind,
    rows: Sequence[Mapping[str, object]],
    *,
    ran: int,
    wanted: Sequence[Key],
    seconds_per_batch: float | None,
    report: str | None,
    waiting: Sequence[str] = (),
) -> dict[str, object]:
    """What a call prints when it ends. A run of ADR-060 or a trial names batch counts, failures and time only
    (decision 9); the first measurement keeps the line it always printed."""
    done = {key_of(r) for r in rows}
    failed = sum(1 for r in rows if r.get("cause") in FAILED_CAUSES)
    per_batch = None if seconds_per_batch is None else round(seconds_per_batch, 1)
    if kind == "first":
        return {
            "ran": ran,
            "batches": len(rows),
            "seconds_per_batch": per_batch,
            "candidates": sum(1 for r in rows if r.get("cause") == "candidate"),
            "errors": sum(1 for r in rows if r.get("cause") == "error"),
            "report": report,
        }
    return {
        "kind": kind,
        "ran": ran,
        "batches": len(rows),
        "this_call_missing": len(set(wanted) - done),
        "seconds_per_batch": per_batch,
        "failed": failed,
        "report": report,
        "report_waits_for": list(waiting),
    }


def oracle_tail(alpha: Alpha, closes: F64, days: int) -> F64:
    """Decision 6: the oracle's pnl on the live bars of the last `days` decisions of a synthetic market's close
    matrix (T x N). A synthetic panel repeats its last closes (the final decisions have no new bar): those
    decisions belong to the split but nothing moves on them, for the oracle as for any strategy. The window
    keeps the split's start and leaves the dead bars out, so it has `days` minus the dead bars."""
    rets = np.nan_to_num(np.diff(np.log(closes), axis=0), nan=0.0)
    live = np.flatnonzero(np.any(rets != 0.0, axis=1))
    if live.shape[0] == 0:
        return np.zeros(0, dtype=np.float64)
    dead = rets.shape[0] - (int(live[-1]) + 1)
    pnl = oracle_pnl(alpha, rets[: int(live[-1]) + 1])
    keep = days - dead
    return np.asarray(pnl[-keep:] if keep > 0 else pnl[:0], dtype=np.float64)
