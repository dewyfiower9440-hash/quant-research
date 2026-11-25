"""ADR-056 decision 3 (c): the cross-sectional planted alpha (low realised volatility earns more).

The two older alphas have no cross-sectional structure, while most campaign families are cross-sectional rank
strategies; the funnel's recovery of such a structure is what the measurement needs."""

import numpy as np

from quant_research.validator.null_pipeline import (
    CROSS_VOL_BARS,
    SLOW_BARS,
    SyntheticSpec,
    calibrate_strength,
    realizable_z,
    synthetic_returns,
)


def _closes(symbols: int = 24, bars: int = 400, seed: int = 0) -> np.ndarray:
    """Random walks with different volatilities (0.5 % to 4 % a bar): the base only supplies returns to resample."""
    rng = np.random.default_rng(seed)
    vol = np.linspace(0.005, 0.04, symbols)
    rets = rng.standard_normal((bars, symbols)) * vol
    return 100.0 * np.exp(np.cumsum(rets, axis=0))


def test_the_cross_alpha_pays_low_volatility_symbols_and_charges_high_ones() -> None:
    closes = _closes()
    spec = SyntheticSpec(alpha="cross", strength=0.01, hours=3_001, block_len=5)
    rets = np.expm1(synthetic_returns(closes, spec, np.random.default_rng(1)))
    vol = rets[: CROSS_VOL_BARS * 5].std(axis=0)  # the source volatilities survive the resampling
    calm, wild = np.argsort(vol)[:6], np.argsort(vol)[-6:]
    assert rets[CROSS_VOL_BARS:, calm].mean() > 0.002
    assert rets[CROSS_VOL_BARS:, wild].mean() < -0.002


def test_the_cross_alpha_adds_no_market_drift() -> None:
    """Every bar's additions sum to zero across symbols: the equal-weight market is the null market's."""
    closes = _closes()
    null = synthetic_returns(closes, SyntheticSpec(hours=1_001, block_len=5), np.random.default_rng(2))
    cross = synthetic_returns(
        closes, SyntheticSpec(alpha="cross", strength=0.01, hours=1_001, block_len=5), np.random.default_rng(2)
    )
    assert np.allclose(cross.mean(axis=1), null.mean(axis=1), atol=1e-12)
    assert not np.allclose(cross, null)


def test_the_first_bars_have_no_alpha_yet() -> None:
    closes = _closes()
    null = synthetic_returns(closes, SyntheticSpec(hours=201, block_len=5), np.random.default_rng(3))
    cross = synthetic_returns(
        closes, SyntheticSpec(alpha="cross", strength=0.01, hours=201, block_len=5), np.random.default_rng(3)
    )
    assert np.array_equal(cross[:CROSS_VOL_BARS], null[:CROSS_VOL_BARS])  # no volatility estimate before that


def test_a_cross_alpha_of_strength_zero_is_the_null_market() -> None:
    closes = _closes()
    null = synthetic_returns(closes, SyntheticSpec(hours=501), np.random.default_rng(4))
    zero = synthetic_returns(closes, SyntheticSpec(alpha="cross", strength=0.0, hours=501), np.random.default_rng(4))
    assert np.array_equal(zero, null)


def test_the_cross_alpha_is_calibrated_to_the_z_of_its_tradable_strategy() -> None:
    closes = _closes()
    bars = 700
    strength = calibrate_strength("cross", 4.0, closes, holdout_hours=bars, draws=12, seed=1, iters=25, block_len=5)
    assert strength > 0
    zs = [
        realizable_z(
            "cross",
            synthetic_returns(
                closes,
                SyntheticSpec(alpha="cross", strength=strength, hours=bars + 1, block_len=5),
                np.random.default_rng(100 + i),
            ),
        )
        for i in range(24)
    ]
    assert 3.0 < float(np.mean(zs)) < 5.0
    # the same strategy on the null market has no edge
    null_z = [
        realizable_z(
            "cross",
            synthetic_returns(closes, SyntheticSpec(hours=bars + 1, block_len=5), np.random.default_rng(200 + i)),
        )
        for i in range(24)
    ]
    assert abs(float(np.mean(null_z))) < 1.0


def test_the_slow_reversion_alpha_leans_against_the_last_five_bars_and_survives_a_one_bar_delay() -> None:
    """ADR-056 addendum 2 (user decision): on daily bars the one-bar mean reversion is gone a bar later, so the
    validator's lag stress (one more bar) rejects it by construction. This one lasts several bars."""
    closes = _closes()
    spec = SyntheticSpec(alpha="slow_reversion", strength=0.5, hours=4_001, block_len=5)
    simple = np.expm1(synthetic_returns(closes, spec, np.random.default_rng(5)))
    past = np.stack([simple[k - SLOW_BARS : k].sum(axis=0) for k in range(SLOW_BARS, simple.shape[0] - 1)])
    now = simple[SLOW_BARS:-1]
    late = simple[SLOW_BARS + 1 :]  # the same signal traded one bar later
    assert float(np.mean(-np.sign(past) * now)) > 0
    assert float(np.mean(-np.sign(past) * late)) > 0
    assert realizable_z("slow_reversion", np.log1p(simple)) > 5.0
    null = synthetic_returns(closes, SyntheticSpec(hours=4_001, block_len=5), np.random.default_rng(5))
    assert abs(realizable_z("slow_reversion", null)) < 3.0


def test_the_slow_reversion_alpha_is_calibrated_and_zero_strength_is_the_null() -> None:
    closes = _closes()
    strength = calibrate_strength("slow_reversion", 4.0, closes, holdout_hours=700, draws=12, seed=2, block_len=5)
    assert 0 < strength < 5.0
    zs = [
        realizable_z(
            "slow_reversion",
            synthetic_returns(
                closes,
                SyntheticSpec(alpha="slow_reversion", strength=strength, hours=701, block_len=5),
                np.random.default_rng(300 + i),
            ),
        )
        for i in range(24)
    ]
    assert 3.0 < float(np.mean(zs)) < 5.0
    null = synthetic_returns(closes, SyntheticSpec(hours=301), np.random.default_rng(6))
    zero = synthetic_returns(closes, SyntheticSpec(alpha="slow_reversion", hours=301), np.random.default_rng(6))
    assert np.array_equal(zero, null)
