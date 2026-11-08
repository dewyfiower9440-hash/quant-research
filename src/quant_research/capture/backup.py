"""Incremental backup of irreplaceable capture data and a restore drill (plan step 4).

Backed up: closed raw segments (`.jsonl.gz`, or `.jsonl` whose UTC hour ended), gap/quarantine logs,
and `state/`. The segment of the current hour is skipped (still being written). A sha256 manifest makes
runs incremental and lets `restore_and_verify` prove that every file (and every line of every
segment) comes back intact. Until an external target is chosen (ADR-008) the destination is a folder
on the same SSD, which protects against deletion/corruption but NOT against SSD failure.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from quant_research.capture.compress import hour_end_ns

_CHUNK = 4 * 1024 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _lines(path: Path) -> int:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        return sum(1 for _ in fh)


def _eligible(data_root: Path, now_ns: int) -> list[Path]:
    out: list[Path] = []
    raw = data_root / "raw"
    for p in sorted(raw.glob("*/*/*.jsonl*")):
        if p.name.endswith(".tmp"):
            continue
        if p.suffix == ".jsonl":
            end = hour_end_ns(p.name)
            if end is None or end + 60 * 10**9 > now_ns:
                continue  # current hour (open; writers close within seconds of the boundary) or not a segment
        out.append(p)
    for folder in ("_gaps", "_quarantine"):
        out += sorted(p for p in (raw / folder).glob("*") if p.is_file())
    out += sorted(p for p in (data_root / "state").glob("*") if p.is_file() and not p.name.endswith(".tmp"))
    return out


@dataclass
class BackupResult:
    copied: list[Path] = field(default_factory=lambda: list[Path]())
    unchanged: int = 0


def backup_closed_files(data_root: Path, dest: Path, *, now_ns: int) -> BackupResult:
    manifest_path = dest / "manifest.json"
    manifest: dict[str, dict[str, object]] = (
        cast(dict[str, dict[str, object]], json.loads(manifest_path.read_text(encoding="utf-8")))
        if manifest_path.exists()
        else {}
    )
    result = BackupResult()
    for src in _eligible(data_root, now_ns):
        rel = src.relative_to(data_root).as_posix()
        size = src.stat().st_size
        known = manifest.get(rel)
        if known is not None and known.get("size") == size:
            digest = _sha256(src) if rel.startswith(("raw/_", "state/")) else str(known["sha256"])
            if digest == known["sha256"]:
                result.unchanged += 1
                continue
        target = dest / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copyfile(src, tmp)
        digest = _sha256(tmp)
        os.replace(tmp, target)  # destination is written only by this job
        manifest[rel] = {"size": size, "sha256": digest, "lines": _lines(target)}
        result.copied.append(src)
    dest.mkdir(parents=True, exist_ok=True)
    tmp_manifest = manifest_path.with_name("manifest.json.tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp_manifest, manifest_path)
    return result


@dataclass
class RestoreReport:
    files: int
    mismatches: list[str]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def restore_and_verify(dest: Path, restore_to: Path) -> RestoreReport:
    """Restore every manifest entry into `restore_to` and compare sha256 and line counts."""
    manifest = cast(dict[str, dict[str, object]], json.loads((dest / "manifest.json").read_text(encoding="utf-8")))
    bad: list[str] = []
    for rel, meta in sorted(manifest.items()):
        target = restore_to / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest / "files" / rel, target)
        if _sha256(target) != meta["sha256"] or _lines(target) != meta["lines"]:
            bad.append(rel)
    return RestoreReport(len(manifest), bad)
