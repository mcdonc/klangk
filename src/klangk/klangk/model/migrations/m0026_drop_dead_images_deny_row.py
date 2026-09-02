"""Migration 0026: drop the dead Deny Everyone row on /images (#2974).

The #2946 seed for ``/images`` was Allow ``view-images`` Authenticated
(position 0) + Deny ``*`` Everyone (position 1). The Deny row can never
fire: unauthenticated requests are rejected by the JWT middleware
before any ACL check, and no-match is already default-deny. #2974 keeps
the Allow Authenticated row as the deliberate, operator-modifiable
default and removes the dead row from the seed — this migration applies
the same cleanup to existing deployments.

Only rows matching the retired seed's Deny exactly (Deny, system
principal Everyone, ``*``) on ``/images`` are deleted; any other row an
operator added is left untouched.

A fresh database (entirely empty ``acl_entries``) is a no-op — the boot
seeds own it.
"""

from klangk.model.acl import (
    ACTION_DENY,
    PRINCIPAL_SYSTEM,
    SYSTEM_EVERYONE,
)
from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it
    await db.execute(
        "DELETE FROM acl_entries"
        " WHERE resource = '/images' AND action = ?"
        " AND principal_type = ? AND system_principal = ?"
        " AND permission = '*'",
        (ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE),
    )


migration = Migration(
    id=26,
    name="0026_drop_dead_images_deny_row",
    apply=apply,
)
