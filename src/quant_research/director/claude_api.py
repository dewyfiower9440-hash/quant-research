"""Claude **API** adapter of the Director (Phase 3 step 8): the PAID path (Anthropic API, per-token billing).

**Not approved (2026-11-10 user decision):** no API key is set up and no paid call may run;
`tools/run_director.py` refuses this path. The module is kept as the separate API-only route; the
subscription route is `director/claude_cli.py` (official Claude Code CLI).

- Official SDK (`anthropic`, pinned in pyproject/uv.lock). Credentials are resolved by the SDK from the
  environment or an `ant auth login` profile; this module never reads, logs or stores a key.
- Construction needs explicit approval (`approved=True`); every call goes through the Director's budget check
  first (Budget: $5 total cap, per-cycle cap, per-call token limits).
- No tools are given to the model. The answer format is constrained with `output_config.format`
  (JSON schema) and validated again with the Director's pydantic models.
- Refusal fallback (server-side `fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) is OFF by
  default (2026-11-10 user decision); `use_fallback=True` turns it on, and the answering model is then priced.
- The structured-output schema sent to the API is the pydantic schema without value constraints (lengths,
  ranges, patterns), which the API may not support (미확인 until the approved paid run); the constraints are
  enforced locally by pydantic after the response.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, cast

import anthropic

from quant_research.director.llm import LLMRequest, LLMResponse, Status
from quant_research.director.schemas import api_schema
from quant_research.errors import AppError

MODEL_ID = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class PaidCallsNotApproved(AppError):
    """The Claude adapter was requested without the user's explicit approval of paid calls."""


class _Messages(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _Beta(Protocol):
    @property
    def messages(self) -> _Messages: ...


class _Client(Protocol):
    @property
    def beta(self) -> _Beta: ...


@dataclass
class ClaudeApiLLM:
    client: _Client
    model_id: str = MODEL_ID
    llm_cutoff: str | None = None  # recorded per trial (§8); filled from the model card when known (미확인)
    billed: bool = True
    effort: str = "high"
    use_fallback: bool = False  # 2026-11-10 user decision: no automatic model fallback by default
    served_model: str | None = None  # the model that answered the last call (may be a fallback)

    @classmethod
    def create(cls, *, approved: bool, timeout_s: float = 120.0) -> ClaudeApiLLM:
        if not approved:
            raise PaidCallsNotApproved(
                "paid Claude API calls need the user's explicit approval (not granted; run_director.py refuses them)"
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
