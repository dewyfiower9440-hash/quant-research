"""Signed risk policy (design v2.1 §11 "승인과 정책 서명"): `policy(version, limits, expires, sig)`.

A policy is applied only when (1) its signature verifies against the configured verifier, (2) its version is
strictly greater than the current one (monotone, no rollback) and (3) it has not expired. Without an active,
unexpired policy every order is refused. The verifier is an interface: `FakeVerifier` (HMAC, tests and the
paper engine) here; the ed25519 public-key verifier arrives with Phase 7 (`live/signing.py`). The research
output schema has no policy fields, so nothing the Director produces can ever become a policy."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol, cast

from quant_research.errors import AppError
from quant_research.risk.limits import Limits


class PolicyError(AppError):
    """A policy was refused (signature, version, expiry, malformed)."""


class Verifier(Protocol):
    def verify(self, message: bytes, signature: bytes) -> bool: ...


class FakeVerifier:
    """HMAC-SHA256 stand-in for tests / paper. NOT a funds control (§11: symmetric secrets cannot stop the AI
    from changing a policy); the live host uses asymmetric ed25519 with the public key only."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def sign(self, message: bytes) -> bytes:
        return hmac.new(self._secret, message, hashlib.sha256).digest()

    def verify(self, message: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(self.sign(message), signature)


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    limits: Limits
    issued_ns: int
    expires_ns: int

    def __post_init__(self) -> None:
        if self.version < 1 or self.expires_ns <= self.issued_ns:
            raise ValueError("policy needs version >= 1 and expires after issue")


def policy_bytes(p: Policy) -> bytes:
    """Canonical bytes that are signed."""
    body = {"version": p.version, "limits": asdict(p.limits), "issued_ns": p.issued_ns, "expires_ns": p.expires_ns}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def parse_policy(message: bytes) -> Policy:
    try:
        raw = cast(dict[str, object], json.loads(message.decode("utf-8")))
        limits_raw = cast(Mapping[str, object], raw["limits"])
        limits = Limits(
            float(cast(float, limits_raw["max_gross_notional"])),
            float(cast(float, limits_raw["max_symbol_notional"])),
            float(cast(float, limits_raw["max_leverage"])),
            float(cast(float, limits_raw["max_daily_loss"])),
            float(cast(float, limits_raw["max_weekly_loss"])),
            float(cast(float, limits_raw["max_total_loss"])),
            int(cast(int, limits_raw["max_orders_per_minute"])),
            float(cast(float, limits_raw["max_order_notional"])),
        )
        return Policy(
            int(cast(int, raw["version"])), limits, int(cast(int, raw["issued_ns"])), int(cast(int, raw["expires_ns"]))
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise PolicyError(f"malformed policy: {e}") from e


class PolicyStore:
    """Holds the active policy; `apply` enforces signature, monotone version and expiry."""

    def __init__(self, verifier: Verifier) -> None:
        self.verifier = verifier
        self.current: Policy | None = None
        self.history: list[int] = []

    def apply(self, message: bytes, signature: bytes, *, now_ns: int) -> Policy:
        if not self.verifier.verify(message, signature):
            raise PolicyError("signature does not verify: policy refused")
        policy = parse_policy(message)
        if self.current is not None and policy.version <= self.current.version:
            raise PolicyError(f"version {policy.version} <= current {self.current.version}: rollback refused")
        if policy.expires_ns <= now_ns:
            raise PolicyError(f"policy {policy.version} expired at {policy.expires_ns} (now {now_ns})")
        self.current = policy
        self.history.append(policy.version)
        return policy

    def active(self, *, now_ns: int) -> Policy:
        """The policy in force; PolicyError when none or expired (then every order is refused)."""
        if self.current is None:
            raise PolicyError("no policy: every order is refused")
        if self.current.expires_ns <= now_ns:
            raise PolicyError(f"policy {self.current.version} expired: every order is refused")
        return self.current
