"""Migration 0030: HMAC integrity column on audit tables (#3174).

Adds a nullable ``hmac`` TEXT column to ``container_events`` and
``egress_consent``.  Rows written with ``KLANGKD_AUDIT_HMAC_KEY``
configured carry an HMAC tag; rows written without a key (and all
pre-feature rows) keep NULL — no tag was computed for them.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE container_events ADD COLUMN hmac TEXT")
    await db.execute("ALTER TABLE egress_consent ADD COLUMN hmac TEXT")


migration = Migration(30, "0030_audit_hmac", apply)
