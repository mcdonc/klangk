"""Migration 0005: users.disabled + users.last_activity_at (#2588).

``last_activity_at`` is stamped (throttled) by ``Auth.record_activity``
on authenticated API access; ``disabled`` is set by the inactivity
sweep (``klangk.inactivity.InactivitySweeper``) or by an admin via
``PATCH /admin/users/{id}``. ``disabled`` defaults to 0 (enabled) and
``last_activity_at`` is nullable — NULL means the user has made no
authenticated request since the column was introduced.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
    )
    await db.execute("ALTER TABLE users ADD COLUMN last_activity_at TEXT")


migration = Migration(5, "0005_user_inactivity", apply)
