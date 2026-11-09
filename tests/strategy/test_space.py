"""Phase 4 step 1: pre-registered search space (parameter ranges + structural axes), its size and the
deterministic generator."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from quant_research.strategy.s0 import check
from quant_research.strategy.space import (
    SPACES,
    ChoiceAxis,
    FloatAxis,
    IntAxis,
    Point,
    Space,
    build_point,
    generate,
)
from quant_research.strategy.templates import FAMILIES


def _fast_lt_slow(p: dict[str, float]) -> str | None:
    return None if p["fast"] < p["slow"] else f"fast {p['fast']:g} >= slow {p['slow']:g}"


SMALL = Space(
    family="trend",
    params=(IntAxis("fast", 1, 3, 1), IntAxis("slow", 2, 4, 1)),
    struct=(ChoiceAxis("side", ("long_short", "long_only")),),
    constraint=_fast_lt_slow,
)


def test_space_size_matches_a_hand_count_on_a_small_space() -> None:
    # valid (fast, slow): (1,2) (1,3) (1,4) (2,3) (2,4) (3,4) = 6, times 2 sides
    assert SMALL.size() == 12
    assert SMALL.nominal_size() == 3 * 3 * 2


def test_axis_values_are_exact_steps() -> None:
    assert FloatAxis("q", 0.6, 0.95, 0.05).values() == (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95)
    assert IntAxis("w", 24, 96, 24).values() == (24, 48, 72, 96)
    assert IntAxis("w", 5, 5, 1).values() == (5,)


@pytest.mark.parametrize(
    "make",
    [
        lambda: IntAxis("w", 10, 5, 1),
        lambda: IntAxis("w", 1, 5, 0),
        lambda: FloatAxis("q", 0.9, 0.1, 0.1),
        lambda: FloatAxis("q", 0.1, 0.9, -0.1),
        lambda: ChoiceAxis("c", ()),
    ],
)
def test_malformed_axes_are_rejected(make: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        make()


def test_every_v1_family_has_a_registered_space_much_larger_than_the_fixed_grid() -> None:
    assert set(SPACES) == set(FAMILIES)
    for space in SPACES.values():
        assert space.size() > 1_000
        assert space.size() <= space.nominal_size()


def test_the_same_seed_generates_the_same_candidates_and_another_seed_differs() -> None:
    a = generate(SPACES["trend"], seed=11, count=50)
    b = generate(SPACES["trend"], seed=11, count=50)
    c = generate(SPACES["trend"], seed=12, count=50)
    assert [x.point for x in a] == [x.point for x in b]
    assert [x.point for x in a] != [x.point for x in c]


@pytest.mark.parametrize("family", FAMILIES)
def test_every_generated_candidate_passes_s0_or_is_rejected_with_reasons(family: str) -> None:
    cands = generate(SPACES[family], seed=3, count=300)
    assert len(cands) == 300
    valid = 0
    for cand in cands:
        if cand.spec is None:
            assert cand.reasons, cand.point
        else:
            assert cand.reasons == ()
            assert check(cand.spec) == []
            valid += 1
    assert valid > 0


def test_a_constraint_violation_is_a_recorded_rejection_not_a_silent_skip() -> None:
    cands = generate(SMALL, seed=0, count=200)
    rejected = [c for c in cands if c.spec is None]
    assert rejected  # 3 of 9 parameter pairs violate fast < slow
    assert all(any(">= slow" in r for r in c.reasons) for c in rejected)


def test_a_negative_count_is_refused() -> None:
    with pytest.raises(ValueError, match="count"):
        generate(SMALL, seed=0, count=-1)
    assert generate(SMALL, seed=0, count=0) == []


def test_structural_axes_change_the_spec() -> None:
    base = Point.of(
        "trend",
        {"fast": 24, "slow": 96},
        {"side": "long_short", "smooth": 0, "universe_n": 5, "sizing": "vol_target:168", "time_stop": 0},
    )
    spec = build_point(SPACES["trend"], base)
    assert spec.entry.short is not None and spec.exit.time_stop_bars is None
    assert spec.universe.n == 5 and spec.sizing.method == "vol_target" and spec.sizing.vol_window == 168
    assert spec.signal == build_point(SPACES["trend"], base).signal  # deterministic

    other = Point.of(
        "trend",
        {"fast": 24, "slow": 96},
        {"side": "long_only", "smooth": 6, "universe_n": 3, "sizing": "equal", "time_stop": 72},
    )
    spec2 = build_point(SPACES["trend"], other)
    assert spec2.entry.short is None
    assert not isinstance(spec2.signal, str) and spec2.signal.op == "ema" and spec2.signal.window == 6
    assert spec2.exit.time_stop_bars == 72
    assert spec2.sizing.method == "equal" and spec2.universe.n == 3


def test_volatility_has_no_side_axis_because_it_is_long_only() -> None:
    assert "side" not in {a.name for a in SPACES["volatility"].struct}
    assert "side" in {a.name for a in SPACES["trend"].struct}


def test_a_point_outside_the_registered_axes_is_refused() -> None:
    bad = Point.of(
        "trend",
        {"fast": 25, "slow": 96},  # 25 is not on the fast axis
        {"side": "long_short", "smooth": 0, "universe_n": 5, "sizing": "equal", "time_stop": 0},
    )
    with pytest.raises(ValueError, match="fast"):
        build_point(SPACES["trend"], bad)
    missing = Point.of("trend", {"fast": 24}, {"side": "long_short"})
    with pytest.raises(ValueError, match="slow"):
        build_point(SPACES["trend"], missing)
    wrong_family = Point.of("momentum", {"lookback": 24, "quantile": 0.8}, {})
    with pytest.raises(ValueError, match="family"):
        build_point(SPACES["trend"], wrong_family)
