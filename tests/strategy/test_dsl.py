"""Phase 2 step 2: StrategySpec parsing, limits, canonical hash and S0 (design v2.1 §7, §9-5)."""

import json
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant_research.strategy.canonical import spec_hash
from quant_research.strategy.ops import MAX_WINDOW, OPS
from quant_research.strategy.s0 import check
from quant_research.strategy.spec import MAX_SPEC_BYTES, Node, SpecError, StrategySpec, parse_spec


def base(**changes: object) -> dict[str, object]:
    """The §7 example (participation lowered to 1%: above that §9-5 requires L2)."""
    body: dict[str, object] = {
        "spec_version": 1,
        "family": "funding_basis",
        "venue": "binance",
        "market": "usdm_futures",
        "universe": {"selector": "pit_top_liquidity", "n": 30, "rebalance": "1d"},
        "timeframe": "15m",
        "features": {"f1": {"op": "zscore", "args": [{"op": "funding_rate"}], "window": 90}},
        "signal": {"op": "rank_xs", "args": ["f1"]},
        "entry": {"long": {"op": "lt", "args": ["signal", 0.2]}, "short": {"op": "gt", "args": ["signal", 0.8]}},
        "exit": {"time_stop_bars": 32, "stop_loss_atr": 3.0},
        "sizing": {"method": "vol_target", "target_ann_vol": 0.15, "max_leverage": 2},
        "execution": {"style": "taker", "max_participation": 0.01},
    }
    body.update(changes)
    return body


def spec(**changes: object) -> StrategySpec:
    return parse_spec(json.dumps(base(**changes)))


def test_the_design_example_parses_and_passes_s0() -> None:
    assert check(spec()) == []


def test_the_design_example_as_written_needs_l2_because_of_two_percent_participation() -> None:
    violations = check(spec(execution={"style": "taker", "max_participation": 0.02}))
    assert violations == ["execution: participation above 1% needs L2 fidelity (aggTrades not collected)"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("x" * (MAX_SPEC_BYTES + 1), "bytes"),
        ("[" * 70 + "]" * 70, "nesting"),
        ("{not json", "not JSON"),
        (json.dumps(base(extra_field=1)), "schema"),
        (json.dumps(base(timeframe="3m")), "schema"),
    ],
)
def test_parse_rejects_oversized_nested_malformed_or_off_schema_input(raw: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(raw)


def test_hash_ignores_feature_names_unused_features_operand_order_and_int_float() -> None:
    renamed = spec(
        features={
            "carry": {"op": "zscore", "args": [{"op": "funding_rate"}], "window": 90},
            "unused": {"op": "close"},
        },
        signal={"op": "rank_xs", "args": ["carry"]},
    )
    assert spec_hash(renamed) == spec_hash(spec())
    a = spec(signal={"op": "add", "args": [{"op": "close"}, 1]})
    b = spec(signal={"op": "add", "args": [1.0, {"op": "close"}]})
    assert spec_hash(a) == spec_hash(b)  # add is commutative; 1 == 1.0
    c = spec(signal={"op": "sub", "args": [1.0, {"op": "close"}]})
    d = spec(signal={"op": "sub", "args": [{"op": "close"}, 1.0]})
    assert spec_hash(c) != spec_hash(d)  # sub is not
    longer = spec(features={"f1": {"op": "zscore", "args": [{"op": "funding_rate"}], "window": 91}})
    assert spec_hash(longer) != spec_hash(spec())


def _deep(depth: int) -> dict[str, object]:
    node: dict[str, object] = {"op": "close"}
    for _ in range(depth - 1):
        node = {"op": "abs", "args": [node]}
    return node


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        # the limit applies to the expanded trees: entry conditions inline the signal one level down
        ({"signal": _deep(7)}, None),
        ({"signal": _deep(8)}, "long: depth 9 exceeds 8"),
        ({"signal": _deep(9)}, "signal: depth 9 exceeds 8"),
        ({"signal": {"op": "sma", "args": [{"op": "close"}], "window": MAX_WINDOW}}, None),
        ({"signal": {"op": "sma", "args": [{"op": "close"}], "window": MAX_WINDOW + 1}}, "window 2001 outside"),
        ({"signal": {"op": "sma", "args": [{"op": "close"}]}}, "sma needs a window"),
        ({"signal": {"op": "rank_xs", "args": [{"op": "close"}], "window": 3}}, "rank_xs takes no window"),
        ({"signal": {"op": "add", "args": [{"op": "close"}, 1e7]}}, "constant 10000000.0 outside"),
        ({"signal": {"op": "add", "args": [{"op": "close"}]}}, "add takes 2..2 arguments, got 1"),
        ({"signal": {"op": "magic", "args": []}}, "unknown operator 'magic'"),
        ({"signal": {"op": "rank_xs", "args": ["nope"]}}, "unresolved reference 'nope'"),
        ({"signal": {"op": "lt", "args": [{"op": "close"}, 1]}}, "signal: must be a number"),
        ({"entry": {"long": {"op": "close"}}}, "long: must be a condition"),
        ({"entry": {"long": {"op": "and", "args": [{"op": "close"}, {"op": "close"}]}}}, "and expects bool"),
        ({"entry": {}}, "entry: needs a long or a short condition"),
        ({"features": {"f1": {"op": "abs", "args": ["f2"]}, "f2": {"op": "close"}}}, "may not reference 'f2'"),
    ],
)
def test_structural_limits_and_types(changes: dict[str, object], expected: str | None) -> None:
    violations = check(spec(**changes))
    if expected is None:
        assert violations == []
    else:
        assert any(expected in v for v in violations), violations


