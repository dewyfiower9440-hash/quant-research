"""Canonical form and hash of a StrategySpec (design v2.1 §7: syntactic de-duplication).

Feature references are inlined (feature names do not matter), unused features disappear, commutative
operands are ordered, constants are floats. Two specs with the same logic get the same `spec_hash`."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from quant_research.strategy.ops import OPS
from quant_research.strategy.spec import Node, StrategySpec

Arg = Node | str | float


def expand(arg: Arg, features: Mapping[str, Node], signal: Node | str | None) -> Arg:
    """Inline references: a feature name becomes its tree; "signal" becomes the signal tree. References
    that do not resolve stay strings (S0 rejects them). Features never expand features (no cycles)."""
    if isinstance(arg, str):
        if arg == "signal" and signal is not None:
            return expand(signal, features, None)
        if arg in features:
            return expand(features[arg], {}, None)
        return arg
    if isinstance(arg, Node):
        return Node(op=arg.op, window=arg.window, args=tuple(expand(a, features, signal) for a in arg.args))
    return float(arg)


def canonical_tree(arg: Arg) -> object:
    if isinstance(arg, Node):
        args = [canonical_tree(a) for a in arg.args]
        spec = OPS.get(arg.op)
        if spec is not None and spec.commutative:
            args.sort(key=lambda a: json.dumps(a, sort_keys=True))
        out: dict[str, object] = {"op": arg.op, "args": args}
        if arg.window is not None:
            out["window"] = arg.window
        return out
    if isinstance(arg, str):
        return {"ref": arg}
    return float(arg)


def expanded_trees(spec: StrategySpec) -> dict[str, Arg | None]:
    signal = expand(spec.signal, spec.features, None)
    return {
        "signal": signal,
        "long": None if spec.entry.long is None else expand(spec.entry.long, spec.features, spec.signal),
        "short": None if spec.entry.short is None else expand(spec.entry.short, spec.features, spec.signal),
    }


def canonical_spec(spec: StrategySpec) -> dict[str, object]:
    body = spec.model_dump(mode="json", exclude={"features", "signal", "entry"})
    raw = body.get("execution")
    if isinstance(raw, dict):  # v2.2 B5 fields at their defaults: the hash of every earlier spec stays
        execution = cast(dict[str, object], raw)
        for key, default in (("rebalance_bars", 1), ("min_trade_weight", 0.0)):
            if execution.get(key) == default:
                del execution[key]
    entry: dict[str, object] = {}
    for name, tree in expanded_trees(spec).items():
        canon = None if tree is None else canonical_tree(tree)
        if name == "signal":
            body["signal"] = canon
        else:
            entry[name] = canon
    body["entry"] = entry
    return body


def spec_hash(spec: StrategySpec) -> str:
    text = json.dumps(canonical_spec(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
