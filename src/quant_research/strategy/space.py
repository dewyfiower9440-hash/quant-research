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
from typing import cast

import numpy as np

from quant_research.strategy.s0 import S0Context, check
from quant_research.strategy.spec import TIMEFRAME_NS, StrategySpec, Timeframe
from quant_research.strategy.templates import (
    CONSTITUENTS,
    MODEL_FAMILIES,
    REGIME_GATES,
    REGIME_SUFFIX,
    build,
    model_term_names,
    regime_gate,
    window_key,
)

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
    warmup_bars: int | None = None  # ADR-051 (v3): points that need more warm-up are refused at S0

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
    # v2.3 step 8 (pre-registered before the metrics / spot tiers are read)
    "oi_positioning": Space("oi_positioning", (IntAxis("window", 4, 336, 4), _QUANTILE), _COMMON),
    "taker_flow": Space("taker_flow", (IntAxis("window", 1, 168, 1), _QUANTILE), _COMMON),
    "spot_basis": Space("spot_basis", (IntAxis("window", 4, 168, 4), _QUANTILE), _COMMON),
}


# ADR-051 (v2.3 design section 5): a research block gives every decision WARMUP_DAYS of warm-up (w2 starts right
# after a sealed holdout, so the warm-up comes out of the block). A window operator may look back that far; an EMA
# needs about three spans to converge, so its span is capped at a third.
WARMUP_DAYS = 90
EMA_SPANS = 3
WINDOW_PARAMS = ("window", "lookback", "short", "long", "fast", "slow")
EMA_FAMILIES = ("trend",)  # families whose window parameters are EMA spans


def window_cap_bars(timeframe: str) -> int:
    """The longest window (bars) a window operator may use on this timeframe."""
    if timeframe not in TIMEFRAME_NS:
        raise ValueError(f"unknown timeframe {timeframe!r} (known: {', '.join(TIMEFRAME_NS)})")
    return WARMUP_DAYS * 86_400_000_000_000 // TIMEFRAME_NS[timeframe]


def ema_cap_bars(timeframe: str) -> int:
    """The longest EMA span (bars) on this timeframe."""
    return window_cap_bars(timeframe) // EMA_SPANS


def exceeds_cap(family: str, timeframe: str, params: Mapping[str, float]) -> bool:
    """Whether a point's longest SIGNAL window is beyond the family's cap on this timeframe. The structural axes
    (sizing volatility window, smoothing) are not looked at: the S0 warm-up check of ADR-051 covers the whole
    spec."""
    cap = ema_cap_bars(timeframe) if family in EMA_FAMILIES else window_cap_bars(timeframe)
    return any(v > cap for k, v in params.items() if k in WINDOW_PARAMS)


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


