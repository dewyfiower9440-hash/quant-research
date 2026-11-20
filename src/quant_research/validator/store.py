"""validator.db: pre-registered submissions, the global query budget per seal epoch, one-bit verdicts with an
internal report, acks for research, and the hash-chained holdout access log (Phase 5 step 7; design §9-2).

Single writer: the validator process. Research never opens this file for writing; it polls `ack` read-only.
Every judgement consumes one query of the epoch's budget k (ADR-022: 20), re-queries of the same spec
included (optional stopping, §9-2). The access log is a SHA-256 chain: tampering with or deleting a row
breaks `verify_chain`."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from quant_research.adapters.sqlite import connect, migrate
from quant_research.errors import AppError

GENESIS = "0" * 64


class ValidatorError(AppError):
    """A validator rule was violated (budget, registration, chain)."""


class BudgetExhausted(ValidatorError):
    """The epoch's global query budget k is used up."""


@dataclass(frozen=True, slots=True)
class Submission:
    request_id: int
    spec_hash: str
    rule_hash: str
    experiment_key: str
    in_sample_sr: float
    dataset_ids: tuple[str, ...]
    epoch: int


@dataclass(frozen=True, slots=True)
class AccessRecord:
    seq: int
    submission_id: int | None
    dataset_id: str
    window_from_ns: int
    window_to_ns: int
    purpose: str
    at_ns: int
    prev_hash: str
    hash: str


