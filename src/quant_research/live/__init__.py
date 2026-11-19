"""Live-trading preparation, laptop side only (Phase 7; design v2.1 §11). Signing and policy packaging,
order idempotency and state rules, dead-man cancellation requests, testnet request formats. No exchange
adapter and no key ever lives here: execution runs on a separate host with the public key only."""
