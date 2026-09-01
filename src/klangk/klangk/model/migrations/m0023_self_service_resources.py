"""Migration 0023: seed the self-service resource ACLs (#2946).

#2946 turns two previously ungoverned surfaces into checked
resources and splits one picker endpoint out of ``manage-users:

- ``/volumes``     — ``manage-volumes`` (self-service volumes, still
                     label-scoped to the caller at runtime)
- ``/images``      — ``view-images`` (the image/nix/sudo capability
                     listing the create/edit UIs read)
- ``/users``       — ``search-users`` (the member-picker type-ahead;
                     ``GET /users/search`` used to need only
                     authentication, and it still must for
                     non-admins — #2943's manage-users does NOT
                     imply it)

Without rows, existing deployments lock every user out of volumes and
images on upgrade (the ACL walk from these resources never passes
through ``/``'s Allow ``view`` — different permission — so nothing
satisfies the new checks). Each resource gets
the two rows a fresh install seeds: Allow for Authenticated, Deny for
Everyone. ``/users`` instead gets one inserted row — Allow
``search-users`` Authenticated at position 1 — with any existing
``/users`` rows at position >= 1 shifted up one, so first-match-wins
order is preserved (seed shape: manage-users @0, search-users @1,
Deny ``*`` @2).

A fresh database (entirely empty ``acl_entries``) is a no-op — the
boot seeds own it (the m0021 precedent; inserting here would trip the
seed's empty-table gate and lose the ``/``, ``/workspaces``, ``/admin``
rows).

Idempotent by construction: the per-resource existence check no-ops on
re-run; the ``/users`` insert checks for an existing ``search-users``
row first.
"""

from klangk.model.acl import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
from klangk.model.migrations.base import Migration

# resource -> the permission authenticated users get on it
RESOURCES = {
    "/volumes": "manage-volumes",
    "/images": "view-images",
}


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it

    for resource, permission in RESOURCES.items():
        cursor = await db.execute(
            "SELECT COUNT(*) FROM acl_entries WHERE resource = ?",
            (resource,),
        )
        if (await cursor.fetchone())[0] > 0:
            continue  # operator-staged or already migrated
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            " group_id, system_principal, permission)"
            " VALUES (?, 0, ?, ?, NULL, NULL, ?, ?)",
            (
                resource,
                ACTION_ALLOW,
                PRINCIPAL_SYSTEM,
                SYSTEM_AUTHENTICATED,
                permission,
            ),
        )
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            " group_id, system_principal, permission)"
            " VALUES (?, 1, ?, ?, NULL, NULL, ?, ?)",
            (resource, ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE, "*"),
        )

    # /users: insert Allow search-users Authenticated at position 1,
    # shifting existing rows at position >= 1 up. The shift runs in two
    # steps via a large offset: a single ``position + 1`` UPDATE
    # violates UNIQUE(resource, position) on any run of two or more
    # consecutive positions (the admin ACL browser's output shape), so
    # rows first move far above the occupied range and then settle
    # back down. The insert itself is idempotent (skip if a
    # search-users row already exists).
    cursor = await db.execute(
        "SELECT COUNT(*) FROM acl_entries"
        " WHERE resource = '/users' AND permission = 'search-users'"
    )
    if (await cursor.fetchone())[0] == 0:
        offset = 1_000_000
        await db.execute(
            "UPDATE acl_entries SET position = position + ?"
            " WHERE resource = '/users' AND position >= 1",
            (offset,),
        )
        await db.execute(
            "UPDATE acl_entries SET position = position - ? + 1"
            " WHERE resource = '/users' AND position > ?",
            (offset, offset),
        )
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            " group_id, system_principal, permission)"
            " VALUES ('/users', 1, ?, ?, NULL, NULL, ?, 'search-users')",
            (ACTION_ALLOW, PRINCIPAL_SYSTEM, SYSTEM_AUTHENTICATED),
        )


migration = Migration(
    id=23,
    name="0023_self_service_resources",
    apply=apply,
)
