"""Pre-registered Phase 4 search space (design v2.1 §7-§8): discrete parameter ranges per family plus
structural axes (entry side, signal smoothing, universe size, sizing, time stop) on top of the v1 templates.

Everything here is pure and deterministic. The ranges are initial priors recorded in an ADR; they change only
from measurements (exhaustion rate, S1 pass rate). `generate` never drops a candidate silently: a constraint
or S0 violation comes back as a rejected candidate with its reasons, so the caller can account for it as a
trial (stage S0)."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np

from quant_research.strategy.s0 import check
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec, Timeframe
from quant_research.strategy.templates import build

Choice = str | int
Constraint = Callable[[dict[str, float]], str | None]
_DIGITS = 10


@dataclass(frozen=True, slots=True)
class IntAxis:
    name: str
    lo: int
    hi: int
    step: int

    def __post_init__(self) -> None:
        if self.step <= 0 or self.lo > self.hi or (self.hi - self.lo) % self.step:
            raise ValueError(f"axis {self.name}: need step > 0, lo <= hi and hi on the grid ({self})")

    def values(self) -> tuple[float, ...]:
        return tuple(float(v) for v in range(self.lo, self.hi + 1, self.step))


@dataclass(frozen=True, slots=True)
class FloatAxis:
    name: str
    lo: float
    hi: float
    step: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.lo) and math.isfinite(self.hi) and math.isfinite(self.step)):
            raise ValueError(f"axis {self.name}: bounds and step must be finite ({self})")
        if self.step <= 0 or self.lo > self.hi:
            raise ValueError(f"axis {self.name}: need step > 0 and lo <= hi ({self})")
        n = round((self.hi - self.lo) / self.step)
        if abs(self.lo + n * self.step - self.hi) > 1e-9:
            raise ValueError(f"axis {self.name}: hi {self.hi} is not on the grid lo + k * step ({self})")

    def values(self) -> tuple[float, ...]:
        n = round((self.hi - self.lo) / self.step)
        return tuple(round(self.lo + i * self.step, _DIGITS) for i in range(n + 1))


@dataclass(frozen=True, slots=True)
class ChoiceAxis:
    name: str
    choices: tuple[Choice, ...]

    def __post_init__(self) -> None:
        if not self.choices or len(set(self.choices)) != len(self.choices):
            raise ValueError(f"axis {self.name}: needs distinct choices ({self})")

    def values(self) -> tuple[Choice, ...]:
        return self.choices


NumAxis = IntAxis | FloatAxis


@dataclass(frozen=True, slots=True)
class Point:
    """One point of a space: family, numeric parameters, structural choices (keys sorted)."""

    family: str
    params: tuple[tuple[str, float], ...]
    struct: tuple[tuple[str, Choice], ...]

    @staticmethod
    def of(family: str, params: Mapping[str, float], struct: Mapping[str, Choice]) -> Point:
        return Point(
            family,
            tuple(sorted((k, round(float(v), _DIGITS)) for k, v in params.items())),
            tuple(sorted(struct.items())),
        )

    def params_dict(self) -> dict[str, float]:
        return dict(self.params)

    def struct_dict(self) -> dict[str, Choice]:
        return dict(self.struct)


@dataclass(frozen=True, slots=True)
class Space:
    family: str  # the registry name a Point carries: "momentum" (v1) or "momentum@4h" (v2.2 B5)
    params: tuple[NumAxis, ...]
    struct: tuple[ChoiceAxis, ...]
    constraint: Constraint | None = field(default=None, compare=False)
    template: str = ""  # the template family (strategy/templates.py); empty = `family`
    timeframe: Timeframe = "1h"

    @property
    def template_family(self) -> str:
        return self.template or self.family

    def violation(self, params: dict[str, float]) -> str | None:
        return None if self.constraint is None else self.constraint(params)

    def nominal_size(self) -> int:
        return math.prod(len(a.values()) for a in (*self.params, *self.struct))

    def size(self) -> int:
        """Parameter combinations that satisfy the constraint, times the structural combinations."""
        names = [a.name for a in self.params]
        valid = sum(
            1
            for combo in itertools.product(*(a.values() for a in self.params))
            if self.violation(dict(zip(names, combo, strict=True))) is None
        )
        return valid * math.prod(len(a.values()) for a in self.struct)


@dataclass(frozen=True, slots=True)
class Candidate:
    point: Point
    spec: StrategySpec | None  # None when rejected
    reasons: tuple[str, ...]  # empty when accepted


def _lt(a: str, b: str) -> Constraint:
    def rule(p: dict[str, float]) -> str | None:
        return None if p[a] < p[b] else f"{a} {p[a]:g} >= {b} {p[b]:g}"

    return rule


_SIDE = ChoiceAxis("side", ("long_short", "long_only"))
_SMOOTH = ChoiceAxis("smooth", (0, 3, 6, 12))  # EMA of the signal in bars; 0 = raw signal
_UNIVERSE = ChoiceAxis("universe_n", (3, 5, 8))
_SIZING = ChoiceAxis("sizing", ("vol_target:48", "vol_target:96", "vol_target:168", "vol_target:336", "equal"))
_TIME_STOP = ChoiceAxis("time_stop", (0, 24, 72, 168))  # bars; 0 = no time stop
_COMMON = (_SIDE, _SMOOTH, _UNIVERSE, _SIZING, _TIME_STOP)
# B6 review: `rank_xs` ranks all members but only the top-n by liquidity are tradable; families whose extreme ranks
# are the illiquid names (least traded, most volatile) need the whole member universe to trade both ends
WIDE_UNIVERSE = (50, 100)
WIDE_FAMILIES = ("liquidity", "low_vol")
_WIDE_COMMON = (_SIDE, _SMOOTH, ChoiceAxis("universe_n", WIDE_UNIVERSE), _SIZING, _TIME_STOP)
_QUANTILE = FloatAxis("quantile", 0.6, 0.95, 0.05)

# Pre-registered initial priors (Phase 4 ADR); 1h bars.
SPACES: dict[str, Space] = {
    "trend": Space("trend", (IntAxis("fast", 6, 96, 6), IntAxis("slow", 48, 672, 24)), _COMMON, _lt("fast", "slow")),
    "mean_reversion": Space(
        "mean_reversion", (IntAxis("window", 12, 336, 12), FloatAxis("threshold", 1.0, 3.0, 0.25)), _COMMON
    ),
    "momentum": Space("momentum", (IntAxis("lookback", 24, 720, 24), _QUANTILE), _COMMON),
    "funding_basis": Space("funding_basis", (IntAxis("window", 10, 180, 10), _QUANTILE), _COMMON),
    # volatility is long-only by construction, so it has no side axis
    "volatility": Space(
        "volatility",
        (IntAxis("short", 6, 96, 6), IntAxis("long", 96, 720, 24), FloatAxis("ratio", 0.4, 1.0, 0.05)),
        (_SMOOTH, _UNIVERSE, _SIZING, _TIME_STOP),
        _lt("short", "long"),
    ),
    # v2.2 B6 (D6): new families (pre-registered priors, bars of the space's timeframe)
    "xs_reversal": Space("xs_reversal", (IntAxis("lookback", 1, 48, 1), _QUANTILE), _COMMON),
    "funding_carry": Space("funding_carry", (IntAxis("window", 8, 336, 8), _QUANTILE), _COMMON),
    "premium_basis": Space("premium_basis", (IntAxis("window", 4, 168, 4), _QUANTILE), _COMMON),
    "low_vol": Space("low_vol", (IntAxis("window", 24, 720, 24), _QUANTILE), _WIDE_COMMON),
    "liquidity": Space("liquidity", (IntAxis("window", 24, 720, 24), _QUANTILE), _WIDE_COMMON),
    "breakout": Space("breakout", (IntAxis("window", 24, 720, 24), FloatAxis("buffer", 0.0, 0.2, 0.05)), _COMMON),
}


# v2.2 B5 (D5, ADR-046 addendum): the same families on 1h / 4h / 1d bars with a rebalance interval and a no-trade
# band. Signal windows stay in bars (the timeframe scales their horizon); volatility windows and time stops are
# chosen per timeframe so the warm-up fits a research window. Pre-registered before any 2020-2022 data is read.
V2_TIMEFRAMES: tuple[Timeframe, ...] = ("1h", "4h", "1d")
REBALANCE_HOURS = (1, 4, 24, 72, 168)
BAND_BP = (0, 200, 500)  # no-trade band in basis points of weight
_VOL_WINDOWS: dict[str, tuple[int, ...]] = {"1h": (48, 96, 168, 336), "4h": (12, 24, 42, 84), "1d": (20, 40, 60, 120)}
_TIME_STOPS: dict[str, tuple[int, ...]] = {"1h": (0, 24, 72, 168), "4h": (0, 6, 18, 42), "1d": (0, 5, 10, 20)}


def _v2_struct(tf: Timeframe, *, side: bool, universe: tuple[int, ...] = (5, 10, 20)) -> tuple[ChoiceAxis, ...]:
    hours = TIMEFRAME_NS[tf] // 3_600_000_000_000
    rebalance = tuple(dict.fromkeys(max(1, h // hours) for h in REBALANCE_HOURS))
    axes = [
        _SMOOTH,
        ChoiceAxis("universe_n", universe),
        ChoiceAxis("sizing", (*(f"vol_target:{w}" for w in _VOL_WINDOWS[tf]), "equal")),
        ChoiceAxis("time_stop", _TIME_STOPS[tf]),
        ChoiceAxis("rebalance", rebalance),
        ChoiceAxis("band", BAND_BP),
    ]
    return (_SIDE, *axes) if side else tuple(axes)


SPACES_V2: dict[str, Space] = {
    f"{family}@{tf}": Space(
        f"{family}@{tf}",
        v1.params,
        _v2_struct(tf, side=family != "volatility", universe=WIDE_UNIVERSE if family in WIDE_FAMILIES else (5, 10, 20)),
        v1.constraint,
        template=family,
        timeframe=tf,
    )
    for tf in V2_TIMEFRAMES
    for family, v1 in SPACES.items()
}


def space_of(name: str) -> Space:
    """The registered space a Point's `family` names (v1 family or v2 `family@timeframe`)."""
    space = SPACES.get(name) or SPACES_V2.get(name)
    if space is None:
        raise KeyError(f"unknown space {name!r}")
    return space


