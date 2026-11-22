"""Phase 2 step 4: operator evaluation (hand-computed values) and leakage tests for every operator."""

import math
from collections.abc import Mapping

import polars as pl
import pytest

from quant_research.backtest.panel import PanelConfig, assemble, decision_grid
from quant_research.strategy.compile import EXPANDING_MIN_HISTORY, compile_spec, evaluate, order
from quant_research.strategy.ops import OPS
from quant_research.strategy.spec import Node, parse_spec

COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "mark_close",
    "premium_close",
    "funding_rate",
    "oi_value",
    "toptrader_ls",
    "taker_ls",
    "spot_close",
)


def panel(values: dict[str, list[float]], *, members: dict[str, list[bool]] | None = None) -> pl.DataFrame:
    """One column `close` per symbol (other inputs derived), decisions 0..n-1."""
    rows: list[dict[str, object]] = []
    for sym, closes in values.items():
        for t, c in enumerate(closes):
            member = True if members is None else members[sym][t]
            rows.append(
                {
                    "symbol": sym,
                    "decision_ns": t,
                    "member": member,
                    "open": c - 0.5,
                    "high": c + 1.0,
                    "low": c - 1.0,
                    "close": c,
                    "volume": 1.0,
                    "quote_volume": c,
                    "mark_close": c,
                    "premium_close": c / 1000,
                    "funding_rate": c / 10000,
                    "oi_value": c * 1000,  # v2.3 step 7 inputs: every data op is covered by the leak test
                    "toptrader_ls": 1 + c / 1000,
                    "taker_ls": 1 - c / 2000,
                    "spot_close": c * 0.999,
                }
            )
    return order(pl.DataFrame(rows))


def ev(frame: pl.DataFrame, node: Mapping[str, object]) -> list[float | None]:
    return evaluate(frame, Node.model_validate(node)).to_list()


def close(op: str, window: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {"op": op, "args": [{"op": "close"}]}
    if window is not None:
        body["window"] = window
    return body


P = panel({"A": [1.0, 2.0, 4.0, 8.0]})


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        (close("sma", 2), [None, 1.5, 3.0, 6.0]),
        (close("ret", 1), [None, 1.0, 1.0, 1.0]),
        (close("delta", 2), [None, None, 3.0, 6.0]),
        (close("lag", 1), [None, 1.0, 2.0, 4.0]),
        (close("rolling_max", 2), [None, 2.0, 4.0, 8.0]),
        (close("rolling_min", 3), [None, None, 1.0, 2.0]),
        (close("std", 2), [None, 0.7071067811865476, 1.4142135623730951, 2.8284271247461903]),
        (close("zscore", 2), [None, 0.7071067811865476, 0.7071067811865476, 0.7071067811865476]),
        (close("ema", 2), [None, 5 / 3, 3.2222222222222223, 6.407407407407407]),
        ({"op": "add", "args": [{"op": "close"}, 1.0]}, [2.0, 3.0, 5.0, 9.0]),
        ({"op": "sub", "args": [{"op": "close"}, 1.0]}, [0.0, 1.0, 3.0, 7.0]),
        ({"op": "mul", "args": [{"op": "close"}, 2.0]}, [2.0, 4.0, 8.0, 16.0]),
        (
            {"op": "div", "args": [{"op": "close"}, {"op": "sub", "args": [{"op": "close"}, 2.0]}]},
            [-1.0, None, 2.0, 8 / 6],
        ),
        ({"op": "neg", "args": [{"op": "close"}]}, [-1.0, -2.0, -4.0, -8.0]),
        ({"op": "abs", "args": [{"op": "sub", "args": [{"op": "close"}, 3.0]}]}, [2.0, 1.0, 1.0, 5.0]),
        ({"op": "sign", "args": [{"op": "sub", "args": [{"op": "close"}, 2.0]}]}, [-1.0, 0.0, 1.0, 1.0]),
        (
            {"op": "log", "args": [{"op": "sub", "args": [{"op": "close"}, 2.0]}]},
            [None, None, 0.6931471805599453, 1.791759469228055],
        ),
        ({"op": "atr", "window": 2}, [None, 2.0, 2.5, 4.0]),  # TR = [2, 2, 3, 5] (high-low 2, gaps 2 and 4+1)
    ],
)
def test_time_series_and_arithmetic_operators_match_hand_values(
    node: dict[str, object], expected: list[float | None]
) -> None:
    assert _same(ev(P, node), expected)


def _same(got: list[float | None], want: list[float | None]) -> bool:
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        assert (g is None) == (w is None), (got, want)
        if g is not None and w is not None:
            assert g == pytest.approx(w)
    return True


def test_comparisons_and_logic() -> None:
    gt = {"op": "gt", "args": [{"op": "close"}, 3.0]}
    lt = {"op": "lt", "args": [{"op": "close"}, 6.0]}
    assert ev(P, gt) == [False, False, True, True]
    assert ev(P, {"op": "and", "args": [gt, lt]}) == [False, False, True, False]
    assert ev(P, {"op": "or", "args": [gt, lt]}) == [True, True, True, True]
    assert ev(P, {"op": "not", "args": [gt]}) == [True, True, False, False]


