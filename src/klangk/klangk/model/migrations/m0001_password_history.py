"""Migration 0001: password history for reuse prevention (#2582).

One row per (user, password hash) at the time it was set. Nothing reads
or writes this table yet — the reuse check and the
``KLANGKD_PASSWORD_HISTORY_COUNT`` setting land with #2582, which stacks
on this schema. ``ON DELETE CASCADE`` keeps history from outliving its
user.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS password_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)  # noqa: S608
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_password_history_user
        ON password_history(user_id, id DESC)
    """)


migration = Migration(1, "0001_password_history", apply)
