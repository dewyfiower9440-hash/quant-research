"""Phase 7 step 18: ed25519 packages — tampering, another key, expiry and rollback are refused; the risk
policy store works with the public-key verifier alone."""

from __future__ import annotations

import json

import pytest

from quant_research.live.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    SignatureError,
    SignedPackage,
    sign_package,
    verify_package,
)
from quant_research.risk.limits import Limits
from quant_research.risk.policy import Policy, PolicyError, PolicyStore, policy_bytes

LIMITS = Limits(10_000.0, 4_000.0, 2.0, 300.0, 800.0, 2_000.0, 10, 2_500.0)


def test_signed_package_round_trips_and_every_refusal_path_is_covered() -> None:
    signer = Ed25519Signer.generate()
    verifier = signer.verifier()
    pkg = sign_package(signer, "strategy", 1, 0, 1_000, {"spec_hash": "abc", "params": {"fast": 24}})
    verify_package(pkg, verifier, now_ns=10, current_version=None)
    back = SignedPackage.from_json(pkg.to_json())
    assert back == pkg
    verify_package(back, verifier, now_ns=10, current_version=0)
    tampered = SignedPackage.from_json(pkg.to_json().replace('"fast":24', '"fast":25'))
    with pytest.raises(SignatureError, match="does not verify"):
        verify_package(tampered, verifier, now_ns=10, current_version=None)
    other = Ed25519Signer.generate().verifier()
    with pytest.raises(SignatureError, match="signed by"):
        verify_package(pkg, other, now_ns=10, current_version=None)
    with pytest.raises(SignatureError, match="expired"):
        verify_package(pkg, verifier, now_ns=1_000, current_version=None)
    with pytest.raises(SignatureError, match="rollback"):
        verify_package(pkg, verifier, now_ns=10, current_version=1)
    with pytest.raises(SignatureError):
        sign_package(signer, "policy", 0, 0, 1, {})
    with pytest.raises(SignatureError):
        SignedPackage.from_json("{}")
    with pytest.raises(SignatureError):
        Ed25519Verifier(b"short")
    bad_hex = SignedPackage(
        pkg.kind, pkg.version, pkg.issued_ns, pkg.expires_ns, pkg.body, "zz", pkg.signer_fingerprint
    )
    with pytest.raises(SignatureError, match="hex"):
        verify_package(bad_hex, verifier, now_ns=10, current_version=None)
    assert len(signer.public_bytes()) == 32 and len(verifier.fingerprint) == 16


def test_policy_store_accepts_the_ed25519_verifier_with_the_public_key_only() -> None:
    signer = Ed25519Signer.generate()
    store = PolicyStore(Ed25519Verifier(signer.public_bytes()))  # the live host holds only these 32 bytes
    msg = policy_bytes(Policy(1, LIMITS, 0, 10_000))
    store.apply(msg, signer.sign(msg), now_ns=5)
    assert store.active(now_ns=5).version == 1
    forged = json.loads(msg)
    forged["limits"]["max_daily_loss"] = 1e9
    forged_bytes = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(PolicyError):
        store.apply(forged_bytes, signer.sign(msg), now_ns=5)  # signature is over the original body
    with pytest.raises(PolicyError):
        store.apply(msg, Ed25519Signer.generate().sign(msg), now_ns=5)  # another key
