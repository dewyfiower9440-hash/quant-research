"""Claude adapter of the Director (Phase 3 step 8): the only module that may reach the network (Anthropic API).

- Official SDK (`anthropic`, pinned in pyproject/uv.lock). Credentials are resolved by the SDK from the
  environment or an `ant auth login` profile; this module never reads, logs or stores a key.
- Construction needs explicit approval (`approved=True`, passed only by `tools/run_director.py
  --approve-paid-calls`); every call goes through the Director's budget check first (Budget: $5 total cap,
  per-cycle cap, per-call token limits).
- No tools are given to the model. The answer format is constrained with `output_config.format`
  (JSON schema) and validated again with the Director's pydantic models.
- Refusal fallback: server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`) is on by
  default for claude-opus-5-5 (skill guidance); the model that actually answered is recorded and priced.
- The structured-output schema sent to the API is the pydantic schema without value constraints (lengths,
  ranges, patterns), which the API may not support (미확인 until the approved paid run); the constraints are
  enforced locally by pydantic after the response.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

import anthropic

from quant_research.director.llm import LLMRequest, LLMResponse, Status
from quant_research.errors import AppError

MODEL_ID = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_DROP = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minItems",
    "maxItems",
    "title",
    "description",
    "default",
}


class PaidCallsNotApproved(AppError):
    """The Claude adapter was requested without the user's explicit approval of paid calls."""


def api_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """Pydantic JSON schema -> structured-output schema: value constraints removed (checked locally),
    `additionalProperties: false` on every object, all properties required."""

    def walk(node: object) -> object:
        if isinstance(node, dict):
            d = {str(k): walk(v) for k, v in cast(dict[str, object], node).items() if k not in _DROP}
            if d.get("type") == "object" and isinstance(d.get("properties"), dict):
                d["additionalProperties"] = False
                d["required"] = sorted(cast(dict[str, object], d["properties"]))
            return d
        if isinstance(node, list):
            return [walk(v) for v in cast(list[object], node)]
        return node

    return cast(dict[str, object], walk(dict(schema)))


class _Messages(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _Beta(Protocol):
    @property
    def messages(self) -> _Messages: ...


class _Client(Protocol):
    @property
    def beta(self) -> _Beta: ...


@dataclass
class ClaudeLLM:
    client: _Client
    model_id: str = MODEL_ID
    llm_cutoff: str | None = None  # recorded per trial (§8); filled from the model card when known (미확인)
    billed: bool = True
    effort: str = "high"
    use_fallback: bool = True
    served_model: str | None = None  # the model that answered the last call (may be a fallback)

    @classmethod
    def create(cls, *, approved: bool, timeout_s: float = 120.0) -> ClaudeLLM:
        if not approved:
            raise PaidCallsNotApproved(
                "paid Claude API calls need the user's explicit approval (run_director.py --approve-paid-calls)"
            )
        return cls(cast(_Client, anthropic.Anthropic(timeout=timeout_s, max_retries=2)))

    def request_kwargs(self, request: LLMRequest) -> dict[str, object]:
        import json

        kwargs: dict[str, object] = {
            "model": self.model_id,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": json.dumps(request.payload, sort_keys=True, ensure_ascii=False)}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": api_schema(request.schema)},
            },
        }
        if self.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def complete(self, request: LLMRequest) -> LLMResponse:
        start = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(**self.request_kwargs(request))
        except anthropic.RateLimitError as e:
            return LLMResponse("error", None, detail=f"rate limited: {e.message}"[:300])
        except anthropic.APIStatusError as e:
            return LLMResponse("error", None, detail=f"HTTP {e.status_code}: {e.message}"[:300])
        except anthropic.APIConnectionError as e:
            return LLMResponse("error", None, detail=f"connection: {type(e).__name__}"[:300])
        latency = (time.perf_counter() - start) * 1000
        usage = getattr(resp, "usage", None)

        def n(name: str) -> int:
            v = getattr(usage, name, 0) if usage is not None else 0
            return int(v) if isinstance(v, int) else 0

        self.served_model = str(getattr(resp, "model", self.model_id))
        stop = getattr(resp, "stop_reason", None)

        def out(status: Status, text: str | None, detail: str) -> LLMResponse:
            return LLMResponse(
                status,
                text,
                input_tokens=n("input_tokens"),
                output_tokens=n("output_tokens"),
                cache_read_tokens=n("cache_read_input_tokens"),
                cache_write_tokens=n("cache_creation_input_tokens"),
                latency_ms=latency,
                detail=detail,
            )

        if stop == "refusal":
            return out("refusal", None, f"refusal ({self.served_model})")
        if stop == "max_tokens":
            return out("error", None, "max_tokens reached before a complete answer")
        blocks = cast(list[object], getattr(resp, "content", []))
        texts = [str(getattr(b, "text", "")) for b in blocks if getattr(b, "type", "") == "text"]
        return out("ok", texts[0] if texts else None, f"served by {self.served_model}")
