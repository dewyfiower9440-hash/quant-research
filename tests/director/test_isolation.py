"""Phase 3 isolation (design v2.1 §3.1, §9 권한 분리, §15): the research loop cannot read the holdout and
cannot reach the network except through the one LLM adapter."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "quant_research"
LOOP_PACKAGES = ("director", "research")
NETWORK_MODULES = {"httpx", "httpx2", "requests", "urllib", "socket", "websockets", "aiohttp", "anthropic"}
LLM_ADAPTER = Path("director/claude.py")  # the only module allowed to import the Anthropic SDK


def _modules() -> list[tuple[Path, ast.Module]]:
    out: list[tuple[Path, ast.Module]] = []
    for pkg in LOOP_PACKAGES:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            out.append((path.relative_to(ROOT), ast.parse(path.read_text(encoding="utf-8"))))
    return out


def test_the_research_loop_never_asks_for_the_validator_role() -> None:
    offenders: list[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "role":
                offenders.append(f"{rel}:{node.value.lineno} passes role=")
            if isinstance(node, ast.Constant) and node.value == "validator" and rel.parts[0] != "director":
                offenders.append(f"{rel}:{node.lineno} mentions 'validator'")
    assert offenders == []


def test_only_the_llm_adapter_may_import_network_clients() -> None:
    offenders: list[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names, line = [a.name for a in node.names], node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names, line = [node.module], node.lineno
            else:
                continue
            for n in names:
                if n.split(".")[0] in NETWORK_MODULES and rel != LLM_ADAPTER:
                    offenders.append(f"{rel}:{line} imports {n}")
    assert offenders == []


def test_the_research_loop_does_not_import_the_collectors() -> None:
    offenders = [
        f"{rel}:{node.lineno}"
        for rel, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("quant_research.capture.")
        and node.module not in ("quant_research.capture.lock", "quant_research.capture.thermal")
    ]
    assert offenders == []
