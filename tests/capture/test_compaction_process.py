"""Review #1: compaction runs in a short-lived child; the collector process never loads Polars."""

import ast
import json
from pathlib import Path

from quant_research.capture.run import compact_in_child, gapscan_in_child
from quant_research.capture.segment import hour_key

HOUR_NS = 3_600 * 10**9
T0 = (1_794_000_000 * 10**9) // HOUR_NS * HOUR_NS
SRC = Path(__file__).resolve().parents[2] / "src"


def _module_level_imports(module: str) -> set[str]:
    """Imports executed when `module` is imported: top-level statements only (not function bodies and
    not `if TYPE_CHECKING:` blocks)."""
    path = SRC / Path(*module.split("."))
    file = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    out: set[str] = set()
    for node in ast.parse(file.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            out |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            for alias in node.names:  # `from pkg import submodule`
                sub = SRC / Path(*node.module.split("."), alias.name)
                if sub.with_suffix(".py").exists() or (sub / "__init__.py").exists():
                    out.add(f"{node.module}.{alias.name}")
    return out


def test_collector_module_does_not_import_polars_transitively() -> None:
    seen: set[str] = set()
    todo = ["quant_research.capture.run"]
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        for imported in _module_level_imports(module):
            assert imported.split(".")[0] != "polars", f"{module} imports {imported} at module level"
            if imported.startswith("quant_research"):
                parts = imported.split(".")
                todo += [".".join(parts[:i]) for i in range(2, len(parts) + 1)]
    assert "quant_research.capture.binance" in seen  # the walk really followed the collector's imports


def test_compaction_in_a_child_process_writes_the_lake_and_returns_the_result(tmp_path: Path) -> None:
    source = "binance.um.book_ticker_1s"
    seg = tmp_path / "raw" / source / "2026-11-08" / f"{source}_{hour_key(T0)}_{T0 + 1}.jsonl"
    seg.parent.mkdir(parents=True)
    book = [
        {
            "symbol": "BTCUSDT",
            "bidPrice": "1",
            "bidQty": "1",
            "askPrice": "2",
            "askQty": "1",
            "time": 1,
            "lastUpdateId": 5,
        }
    ]
    lines = [json.dumps({"header": {"format": 2, "source": source}})]
    lines.append(json.dumps({"recv_ns": T0 + 10, "offset_ms": -5.0, "clock_suspect": False, "payload": book}))
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = compact_in_child(tmp_path)  # the segment's hour ended long before "now"
    assert (done.compacted, done.rows, done.failed) == (1, 1, [])
    assert len(list((tmp_path / "lake" / source).rglob("*.parquet"))) == 1


def test_gap_scan_in_a_child_process_records_a_silence(tmp_path: Path) -> None:
    source = "upbit.orderbook_l1_1s"
    seg = tmp_path / "raw" / source / "2026-11-08" / f"{source}_{hour_key(T0)}_{T0 + 1}.jsonl"
    seg.parent.mkdir(parents=True)
    lines = [json.dumps({"header": {"format": 2, "source": source}})]
    for t in (T0 + 10**9, T0 + 600 * 10**9):  # ten silent minutes
        lines.append(json.dumps({"recv_ns": t, "offset_ms": 1.0, "clock_suspect": False, "payload": []}))
    seg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = gapscan_in_child(tmp_path)
    assert (done.scanned, done.gaps) == (1, 1)
