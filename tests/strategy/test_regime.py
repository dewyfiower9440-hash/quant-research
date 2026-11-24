"""v2.4 step 9 (ADR-055 decision 3): the regime gate axis. A gate is a condition on where the reference asset
(BTCUSDT) stands in its own past; a gated family takes positions only while the gate holds."""

from __future__ import annotations

import math
from collections.abc import Mapping

import polars as pl
import pytest

from quant_research.strategy.compile import EXPANDING_MIN_HISTORY, REFERENCE_SYMBOL, compile_spec, evaluate
from quant_research.strategy.s0 import S0Context, check, warmup_bars
from quant_research.strategy.space import (
    INFEASIBLE_V3,
    REGIME_SPACES,
    SPACES_V3,
    Point,
    build_point,
    space_of,
    window_cap_bars,
)
from quant_research.strategy.spec import Node
from quant_research.strategy.templates import (
    MODEL_FAMILIES,
    REGIME_GATES,
    REGIME_SUFFIX,
    base_family,
    regime_gate,
)


def frame(values: Mapping[str, list[float]]) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for sym, closes in values.items():
        for t, c in enumerate(closes):
            rows.append({"symbol": sym, "decision_ns": t, "member": True, "close": c})
    return pl.DataFrame(rows).sort(["symbol", "decision_ns"])


def ev(data: pl.DataFrame, node: Mapping[str, object]) -> list[float | None]:
    return evaluate(data, Node.model_validate(node)).to_list()


REF_Q = {"op": "ref_quantile", "args": [{"op": "close"}]}


def test_ref_quantile_is_the_reference_assets_expanding_quantile_for_every_symbol() -> None:
    n = EXPANDING_MIN_HISTORY + 4
    btc = [float((i * 7) % 11) for i in range(n)]
    other = [100.0 + i for i in range(n)]
    data = frame({REFERENCE_SYMBOL: btc, "ZZZUSDT": other})
    got = ev(data, REF_Q)
    own = ev(frame({REFERENCE_SYMBOL: btc}), {"op": "expanding_quantile", "args": [{"op": "close"}]})
    by_symbol = dict(zip(["first", "second"], [got[:n], got[n:]], strict=True))
    assert by_symbol["first"] == own and by_symbol["second"] == own  # the same series on every symbol's rows
    assert own[:EXPANDING_MIN_HISTORY] == [None] * EXPANDING_MIN_HISTORY and own[-1] is not None


def test_ref_quantile_reads_the_past_only_and_is_missing_without_the_reference() -> None:
    n = EXPANDING_MIN_HISTORY + 6
    btc = [float((i * 5) % 13) for i in range(n)]
    data = frame({REFERENCE_SYMBOL: btc, "ZZZUSDT": [1.0] * n})
    changed = frame({REFERENCE_SYMBOL: [*btc[:-1], 1e9], "ZZZUSDT": [1.0] * n})  # only the last bar differs
    assert ev(data, REF_Q)[: n - 1] == ev(changed, REF_Q)[: n - 1]
    assert ev(frame({"ZZZUSDT": [1.0] * n}), REF_Q) == [None] * n  # no reference asset in the panel: no regime


def test_gates_are_conditions_on_the_reference_trend_and_volatility() -> None:
    assert REGIME_GATES == ("trend_up", "trend_not_down", "vol_low", "vol_not_high")
    up = regime_gate("trend_up", "1d")
    assert up == {
        "op": "gt",
        "args": [{"op": "ref_quantile", "args": [{"op": "ret", "args": [{"op": "close"}], "window": 45}]}, 0.6667],
    }
    assert "0.3333" in str(regime_gate("trend_not_down", "4h")) and "'window': 270" in str(
        regime_gate("trend_not_down", "4h")
    )  # the lower third; 45 days of 4h bars
    calm = regime_gate("vol_low", "4h")
    assert calm["op"] == "lt" and "'window': 180" in str(calm)  # 30 days of 4h bars
    assert regime_gate("vol_not_high", "1h")["op"] == "lt"
    with pytest.raises(ValueError, match="unknown regime gate"):
        regime_gate("sideways", "1d")
    assert base_family("momentum" + REGIME_SUFFIX) == "momentum" and base_family("momentum") == "momentum"


