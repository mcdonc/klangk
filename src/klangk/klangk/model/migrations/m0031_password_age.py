"""Migration 0031: users.password_set_at (#3177).

The password-age policy (minimum and maximum password age)
needs to know when the current password was set. ``update_password``
stamps the column on every password write; rows whose password predates
the migration keep ``NULL`` and the age predicates fall back to
``created_at`` (the password is as old as the account — the honest
upper bound for a deploy that turns on ``KLANGKD_PASSWORD_MAX_AGE_DAYS``
after the fact). Nullable, like ``last_login_at`` (#2583 precedent):
NULL simply means "not changed since the column was introduced".
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute("ALTER TABLE users ADD COLUMN password_set_at TEXT")


migration = Migration(31, "0031_password_age", apply)
