"""StrategySpec v1: JSON AST validated with pydantic (design v2.1 §7). Size and nesting are checked
BEFORE parsing; operator semantics, limits and venue rules are checked by S0 (`strategy/s0.py`)."""

from __future__ import annotations

import json
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from quant_research.errors import AppError

MAX_SPEC_BYTES = 64 * 1024
MAX_JSON_NESTING = 64


class SpecError(AppError):
    """A strategy spec that cannot be parsed (size, nesting, schema)."""


class Node(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    op: str
    args: tuple[Node | str | float, ...] = ()  # sub-expression, reference ("signal" / feature name) or constant
    window: int | None = None


class Universe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    selector: Literal["pit_top_liquidity", "fixed"]
    n: int = Field(ge=1, le=100)
    rebalance: Literal["1d"] = "1d"
    symbols: tuple[str, ...] = ()  # selector "fixed" only


class Entry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    long: Node | None = None
    short: Node | None = None


class Exit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    time_stop_bars: int | None = Field(default=None, ge=1, le=10_000)
    stop_loss_atr: float | None = Field(default=None, gt=0, le=50)


class Sizing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    method: Literal["vol_target", "equal"]
    target_ann_vol: float = Field(default=0.15, gt=0, le=1)
    max_leverage: float = Field(default=1.0, gt=0, le=10)
    vol_window: int = Field(default=96, ge=2, le=2_000)


class Execution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    style: Literal["taker", "maker"] = "taker"
    max_participation: float = Field(default=0.01, gt=0, le=0.1)


Family = Literal["trend", "mean_reversion", "momentum", "funding_basis", "volatility"]  # v1 families (§7)
Timeframe = Literal["5m", "15m", "1h", "4h", "1d"]
TIMEFRAME_NS: dict[str, int] = {
    "5m": 300 * 10**9,
    "15m": 900 * 10**9,
    "1h": 3_600 * 10**9,
    "4h": 14_400 * 10**9,
    "1d": 86_400 * 10**9,
}


class StrategySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    spec_version: Literal[1]
    family: Family
    venue: Literal["binance", "upbit"]
    market: Literal["usdm_futures", "spot"]
    universe: Universe
    timeframe: Timeframe
    features: dict[str, Node] = Field(default_factory=lambda: dict[str, Node]())
    signal: Node | str
    entry: Entry
    exit: Exit = Exit()
    sizing: Sizing
    execution: Execution = Execution()


def _nesting(value: object) -> int:
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(item, dict):
            children = list(cast(dict[str, object], item).values())
        elif isinstance(item, list):
            children = cast(list[object], item)
        else:
            continue
        stack.extend((child, depth + 1) for child in children)
    return deepest


def parse_spec(raw: str | bytes) -> StrategySpec:
    """Boundary parser: byte size and JSON nesting first (cheap, before building models), then schema."""
    size = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
    if size > MAX_SPEC_BYTES:
        raise SpecError(f"spec is {size} bytes (limit {MAX_SPEC_BYTES})")
    try:
        body: object = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SpecError(f"spec is not JSON: {e}") from e
    if (depth := _nesting(body)) > MAX_JSON_NESTING:
        raise SpecError(f"spec nesting {depth} exceeds {MAX_JSON_NESTING}")
    try:
        return StrategySpec.model_validate(body)
    except ValueError as e:  # pydantic ValidationError is a ValueError
        raise SpecError(f"spec schema: {e}") from e