def test_cross_section_uses_members_at_that_decision_only() -> None:
    frame = panel(
        {"A": [1.0, 1.0], "B": [2.0, 2.0], "C": [3.0, 30.0]},
        members={"A": [True, True], "B": [True, True], "C": [True, False]},
    )
    ranks = dict(zip(zip(frame["symbol"], frame["decision_ns"], strict=True), ev(frame, close("rank_xs")), strict=True))
    assert ranks[("A", 0)] == 0.0 and ranks[("B", 0)] == 0.5 and ranks[("C", 0)] == 1.0
    assert ranks[("A", 1)] == 0.0 and ranks[("B", 1)] == 1.0 and ranks[("C", 1)] is None  # C left the universe
    demeaned = dict(
        zip(zip(frame["symbol"], frame["decision_ns"], strict=True), ev(frame, close("demean_xs")), strict=True)
    )
    assert demeaned[("A", 0)] == -1.0 and demeaned[("A", 1)] == -0.5  # mean of members only
    single = panel({"A": [5.0]})
    assert ev(single, close("rank_xs")) == [0.5] and ev(single, close("zscore_xs")) == [None]


def test_expanding_quantile_ranks_against_the_past_only_after_a_minimum_history() -> None:
    values = [float(i % 7) for i in range(EXPANDING_MIN_HISTORY + 3)]
    got = ev(panel({"A": values}), close("expanding_quantile"))
    assert got[:EXPANDING_MIN_HISTORY] == [None] * EXPANDING_MIN_HISTORY
    past = sorted(values[:EXPANDING_MIN_HISTORY])
    v = values[EXPANDING_MIN_HISTORY]
    assert got[EXPANDING_MIN_HISTORY] == sum(p <= v for p in past) / len(past)


def representative(op: str) -> dict[str, object]:
    s = OPS[op]
    if s.kind == "data":
        return {"op": op, "window": 3} if s.window else {"op": op}
    arg = {"op": "close"}
    args = (
        [arg, {"op": "mark_close"}][: s.max_args]
        if s.kind != "logic"
        else [
            {"op": "gt", "args": [{"op": "close"}, 50.0]},
            {"op": "lt", "args": [{"op": "funding_rate"}, 0.005]},
        ][: s.max_args]
    )
    body: dict[str, object] = {"op": op, "args": args}
    if s.window:
        body["window"] = 3
    return body


def noise_value(i: int, k: int) -> float:
    """Deterministic irregular values in [10, 100) (hash-like; no RNG state, reproducible)."""
    frac = (math.sin(i * 12.9898 + k * 78.233) * 43_758.5453) % 1.0
    return 10.0 + 90.0 * frac


@pytest.mark.parametrize("op", sorted(OPS))
def test_no_operator_can_see_the_future(op: str) -> None:
    """Change every input at decisions >= T: outputs before T must not move (future-value injection)."""
    n, cut = 80, 50
    base = panel({s: [noise_value(i, k) for i in range(n)] for k, s in enumerate(("A", "B", "C"))})
    noise = base.with_columns(
        *(
            pl.when(pl.col("decision_ns") >= cut).then(pl.col(c) * (1.5 + j / 10) + 7).otherwise(pl.col(c)).alias(c)
            for j, c in enumerate(COLUMNS)
        )
    )
    node = representative(op)
    before = [v for v, t in zip(ev(base, node), base["decision_ns"], strict=True) if t < cut]
    after = [v for v, t in zip(ev(noise, node), noise["decision_ns"], strict=True) if t < cut]
    assert before == after


def test_signals_at_a_decision_ignore_the_bar_still_forming_in_the_panel() -> None:
    """Panel-level injection: rewriting 1m data from minute T on cannot change signals at decisions <= T."""
    m = 60 * 10**9
    cfg = PanelConfig(5 * m)
    rows = 200

    def minutes(scale: float) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": ["A"] * rows,
                "event_ns": [i * m for i in range(rows)],
                "open": [100.0 + (i % 13) * (scale if i >= 120 else 1) for i in range(rows)],
                "high": [101.0 + (i % 13) * (scale if i >= 120 else 1) for i in range(rows)],
                "low": [99.0 + (i % 13) * (scale if i >= 120 else 1) for i in range(rows)],
                "close": [100.5 + (i % 13) * (scale if i >= 120 else 1) for i in range(rows)],
                "volume": [1.0] * rows,
                "quote_volume": [1.0] * rows,
                "available_ns": [i * m + 2 * m for i in range(rows)],  # interval + lag as for bulk 1m klines
            }
        )

    spec = parse_spec(
        '{"spec_version":1,"family":"trend","venue":"binance","market":"usdm_futures",'
        '"universe":{"selector":"pit_top_liquidity","n":1},"timeframe":"5m",'
        '"signal":{"op":"zscore","args":[{"op":"close"}],"window":4},'
        '"entry":{"long":{"op":"gt","args":["signal",0.0]}},"sizing":{"method":"equal"}}'
    )
    decisions = decision_grid(0, (rows - 5) * m, cfg)
    clean = compile_spec(spec, assemble(decisions, ["A"], minutes(1.0), cfg)).frame
    dirty = compile_spec(spec, assemble(decisions, ["A"], minutes(9.0), cfg)).frame
    cut = 120 * m
    keep = pl.col("decision_ns") <= cut
    assert clean.filter(keep).equals(dirty.filter(keep))
    assert not clean.filter(~keep).equals(dirty.filter(~keep))  # the injection itself is visible later
