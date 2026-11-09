"""research.db: experiments, trials, backtest runs (Phase 2 step 1). Single writer: the research process."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from quant_research.adapters.sqlite import connect, migrate


class ResearchStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> ResearchStore:
        conn = connect(path)
        migrate(conn, "quant_research.research")
        return cls(conn)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.conn.execute("begin immediate")
        try:
            yield
            self.conn.execute("commit")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("rollback")
            raise
