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
    from quant_research.strategy.templates import ALL_FAMILIES

    assert set(SPACES) == set(ALL_FAMILIES)
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


def test_v2_spaces_cover_every_family_on_1h_4h_and_1d_and_leave_the_v1_registry_alone() -> None:
    """v2.2 B5 (D5): timeframe, rebalance interval and no-trade band axes; v1 `SPACES` (Phase 4 prior) is
    unchanged, so every existing caller and archived point keeps its meaning."""
    from quant_research.strategy.space import SPACES_V2, space_of
    from quant_research.strategy.templates import ALL_FAMILIES

    assert set(SPACES) == set(ALL_FAMILIES)
    assert set(SPACES_V2) == {f"{f}@{tf}" for f in ALL_FAMILIES for tf in ("1h", "4h", "1d")}
    for name, space in SPACES_V2.items():
        family, tf = name.split("@")
        assert space.template_family == family and space.timeframe == tf
        assert {"rebalance", "band"} <= {a.name for a in space.struct}
        assert space_of(name) is space
    assert space_of("trend") is SPACES["trend"]
    with pytest.raises(KeyError, match="unknown space"):
        space_of("trend@5m")


def test_a_v2_point_builds_a_spec_with_its_timeframe_rebalance_and_band() -> None:
    from quant_research.strategy.space import SPACES_V2, Point

    space = SPACES_V2["momentum@4h"]
    rebalance = dict((a.name, a.values()) for a in space.struct)["rebalance"]
    assert rebalance == (1, 6, 18, 42)  # 1 h, 4 h -> 1 bar (deduplicated), 24 h, 72 h, 168 h on 4 h bars
    point = Point.of(
        "momentum@4h",
        {"lookback": 48.0, "quantile": 0.8},
        {
            "side": "long_short",
            "smooth": 0,
            "universe_n": 10,
            "sizing": "vol_target:42",
            "time_stop": 0,
            "rebalance": 6,
            "band": 200,
        },
    )
    spec = build_point(space, point)
    assert spec.family == "momentum" and spec.timeframe == "4h"
    assert (spec.execution.rebalance_bars, spec.execution.min_trade_weight) == (6, 0.02)
    assert check(spec) == []


@pytest.mark.parametrize("timeframe", ["1h", "4h", "1d"])
def test_every_generated_v2_candidate_passes_s0_or_is_rejected_with_reasons(timeframe: str) -> None:
    from quant_research.strategy.space import SPACES_V2

    for family in FAMILIES:
        cands = generate(SPACES_V2[f"{family}@{timeframe}"], seed=5, count=40)
        assert len(cands) == 40
        for c in cands:
            assert (c.spec is None) == bool(c.reasons)
            if c.spec is not None:
                assert c.spec.timeframe == timeframe and check(c.spec) == []


def test_equal_sizing_in_v2_uses_a_timeframe_sized_volatility_window() -> None:
    """B5 review 6: 'equal' kept vol_window 168 bars on every timeframe (168 days at 1d), so the impact-cost
    volatility was missing for the whole research window."""
    from quant_research.strategy.space import SPACES_V2

    for tf, window in (("1h", 96), ("4h", 24), ("1d", 40)):
        space = SPACES_V2[f"momentum@{tf}"]
        point = Point.of(
            f"momentum@{tf}",
            {"lookback": 48.0, "quantile": 0.8},
            {
                "side": "long_short",
                "smooth": 0,
                "universe_n": 10,
                "sizing": "equal",
                "time_stop": 0,
                "rebalance": 1,
                "band": 0,
            },
        )
        assert build_point(space, point).sizing.vol_window == window
    v1 = Point.of(
        "momentum",
        {"lookback": 48.0, "quantile": 0.8},
        {"side": "long_short", "smooth": 0, "universe_n": 5, "sizing": "equal", "time_stop": 0},
    )
    assert build_point(SPACES["momentum"], v1).sizing.vol_window == 168  # v1 unchanged


def test_rank_families_whose_extremes_are_illiquid_trade_a_wide_universe() -> None:
    """B6 review 1: `rank_xs` ranks all members but only the top-n by liquidity are tradable, so 'long the least
    liquid' never traded at n = 5..20. liquidity and low_vol use n in (50, 100)."""
    from quant_research.strategy.space import SPACES_V2

    for name in ("liquidity", "low_vol"):
        for space in (SPACES[name], SPACES_V2[f"{name}@1d"]):
            assert dict((a.name, a.values()) for a in space.struct)["universe_n"] == (50, 100)
    assert dict((a.name, a.values()) for a in SPACES_V2["momentum@1d"].struct)["universe_n"] == (5, 10, 20)


def test_templates_stay_the_v1_defaults_and_all_templates_add_the_new_families() -> None:
    """B6 review 4: the Phase 2 smoke tool runs `templates()`; it must not start recording six more families."""
    from quant_research.strategy.templates import ALL_FAMILIES, all_templates, templates

    assert tuple(templates()) == FAMILIES and tuple(all_templates()) == ALL_FAMILIES


def test_lookback_caps_follow_the_ninety_day_warmup_per_timeframe() -> None:
    """ADR-051: window operators may look back 90 days, an EMA a third of that (span x 3 to converge)."""
    from quant_research.strategy.space import ema_cap_bars, exceeds_cap, window_cap_bars

    assert [window_cap_bars(tf) for tf in ("1h", "4h", "1d")] == [2160, 540, 90]
    assert [ema_cap_bars(tf) for tf in ("1h", "4h", "1d")] == [720, 180, 30]
    assert not exceeds_cap("momentum", "1d", {"lookback": 90.0, "quantile": 0.8})
    assert exceeds_cap("momentum", "1d", {"lookback": 96.0, "quantile": 0.8})
    assert not exceeds_cap("momentum", "4h", {"lookback": 540.0, "quantile": 0.8})
    assert exceeds_cap("volatility", "1d", {"short": 12.0, "long": 96.0, "ratio": 0.7})  # the longest window counts
    assert not exceeds_cap("trend", "4h", {"fast": 24.0, "slow": 168.0})  # EMA spans use the EMA cap
    assert exceeds_cap("trend", "4h", {"fast": 24.0, "slow": 192.0})
    assert exceeds_cap("trend", "1d", {"fast": 6.0, "slow": 48.0})
    assert not exceeds_cap("mean_reversion", "1d", {"window": 84.0, "threshold": 2.5})  # thresholds are not windows
    with pytest.raises(ValueError, match="timeframe"):
        window_cap_bars("1w")