def test_regime_spaces_add_one_axis_to_every_searched_v3_space() -> None:
    assert set(REGIME_SPACES) == {
        name.replace("@", REGIME_SUFFIX + "@") for name in SPACES_V3 if name.split("@")[0] not in MODEL_FAMILIES
    }
    base, gated = SPACES_V3["momentum@4h/v3"], REGIME_SPACES["momentum~regime@4h/v3"]
    assert gated.params == base.params and gated.template_family == "momentum" and gated.warmup_bars == base.warmup_bars
    assert [a.name for a in gated.struct] == [*(a.name for a in base.struct), "regime_gate"]
    assert next(a.choices for a in gated.struct if a.name == "regime_gate") == REGIME_GATES
    assert space_of("momentum~regime@4h/v3") is gated
    assert not any(n.startswith(("trend~regime@1d", "volatility~regime@1d")) for n in REGIME_SPACES)
    assert {"trend@1d", "volatility@1d"} == INFEASIBLE_V3


def _point(name: str, gate: str) -> Point:
    space = REGIME_SPACES[name]
    params = {a.name: a.values()[0] for a in space.params}
    struct = {a.name: a.choices[0] for a in space.struct} | {"regime_gate": gate, "smooth": 0}
    return Point.of(space.family, params, struct)


@pytest.mark.parametrize("gate", REGIME_GATES)
@pytest.mark.parametrize("name", ["momentum~regime@1d/v3", "xs_reversal~regime@4h/v3", "low_vol~regime@1h/v3"])
def test_a_gated_spec_passes_s0_inside_the_block_warmup(name: str, gate: str) -> None:
    space = REGIME_SPACES[name]
    spec = build_point(space, _point(name, gate))
    assert spec.family == space.template_family
    assert check(spec, S0Context(max_warmup_bars=space.warmup_bars)) == []
    assert warmup_bars(spec) < window_cap_bars(space.timeframe)
    assert "ref_quantile" in spec.model_dump_json()


def test_the_warmup_of_a_gate_counts_the_quantiles_minimum_history() -> None:
    """Review (step 9): a quantile against the own past is undefined for its first EXPANDING_MIN_HISTORY values;
    the warm-up a gated spec needs is the window plus that history, and every gate fits every timeframe."""
    from quant_research.strategy.s0 import tree_lookback
    from quant_research.strategy.templates import REGIME_TREND_DAYS, REGIME_VOL_DAYS

    per_day = {"1h": 24, "4h": 6, "1d": 1}
    for tf, bars in per_day.items():
        trend = tree_lookback(Node.model_validate(regime_gate("trend_up", tf)))
        vol = tree_lookback(Node.model_validate(regime_gate("vol_low", tf)))
        assert trend == REGIME_TREND_DAYS * bars + EXPANDING_MIN_HISTORY
        assert vol == REGIME_VOL_DAYS * bars + 1 + EXPANDING_MIN_HISTORY  # std of 1-bar returns
        assert max(trend, vol) < window_cap_bars(tf)
    own_past = Node.model_validate({"op": "expanding_quantile", "args": [{"op": "close"}]})
    assert tree_lookback(own_past) == EXPANDING_MIN_HISTORY


def test_a_gated_strategy_holds_nothing_while_the_gate_is_closed() -> None:
    """The reference asset falls faster and faster: its 60-bar return sits at the bottom of its own past, so
    `trend_up` never opens, while the same rule without a gate does trade."""
    n = 60 + EXPANDING_MIN_HISTORY + 40
    # an accelerating fall: every 45-bar return is lower than all before it, so its quantile in its past is 0
    falling = [1000.0 * math.exp(-0.00002 * i * i) for i in range(n)]
    rows: list[dict[str, object]] = []
    for k, sym in enumerate([REFERENCE_SYMBOL, "AAAUSDT", "BBBUSDT", "CCCUSDT"]):
        for t in range(n):
            price = falling[t] if k == 0 else 100.0 * (1.0 + 0.01 * ((t * (k + 2)) % 9))
            rows.append(
                {
                    "symbol": sym,
                    "decision_ns": t,
                    "member": True,
                    "tradable": True,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1.0,
                    "quote_volume": 1.0 + k,
                }
            )
    panel = pl.DataFrame(rows)
    name = "xs_reversal~regime@1d/v3"
    gated = compile_spec(build_point(REGIME_SPACES[name], _point(name, "trend_up")), panel).frame
    assert not gated["long"].any() and not gated["short"].any()
    base = SPACES_V3["xs_reversal@1d/v3"]
    plain_point = Point.of(
        base.family,
        {a.name: a.values()[0] for a in base.params},
        {a.name: a.choices[0] for a in base.struct} | {"smooth": 0},
    )
    plain = compile_spec(build_point(base, plain_point), panel).frame
    assert plain["long"].any() and plain["short"].any()  # the same rule without a gate does take positions
    opened = compile_spec(build_point(REGIME_SPACES[name], _point(name, "vol_not_high")), panel).frame
    late = opened.filter(pl.col("decision_ns") >= n - 20)
    assert set(late["long"].to_list()) <= {True, False}  # a gate that can open leaves the rule's own entries
