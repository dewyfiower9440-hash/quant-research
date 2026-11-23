"""v2.4 step 4 (ADR-051): the executable spaces (lookback caps of a 90-day warm-up), the S0 warm-up check, the v3
split of a research window and `infeasible` cells."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quant_research.backtest.panel import Panel
from quant_research.backtest.panel_store import Window
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import BatchSummary, CampaignState, Cell, classify, relabel
from quant_research.search.campaign_runner import (
    CampaignConfig,
    SplitRule,
    infeasible,
    run_campaign,
    space_name,
    split_bounds,
)
from quant_research.search.optimizers import RandomSearcher, candidate_of
from quant_research.strategy.s0 import S0Context, check, warmup_bars
from quant_research.strategy.space import (
    INFEASIBLE_V3,
    SPACES_V2,
    SPACES_V3,
    Point,
    build_point,
    ema_cap_bars,
    generate,
    space_of,
    window_cap_bars,
)
from quant_research.strategy.templates import ALL_FAMILIES


def _ns(y: int, m: int) -> int:
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp()) * 10**9


def _axis(space_name_: str, axis: str) -> tuple[float, ...]:
    return next(a.values() for a in SPACES_V3[space_name_].params if a.name == axis)


def test_v3_spaces_keep_the_start_and_step_and_cut_the_end_at_the_cap() -> None:
    assert _axis("momentum@1d/v3", "lookback") == (24.0, 48.0, 72.0)  # 24..720 step 24, cap 90
    assert max(_axis("momentum@4h/v3", "lookback")) == 528.0  # cap 540 on the 24-step grid
    assert max(_axis("trend@4h/v3", "slow")) == 168.0 and max(_axis("trend@4h/v3", "fast")) == 96.0  # EMA cap 180
    assert max(_axis("mean_reversion@1d/v3", "window")) == 84.0
    # review (step 4): an axis value equal to the cap can never pass the S0 warm-up check (need >= cap is refused)
    assert max(_axis("funding_basis@1d/v3", "window")) == 80.0  # 10..180 step 10: 90 is the cap itself
    assert max(_axis("taker_flow@1d/v3", "window")) == 89.0  # 1..168 step 1
    for name, space in SPACES_V3.items():
        for a in space.params:
            if a.name in ("window", "lookback", "short", "long", "fast", "slow"):
                assert max(a.values()) < window_cap_bars(space.timeframe), (name, a.name)
    assert _axis("mean_reversion@1d/v3", "threshold") == next(  # not a window: untouched
        a.values() for a in SPACES_V2["mean_reversion@1d"].params if a.name == "threshold"
    )
    for family in ALL_FAMILIES:  # 1h: the v2 axes already fit (720 bars = 30 days)
        assert SPACES_V3[f"{family}@1h/v3"].params == SPACES_V2[f"{family}@1h"].params


def test_a_family_whose_axis_starts_beyond_the_cap_has_no_v3_space() -> None:
    assert {"trend@1d", "volatility@1d"} == INFEASIBLE_V3  # slow EMA from 48 > 30; long window from 96 > 90
    assert set(SPACES_V3) == {f"{n}/v3" for n in SPACES_V2 if n not in INFEASIBLE_V3}
    assert space_of("momentum@4h/v3") is SPACES_V3["momentum@4h/v3"]
    assert space_of("momentum@4h") is SPACES_V2["momentum@4h"]  # the v2 lookup is unchanged
    with pytest.raises(KeyError):
        space_of("trend@1d/v3")


def test_v3_sizing_drops_volatility_windows_beyond_the_warmup() -> None:
    sizing = next(a.choices for a in SPACES_V3["momentum@1d/v3"].struct if a.name == "sizing")
    assert sizing == ("vol_target:20", "vol_target:40", "vol_target:60", "equal")  # v2 also had vol_target:120
    v2 = next(a.choices for a in SPACES_V2["momentum@1d"].struct if a.name == "sizing")
    assert "vol_target:120" in v2


def test_the_warmup_of_a_spec_adds_nested_windows_and_covers_the_sizing_window() -> None:
    space = SPACES_V3["mean_reversion@1d/v3"]
    struct = {a.name: a.choices[0] for a in space.struct} | {"smooth": 12, "sizing": "vol_target:20"}
    spec = build_point(space, Point.of(space.family, {"window": 84.0, "threshold": 2.0}, struct))
    assert warmup_bars(spec) == 96  # zscore over 84 bars inside an EMA of 12
    assert any("infeasible: needs 96 bars" in r for r in check(spec, S0Context(max_warmup_bars=90)))
    # one bar of margin: a shifted operator (ret over w bars) reads w + 1 rows, the current one included
    assert any("infeasible" in r for r in check(spec, S0Context(max_warmup_bars=96)))
    assert check(spec, S0Context(max_warmup_bars=97)) == [] and check(spec) == []  # no limit given: not checked
    calm = build_point(space, Point.of(space.family, {"window": 12.0, "threshold": 2.0}, struct | {"smooth": 0}))
    assert warmup_bars(calm) == 20  # the volatility window of the sizing is the longest


@pytest.mark.parametrize("name", ["momentum@1d/v3", "mean_reversion@1d/v3", "low_vol@4h/v3", "volatility@4h/v3"])
def test_every_accepted_v3_candidate_fits_the_block_warmup(name: str) -> None:
    space = SPACES_V3[name]
    assert space.warmup_bars == window_cap_bars(space.timeframe)
    cands = generate(space, seed=11, count=300)
    accepted = [c for c in cands if c.spec is not None]
    assert len(accepted) > 200  # the cap leaves most of the space executable
    assert all(warmup_bars(c.spec) < window_cap_bars(space.timeframe) for c in accepted if c.spec is not None)
    for c in cands:  # the optimiser path judges a point like `generate` does
        assert candidate_of(space, c.point).reasons == c.reasons


def test_no_v3_point_needs_more_warmup_than_a_split_keeps() -> None:
    """Review (step 4): on 1h the S0 limit (2,160 bars) is above what `split_window` keeps as warm-up rows
    (MAX_WARMUP_BARS); the axes must stay below the smaller of the two."""
    from quant_research.search.campaign_runner import MAX_WARMUP_BARS

    for name, space in SPACES_V3.items():
        longest = max(
            (warmup_bars(c.spec) for c in generate(space, seed=3, count=200) if c.spec is not None), default=0
        )
        assert longest < min(MAX_WARMUP_BARS, window_cap_bars(space.timeframe)), name


def test_a_v2_space_is_not_checked_for_warmup() -> None:
    space = SPACES_V2["momentum@1d"]
    long = Point.of(space.family, {"lookback": 720.0, "quantile": 0.8}, {a.name: a.choices[0] for a in space.struct})
    assert candidate_of(space, long).reasons == ()  # c1's points still rebuild (replay, validator)
    assert ema_cap_bars("1d") == 30


def test_the_v3_split_takes_three_warmup_months_and_keeps_the_minimum_lengths() -> None:
    rule = SplitRule.of(3)
    assert rule == SplitRule(3, 9, 4) and SplitRule.of(2) == SplitRule()
    sel, conf, end = split_bounds(Window(_ns(2024, 12), _ns(2026, 7)), rule=rule)  # w2: 19 months
    assert (sel, conf, end) == (_ns(2025, 3), _ns(2026, 2), _ns(2026, 7))  # 3 warm-up, 11 selection, 5 confirmation
    sel, conf, _ = split_bounds(Window(_ns(2020, 1), _ns(2023, 1)), rule=rule)  # w1: 36 months
    assert (sel, conf) == (_ns(2020, 4), _ns(2022, 3))  # 23 selection, 10 confirmation
    with pytest.raises(ValueError, match="the rule needs 9 and 4"):
        split_bounds(Window(_ns(2025, 1), _ns(2026, 1)), rule=rule)  # 12 months: 6 + 3 after the warm-up
    with pytest.raises(ValueError, match="at least 5 months"):
        split_bounds(Window(_ns(2025, 1), _ns(2025, 5)), rule=rule)


def test_an_infeasible_summary_is_its_own_cause_and_round_trips() -> None:
    b = BatchSummary(0, 0, 0, 0, None, None, None, None, None, (), infeasible=True)
    assert classify(b, min_exposure=0.05) == "infeasible"
    cell = Cell("trend", "1d", 100, "w2")
    state = CampaignState.new("i", [cell])
    state.record(cell, "infeasible", b, reward=0.0)
    assert cell.key in state.retired  # nothing to try again: the space does not change
    stored = state.history[0]["summary"]
    assert isinstance(stored, dict) and stored["infeasible"] is True
    assert relabel(state.history, min_exposure=0.05)[0].old == "infeasible"
    assert [r.new for r in relabel(state.history, min_exposure=0.05)] == ["infeasible"]


def test_space_names_follow_the_campaign_space_version() -> None:
    cell = Cell("momentum", "4h", 100, "w2")
    assert space_name(cell, 2) == "momentum@4h" and space_name(cell, 3) == "momentum@4h/v3"
    assert infeasible(Cell("trend", "1d", 100, "w2"), 3) and not infeasible(Cell("trend", "1d", 100, "w2"), 2)
    assert not infeasible(cell, 3)
    assert "space_version" not in CampaignConfig().identity()  # a campaign saved before ADR-051 still resumes
    assert CampaignConfig(space_version=3).identity()["space_version"] == 3
    with pytest.raises(ValueError, match="space_version"):
        CampaignConfig(space_version=4)
    assert RandomSearcher(space_of(space_name(cell, 3)), 1).ask().family == "momentum@4h/v3"


def test_an_infeasible_cell_is_recorded_without_a_panel_or_a_trial(
    env: tuple[Catalog, TrialLedger],
    panels: tuple[Panel, Panel],
) -> None:
    catalog, ledger = env
    cell = Cell("trend", "1d", 100, "w2")
    state = CampaignState.new("v", [cell], max_pulls=3)
    loaded: list[str] = []

    def provider(c: Cell) -> tuple[Panel, Panel]:
        loaded.append(c.key)
        return panels

    out = run_campaign(
        state,
        FamilyBandit((cell.key,), BanditPrior(min_pulls=0.0)),
        ledger=ledger,
        catalog=catalog,
        panels=provider,
        config=CampaignConfig(seed=1, s1_count=4, space_version=3),
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    assert out.status == "exhausted" and [h["cause"] for h in state.history] == ["infeasible"]
    assert loaded == [] and int(ledger.store.conn.execute("select count(*) from trial").fetchone()[0]) == 0
