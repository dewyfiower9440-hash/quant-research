"""Phase 3 step 8: Claude adapter without any network (a fake client stands in for the SDK client)."""

from dataclasses import dataclass, field

import anthropic
import httpx2 as httpx
import pytest

from quant_research.director.claude import FALLBACK_BETA, MODEL_ID, ClaudeLLM, PaidCallsNotApproved, api_schema
from quant_research.director.llm import LLMRequest
from quant_research.director.schemas import HypothesisDraft


@dataclass
class Block:
    type: str
    text: str = ""


@dataclass
class Usage:
    input_tokens: int = 1_200
    output_tokens: int = 3_400
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 900


@dataclass
class Resp:
    content: list[Block]
    stop_reason: str = "end_turn"
    model: str = MODEL_ID
    usage: Usage = field(default_factory=Usage)


@dataclass
class FakeMessages:
    reply: Resp | Exception
    seen: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())

    def create(self, **kwargs: object) -> object:
        self.seen.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@dataclass
class FakeBeta:
    messages: FakeMessages


@dataclass
class FakeClient:
    beta: FakeBeta


def llm(reply: Resp | Exception) -> tuple[ClaudeLLM, FakeMessages]:
    msgs = FakeMessages(reply)
    return ClaudeLLM(FakeClient(FakeBeta(msgs))), msgs


REQ = LLMRequest("hypothesize", "system text", {"required_family": "trend"}, HypothesisDraft.model_json_schema(), 8_000)


def test_construction_without_approval_is_refused() -> None:
    with pytest.raises(PaidCallsNotApproved, match="explicit approval"):
        ClaudeLLM.create(approved=False)


def test_request_uses_the_pinned_model_structured_output_effort_fallback_and_no_tools() -> None:
    client, msgs = llm(Resp([Block("thinking"), Block("text", '{"ok": 1}')]))
    client.complete(REQ)
    sent = msgs.seen[0]
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] == 8_000 and "tools" not in sent
    assert sent["betas"] == [FALLBACK_BETA] and sent["fallbacks"] == "default"
    config = sent["output_config"]
    assert isinstance(config, dict) and config["effort"] == "high" and config["format"]["type"] == "json_schema"


def test_api_schema_keeps_structure_and_drops_value_constraints() -> None:
    schema = api_schema(HypothesisDraft.model_json_schema())
    assert schema["additionalProperties"] is False and schema["required"] == sorted(
        ["family", "statement", "falsification_test", "required_data", "required_features", "prior"]
    )
    text = str(schema)
    assert "maxLength" not in text and "minimum" not in text and "pattern" not in text


def test_response_text_usage_and_served_model_are_read() -> None:
    client, _ = llm(Resp([Block("thinking"), Block("text", '{"a": 1}')], model="claude-opus-5"))
    out = client.complete(REQ)
    assert (out.status, out.text, out.input_tokens, out.output_tokens, out.cache_write_tokens) == (
        "ok",
        '{"a": 1}',
        1_200,
        3_400,
        900,
    )
    assert client.served_model == "claude-opus-5" and out.detail == "served by claude-opus-5"


def test_refusal_and_truncation_are_not_treated_as_answers() -> None:
    refused, _ = llm(Resp([], stop_reason="refusal"))
    assert refused.complete(REQ).status == "refusal"
    cut, _ = llm(Resp([Block("text", '{"a":')], stop_reason="max_tokens"))
    assert cut.complete(REQ).status == "error"


def test_api_errors_become_error_responses_without_leaking_headers() -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(429, request=req, headers={"x-api-key": "sk-ant-secret"})
    err = anthropic.RateLimitError("slow down", response=resp, body=None)
    client, _ = llm(err)
    out = client.complete(REQ)
    assert out.status == "error" and out.detail is not None and "sk-ant" not in out.detail
