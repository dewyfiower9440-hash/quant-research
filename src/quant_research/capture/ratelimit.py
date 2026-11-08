"""Rate-limit and HTTP outcome policy for Binance and Upbit (pure; design v2.1 §5.1, §5.2).

- Binance: limits are per IP; the used weight comes back in `X-MBX-USED-WEIGHT-1m` and the limit is
  read at runtime from `exchangeInfo.rateLimits`. 429 -> honour Retry-After; 418 -> IP ban, stop all
  collection; 451 -> restricted location, record as "collection impossible".
- Upbit: per-group per-second budget in `Remaining-Req` (`sec` is the only valid field); 429 -> retry
  next second; 418 -> temporary block with the duration in the response.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Literal

from quant_research.errors import ExternalServiceError

Outcome = Literal["ok", "retry", "fail"]


class BannedError(ExternalServiceError):
    """HTTP 418: the IP is blocked; every collector must stop and alert."""


class GeoBlockedError(ExternalServiceError):
    """HTTP 451: service unavailable from this location."""


def classify_status(status: int, headers: Mapping[str, str], *, default_retry_s: float) -> tuple[Outcome, float]:
    """(outcome, wait seconds) for an HTTP status. Raises for statuses that must stop collection."""
    if 200 <= status < 300:
        return "ok", 0.0
    if status == 418:
        raise BannedError(f"HTTP 418 IP ban (Retry-After={headers.get('Retry-After')})")
    if status == 451:
        raise GeoBlockedError("HTTP 451 unavailable for legal reasons (restricted location)")
    if status == 429:
        retry = headers.get("Retry-After")
        return "retry", float(retry) if retry and retry.replace(".", "", 1).isdigit() else default_retry_s
    if status >= 500:
        return "retry", 1.0
    return "fail", 0.0


def binance_delay_s(*, used_weight: int | None, limit: int, now_s: float, safety: float = 0.8) -> float:
    """Seconds to wait before the next request so the 1-minute weight window stays under safety * limit."""
    if used_weight is None or used_weight < safety * limit:
        return 0.0
    return 60.0 - math.fmod(now_s, 60.0) + 0.5


_REMAINING = re.compile(r"group=([\w-]+).*?sec=(\d+)")


def parse_upbit_remaining(header: str | None) -> tuple[str | None, int | None]:
    m = _REMAINING.search(header or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def upbit_delay_s(*, remaining_sec: int | None, now_s: float, margin_s: float = 0.05) -> float:
    """Wait to the next wall-clock second when the per-second budget is exhausted."""
    if remaining_sec is None or remaining_sec > 0:
        return 0.0
    return 1.0 - math.fmod(now_s, 1.0) + margin_s
