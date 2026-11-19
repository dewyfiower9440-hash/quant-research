"""Risk library (design v2.1 §4 J, §11): position / loss / order limits and the signed, monotone, expiring
policy that carries them. Used as a library by the paper engine (Phase 6); the live host (Phase 7) runs the
same checks with the ed25519 verifier and the public key only."""
