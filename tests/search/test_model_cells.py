"""v2.4 step 6 (ADR-053, design v2.3 section 6): the linear combination cell M0. Constituents at canonical
parameters, weights estimated on the fit part only and written into the point, the same funnel as any batch."""

from __future__ import annotations

import json
from dataclasses import replace

import polars as pl
import pytest
from search_fixtures import T0, D, funded_pair, planted_full

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import CampaignState, Cell, summary_from_json
from quant_research.search.campaign_runner import CampaignConfig, is_model, run_campaign, space_name
from quant_research.search.model_cells import (
    TermFit,
    combo_points,
    equal_weights,
    fit_ics,
    fixed_params,
    fixed_struct,
    ic_weights,
    model_point,
    normalise,
    term_names,
)
from quant_research.search.optimizers import FixedSearcher, candidate_of
from quant_research.search.pipeline import split_for_reward
from quant_research.search.s1 import S1Config, evaluate_s1
from quant_research.strategy.s0 import check
from quant_research.strategy.space import MODEL_SPACES, SPACES_V3, build_point, canonical_window, space_of
from quant_research.strategy.templates import CONSTITUENTS, GRID, build, constituent, model_terms, window_key

COMBO_1H = MODEL_SPACES["combo@1h/v3"]


def test_canonical_windows_are_the_middle_of_the_capped_axes() -> None:
    """Design v2.3 appendix A (fixed before the first run): mechanical, no result decides them."""
    at_4h = {f: canonical_window(f, "4h") for f in CONSTITUENTS}
    assert at_4h == {
        "xs_reversal": 24,
        "momentum": 264,
        "low_vol": 264,
        "liquidity": 264,
        "premium_basis": 84,
        "funding_carry": 168,
        "funding_basis": 90,
    }
    at_1d = {f: canonical_window(f, "1d") for f in CONSTITUENTS}
    assert at_1d == {
        "xs_reversal": 24,
        "momentum": 48,
        "low_vol": 48,
        "liquidity": 48,
        "premium_basis": 44,
        "funding_carry": 48,
        "funding_basis": 40,
    }
    for family in CONSTITUENTS:  # always a value of the registered v3 axis
        axis = next(a for a in SPACES_V3[f"{family}@4h/v3"].params if a.name == window_key(family))
        assert float(canonical_window(family, "4h")) in axis.values()


def test_a_constituent_is_read_off_the_single_family_template() -> None:
    tree, sign = constituent("momentum", 48)
    assert tree == {"op": "ret", "args": [{"op": "close", "args": []}], "window": 48}
    assert sign == 1.0  # long the winners
    assert constituent("xs_reversal", 4)[1] == -1.0  # long the losers
    assert constituent("low_vol", 48)[1] == -1.0 and constituent("funding_carry", 24)[1] == -1.0
    spec = build("liquidity", {**GRID["liquidity"][0], "window": 48.0})
    ranked = next(iter(spec.features.values())).args[0]
    assert not isinstance(ranked, str | float | int)
    assert constituent("liquidity", 48)[0] == ranked.model_dump(mode="json", exclude_none=True)
    with pytest.raises(ValueError, match="rank family"):
        constituent("trend", 24)  # two windows, not a rank family
    assert [name for name, _t, _s in model_terms("combo", fixed_params(COMBO_1H))] == list(CONSTITUENTS)
    with pytest.raises(ValueError, match="not a model family"):
        model_terms("momentum", {})


def test_a_model_space_fixes_everything_but_the_weights() -> None:
    assert {f"combo@{tf}/v3" for tf in ("1h", "4h", "1d")} <= set(MODEL_SPACES)  # the ridge spaces: step 7
    assert space_of("combo@4h/v3") is MODEL_SPACES["combo@4h/v3"]
    assert fixed_struct(MODEL_SPACES["combo@4h/v3"]) == {
        "side": "long_short",
        "smooth": 0,
        "universe_n": 20,
        "sizing": "vol_target:24",
        "time_stop": 0,
        "rebalance": 6,  # 24 hours of 4h bars
        "band": 200,
    }
    assert fixed_params(COMBO_1H) == {f"p_{f}": float(canonical_window(f, "1h")) for f in CONSTITUENTS}
    assert term_names(COMBO_1H) == CONSTITUENTS
    with pytest.raises(ValueError, match="fixes its portfolio"):
        fixed_struct(SPACES_V3["momentum@4h/v3"])


