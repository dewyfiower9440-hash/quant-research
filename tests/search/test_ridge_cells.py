"""v2.4 step 7 (ADR-054, design v2.3 section 7): the ridge cell M1-R. Terms = the combination's constituents plus
fixed auxiliary features; coefficients fitted on the fit part with purge and embargo, written into the point."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from search_fixtures import T0, D, planted_full

from quant_research.backtest.panel import Panel, slice_panel
from quant_research.catalog.store import Catalog
from quant_research.research.ledger import TrialLedger
from quant_research.search.bandit import BanditPrior, FamilyBandit
from quant_research.search.campaign import CampaignState, Cell
from quant_research.search.campaign_runner import CampaignConfig, run_campaign
from quant_research.search.model_cells import (
    MIN_TRAINING_ROWS,
    MODEL_HORIZON_BARS,
    RIDGE_ALPHAS,
    longest_window,
    model_points,
    ridge_coefficients,
    ridge_points,
    term_names,
    training_rows,
)
from quant_research.search.optimizers import candidate_of
from quant_research.search.pipeline import split_for_reward
from quant_research.search.s1 import S1Config, evaluate_s1
from quant_research.strategy.s0 import check
from quant_research.strategy.space import MODEL_SPACES, build_point
from quant_research.strategy.templates import AUX_TERMS, CONSTITUENTS, aux_tree, model_term_names

RIDGE_1H = MODEL_SPACES["ridge@1h/v3"]


@pytest.fixture(scope="module")
def planted60() -> Panel:
    return planted_full(60)


@pytest.fixture(scope="module")
def fit_part(planted60: Panel) -> Panel:
    selection = slice_panel(planted60, T0 + 16 * D, T0 + 45 * D, warmup_ns=16 * D)
    return split_for_reward(selection, 0.25)[0]


def test_the_ridge_space_adds_the_fixed_auxiliary_features() -> None:
    assert set(MODEL_SPACES) == {f"{f}@{tf}/v3" for f in ("combo", "ridge") for tf in ("1h", "4h", "1d")}
    assert model_term_names("ridge") == (*CONSTITUENTS, *AUX_TERMS) and term_names(RIDGE_1H) == (
        *CONSTITUENTS,
        *AUX_TERMS,
    )
    assert AUX_TERMS == ("ret1", "ret5", "ret20", "vol20", "volume20", "premium_z20")
    assert aux_tree("vol20") == {
        "op": "std",
        "args": [{"op": "ret", "args": [{"op": "close"}], "window": 1}],
        "window": 20,
    }
    with pytest.raises(ValueError, match="unknown auxiliary"):
        aux_tree("ret99")
    names = [a.name for a in RIDGE_1H.params]
    assert "w_ret1" in names and "p_ret1" not in names and "p_momentum" in names  # aux windows are not parameters
    for tf in ("1h", "4h", "1d"):
        assert longest_window(MODEL_SPACES[f"ridge@{tf}/v3"]) < (MODEL_SPACES[f"ridge@{tf}/v3"].warmup_bars or 0)
    assert MODEL_HORIZON_BARS == {"1h": 24, "4h": 6, "1d": 1} and RIDGE_ALPHAS == (1.0, 10.0, 100.0)


def test_ridge_coefficients_solve_the_penalised_normal_equations() -> None:
    x = np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [-1.0, 0.5]], dtype=np.float64)
    y = np.asarray([1.0, 2.0, 3.0, 0.0], dtype=np.float64)
    for alpha in (0.5, 10.0):
        b = ridge_coefficients(x, y, alpha)
        n = x.shape[0]  # the penalty is on the per-row scale: (X'X / N + alpha I) b = X'y / N
        assert (x.T @ x / n + alpha * np.eye(2)) @ b == pytest.approx(x.T @ y / n)
    weak, strong = ridge_coefficients(x, y, 0.5), ridge_coefficients(x, y, 100.0)
    assert float(np.dot(strong, strong)) < float(np.dot(weak, weak))  # a larger penalty shrinks
    assert ridge_coefficients(x, y, 3.0).tolist() == ridge_coefficients(x, y, 3.0).tolist()  # deterministic
    with pytest.raises(ValueError, match="alpha"):
        ridge_coefficients(x, y, 0.0)
    with pytest.raises(ValueError, match="rows"):
        ridge_coefficients(x[:0], y[:0], 1.0)


def test_the_three_alphas_stay_three_different_models_at_real_row_counts() -> None:
    """Review (step 7): with the penalty added to X'X itself, alpha = 100 vanished against N = 150,000 rows and the
    three registered settings collapsed into one point on the weight grid. On the per-row scale they do not."""
    from quant_research.search.model_cells import normalise

    rng = np.random.default_rng(3)
    n, k = 150_000, 13
    common = rng.standard_normal((n, 1))
    x = np.asarray(0.6 * common + 0.8 * rng.standard_normal((n, k)), dtype=np.float64)  # correlated features
    beta = np.linspace(-0.02, 0.03, k)
    y = np.asarray(x @ beta + rng.standard_normal(n), dtype=np.float64)
    weights = [
        normalise({str(i): float(c) for i, c in enumerate(ridge_coefficients(x, y, alpha))}) for alpha in RIDGE_ALPHAS
    ]
    assert all(w is not None for w in weights)
    assert weights[0] != weights[1] and weights[1] != weights[2] and weights[0] != weights[2]


def test_training_rows_are_purged_embargoed_complete_and_standardised(fit_part: Panel) -> None:
    horizon = 24
    x, y = training_rows(RIDGE_1H, fit_part, horizon_bars=horizon, embargo_bars=0)
    assert x.shape[1] == len(CONSTITUENTS) + len(AUX_TERMS) and x.shape[0] == y.shape[0] > MIN_TRAINING_ROWS
    assert bool(np.isfinite(x).all()) and bool(np.isfinite(y).all())  # rows with a missing value are dropped
    assert float(y.min()) == pytest.approx(-0.5) and float(y.max()) == pytest.approx(0.5)  # centred rank
    assert abs(float(x.mean())) < 1e-9  # standardised across the members of each decision
    start = int(fit_part.evaluate_from_ns or 0)
    decisions = sorted(d for d in set(fit_part.frame["decision_ns"].to_list()) if d >= start)
    symbols = fit_part.frame["symbol"].n_unique()
    # purge: the last `horizon` decisions have no label inside the fit part, so they give no row
    assert x.shape[0] <= (len(decisions) - horizon) * symbols
    fewer, _ = training_rows(RIDGE_1H, fit_part, horizon_bars=horizon, embargo_bars=100)
    assert x.shape[0] - fewer.shape[0] == (100 - horizon) * symbols  # the embargo drops whole decisions
    nothing, _ = training_rows(RIDGE_1H, fit_part, horizon_bars=horizon, embargo_bars=10**6)
    assert nothing.shape[0] == 0
    with pytest.raises(ValueError, match="embargo_bars"):
        training_rows(RIDGE_1H, fit_part, horizon_bars=horizon, embargo_bars=-1)


def test_a_feature_built_from_the_future_would_be_seen_in_the_label_alignment(fit_part: Panel) -> None:
    """Leak check of the label: a training label never reaches past the fit part (it is the fill `horizon`
    decisions ahead INSIDE the part), so changing every price after the part cannot change the training set."""
    x, y = training_rows(RIDGE_1H, fit_part, horizon_bars=24, embargo_bars=0)
    last = max(int(d) for d in fit_part.frame["decision_ns"].unique().to_list())
    first_symbol = sorted(set(fit_part.frame["symbol"].to_list()))[0]
    bumped = pl.when(pl.col("symbol") == first_symbol).then(pl.col("exec_open") * 2.0).otherwise(pl.col("exec_open"))
    tail = fit_part.frame.filter(pl.col("decision_ns") == last).with_columns(bumped.alias("exec_open"))
    extended = Panel(
        pl.concat([fit_part.frame.filter(pl.col("decision_ns") < last), tail]).sort(["symbol", "decision_ns"]),
        fit_part.funding,
        fit_part.files,
        fit_part.config,
        fit_part.evaluate_from_ns,
    )
    # the detector works: with the purge alone, the last labelled decision reads the last bar's fill
    _, y_purged = training_rows(RIDGE_1H, extended, horizon_bars=24, embargo_bars=0)
    assert y_purged.tolist() != y.tolist()
    # one decision of embargo beyond the label horizon: no training row reads the last bar any more
    x2, y2 = training_rows(RIDGE_1H, extended, horizon_bars=24, embargo_bars=25)
    x1, y1 = training_rows(RIDGE_1H, fit_part, horizon_bars=24, embargo_bars=25)
    assert x1.tolist() == x2.tolist() and y1.tolist() == y2.tolist()
    assert x.shape[0] - x1.shape[0] == fit_part.frame["symbol"].n_unique()  # exactly one decision fewer


def test_ridge_recovers_the_planted_terms_and_is_reproducible(fit_part: Panel) -> None:
    points, record = ridge_points(RIDGE_1H, fit_part, horizon_bars=24, embargo_bars=24)
    again, _ = ridge_points(RIDGE_1H, fit_part, horizon_bars=24, embargo_bars=24)
    assert points == again and 1 <= len(points) <= len(RIDGE_ALPHAS)
    assert set(record) == {"rows", "embargo_bars", "alphas"} and record["embargo_bars"] == 24
    weights = points[0].params_dict()
    carriers = ("liquidity", "premium_basis", "funding_carry")
    assert sum(weights[f"w_{n}"] for n in carriers) > 0  # oriented as registered: the planted side is positive
    assert sum(abs(weights[f"w_{n}"]) for n in term_names(RIDGE_1H)) == pytest.approx(1.0, abs=2e-3)
    cand = candidate_of(RIDGE_1H, points[0])
    assert cand.reasons == () and cand.spec is not None and cand.spec.family == "ridge"
    assert build_point(RIDGE_1H, points[0]) == cand.spec and check(cand.spec) == []
    default_points, default_record = model_points(RIDGE_1H, fit_part)  # the registered embargo: the longest window
    assert default_record["embargo_bars"] == longest_window(RIDGE_1H) == 361
    assert isinstance(default_record["rows"], int) and (default_points or default_record["rows"] < MIN_TRAINING_ROWS)


def test_too_few_training_rows_give_no_point(fit_part: Panel) -> None:
    points, record = ridge_points(RIDGE_1H, fit_part, horizon_bars=24, embargo_bars=10**6)
    assert points == [] and record["rows"] == 0 and record["alphas"] == {}


def test_ridge_on_characteristics_unrelated_to_returns_shows_no_edge_later() -> None:
    """Overfit probe (design v2.3 section 7): the same characteristics with no effect on the returns. Whatever
    the fit finds must not carry over to the part it never saw."""
    full = planted_full(60, effect=0.0)
    selection = slice_panel(full, T0 + 16 * D, T0 + 45 * D, warmup_ns=16 * D)
    fit, validation = split_for_reward(selection, 0.25)
    points, _ = ridge_points(RIDGE_1H, fit, horizon_bars=24, embargo_bars=24)
    assert points
    later = slice_panel(full, T0 + 45 * D, T0 + 59 * D, warmup_ns=16 * D)
    for panel in (validation, later):
        result = evaluate_s1(build_point(RIDGE_1H, points[0]), panel, S1Config(horizon_bars=24))
        assert result.ic_hac_t is None or abs(result.ic_hac_t) < 4.0


def test_a_ridge_cell_runs_its_fixed_points_through_the_funnel(
    env: tuple[Catalog, TrialLedger], planted60: Panel
) -> None:
    catalog, ledger = env
    cell = Cell("ridge", "1h", 30, "w2")
    provider = (
        slice_panel(planted60, T0 + 16 * D, T0 + 45 * D, warmup_ns=16 * D),
        slice_panel(planted60, T0 + 45 * D, T0 + 59 * D, warmup_ns=16 * D),
    )
    state = CampaignState.new("rg", [cell], max_pulls=1)
    out = run_campaign(
        state,
        FamilyBandit(tuple(c.key for c in state.all_cells()), BanditPrior(min_pulls=0.0)),
        ledger=ledger,
        catalog=catalog,
        panels=lambda _cell: provider,
        config=CampaignConfig(seed=2, s1_count=60, top_k=5, space_version=3, max_batches=1),
        gate=lambda: (True, ""),
        stop=lambda: False,
        persist=lambda _s, _b: None,
    )
    assert out.batches == 1 and state.history[0]["cause"] not in ("error", "data_missing", "infeasible")
    stages = dict(ledger.store.conn.execute("select stage, count(*) from trial group by stage").fetchall())
    assert stages.get("S0", 0) <= len(RIDGE_ALPHAS)  # at most one point per alpha, never the campaign's s1_count