def test_more_than_64_nodes_are_rejected() -> None:
    wide: dict[str, object] = {"op": "close"}
    for _ in range(7):  # a balanced tree of depth 8 has 2^8 - 1 = 255 nodes
        wide = {"op": "add", "args": [wide, wide]}
    assert any("nodes exceed 64" in v for v in check(spec(signal=wide)))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"market": "spot"}, "short positions are impossible on spot"),
        ({"venue": "upbit", "market": "spot"}, "panel is not implemented"),
        ({"venue": "upbit"}, "Upbit has spot markets only"),
        ({"execution": {"style": "maker", "max_participation": 0.01}}, "paper-trading only"),
        ({"exit": {"time_stop_bars": 3}}, "holding below 1 hour needs L2"),
        ({"universe": {"selector": "fixed", "n": 2}}, "needs symbols"),
        ({"universe": {"selector": "pit_top_liquidity", "n": 2, "symbols": ["BTCUSDT"]}}, "only allowed"),
        ({"sizing": {"method": "equal", "max_leverage": 0.2}}, "below the minimum notional"),
        ({"exit": {"time_stop_bars": 4}, "sizing": {"method": "equal", "max_leverage": 10}}, "exceeds the cap"),
    ],
)
def test_s0_venue_fidelity_universe_filter_and_cost_rules(changes: dict[str, object], expected: str) -> None:
    assert any(expected in v for v in check(spec(**changes))), check(spec(**changes))


NUMERIC_UNARY = sorted(n for n, s in OPS.items() if s.kind in ("arith", "xs", "regime") and s.max_args == 1)
NUMERIC_BINARY = sorted(n for n, s in OPS.items() if s.kind == "arith" and s.max_args == 2)
TS = sorted(n for n, s in OPS.items() if s.kind == "ts")
LEAVES = sorted(n for n, s in OPS.items() if s.kind == "data" and not s.window)


Tree = dict[str, object]


def _leaf(op: str) -> Tree:
    return {"op": op}


def _unary(op: str, a: Tree) -> Tree:
    return {"op": op, "args": [a]}


def _binary(op: str, a: Tree, b: Tree) -> Tree:
    return {"op": op, "args": [a, b]}


def _ts(op: str, a: Tree, window: int) -> Tree:
    return {"op": op, "args": [a], "window": window}


def numeric_trees(max_depth: int) -> st.SearchStrategy[Tree]:
    leaf = st.sampled_from(LEAVES).map(_leaf)

    def extend(children: st.SearchStrategy[Tree]) -> st.SearchStrategy[Tree]:
        return st.one_of(
            st.builds(_unary, st.sampled_from(NUMERIC_UNARY), children),
            st.builds(_binary, st.sampled_from(NUMERIC_BINARY), children, children),
            st.builds(_ts, st.sampled_from(TS), children, st.integers(1, MAX_WINDOW)),
        )

    return st.recursive(leaf, extend, max_leaves=6).filter(lambda t: _depth(t) <= max_depth)


def _depth(t: Tree) -> int:
    args = t.get("args")
    if not isinstance(args, list):
        return 1
    return 1 + max((_depth(cast(Tree, a)) for a in cast(list[object], args)), default=0)


@settings(max_examples=150, deadline=None)
@given(tree=numeric_trees(max_depth=7), threshold=st.floats(-10, 10, allow_nan=False))
def test_grammar_property_valid_trees_pass_s0_and_hash_survives_a_json_round_trip(
    tree: dict[str, object], threshold: float
) -> None:
    s = spec(
        features={"f1": tree},
        signal="f1",
        entry={"long": {"op": "gt", "args": ["signal", threshold]}},
    )
    assert check(s) == []
    again = parse_spec(s.model_dump_json())
    assert again == s and spec_hash(again) == spec_hash(s)
    inline = spec(features={}, signal=tree, entry={"long": {"op": "gt", "args": ["signal", threshold]}})
    assert spec_hash(inline) == spec_hash(s)  # naming a feature does not change the strategy


def test_node_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Node.model_validate({"op": "close", "lookahead": 1})