def rule_hash(rule: Mapping[str, object]) -> str:
    """Pre-registration hash of the decision rule (alpha, k, c, stress multipliers, ...)."""
    body = json.dumps(dict(rule), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chain_hash(prev_hash: str, fields: Sequence[object]) -> str:
    body = prev_hash + "|" + "|".join(str(f) for f in fields)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ValidatorStore:
    def __init__(self, conn: sqlite3.Connection, *, now_ns: Callable[[], int] = time.time_ns) -> None:
        self.conn = conn
        self.now_ns = now_ns

    @classmethod
    def open(cls, path: Path, *, now_ns: Callable[[], int] = time.time_ns) -> ValidatorStore:
        conn = connect(path)
        migrate(conn, "quant_research.validator")
        return cls(conn, now_ns=now_ns)

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

    # ------------------------------------------------------------------ budget
    def ensure_budget(self, epoch: int, k: int, *, rule_hash: str) -> None:
        """Register the epoch's k and decision rule once; a later call with a different k or rule is refused
        (pre-registration). Every submission and verdict of the epoch must carry this rule hash."""
        row = self.conn.execute("select k, rule_hash from query_budget where epoch = ?", (epoch,)).fetchone()
        if row is None:
            self.conn.execute(
                "insert into query_budget(epoch, k, used, rule_hash) values (?, ?, 0, ?)", (epoch, k, rule_hash)
            )
        elif int(row[0]) != k:
            raise ValidatorError(f"epoch {epoch} budget is pre-registered as k={int(row[0])}, not {k}")
        elif row[1] is None:  # a row from before migration 0003: bind it once, only if nothing was spent
            used = int(self.conn.execute("select used from query_budget where epoch = ?", (epoch,)).fetchone()[0])
            if used:
                raise ValidatorError(f"epoch {epoch}: {used} queries already spent under an unrecorded rule")
            self.conn.execute("update query_budget set rule_hash = ? where epoch = ?", (rule_hash, epoch))
        elif row[1] != rule_hash:
            raise ValidatorError(f"epoch {epoch} budget is pre-registered under rule {row[1]}, not {rule_hash}")

    def budget_rule(self, epoch: int) -> str:
        row = self.conn.execute("select rule_hash from query_budget where epoch = ?", (epoch,)).fetchone()
        if row is None:
            raise ValidatorError(f"no query budget registered for epoch {epoch}")
        if row[0] is None:
            raise ValidatorError(f"epoch {epoch} budget has no registered decision rule (call ensure_budget)")
        return str(row[0])

    def budget(self, epoch: int) -> tuple[int, int]:
        row = self.conn.execute("select k, used from query_budget where epoch = ?", (epoch,)).fetchone()
        if row is None:
            raise ValidatorError(f"no query budget registered for epoch {epoch}")
        return int(row[0]), int(row[1])

    def _consume(self, epoch: int) -> int:
        k, used = self.budget(epoch)
        if used >= k:
            raise BudgetExhausted(f"epoch {epoch}: all {k} holdout queries are spent")
        self.conn.execute("update query_budget set used = used + 1 where epoch = ?", (epoch,))
        return used + 1

    # ------------------------------------------------------------------ submissions
    def register(self, s: Submission) -> int:
        """Pre-register a submission (spec + rule) before any holdout read. Duplicate request ids are refused."""
        if self.conn.execute("select 1 from submission where request_id = ?", (s.request_id,)).fetchone():
            raise ValidatorError(f"request {s.request_id} is already registered")
        if s.rule_hash != self.budget_rule(s.epoch):
            raise ValidatorError(
                f"request {s.request_id}: rule {s.rule_hash} is not the rule epoch {s.epoch} was registered with"
            )
        cur = self.conn.execute(
            "insert into submission(request_id, spec_hash, rule_hash, experiment_key, in_sample_sr, dataset_ids_json, "
            "epoch, registered_ns) values (?,?,?,?,?,?,?,?)",
            (
                s.request_id,
                s.spec_hash,
                s.rule_hash,
                s.experiment_key,
                s.in_sample_sr,
                json.dumps(sorted(s.dataset_ids)),
                s.epoch,
                self.now_ns(),
            ),
        )
        return int(cur.lastrowid or 0)

    def record_refused(self, s: Submission, why: str) -> int:
        """A request refused before it could be registered (rule mismatch, unreadable spec, used-up budget): kept
        as a `refused` submission so it is never retried and never spends a query (B8 review)."""
        if self.conn.execute("select 1 from submission where request_id = ?", (s.request_id,)).fetchone():
            raise ValidatorError(f"request {s.request_id} is already registered")
        cur = self.conn.execute(
            "insert into submission(request_id, spec_hash, rule_hash, experiment_key, in_sample_sr, dataset_ids_json, "
            "epoch, registered_ns, status, refusal) values (?,?,?,?,?,?,?,?, 'refused', ?)",
            (
                s.request_id,
                s.spec_hash,
                s.rule_hash,
                s.experiment_key,
                s.in_sample_sr,
                json.dumps(sorted(s.dataset_ids)),
                s.epoch,
                self.now_ns(),
                why,
            ),
        )
        return int(cur.lastrowid or 0)

    def submission_of(self, request_id: int) -> tuple[int, str] | None:
        """(submission id, status) of a request, None when the validator has not seen it."""
        row = self.conn.execute(
            "select submission_id, status from submission where request_id = ?", (request_id,)
        ).fetchone()
        return None if row is None else (int(row[0]), str(row[1]))

    def submission(self, submission_id: int) -> tuple[Submission, str]:
        row = self.conn.execute(
            "select request_id, spec_hash, rule_hash, experiment_key, in_sample_sr, dataset_ids_json, epoch, status "
            "from submission where submission_id = ?",
            (submission_id,),
        ).fetchone()
        if row is None:
            raise ValidatorError(f"unknown submission {submission_id}")
        ids = tuple(str(d) for d in json.loads(str(row[5])))
        return Submission(int(row[0]), str(row[1]), str(row[2]), str(row[3]), float(row[4]), ids, int(row[6])), str(
            row[7]
        )

    def refuse(self, submission_id: int, why: str) -> None:
        self.conn.execute(
            "update submission set status = 'refused', refusal = ? where submission_id = ? and status = 'registered'",
            (why, submission_id),
        )

    # ------------------------------------------------------------------ holdout access log (hash chain)
    def log_access(
        self, submission_id: int | None, dataset_id: str, window_from_ns: int, window_to_ns: int, purpose: str
    ) -> AccessRecord:
        prev = self.conn.execute("select hash from holdout_access_log order by seq desc limit 1").fetchone()
        prev_hash = str(prev[0]) if prev is not None else GENESIS
        at = self.now_ns()
        h = chain_hash(prev_hash, (submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at))
        cur = self.conn.execute(
            "insert into holdout_access_log(submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at_ns, "
            "prev_hash, hash) values (?,?,?,?,?,?,?,?)",
            (submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at, prev_hash, h),
        )
        return AccessRecord(
            int(cur.lastrowid or 0), submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at, prev_hash, h
        )

    def access_log(self) -> list[AccessRecord]:
        rows = self.conn.execute(
            "select seq, submission_id, dataset_id, window_from_ns, window_to_ns, purpose, at_ns, prev_hash, hash "
            "from holdout_access_log order by seq"
        ).fetchall()
        return [
            AccessRecord(
                int(r[0]),
                None if r[1] is None else int(r[1]),
                str(r[2]),
                int(r[3]),
                int(r[4]),
                str(r[5]),
                int(r[6]),
                str(r[7]),
                str(r[8]),
            )
            for r in rows
        ]

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for r in self.access_log():
            if r.prev_hash != prev:
                return False, f"seq {r.seq}: prev_hash does not match the previous row"
            expect = chain_hash(
                prev, (r.submission_id, r.dataset_id, r.window_from_ns, r.window_to_ns, r.purpose, r.at_ns)
            )
            if r.hash != expect:
                return False, f"seq {r.seq}: hash does not match its fields"
            prev = r.hash
        return True, "ok"

    # ------------------------------------------------------------------ verdicts
    def judge(self, submission_id: int, passed: bool, detail: Mapping[str, object]) -> int:
        """Spend one query of the submission's epoch and record the one-bit verdict + internal detail + ack.
        The budget is consumed even for a repeat of an already judged spec (a new submission is a new query)."""
        sub, status = self.submission(submission_id)
        if status != "registered":
            raise ValidatorError(f"submission {submission_id} is {status}, not registered")
        if sub.rule_hash != self.budget_rule(sub.epoch):  # e.g. registered before the epoch's rule was bound
            raise ValidatorError(
                f"submission {submission_id}: rule {sub.rule_hash} is not the rule of epoch {sub.epoch}"
            )
        if detail.get("rule_hash") != sub.rule_hash:
            raise ValidatorError(
                f"submission {submission_id}: the verdict was computed under rule {detail.get('rule_hash')}, "
                f"not the pre-registered {sub.rule_hash}"
            )
        with self.transaction():
            index = self._consume(sub.epoch)
            cur = self.conn.execute(
                "insert into verdict(submission_id, epoch, query_index, passed, created_ns) values (?,?,?,?,?)",
                (submission_id, sub.epoch, index, int(passed), self.now_ns()),
            )
            vid = int(cur.lastrowid or 0)
            self.conn.execute(
                "insert into verdict_detail(verdict_id, detail_json) values (?, ?)",
                (vid, json.dumps(dict(detail), sort_keys=True, default=str)),
            )
            self.conn.execute("update submission set status = 'judged' where submission_id = ?", (submission_id,))
            self.conn.execute(
                "insert into ack(request_id, submission_id, passed, at_ns) values (?,?,?,?)",
                (sub.request_id, submission_id, int(passed), self.now_ns()),
            )
        return vid

    def ack(self, request_id: int) -> bool | None:
        """The one bit for a request (None: not judged yet). This is all research ever learns (§9-2)."""
        row = self.conn.execute("select passed from ack where request_id = ?", (request_id,)).fetchone()
        return None if row is None else bool(row[0])


def open_readonly(path: Path) -> sqlite3.Connection:
    """How research reads acks: a read-only connection (no writes possible, design §3.1)."""
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute("pragma query_only = 1")
    return conn
