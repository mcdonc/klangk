"""Migration 0034: the ``audit_events`` table (#3205).

The structured audit stream for identity and privilege actions —
account create/update/delete, group membership and ACL changes,
workspace role assignments, login/logout/failed-login, and session
revocation — each row carrying the acting principal (id + denormalized
email), the target it acted on, a JSON detail blob, and the
per-request HTTP metadata (effective client IP, user agent).

Written at the route / auth choke points via
``app.state.model.audit_events.record_best_effort`` (#3205); every
write is best-effort (logged, never fatal to the action). Integrity
protection rides the shared opt-in HMAC tagging (#3174): rows are
tagged at insert time when ``KLANGKD_AUDIT_HMAC_KEY`` is configured,
hence the ``hmac`` column ships with the table. Retention/bounding is
the ``prune`` sweep (retention window + deploy-wide row cap, swept
hourly by the consent sweeper).
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY,
            event TEXT NOT NULL,
            actor_id TEXT,
            actor_email TEXT,
            target_type TEXT,
            target_id TEXT,
            detail TEXT,
            source_ip TEXT,
            user_agent TEXT,
            created_at REAL NOT NULL,
            hmac TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_time
            ON audit_events (created_at DESC)
        """
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_event
            ON audit_events (event)
        """
    )


migration = Migration(34, "0034_audit_events", apply)
