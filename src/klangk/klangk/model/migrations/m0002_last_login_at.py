"""Migration 0002: users.last_login_at (#2583).

Stamped by ``UsersModel.record_login`` on every successful login and
shown back to the user via ``GET /auth/me`` (the TUI main screen and
``klangk account show`` display it). Nullable — NULL means the user has
not logged in since the column was introduced.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")


migration = Migration(2, "0002_last_login_at", apply)
