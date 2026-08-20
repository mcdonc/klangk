"""Migration 0001: password history for reuse prevention (#2582).

One row per retired hash: ``update_password`` inserts the *old* hash
here when the user changes away from it, in the same transaction as
the swap. ``Auth.validate_password_not_reused`` checks the window
before every set; ``KLANGKD_PASSWORD_HISTORY_COUNT`` bounds it.
``ON DELETE CASCADE`` keeps history from outliving its user.
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
