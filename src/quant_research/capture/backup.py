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
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from quant_research.capture.compress import hour_end_ns
from quant_research.catalog.store import ROW_COUNT_SQL

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


def eligible_files(data_root: Path, now_ns: int) -> list[Path]:
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
    vanished: int = 0  # listed, then replaced by its .gz (compress job) before it was copied


def backup_closed_files(data_root: Path, dest: Path, *, now_ns: int) -> BackupResult:
    manifest_path = dest / "manifest.json"
    manifest: dict[str, dict[str, object]] = (
        cast(dict[str, dict[str, object]], json.loads(manifest_path.read_text(encoding="utf-8")))
        if manifest_path.exists()
        else {}
    )
    result = BackupResult()
    for src in eligible_files(data_root, now_ns):
        rel = src.relative_to(data_root).as_posix()
        target = dest / "files" / rel
        tmp = target.with_name(target.name + ".tmp")
        try:
            size = src.stat().st_size
            known = manifest.get(rel)
            if known is not None and known.get("size") == size:
                digest = _sha256(src) if rel.startswith(("raw/_", "state/")) else str(known["sha256"])
                if digest == known["sha256"]:
                    result.unchanged += 1
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tmp)
        except FileNotFoundError:
            # compress replaced this .jsonl by its .gz after listing; the .gz is copied on the next run
            tmp.unlink(missing_ok=True)
            result.vanished += 1
            continue
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


KEEP_CATALOG_SNAPSHOTS = 7
_SNAPSHOT_INDEX = "snapshots.json"


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    path: Path
    sha256: str
    tables: dict[str, int]


def _row_counts(db: Path) -> dict[str, int]:
    """Rows per catalog table that EXISTS in this file (a snapshot taken before a migration lacks its table)."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        present = {str(r[0]) for r in conn.execute("select name from sqlite_master where type = 'table'")}
        return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in ROW_COUNT_SQL.items() if name in present}
    finally:
        conn.close()


def _integrity_ok(db: Path) -> bool:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def snapshot_catalog(data_root: Path, dest: Path, *, now_ns: int) -> CatalogSnapshot | None:
    """Consistent copy of `meta/catalog.db` via `VACUUM INTO` (safe while other connections write, WAL
    included), one per UTC day (replaced during the day), the last KEEP_CATALOG_SNAPSHOTS days kept.
    sha256 and per-table row counts go to `catalog/snapshots.json` for the restore drill."""
    src = data_root / "meta" / "catalog.db"
    if not src.exists():
        return None
    folder = dest / "catalog"
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(now_ns / 1e9, UTC).strftime("%Y%m%d")
    target = folder / f"catalog-{day}.db"
    tmp = folder / f"catalog-{day}.db.tmp"
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(src, timeout=30)
    try:
        conn.execute("vacuum into ?", (str(tmp),))
    finally:
        conn.close()
    snap = CatalogSnapshot(target, _sha256(tmp), _row_counts(tmp))
    os.replace(tmp, target)
    index_path = folder / _SNAPSHOT_INDEX
    index = (
        cast(dict[str, dict[str, object]], json.loads(index_path.read_text(encoding="utf-8")))
        if index_path.exists()
        else {}
    )
    index[target.name] = {"sha256": snap.sha256, "tables": snap.tables, "taken_ns": now_ns}
    for old in sorted(index)[:-KEEP_CATALOG_SNAPSHOTS]:
        (folder / old).unlink(missing_ok=True)
        del index[old]
    tmp_index = folder / f"{_SNAPSHOT_INDEX}.tmp"
    tmp_index.write_text(json.dumps(index, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp_index, index_path)
    return snap


def restore_catalog_and_verify(dest: Path, restore_to: Path) -> RestoreReport:
    """Restore every kept catalog snapshot; each must match its sha256, pass `integrity_check` and have
    the recorded row count in every table."""
    folder = dest / "catalog"
    index = cast(dict[str, dict[str, object]], json.loads((folder / _SNAPSHOT_INDEX).read_text(encoding="utf-8")))
    bad: list[str] = []
    restore_to.mkdir(parents=True, exist_ok=True)
    for name, meta in sorted(index.items()):
        target = restore_to / name
        shutil.copyfile(folder / name, target)
        if _sha256(target) != meta["sha256"] or not _integrity_ok(target) or _row_counts(target) != meta["tables"]:
            bad.append(name)
    return RestoreReport(len(index), bad)


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
