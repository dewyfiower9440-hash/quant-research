"""Claude Code CLI adapter (subscription route) without running the CLI: a fake runner stands in for it."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from director.conftest import Env
from quant_research.director.budget import Budget
from quant_research.director.claude_cli import (
    BILLING_ENV,
    ClaudeCliLLM,
    SubscriptionAuthRequired,
    UsageLimitReached,
)
from quant_research.director.llm import LLMRequest
from quant_research.director.schemas import HypothesisDraft

REQ = LLMRequest("hypothesize", "system text", {"required_family": "trend"}, HypothesisDraft.model_json_schema(), 8_000)
OK_AUTH = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "x@example.com"}).encode()


@dataclass
class FakeRunner:
    replies: list[tuple[int, bytes, bytes]]
    calls: list[tuple[list[str], bytes, dict[str, str]]] = field(
        default_factory=lambda: list[tuple[list[str], bytes, dict[str, str]]]()
    )

    async def __call__(
        self, argv: Sequence[str], stdin: bytes, timeout_s: float, cwd: Path, env: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((list(argv), stdin, dict(env)))
        return self.replies.pop(0)


def result(**over: object) -> bytes:
    body: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": '{"ok":true}',
        "structured_output": {"ok": True},
        "stop_reason": "tool_use",
        "terminal_reason": "completed",
        "total_cost_usd": 0.0115,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 52,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 1290,
        },
        "modelUsage": {"claude-opus-5-5": {}, "claude-haiku-5-5": {}},
    }
    body.update(over)
    return json.dumps(body).encode()


def make(
    tmp_path: Path, *replies: tuple[int, bytes, bytes], env: dict[str, str] | None = None
) -> tuple[ClaudeCliLLM, FakeRunner]:
    runner = FakeRunner([(0, OK_AUTH, b""), *replies])
    llm = ClaudeCliLLM.create(tmp_path / "cli", environ=env or {"PATH": "x"}, runner=runner, executable="claude")
    return llm, runner


@pytest.mark.parametrize("var", BILLING_ENV)
def test_construction_refuses_any_variable_that_would_switch_to_api_billing(tmp_path: Path, var: str) -> None:
    with pytest.raises(SubscriptionAuthRequired, match="API billing"):
        ClaudeCliLLM.create(tmp_path, environ={var: "set"}, runner=FakeRunner([]), executable="claude")


@pytest.mark.parametrize(
    "status",
    [{"loggedIn": True, "authMethod": "api_key"}, {"loggedIn": False, "authMethod": "none"}, {"loggedIn": True}],
)
def test_construction_requires_the_claude_ai_subscription_login(tmp_path: Path, status: dict[str, object]) -> None:
    runner = FakeRunner([(0, json.dumps(status).encode(), b"")])
    with pytest.raises(SubscriptionAuthRequired, match=r"need claude\.ai"):
        ClaudeCliLLM.create(tmp_path, environ={}, runner=runner, executable="claude")


def test_call_uses_print_mode_without_tools_settings_sessions_bare_or_fallback(tmp_path: Path) -> None:
    llm, runner = make(tmp_path, (0, result(), b""))
    out = llm.complete(REQ)
    argv, stdin, env = runner.calls[1]
    assert argv[:2] == ["claude", "-p"] and "--bare" not in argv and "--fallback-model" not in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == "local"
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv and "--json-schema" in argv
    assert json.loads(stdin) == {"required_family": "trend"} and "required_family" not in " ".join(
        argv
    )  # data on stdin
    assert not any(k in env for k in BILLING_ENV)
    assert (out.status, out.text, out.input_tokens, out.output_tokens, out.cache_write_tokens) == (
        "ok",
        '{"ok": true}',
        2,
        52,
        1290,
    )
    assert llm.served_models == ["claude-haiku-5-5", "claude-opus-5-5"] and "notional_total_cost_usd=0.0115" in (
        out.detail or ""
    )
    assert llm.billed is False


def test_the_child_environment_never_carries_billing_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm, runner = make(tmp_path, (0, result(), b""))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    llm.complete(REQ)
    assert "ANTHROPIC_API_KEY" not in runner.calls[1][2]


@pytest.mark.parametrize(
    "reply",
    [
        (1, result(is_error=True, result="You've hit your session limit · resets 3:45pm", structured_output=None), b""),
        (1, b"You've hit your weekly limit", b""),
        (0, result(api_error_status=429, structured_output=None, result="slow"), b""),
    ],
)
def test_usage_limits_raise_so_the_director_pauses(tmp_path: Path, reply: tuple[int, bytes, bytes]) -> None:
    llm, _ = make(tmp_path, reply)
    with pytest.raises(UsageLimitReached):
        llm.complete(REQ)


def test_errors_refusals_and_plain_text_answers(tmp_path: Path) -> None:
    llm, _ = make(
        tmp_path,
        (1, result(is_error=True, subtype="error_during_execution", structured_output=None), b""),
        (0, result(stop_reason="refusal", structured_output=None), b""),
        (0, result(structured_output=None, result='{"a": 1}'), b""),
        (2, b"not json at all", b"boom"),
    )
    assert llm.complete(REQ).status == "error"
    assert llm.complete(REQ).status == "refusal"
    plain = llm.complete(REQ)
    assert plain.status == "ok" and plain.text == '{"a": 1}'
    assert llm.complete(REQ).status == "error"


def test_a_usage_limit_pauses_the_director_and_stops_the_run(env: Env, tmp_path: Path) -> None:
    limit = (1, result(is_error=True, result="You've hit your session limit · resets 3:45pm"), b"")
    llm, runner = make(tmp_path, limit)
    results = env.director(llm, budget=Budget()).run(3)
    assert [r.status for r in results] == ["paused"]  # the run stops at once; nothing is retried
    assert len(runner.calls) == 2  # auth status + the one call that hit the limit
    reason = env.store.conn.execute("select reason from transition order by transition_id desc limit 1").fetchone()[0]
    assert "usage limit" in reason
