"""Incident bookkeeping in the catalog (plan step 10): a failing check opens one incident per key,
keeps it open while it fails, closes it when it passes. Returns what needs a notification."""

from __future__ import annotations

from dataclasses import dataclass

from quant_research.catalog.store import Catalog
from quant_research.monitor.checks import Finding, Severity

_RANK = {"warn": 1, "critical": 2}
REALERT_NS = 6 * 3_600 * 10**9  # OPS-009: an open critical incident is notified again this often (initial prior)


@dataclass(frozen=True, slots=True)
class OpenIncident:
    incident_id: int
    key: str
    severity: Severity
    opened_ns: int


def open_incidents(catalog: Catalog) -> list[OpenIncident]:
    rows = catalog.conn.execute(
        "select incident_id, key, severity, opened_ns from incident where closed_ns is null order by key"
    )
    return [OpenIncident(int(r[0]), str(r[1]), "critical" if r[2] == "critical" else "warn", int(r[3])) for r in rows]


def reconcile(catalog: Catalog, findings: list[Finding], *, now_ns: int, realert_ns: int = REALERT_NS) -> list[Finding]:
    """Open new incidents, refresh or escalate open ones, close cleared ones. The returned findings are the
    newly opened or escalated incidents, plus open CRITICAL ones not notified for `realert_ns` (OPS-009: the F-017
    crash loop toasted once and then stayed silent for 7 days)."""
    current = {i.key: i for i in open_incidents(catalog)}
    notify: list[Finding] = []
    with catalog.transaction():
        for f in findings:
            known = current.pop(f.key, None)
            if known is None:
                catalog.conn.execute(
                    "insert into incident(key, severity, opened_ns, last_seen_ns, detail, last_notified_ns) "
                    "values (?,?,?,?,?,?)",
                    (f.key, f.severity, now_ns, now_ns, f.detail, now_ns),
                )
                notify.append(f)
                continue
            row = catalog.conn.execute(
                "select coalesce(last_notified_ns, opened_ns) from incident where incident_id = ?",
                (known.incident_id,),
            ).fetchone()
            escalated = _RANK[f.severity] > _RANK[known.severity]
            due = f.severity == "critical" and now_ns - int(row[0]) >= realert_ns
            catalog.conn.execute(
                "update incident set last_seen_ns = ?, severity = ?, detail = ?, "
                "last_notified_ns = case when ? then ? else last_notified_ns end where incident_id = ?",
                (now_ns, f.severity, f.detail, int(escalated or due), now_ns, known.incident_id),
            )
            if escalated or due:
                notify.append(f)
        for gone in current.values():
            catalog.conn.execute("update incident set closed_ns = ? where incident_id = ?", (now_ns, gone.incident_id))
    return notify
