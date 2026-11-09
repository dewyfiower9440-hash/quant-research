"""Incident bookkeeping in the catalog (plan step 10): a failing check opens one incident per key,
keeps it open while it fails, closes it when it passes. Returns what needs a notification."""

from __future__ import annotations

from dataclasses import dataclass

from quant_research.catalog.store import Catalog
from quant_research.monitor.checks import Finding, Severity

_RANK = {"warn": 1, "critical": 2}


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


def reconcile(catalog: Catalog, findings: list[Finding], *, now_ns: int) -> list[Finding]:
    """Open new incidents, refresh or escalate open ones, close cleared ones. The returned findings are
    the newly opened or escalated incidents (the ones to notify)."""
    current = {i.key: i for i in open_incidents(catalog)}
    notify: list[Finding] = []
    with catalog.transaction():
        for f in findings:
            known = current.pop(f.key, None)
            if known is None:
                catalog.conn.execute(
                    "insert into incident(key, severity, opened_ns, last_seen_ns, detail) values (?,?,?,?,?)",
                    (f.key, f.severity, now_ns, now_ns, f.detail),
                )
                notify.append(f)
                continue
            catalog.conn.execute(
                "update incident set last_seen_ns = ?, severity = ?, detail = ? where incident_id = ?",
                (now_ns, f.severity, f.detail, known.incident_id),
            )
            if _RANK[f.severity] > _RANK[known.severity]:
                notify.append(f)
        for gone in current.values():
            catalog.conn.execute("update incident set closed_ns = ? where incident_id = ?", (now_ns, gone.incident_id))
    return notify