def test_a_model_point_builds_a_spec_that_carries_its_weights() -> None:
    weights = equal_weights(CONSTITUENTS)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3) and len(set(weights.values())) == 1
    point = model_point(COMBO_1H, weights)
    cand = candidate_of(COMBO_1H, point)
    assert cand.reasons == () and cand.spec is not None and cand.spec.family == "combo"
    assert build_point(COMBO_1H, point) == cand.spec  # the point alone reproduces the spec (validator replay)
    text = cand.spec.model_dump_json()
    assert text.count("zscore_xs") == len(CONSTITUENTS) and "0.1429" in text and "-0.1429" in text  # oriented
    only = model_point(COMBO_1H, {"momentum": 1.0})  # zero-weight terms are left out of the tree
    assert build_point(COMBO_1H, only).model_dump_json().count("zscore_xs") == 1
    with pytest.raises(ValueError, match="every weight is zero"):
        build_point(COMBO_1H, model_point(COMBO_1H, {}))
    with pytest.raises(ValueError, match="registered axis"):
        build_point(COMBO_1H, model_point(COMBO_1H, {"momentum": 0.123456}))  # off the 1e-4 weight grid


def test_only_model_families_may_exceed_the_searched_spec_limits() -> None:
    spec = build_point(COMBO_1H, model_point(COMBO_1H, equal_weights(CONSTITUENTS)))
    assert check(spec) == []  # about a hundred nodes: a registered sum, not a searched structure
    as_searched = spec.model_copy(update={"family": "momentum"})
    assert any("nodes exceed 64" in r for r in check(as_searched))


def test_ic_weights_cut_negatives_and_normalise() -> None:
    fits = [TermFit("a", 0.02, 3.0, 50), TermFit("b", -0.01, -2.0, 50), TermFit("c", 0.01, 1.0, 50)]
    assert ic_weights(fits) == {"a": 0.75, "b": 0.0, "c": 0.25}
    assert ic_weights([TermFit("a", -0.01, -1.0, 50), TermFit("b", None, None, 0)]) is None  # nothing to weight by
    assert normalise({"a": 2.0, "b": -2.0}) == {"a": 0.5, "b": -0.5}
    assert normalise({"a": 0.0}) is None and normalise({"a": float("nan")}) is None
    with pytest.raises(ValueError, match="at least one"):
        equal_weights(())


@pytest.fixture(scope="module")
def planted() -> Panel:
    """40 days with the planted effect, evaluated from day 16 (the 360-bar terms need 15 days of warm-up)."""
    return slice_panel(planted_full(40), T0 + 16 * D, T0 + 39 * D, warmup_ns=16 * D)


def test_independent_weak_signals_combine_into_a_stronger_one(planted: Panel) -> None:
    """ADR-053's purpose, on planted data: three independent characteristics each explain a third of the drift,
    so their combination ranks the drift better than any one of them."""
    fits = {f.name: f for f in fit_ics(COMBO_1H, planted, horizon_bars=24)}
    carriers = ("liquidity", "premium_basis", "funding_carry")
    for name in carriers:
        assert (fits[name].ic_hac_t or 0.0) > 2.0, name  # the planted effect is recovered, oriented as registered
    weights = ic_weights(list(fits.values()))
    assert weights is not None and all(w >= 0 for w in weights.values())
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-3)
    cfg = S1Config(horizon_bars=24)
    combined = evaluate_s1(build_point(COMBO_1H, model_point(COMBO_1H, equal_weights(carriers))), planted, cfg)
    singles = [
        evaluate_s1(build_point(COMBO_1H, model_point(COMBO_1H, {n: 1.0})), planted, cfg).ic_mean for n in carriers
    ]
    assert combined.ic_mean is not None and all(s is not None for s in singles)
    values = [s for s in singles if s is not None]
    # sqrt(3) = 1.73 in the limit of weak effects on a wide cross-section; rank ICs over 8 symbols saturate
    # earlier, so the test asks for a clear gain: above every single one and 1.2 times their average
    assert combined.ic_mean > max(values)
    assert combined.ic_mean > 1.2 * sum(values) / len(values)


