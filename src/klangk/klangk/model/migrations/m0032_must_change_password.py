"""Migration 0032: add ``must_change_password`` flag to users (#3172).

An admin-chosen password (user creation with a password, or an admin
password reset) is temporary: the user must be forced to change it on
first login. The flag is set by the admin paths and cleared atomically
when the user changes their own password.

The column defaults to 0 (false): existing users keep their current
passwords without being forced to change. Only new admin-set passwords
trigger the flag going forward.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        "ALTER TABLE users ADD COLUMN"
        " must_change_password INTEGER NOT NULL DEFAULT 0"
    )


migration = Migration(
    id=32,
    name="0032_must_change_password",
    apply=apply,
)
