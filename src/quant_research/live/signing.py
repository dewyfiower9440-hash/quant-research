"""Ed25519 signing of strategy packages and risk policies (Phase 7 step 18; design v2.1 §11).

The private key is generated and kept OFF this machine (hardware key or the operator's offline device);
only the public key is configured on the live host. The research side can produce unsigned packages and
verify signed ones; it cannot sign (no private key here). `Ed25519Verifier` plugs into `risk.policy`.

Package format: canonical JSON body + detached signature (hex) + the public key fingerprint, with a
monotone `version` and an `expires_ns`; the verifier refuses a package whose body was altered after
signing, one signed by another key, an expired one and a version that does not increase."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from quant_research.errors import AppError


class SignatureError(AppError):
    """A signed package was refused."""


def fingerprint(public_key_bytes: bytes) -> str:
    return hashlib.sha256(public_key_bytes).hexdigest()[:16]


class Ed25519Verifier:
    """Public-key only verifier (what the live host and the paper engine hold)."""

    def __init__(self, public_key_bytes: bytes) -> None:
        if len(public_key_bytes) != 32:
            raise SignatureError("an Ed25519 public key is 32 raw bytes")
        self._key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        self.fingerprint = fingerprint(public_key_bytes)

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._key.verify(signature, message)
        except InvalidSignature:
            return False
        return True


class Ed25519Signer:
    """Holder of a private key. Exists for tests and for the OFFLINE signing device; never on the laptop."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def generate(cls) -> Ed25519Signer:
        return cls(Ed25519PrivateKey.generate())

    def public_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def verifier(self) -> Ed25519Verifier:
        return Ed25519Verifier(self.public_bytes())


@dataclass(frozen=True, slots=True)
class SignedPackage:
    kind: str  # "strategy" | "policy"
    version: int
    issued_ns: int
    expires_ns: int
    body: dict[str, object]  # the payload (spec JSON, limits, ...)
    signature_hex: str
    signer_fingerprint: str

    def message(self) -> bytes:
        return canonical_message(self.kind, self.version, self.issued_ns, self.expires_ns, self.body)

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "version": self.version,
                "issued_ns": self.issued_ns,
                "expires_ns": self.expires_ns,
                "body": self.body,
                "signature": self.signature_hex,
                "signer": self.signer_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(text: str) -> SignedPackage:
        try:
            raw = cast(dict[str, object], json.loads(text))
            return SignedPackage(
                str(raw["kind"]),
                int(cast(int, raw["version"])),
                int(cast(int, raw["issued_ns"])),
                int(cast(int, raw["expires_ns"])),
                cast(dict[str, object], raw["body"]),
                str(raw["signature"]),
                str(raw["signer"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            raise SignatureError(f"malformed package: {e}") from e


def canonical_message(kind: str, version: int, issued_ns: int, expires_ns: int, body: Mapping[str, object]) -> bytes:
    payload = {"kind": kind, "version": version, "issued_ns": issued_ns, "expires_ns": expires_ns, "body": dict(body)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sign_package(
    signer: Ed25519Signer, kind: str, version: int, issued_ns: int, expires_ns: int, body: Mapping[str, object]
) -> SignedPackage:
    if version < 1 or expires_ns <= issued_ns:
        raise SignatureError("package needs version >= 1 and expires after issue")
    msg = canonical_message(kind, version, issued_ns, expires_ns, body)
    return SignedPackage(
        kind, version, issued_ns, expires_ns, dict(body), signer.sign(msg).hex(), fingerprint(signer.public_bytes())
    )


def verify_package(pkg: SignedPackage, verifier: Ed25519Verifier, *, now_ns: int, current_version: int | None) -> None:
    """Raise SignatureError unless signature, signer, expiry and monotone version all hold."""
    if pkg.signer_fingerprint != verifier.fingerprint:
        raise SignatureError(f"package signed by {pkg.signer_fingerprint}, expected {verifier.fingerprint}")
    try:
        signature = bytes.fromhex(pkg.signature_hex)
    except ValueError as e:
        raise SignatureError("signature is not hex") from e
    if not verifier.verify(pkg.message(), signature):
        raise SignatureError("signature does not verify (body altered or wrong key)")
    if pkg.expires_ns <= now_ns:
        raise SignatureError(f"package {pkg.kind} v{pkg.version} expired")
    if current_version is not None and pkg.version <= current_version:
        raise SignatureError(f"package version {pkg.version} <= current {current_version}: rollback refused")
