-- Monitor incidents (plan step 10): one row per episode of a failing check. At most one open row per
-- key; the monitor updates last_seen_ns/severity while it stays open and sets closed_ns when it clears.
create table incident (
    incident_id integer primary key,
    key text not null,
    severity text not null check (severity in ('warn', 'critical')),
    opened_ns integer not null,
    last_seen_ns integer not null,
    closed_ns integer,
    detail text not null,
    check (last_seen_ns >= opened_ns),
    check (closed_ns is null or closed_ns >= last_seen_ns)
);
create unique index incident_one_open on incident(key) where closed_ns is null;