def test_past_only_constituents_show_no_ic_on_a_random_walk() -> None:
    """Leak check: on exchangeable walks (no drift, no alpha) no constituent may look significant; a term that
    read the future would. (The shared fixture gives each symbol its own trend, which momentum rightly finds.)"""
    fits = fit_ics(COMBO_1H, funded_pair(drift=0.0, seed=31)[0], horizon_bars=24)
    assert [f.name for f in fits] == list(CONSTITUENTS)
    for f in fits:
        if f.name in ("xs_reversal", "momentum", "low_vol", "premium_basis"):  # the price / premium driven ones
            assert f.ic_hac_t is None or abs(f.ic_hac_t) < 4.0, f


def test_the_weights_do_not_depend_on_anything_after_the_fit_part(funded_panels: tuple[Panel, Panel]) -> None:
    selection, _ = funded_panels
    fit, validation = split_for_reward(selection, 0.25)
    points, record = combo_points(COMBO_1H, fit, horizon_bars=24)
    cut = int(validation.evaluate_from_ns or 0)
    shuffled = selection.frame.with_columns(
        pl.when(pl.col("decision_ns") >= cut).then(pl.col("close") * 1.5).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("decision_ns") >= cut)
        .then(pl.col("exec_open") * 1.5)
        .otherwise(pl.col("exec_open"))
        .alias("exec_open"),
    )
    other_fit, _ = split_for_reward(replace(selection, frame=shuffled), 0.25)
    assert other_fit.frame.equals(fit.frame)  # the fit part holds no row from the cut on
    again, _ = combo_points(COMBO_1H, other_fit, horizon_bars=24)
    assert again == points and 1 <= len(points) <= 2
    assert set(record) == {"lin_eq", "lin_ic", "fit"} and points[0] == model_point(
        COMBO_1H, equal_weights(CONSTITUENTS)
    )


def test_a_fixed_searcher_returns_its_points_in_order() -> None:
    a = model_point(COMBO_1H, equal_weights(CONSTITUENTS))
    b = model_point(COMBO_1H, {"momentum": 1.0})
    searcher = FixedSearcher(COMBO_1H, [a, b])
    assert [searcher.ask() for _ in range(3)] == [a, b, a] and searcher.asked() == 3
    searcher.tell(a, 1.0)
    with pytest.raises(ValueError, match="at least one point"):
        FixedSearcher(COMBO_1H, [])


def test_a_combo_cell_runs_its_fixed_points_through_the_funnel(
    env: tuple[Catalog, TrialLedger], funded_panels: tuple[Panel, Panel]
) -> None:
    catalog, ledger = env
    cell = Cell("combo", "1h", 30, "w2")
    assert is_model(cell) and space_name(cell, 3) == "combo@1h/v3"
    with pytest.raises(ValueError, match="space version 3"):
        space_name(cell, 2)
    state = CampaignState.new("m", [cell], max_pulls=1)
    out = run_campaign(
        state,
        FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0)),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: funded_panels,
        config=CampaignConfig(seed=2, s1_count=60, top_k=5, space_version=3, max_batches=1),
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    assert out.batches == 1 and state.history[0]["cause"] not in ("error", "data_missing", "infeasible")
    stages = dict(ledger.store.conn.execute("select stage, count(*) from trial group by stage").fetchall())
    assert 1 <= stages["S0"] <= 2 and stages.get("S1", 0) <= 2  # the fixed points, not the campaign's s1_count
    # review (step 6): nothing is screened out; every scored point is backtested, whatever its S1 score
    assert stages.get("S2", 0) == stages.get("S1", 0) > 0
    # review (step 6): the points of a model cell are a function of the panel, so a second batch would repeat
    # the first one trial for trial; the cell retires after one batch although max_pulls allows more
    again = CampaignState.new("m2", [cell], max_pulls=3)
    again.record(cell, "unstable", summary_from_json(json.loads(json.dumps(state.history[0]["summary"]))), reward=0.0)
    assert cell.key in again.retired
    plain = Cell("momentum", "1h", 30, "w2")
    other = CampaignState.new("m3", [plain], max_pulls=3)
    other.record(plain, "unstable", summary_from_json(json.loads(json.dumps(state.history[0]["summary"]))), reward=0.0)
    assert plain.key not in other.retired  # searched cells keep their pulls
    families = {
        str(r[0]) for r in ledger.store.conn.execute("select distinct metrics_json from trial where stage = 'S1'")
    }
    assert families and all('"combo@1h/v3"' in f for f in families)
