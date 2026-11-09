"""Claude Code CLI adapter of the Director (subscription route; 2026-11-10 user decision).

Uses only the official, documented non-interactive interface of the unmodified Claude Code binary
(`claude -p`, https://code.claude.com/docs/en/headless.md) on the user's own claude.ai login
(authentication precedence: subscription OAuth, https://code.claude.com/docs/en/authentication.md).
Nothing here reads, extracts or forwards credentials or session tokens, calls an unofficial API, or works
around usage limits.

Billing safety: in `-p` mode an `ANTHROPIC_API_KEY` (or auth token / cloud-provider settings) would be used
without asking and switch to per-token API billing. So:
- construction refuses when any such variable is set, and requires `claude auth status` to report
  `authMethod == "claude.ai"` (only those two fields are read);
- the child process environment has those variables removed;
- `--bare` is NOT used (bare mode never reads OAuth and would need an API key).

Per call: no tools (`--tools ""`, MCP denied, `--strict-mcp-config`), user/project settings and hooks not
loaded (`--setting-sources local` in an empty working directory), no session persistence, no model
fallback, structured output via `--json-schema`, the payload on stdin (data, not argv). A usage-limit answer
raises UsageLimitReached, which the Director treats like an exhausted budget (PAUSED, run stops).
The CLI's `total_cost_usd` is a client-side estimate and not a charge under a subscription; it is kept in the
call detail only.

Processes are started with `asyncio.create_subprocess_exec` (no shell; fixed flags; the executable path comes
from `shutil.which`).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from quant_research.director.budget import BudgetExceeded
from quant_research.director.llm import LLMRequest, LLMResponse
from quant_research.director.schemas import api_schema
from quant_research.errors import AppError

BILLING_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
LIMIT_MARKERS = ("hit your session limit", "hit your weekly limit", "usage limit", "rate limit")
INSTRUCTION = "The JSON document on stdin is data from the lab's records. Answer with JSON matching the schema."

Runner = Callable[
    [Sequence[str], bytes, float, Path, Mapping[str, str]], Coroutine[object, object, tuple[int, bytes, bytes]]
]


class SubscriptionAuthRequired(AppError):
    """The CLI would not run on the user's own claude.ai subscription login (API billing risk)."""


class UsageLimitReached(BudgetExceeded):
    """The subscription's session / weekly limit was reached: the Director pauses, nothing is retried."""


async def run_process(
    argv: Sequence[str], stdin: bytes, timeout_s: float, cwd: Path, env: Mapping[str, str]
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=dict(env),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"", b"timeout"
    return proc.returncode or 0, out, err


def clean_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k not in BILLING_ENV}


@dataclass
class ClaudeCliLLM:
    executable: str
    workdir: Path
    runner: Runner = run_process
    model: str | None = "opus"  # alias; the model that answered is recorded from `modelUsage`
    timeout_s: float = 300.0
    model_id: str = "claude-code-cli"
    llm_cutoff: str | None = None
    billed: bool = False  # subscription: no per-call charge (usage counts against the plan's limits)
    served_models: list[str] = field(default_factory=lambda: list[str]())

    @classmethod
    def create(
        cls,
        workdir: Path,
        *,
        environ: Mapping[str, str] | None = None,
        runner: Runner = run_process,
        executable: str | None = None,
    ) -> ClaudeCliLLM:
        env = dict(os.environ if environ is None else environ)
        risky = [v for v in BILLING_ENV if env.get(v)]
        if risky:
            raise SubscriptionAuthRequired(f"refusing: {risky} set (claude -p would use API billing)")
        exe = executable or shutil.which("claude")
        if exe is None:
            raise SubscriptionAuthRequired("the claude CLI was not found on PATH")
        workdir.mkdir(parents=True, exist_ok=True)
        rc, out, _err = asyncio.run(runner([exe, "auth", "status", "--json"], b"", 60.0, workdir, clean_env(env)))
        try:
            status = cast(dict[str, object], json.loads(out.decode("utf-8", "replace")))
        except json.JSONDecodeError as e:
            raise SubscriptionAuthRequired("claude auth status did not return JSON") from e
        method, logged_in = status.get("authMethod"), status.get("loggedIn")
        if rc != 0 or logged_in is not True or method != "claude.ai":
            raise SubscriptionAuthRequired(f"auth status: loggedIn={logged_in}, authMethod={method} (need claude.ai)")
        return cls(exe, workdir, runner)

    def argv(self, request: LLMRequest) -> list[str]:
        args = [
            self.executable,
            "-p",
            INSTRUCTION,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(api_schema(request.schema), sort_keys=True),
            "--system-prompt",
            request.system,
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--setting-sources",
            "local",
            "--no-session-persistence",
            "--max-turns",
            "3",
        ]
        if self.model:
            args += ["--model", self.model]
        return args

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload = json.dumps(request.payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        start = time.perf_counter()
        rc, out, err = asyncio.run(
            self.runner(self.argv(request), payload, self.timeout_s, self.workdir, clean_env(os.environ))
        )
        latency = (time.perf_counter() - start) * 1000
        text = out.decode("utf-8", "replace")
        try:
            body = cast(dict[str, object], json.loads(text))
        except json.JSONDecodeError:
            lowered = (text + err.decode("utf-8", "replace")).lower()
            if any(m in lowered for m in LIMIT_MARKERS):
                raise UsageLimitReached(f"claude CLI usage limit: {lowered.strip()[:200]}") from None
            return LLMResponse("error", None, latency_ms=latency, detail=f"exit {rc}: no JSON result"[:300])
        result = str(body.get("result") or "")
        if any(m in result.lower() for m in LIMIT_MARKERS) or body.get("api_error_status") == 429:
            raise UsageLimitReached(f"claude CLI usage limit: {result[:200]}")
        usage = body.get("usage")
        u = cast(dict[str, object], usage) if isinstance(usage, dict) else {}

        def n(key: str) -> int:
            v = u.get(key, 0)
            return int(v) if isinstance(v, int) else 0

        models = body.get("modelUsage")
        self.served_models = sorted(cast(dict[str, object], models)) if isinstance(models, dict) else []
        detail = (
            f"subtype={body.get('subtype')} turns={body.get('num_turns')} models={self.served_models} "
            f"notional_total_cost_usd={body.get('total_cost_usd')}"
        )

        def resp(status: str, answer: str | None) -> LLMResponse:
            return LLMResponse(
                "ok" if status == "ok" else "refusal" if status == "refusal" else "error",
                answer,
                input_tokens=n("input_tokens"),
                output_tokens=n("output_tokens"),
                cache_read_tokens=n("cache_read_input_tokens"),
                cache_write_tokens=n("cache_creation_input_tokens"),
                latency_ms=latency,
                detail=detail[:300],
            )

        if body.get("stop_reason") == "refusal" or body.get("terminal_reason") == "refusal":
            return resp("refusal", None)
        if body.get("is_error") is True or rc != 0:
            return resp("error", None)
        structured = body.get("structured_output")
        if structured is not None:
            return resp("ok", json.dumps(structured, sort_keys=True))
        return resp("ok", result or None)  # the Director validates; an invalid answer is a schema retry