def _capped(axis: NumAxis, cap: int) -> NumAxis | None:
    """The axis with its end cut below the cap (start and step kept): S0 refuses a warm-up need equal to the cap,
    so the cap itself is not a value. None when even the start does not fit."""
    if not isinstance(axis, IntAxis) or axis.name not in WINDOW_PARAMS or axis.hi < cap:
        return axis
    if axis.lo >= cap:
        return None
    return IntAxis(axis.name, axis.lo, axis.lo + (cap - 1 - axis.lo) // axis.step * axis.step, axis.step)


def _v3_space(name: str, v2: Space) -> Space | None:
    cap = window_cap_bars(v2.timeframe)
    axis_cap = ema_cap_bars(v2.timeframe) if v2.template_family in EMA_FAMILIES else cap
    params = [_capped(a, axis_cap) for a in v2.params]
    if any(a is None for a in params):
        return None
    struct: list[ChoiceAxis] = []
    for a in v2.struct:
        if a.name == "sizing":  # volatility windows beyond the warm-up go (1d: vol_target:120)
            keep = tuple(v for v in a.choices if not str(v).startswith("vol_target:") or int(str(v)[11:]) <= cap)
            struct.append(ChoiceAxis("sizing", keep))
        else:
            struct.append(a)
    return Space(
        name + SPACE_V3_SUFFIX,
        tuple(a for a in params if a is not None),
        tuple(struct),
        v2.constraint,
        template=v2.template_family,
        timeframe=v2.timeframe,
        warmup_bars=cap,
    )


# ADR-051 (design v2.3 section 5): the v2 spaces with every window axis cut to what a 90-day warm-up can feed. A
# registered name is the v2 name + "/v3"; a (family, timeframe) whose axis starts beyond the cap has no v3 space.
SPACE_V3_SUFFIX = "/v3"
_V3 = {name: _v3_space(name, v2) for name, v2 in SPACES_V2.items()}
SPACES_V3: dict[str, Space] = {s.family: s for s in _V3.values() if s is not None}
INFEASIBLE_V3: frozenset[str] = frozenset(name for name, s in _V3.items() if s is None)


def canonical_window(family: str, timeframe: str) -> int:
    """ADR-053: the canonical parameter of a constituent is the middle grid point of its capped window axis
    (rounded down). Mechanical: no result decides it."""
    space = SPACES_V3[f"{family}@{timeframe}{SPACE_V3_SUFFIX}"]
    axis = next(a for a in space.params if isinstance(a, IntAxis) and a.name == window_key(family))
    return axis.lo + (axis.hi - axis.lo) // axis.step // 2 * axis.step


MODEL_UNIVERSE = 20
MODEL_REBALANCE_HOURS = 24
MODEL_BAND_BP = 200
WEIGHT_STEP = 0.0001


def _model_space(family: str, tf: Timeframe) -> Space:
    """A model space fixes everything but the term weights: the constituents' canonical windows are single-valued
    axes and the portfolio construction has one choice per structural axis (design v2.3 appendix A)."""
    hours = TIMEFRAME_NS[tf] // 3_600_000_000_000
    params: list[NumAxis] = []
    for name in model_term_names(family):
        if name in CONSTITUENTS:
            window = canonical_window(name, tf)
            params.append(IntAxis(f"p_{name}", window, window, 1))
        params.append(FloatAxis(f"w_{name}", -1.0, 1.0, WEIGHT_STEP))
    struct = (
        ChoiceAxis("side", ("long_short",)),
        ChoiceAxis("smooth", (0,)),
        ChoiceAxis("universe_n", (MODEL_UNIVERSE,)),
        ChoiceAxis("sizing", (f"vol_target:{_VOL_WINDOWS[tf][1]}",)),
        ChoiceAxis("time_stop", (0,)),
        ChoiceAxis("rebalance", (max(1, MODEL_REBALANCE_HOURS // hours),)),
        ChoiceAxis("band", (MODEL_BAND_BP,)),
    )
    return Space(
        f"{family}@{tf}{SPACE_V3_SUFFIX}",
        tuple(params),
        struct,
        template=family,
        timeframe=tf,
        warmup_bars=window_cap_bars(tf),
    )


# ADR-053: the model cells' spaces (same naming as v3). They are not drawn from: `search.model_cells` builds
# their few points from weights estimated on the fit part of a selection block.
MODEL_SPACES: dict[str, Space] = {
    s.family: s for s in (_model_space(family, tf) for family in MODEL_FAMILIES for tf in V2_TIMEFRAMES)
}


def _regime_space(v3: Space) -> Space:
    return Space(
        f"{v3.template_family}{REGIME_SUFFIX}@{v3.timeframe}{SPACE_V3_SUFFIX}",
        v3.params,
        (*v3.struct, ChoiceAxis("regime_gate", REGIME_GATES)),
        v3.constraint,
        template=v3.template_family,
        timeframe=v3.timeframe,
        warmup_bars=v3.warmup_bars,
    )


# ADR-055 decision 3: every searched v3 space again with a regime gate axis (the ungated point is the v3 space
# itself, so `none` is not a choice here). Cell family `<family>~regime`; opened in the last round.
REGIME_SPACES: dict[str, Space] = {s.family: s for s in (_regime_space(v3) for v3 in SPACES_V3.values())}


def space_of(name: str) -> Space:
    """The registered space a Point's `family` names (v1 family, v2 `family@timeframe`, v3 `...@timeframe/v3`,
    a model space or a regime-gated space)."""
    space = (
        SPACES.get(name)
        or SPACES_V2.get(name)
        or SPACES_V3.get(name)
        or MODEL_SPACES.get(name)
        or REGIME_SPACES.get(name)
    )
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
    if "regime_gate" in struct:  # ADR-055: both entries only while the gate holds
        gate = regime_gate(str(struct["regime_gate"]), space.timeframe)
        entry = cast(dict[str, object], body["entry"])
        body["entry"] = {
            side: None if entry.get(side) is None else {"op": "and", "args": [entry[side], gate]}
            for side in ("long", "short")
        }
    return StrategySpec.model_validate(body)


def s0_context(space: Space) -> S0Context:
    """The S0 context of a space's points (ADR-051: v3 spaces check the warm-up)."""
    return S0Context(max_warmup_bars=space.warmup_bars)


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
        reasons = tuple(check(spec, s0_context(space)))
        out.append(Candidate(point, None if reasons else spec, reasons))
    return out
