"""Dead-man switch and safe-stop rules (Phase 7 step 19; design v2.1 §11). Pure.

When the strategy host's heartbeat stops, the live executor must cancel resting orders and stop placing new
ones. Binance USDⓈ-M offers a venue-side auto-cancel timer (the `countdownCancelAll` family — endpoint
and parameters are **unverified** here and must be confirmed against the official documentation before
use); Upbit has no timer, so the executor issues cancel-all requests itself, at most one every 2 s (§11).
`safe_stop_reasons` lists the §11 conditions under which no new order may go out."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Venue = Literal["binance", "upbit"]
UPBIT_CANCEL_ALL_MIN_INTERVAL_NS = 2 * 10**9


@dataclass(frozen=True, slots=True)
class HeartbeatState:
    last_beat_ns: int
    now_ns: int
    timeout_ns: int

    @property
    def stale(self) -> bool:
        return self.now_ns - self.last_beat_ns > self.timeout_ns


@dataclass(frozen=True, slots=True)
class CancelRequest:
    venue: Venue
    kind: Literal["cancel_all", "arm_countdown"]
    countdown_ms: int | None
    unverified_endpoint: bool  # True for the Binance countdown family until confirmed in the docs


def deadman_requests(
    venue: Venue, hb: HeartbeatState, *, last_cancel_all_ns: int | None, countdown_ms: int = 60_000
) -> list[CancelRequest]:
    """Requests the executor should send now. Binance: keep the venue-side countdown armed while alive and
    cancel all when the heartbeat is stale. Upbit: cancel all when stale, rate limited to one per 2 s."""
    if venue == "binance":
        out = [CancelRequest("binance", "arm_countdown", countdown_ms, True)]
        if hb.stale:
            out.append(CancelRequest("binance", "cancel_all", None, False))
        return out
    if not hb.stale:
        return []
    if last_cancel_all_ns is not None and hb.now_ns - last_cancel_all_ns < UPBIT_CANCEL_ALL_MIN_INTERVAL_NS:
        return []  # respect the 2 s limit; the next tick retries
    return [CancelRequest("upbit", "cancel_all", None, False)]


@dataclass(frozen=True, slots=True)
class HostHealth:
    data_gap: bool
    clock_offset_ms: float
    heartbeat_stale: bool
    error_rate: float  # recent failed requests / requests
    reconciliation_mismatch: bool


def safe_stop_reasons(h: HostHealth, *, max_clock_offset_ms: float = 250.0, max_error_rate: float = 0.2) -> list[str]:
    """§11 "장애 시 안전 정지": any reason here means no new orders (and a reconciliation mismatch kills)."""
    reasons: list[str] = []
    if h.data_gap:
        reasons.append("market data gap")
    if abs(h.clock_offset_ms) > max_clock_offset_ms:
        reasons.append(f"clock offset {h.clock_offset_ms:.0f} ms > {max_clock_offset_ms:.0f} ms")
    if h.heartbeat_stale:
        reasons.append("heartbeat stale")
    if h.error_rate > max_error_rate:
        reasons.append(f"error rate {h.error_rate:.2f} > {max_error_rate:.2f}")
    if h.reconciliation_mismatch:
        reasons.append("reconciliation mismatch: kill")
    return reasons