def _checked(space: Space, point: Point) -> tuple[dict[str, float], dict[str, Choice]]:
    if point.family != space.family:
        raise ValueError(f"point family {point.family!r} does not match space family {space.family!r}")
    params, struct = point.params_dict(), point.struct_dict()
    for axes, given in ((space.params, params), (space.struct, struct)):
        names = {a.name for a in axes}
        if extra := set(given) - names:
            raise ValueError(f"{space.family}: unknown axes {sorted(extra)}")
        for axis in axes:
            if axis.name not in given:
                raise ValueError(f"{space.family}: missing axis {axis.name!r}")
            if given[axis.name] not in axis.values():
                raise ValueError(f"{space.family}: {axis.name}={given[axis.name]!r} is not on the registered axis")
    return params, struct


def build_point(space: Space, point: Point) -> StrategySpec:
    """The StrategySpec of a registered point (template + structural edits, re-validated)."""
    params, struct = _checked(space, point)
    if (why := space.violation(params)) is not None:
        raise ValueError(f"{space.family}: constraint violated: {why}")
    n = int(struct.get("universe_n", 10))
    base = build(space.template_family, params, n=n)
    body: dict[str, object] = base.model_dump(mode="json")
    body["timeframe"] = space.timeframe
    if "rebalance" in struct or "band" in struct:  # v2.2 B5 axes
        execution = dict(base.execution.model_dump(mode="json"))
        execution["rebalance_bars"] = int(struct.get("rebalance", 1))
        execution["min_trade_weight"] = int(struct.get("band", 0)) / 10_000  # basis points of weight
        body["execution"] = execution
    if struct.get("side") == "long_only":
        long = base.entry.long
        body["entry"] = {"long": None if long is None else long.model_dump(mode="json"), "short": None}
    if (k := int(struct.get("smooth", 0))) > 0:
        body["signal"] = {"op": "ema", "args": [body["signal"]], "window": k}
    sizing = str(struct.get("sizing", "vol_target:168"))
    if sizing == "equal":  # the volatility window still sets the impact-cost estimate: sized per timeframe in v2
        window = 168 if not space.template else _VOL_WINDOWS[space.timeframe][1]
        body["sizing"] = {"method": "equal", "target_ann_vol": 0.15, "max_leverage": 1.0, "vol_window": window}
    else:
        method, _, window = sizing.partition(":")
        body["sizing"] = {"method": method, "target_ann_vol": 0.15, "max_leverage": 1.0, "vol_window": int(window)}
    if (stop := int(struct.get("time_stop", 0))) > 0:
        body["exit"] = {"time_stop_bars": stop, "stop_loss_atr": None}
    return StrategySpec.model_validate(body)


def generate(space: Space, *, seed: int, count: int) -> list[Candidate]:
    """`count` uniform draws (with replacement) from the space; deterministic for a seed. Constraint and
    S0 violations are returned as rejected candidates with reasons, never dropped."""
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    rng = np.random.default_rng(seed)
    out: list[Candidate] = []
    for _ in range(count):
        params = {a.name: a.values()[int(rng.integers(len(a.values())))] for a in space.params}
        struct = {a.name: a.values()[int(rng.integers(len(a.values())))] for a in space.struct}
        point = Point.of(space.family, params, struct)
        if (why := space.violation(params)) is not None:
            out.append(Candidate(point, None, (f"constraint: {why}",)))
            continue
        spec = build_point(space, point)
        reasons = tuple(check(spec))
        out.append(Candidate(point, None if reasons else spec, reasons))
    return out
