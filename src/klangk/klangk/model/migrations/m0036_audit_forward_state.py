"""Migration 0036: the ``audit_forward_state`` table (#3252).

One row per forwarded audit source (``audit_events``,
``container_events``, ``egress_consent``) holding the forwarding
watermark — the rowid of the last record the target accepted. A
restarted klangkd resumes exactly after that record: the forwarder
reads rows past the watermark, delivers them, then advances it, so a
crash between delivery and advance replays that batch (at-least-once
delivery, #3252). Rows are written only by
:meth:`klangk.model.audit_forward.AuditForwardModel.advance`.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_forward_state (
            source TEXT PRIMARY KEY,
            watermark INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


migration = Migration(
    id=36,
    name="0036_audit_forward_state",
    apply=apply,
)
