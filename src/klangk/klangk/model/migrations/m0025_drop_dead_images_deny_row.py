"""Migration 0025: drop the retired Deny Everyone row on /images (#2994).

The #2946 seed for ``/images`` was Allow ``view-images`` Authenticated
(position 0) + Deny ``*`` Everyone (position 1). #2994 keeps the Allow
Authenticated row as the deliberate, operator-modifiable default and
removes the Deny row from the seed — this migration applies the same
cleanup to existing deployments.

No ``/images`` route checks a permission other than ``view-images``
(``GET /images``), and unauthenticated requests die at the JWT
middleware before any ACL walk — so the row gates no route. It did,
however, mask the root ``/`` Allow ``view`` Authenticated inheritance
in the ``/my-permissions`` map: after this migration an authenticated
user's effective permissions on ``/images`` include the inherited
``view`` (informational only — nothing checks it).

Only a row matching the retired seed's exact shape (Deny, system
Everyone, ``*``, position 1) on ``/images`` is deleted. A fresh
database (entirely empty ``acl_entries``) is a no-op — the boot seeds
own it.
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
        " AND permission = '*' AND position = 1",
        (ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE),
    )


migration = Migration(
    id=25,
    name="0025_drop_dead_images_deny_row",
    apply=apply,
)
