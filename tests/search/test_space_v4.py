"""v2.5 step 8a (ADR-059): space version 4. Every v3 space again, with the portfolio-level volatility target in
place of the per-name one; the earlier spaces and their specs are untouched."""

from __future__ import annotations

import pytest

from quant_research.search.campaign import Cell
from quant_research.search.campaign_runner import (
    MAX_WARMUP_BARS,
    CampaignConfig,
    SplitRule,
    infeasible,
    requires_funding,
    space_name,
)
from quant_research.strategy.canonical import spec_hash
from quant_research.strategy.s0 import warmup_bars
from quant_research.strategy.space import (
    MODEL_SPACES,
    REGIME_SPACES,
    SPACES_V3,
    SPACES_V4,
    Point,
    build_point,
    generate,
    space_of,
    window_cap_bars,
)


def _sizing(name: str) -> tuple[str, ...]:
    return tuple(str(v) for v in next(a for a in space_of(name).struct if a.name == "sizing").choices)


def test_every_v3_space_has_a_v4_twin_that_differs_in_the_sizing_axis_only() -> None:
    earlier = {**SPACES_V3, **MODEL_SPACES, **REGIME_SPACES}
    assert len(SPACES_V4) == len(earlier) > 0
    for name, v3 in earlier.items():
        v4 = SPACES_V4[name.removesuffix("/v3") + "/v4"]
        assert v4.family == name.removesuffix("/v3") + "/v4"
        assert (v4.params, v4.timeframe, v4.warmup_bars, v4.template_family) == (
            v3.params,
            v3.timeframe,
            v3.warmup_bars,
            v3.template_family,
        )
        assert [a.name for a in v4.struct] == [a.name for a in v3.struct]
        for a3, a4 in zip(v3.struct, v4.struct, strict=True):
            if a3.name != "sizing":
                assert a4 == a3
    assert _sizing("mean_reversion@1d/v3") == ("vol_target:20", "vol_target:40", "vol_target:60", "equal")
    assert _sizing("mean_reversion@1d/v4") == (
        "portfolio_vol_target:20",
        "portfolio_vol_target:40",
        "portfolio_vol_target:60",
        "equal",  # ADR-059 decision 3: unchanged, and not under the per-name cap
    )
    assert _sizing("combo@1d/v4") == ("portfolio_vol_target:40",)
    assert _sizing("momentum~regime@4h/v4")[0].startswith("portfolio_vol_target:")


def test_a_v4_point_builds_the_new_sizing_and_an_equal_point_is_the_v3_spec() -> None:
    v3, v4 = space_of("mean_reversion@1d/v3"), space_of("mean_reversion@1d/v4")
    struct = {a.name: a.choices[0] for a in v4.struct}
    params = {"window": 24.0, "threshold": 1.5}
    spec = build_point(v4, Point.of(v4.family, params, struct | {"sizing": "portfolio_vol_target:60"}))
    assert (spec.sizing.method, spec.sizing.vol_window) == ("portfolio_vol_target", 60)
    assert (spec.sizing.target_ann_vol, spec.sizing.max_leverage) == (0.15, 1.0)
    assert warmup_bars(spec) == 75  # 60 + max(5, 60 // 4), inside the 90-day cap
    equal4 = build_point(v4, Point.of(v4.family, params, struct | {"sizing": "equal"}))
    equal3 = build_point(v3, Point.of(v3.family, params, struct | {"sizing": "equal"}))
    assert spec_hash(equal4) == spec_hash(equal3)  # the same strategy: one hash
    with pytest.raises(ValueError, match="not on the registered axis"):
        build_point(v4, Point.of(v4.family, params, struct | {"sizing": "vol_target:20"}))


def test_the_specs_of_the_earlier_spaces_keep_their_hashes() -> None:
    """ADR-059 decision 4. The values are the hashes on the commit before the change (6dd4633)."""
    space = SPACES_V3["mean_reversion@1d/v3"]
    struct = {a.name: a.choices[0] for a in space.struct}
    params = {"window": 24.0, "threshold": 1.5}
    hashes = {
        sizing: spec_hash(build_point(space, Point.of(space.family, params, struct | {"sizing": sizing})))
        for sizing in ("vol_target:20", "equal")
    }
    assert hashes == {
        "vol_target:20": "d16029706923e290d013f86a3f28ab1fcc33356016366ac48ff3df3dd679befe",
        "equal": "7b084c41bd06a65247a9831d1efd0c9e634365c94367c90527de883321a20a50",
    }
    model = MODEL_SPACES["combo@1d/v3"]
    fixed = {a.name: a.values()[0] for a in model.params}
    combo = build_point(model, Point.of(model.family, fixed, {a.name: a.choices[0] for a in model.struct}))
    assert spec_hash(combo) == "d5ec0f0990719b023abc7ff134fe915393fdefffeb492b37939dd214d9d93b90"


def test_no_v4_point_needs_more_warmup_than_a_split_keeps() -> None:
    """The correlation estimate adds bars to the sizing's warm-up: every v4 space must still fit (ADR-051)."""
    for name, space in SPACES_V4.items():
        if space.template_family in ("combo", "ridge"):
            continue  # not drawn from: `search.model_cells` builds their points (their sizing window is checked above)
        accepted = [c.spec for c in generate(space, seed=3, count=200) if c.spec is not None]
        assert len(accepted) > 100, name
        longest = max(warmup_bars(spec) for spec in accepted)
        assert longest < min(MAX_WARMUP_BARS, window_cap_bars(space.timeframe)), name
        assert any(spec.sizing.method == "portfolio_vol_target" for spec in accepted), name


def test_a_version_4_campaign_keeps_the_rules_of_version_3() -> None:
    cell = Cell("momentum", "4h", 100, "w2")
    assert space_name(cell, 4) == "momentum@4h/v4" and space_name(cell, 3) == "momentum@4h/v3"
    assert space_name(Cell("combo", "1d", 100, "w2"), 4) == "combo@1d/v4"
    assert space_name(Cell("momentum~regime", "4h", 100, "w2"), 4) == "momentum~regime@4h/v4"
    with pytest.raises(ValueError, match="model cells need space version 3 or later"):
        space_name(Cell("combo", "1d", 100, "w2"), 2)
    assert infeasible(Cell("trend", "1d", 100, "w2"), 4) and not infeasible(cell, 4)
    assert requires_funding(CampaignConfig(space_version=4)) and not requires_funding(CampaignConfig())
    assert SplitRule.of(4) == SplitRule.of(3) != SplitRule.of(2)
    assert CampaignConfig(space_version=4).identity()["space_version"] == 4
    with pytest.raises(ValueError, match="space_version"):
        CampaignConfig(space_version=5)
