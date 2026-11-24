"""HTTP GET with exchange pacing and bounded retries (design v2.1 §4 common rules).

Retries only for idempotent GETs: transport errors/timeouts, 429, Upbit 418 and 5xx, with exponential
backoff plus jitter and a hard attempt limit. Limits are per IP, so every Fetcher of one venue shares a
`VenueGate`: a 429/418 wait observed by one coroutine blocks all requests to that venue (otherwise the
1 Hz pollers would keep hitting a rate-limited IP and escalate to a ban).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx

from quant_research.capture.ratelimit import (
    binance_delay_s,
    classify_status,
    parse_upbit_remaining,
    upbit_delay_s,
)
from quant_research.errors import ExternalServiceError


class HttpStatusError(ExternalServiceError):
    """A response the venue will keep giving for this request (not retried). `status` lets a caller tell an
    unknown code (404: a stale market list) from the other client errors."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class ThrottledError(ExternalServiceError):
    """A call that was not made because it ran too recently (our own limit, not an answer of the venue)."""


Venue = Literal["binance", "upbit"]
Params = Mapping[str, str | int | float]


@dataclass
class VenueGate:
    """Shared 'do not send before' instant (time.time seconds) for one venue/IP."""

    blocked_until: float = 0.0


@dataclass
class Fetcher:
    client: httpx.AsyncClient
    venue: Venue
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    now_s: Callable[[], float] = time.time
    rng: random.Random = field(default_factory=random.SystemRandom)  # jitter only; tests inject a stub
    gate: VenueGate = field(default_factory=VenueGate)
    binance_weight_limit: int = 2400  # replaced at runtime from exchangeInfo.rateLimits
    max_attempts: int = 4
    requests: int = 0

    async def get(self, url: str, params: Params | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            await self._respect_gate()
            try:
                self.requests += 1
                r = await self.client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
                await self.sleep(self._backoff(attempt))
                continue
            outcome, wait = classify_status(
                r.status_code,
                r.headers,
                venue=self.venue,
                default_retry_s=60.0 if self.venue == "binance" else 1.0,
            )
            if outcome == "ok":
                await self._pace(r)
                return r
            if outcome == "fail":
                raise HttpStatusError(r.status_code, f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}")
            last_error = ExternalServiceError(f"GET {url} -> HTTP {r.status_code}")
            delay = max(wait, self._backoff(attempt))
            self.gate.blocked_until = max(self.gate.blocked_until, self.now_s() + delay)
        raise ExternalServiceError(f"GET {url} failed after {self.max_attempts} attempts") from last_error

    async def _respect_gate(self) -> None:
        wait = self.gate.blocked_until - self.now_s()
        if wait > 0:
            await self.sleep(wait)

    def _backoff(self, attempt: int) -> float:
        return min(30.0, 0.5 * 2**attempt) + self.rng.uniform(0, 0.25)

    async def _pace(self, r: httpx.Response) -> None:
        if self.venue == "binance":
            raw = r.headers.get("x-mbx-used-weight-1m")
            used = int(raw) if raw and raw.isdigit() else None
            delay = binance_delay_s(used_weight=used, limit=self.binance_weight_limit, now_s=self.now_s())
        else:
            _group, sec = parse_upbit_remaining(r.headers.get("remaining-req"))
            delay = upbit_delay_s(remaining_sec=sec, now_s=self.now_s())
        if delay > 0:
            self.gate.blocked_until = max(self.gate.blocked_until, self.now_s() + delay)
            await self.sleep(delay)
