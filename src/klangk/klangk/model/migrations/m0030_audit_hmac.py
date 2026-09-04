"""Migration 0030: HMAC integrity column on audit tables (#3174).

Adds a nullable ``hmac`` TEXT column to ``container_events`` and
``egress_consent``.  New rows are written with an HMAC tag; existing
rows keep NULL (pre-feature data has no tag to verify — the
verification endpoint reports them as ``no_hmac`` rather than
``tampered``).
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE container_events ADD COLUMN hmac TEXT")
    await db.execute("ALTER TABLE egress_consent ADD COLUMN hmac TEXT")


migration = Migration(30, "0030_audit_hmac", apply)
