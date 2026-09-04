"""Migration 0032: add ``must_change_password`` flag to users (#3172).

DISA ASD STIG finding V-222547 (CAT II) requires forcing a password
change on first login when an admin creates a user with a password or
resets an existing user's password. The flag is set by the admin paths
and cleared atomically when the user changes their own password.

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
