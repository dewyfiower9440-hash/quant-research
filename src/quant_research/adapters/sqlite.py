"""SQLite helpers shared by the owner-per-file databases (catalog.db, research.db; design v2.1 §3.1):
connection pragmas and numbered-SQL migrations tracked in `pragma user_version`."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions by the caller
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma foreign_keys=on")
    conn.execute("pragma busy_timeout=10000")
    conn.execute("pragma synchronous=full")
    return conn


def migrations(package: str) -> list[tuple[int, str]]:
    """(number, sql) of `<package>/migrations/NNNN_*.sql`, ascending."""
    folder = resources.files(package) / "migrations"
    found: list[tuple[int, str]] = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return sorted(found)


FOREIGN_KEYS_OFF = "-- migrate: foreign_keys=off"


def migrate(conn: sqlite3.Connection, package: str) -> None:
    """Apply the pending migrations, one transaction each. A migration whose first line is `FOREIGN_KEYS_OFF`
    (a table rebuild) runs with foreign keys off (the pragma is a no-op inside a transaction, so it is set
    around it) and must leave `pragma foreign_key_check` empty, or it is rolled back."""
    current = int(conn.execute("pragma user_version").fetchone()[0])
    for number, sql in migrations(package):
        if number <= current:
            continue
        keys_off = sql.lstrip().startswith(FOREIGN_KEYS_OFF)
        if keys_off:
            conn.execute("pragma foreign_keys=off")
        try:
            conn.execute("begin immediate")
            try:
                for statement in split_sql(sql):
                    conn.execute(statement)
                if keys_off:
                    broken = conn.execute("pragma foreign_key_check").fetchall()
                    if broken:
                        raise sqlite3.IntegrityError(f"migration {number} leaves foreign key violations: {broken[:5]}")
                conn.execute(f"pragma user_version = {number}")
                conn.execute("commit")
            except sqlite3.Error:
                conn.execute("rollback")
                raise
        finally:
            if keys_off:
                conn.execute("pragma foreign_keys=on")


def split_sql(sql: str) -> list[str]:
    """Split a migration into statements; trigger bodies (begin ... end;) stay whole."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        lowered = stripped.lower()
        if lowered.startswith("create trigger"):
            in_trigger = True
        if (in_trigger and lowered.endswith("end;")) or (not in_trigger and stripped.endswith(";")):
            statements.append("\n".join(buf))
            buf, in_trigger = [], False
    if buf:
        statements.append("\n".join(buf))
    return statements
