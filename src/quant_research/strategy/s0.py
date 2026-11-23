"""S0 static check of a StrategySpec (design v2.1 §7, §9-5). Returns every violation with its reason;
an empty list means the spec may be evaluated. Pure."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from quant_research.strategy.canonical import Arg, expanded_trees
from quant_research.strategy.ops import MAX_ABS_CONST, MAX_DEPTH, MAX_NODES, MAX_WINDOW, OPS, lookback
from quant_research.strategy.spec import TIMEFRAME_NS, Node, StrategySpec

ValueType = Literal["num", "bool"]
HOUR_NS = 3_600 * 10**9
YEAR_NS = 365 * 86_400 * 10**9
SUPPORTED_MARKETS = {("binance", "usdm_futures")}  # Phase 2 panel; spot / Upbit need OPS-003/006 data


@dataclass(frozen=True, slots=True)
class S0Context:
    capital: float = 10_000.0
    min_notional: float = 100.0  # conservative exchange minimum per order (filters before snapshots unknown)
    round_trip_cost: float = 0.0016  # 2 x taker 0.05% + 2 x 3 bps half-spread prior, fraction of notional
    # Upper bound (always in the market, a round trip every time stop) per year, as a fraction of capital.
    # Initial prior (ADR-021): only pathological churn is refused statically; realized cost is reported per run.
    max_annual_cost: float = 10.0
    # ADR-051: bars of warm-up the research block gives every decision (None: not checked, e.g. v1 / v2 spaces)
    max_warmup_bars: int | None = None


def _tree_lookback(arg: Arg) -> int:
    if not isinstance(arg, Node) or arg.op not in OPS:
        return 0
    return lookback(arg.op, arg.window, [_tree_lookback(a) for a in arg.args])


def warmup_bars(spec: StrategySpec) -> int:
    """Bars of history the spec needs before its first decision: the deepest window chain of its signal and entry
    trees (nested windows add up), or the sizing volatility window if that is longer."""
    trees = [t for t in expanded_trees(spec).values() if t is not None]
    return max([spec.sizing.vol_window, *(_tree_lookback(t) for t in trees)])


def _type(arg: Arg) -> ValueType:
    if isinstance(arg, Node) and arg.op in OPS and OPS[arg.op].kind in ("compare", "logic"):
        return "bool"
    return "num"


def _walk(arg: Arg, where: str, depth: int, out: list[str]) -> tuple[int, int]:
    """(max depth, node count) of an expanded tree; structural violations go to `out`."""
    if isinstance(arg, str):
        out.append(f"{where}: unresolved reference {arg!r}")
        return depth, 0
    if not isinstance(arg, Node):
        if not math.isfinite(arg) or abs(arg) > MAX_ABS_CONST:
            out.append(f"{where}: constant {arg!r} outside [-{MAX_ABS_CONST:g}, {MAX_ABS_CONST:g}]")
        return depth, 0
    spec = OPS.get(arg.op)
    if spec is None:
        out.append(f"{where}: unknown operator {arg.op!r}")
        return depth, 1
    if not spec.min_args <= len(arg.args) <= spec.max_args:
        out.append(f"{where}: {arg.op} takes {spec.min_args}..{spec.max_args} arguments, got {len(arg.args)}")
    if spec.window and arg.window is None:
        out.append(f"{where}: {arg.op} needs a window")
    if not spec.window and arg.window is not None:
        out.append(f"{where}: {arg.op} takes no window")
    if arg.window is not None and not 1 <= arg.window <= MAX_WINDOW:
        out.append(f"{where}: window {arg.window} outside [1, {MAX_WINDOW}]")
    want: ValueType = "bool" if spec.kind == "logic" else "num"
    for a in arg.args:
        if not isinstance(a, str) and _type(a) != want:  # unresolved references are reported by _walk
            out.append(f"{where}: {arg.op} expects {want} arguments")
    deepest, nodes = depth, 1
    for a in arg.args:
        d, n = _walk(a, where, depth + 1, out)
        deepest, nodes = max(deepest, d), nodes + n
    return deepest, nodes


def check(spec: StrategySpec, ctx: S0Context | None = None) -> list[str]:
    c = ctx or S0Context()
    out: list[str] = []
    for name, feature in spec.features.items():  # features may not reference features or the signal
        for ref in _refs(feature):
            out.append(f"feature {name}: may not reference {ref!r}")
    if isinstance(spec.signal, str) and spec.signal not in spec.features:
        out.append(f"signal: unresolved reference {spec.signal!r}")
    if "signal" in _refs(spec.signal):
        out.append("signal: may not reference itself")
    trees = expanded_trees(spec)
    total = 0
    for where, tree in trees.items():
        if tree is None:
            continue
        depth, nodes = _walk(tree, where, 1, out)
        total += nodes
        if depth > MAX_DEPTH:
            out.append(f"{where}: depth {depth} exceeds {MAX_DEPTH}")
        want: ValueType = "num" if where == "signal" else "bool"
        if _type(tree) != want:
            out.append(f"{where}: must be a {'number' if want == 'num' else 'condition'}")
    if total > MAX_NODES:
        out.append(f"spec: {total} nodes exceed {MAX_NODES}")
    if spec.entry.long is None and spec.entry.short is None:
        out.append("entry: needs a long or a short condition")
    # >= : one bar of margin, a shifted operator (ret over w bars) reads w + 1 rows with the current one
    if c.max_warmup_bars is not None and (need := warmup_bars(spec)) >= c.max_warmup_bars:
        out.append(f"infeasible: needs {need} bars of warm-up, the research block gives {c.max_warmup_bars}")
    # venue rules (§7): no short selling on Binance spot or Upbit
    if spec.venue == "upbit" and spec.market != "spot":
        out.append("venue: Upbit has spot markets only")
    if spec.market == "spot" and spec.entry.short is not None:
        out.append("entry: short positions are impossible on spot (Binance spot and Upbit forbid short selling)")
    if (spec.venue, spec.market) not in SUPPORTED_MARKETS:
        out.append(f"venue: {spec.venue} {spec.market} panel is not implemented in Phase 2 (data: OPS-003, OPS-006)")
    # fidelity (§9-5): maker is paper-only; < 1 h holding or > 1 % participation needs L2 (aggTrades, not collected)
    if spec.execution.style == "maker":
        out.append("execution: maker / limit strategies are paper-trading only (no historical order book)")
    if spec.execution.max_participation > 0.01:
        out.append("execution: participation above 1% needs L2 fidelity (aggTrades not collected)")
    bar = TIMEFRAME_NS[spec.timeframe]
    if spec.exit.time_stop_bars is not None and spec.exit.time_stop_bars * bar < HOUR_NS:
        out.append("exit: holding below 1 hour needs L2 fidelity (aggTrades not collected)")
    # universe
    if spec.universe.selector == "fixed" and not spec.universe.symbols:
        out.append("universe: selector 'fixed' needs symbols")
    if spec.universe.selector != "fixed" and spec.universe.symbols:
        out.append("universe: symbols are only allowed with selector 'fixed'")
    # filters and costs (§7)
    per_position = c.capital * spec.sizing.max_leverage / spec.universe.n
    if per_position < c.min_notional:
        out.append(f"sizing: {per_position:.2f} per position is below the minimum notional {c.min_notional:g}")
    if spec.exit.time_stop_bars is not None:
        trips = YEAR_NS / (spec.exit.time_stop_bars * bar)
        annual = trips * c.round_trip_cost * spec.sizing.max_leverage
        if annual > c.max_annual_cost:
            out.append(f"cost: up to {annual:.2f} x capital per year exceeds the cap {c.max_annual_cost:g}")
    return out


def _refs(arg: Arg) -> set[str]:
    if isinstance(arg, str):
        return {arg}
    found: set[str] = set()
    if isinstance(arg, Node):
        for a in arg.args:
            found |= _refs(a)
    return found
