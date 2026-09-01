"""Migration 0021: seed the first-class resource ACLs (#2944).

Every governed surface moved off the ``/admin`` subtree onto first-class
top-level resources — ``/users``, ``/groups``, ``/invitations``,
``/server``, ``/events``, ``/acl`` — each checked against its flat
``manage-*`` permission. The ACL walk from those resources goes up to
``/`` and never passes through ``/admin``, so an existing deployment's
``/admin`` Allow ``*`` wildcard (admins) cannot satisfy the moved
checks: without new rows, **admins lock themselves out** of users,
groups, invitations, server scheduling, events, and the ACL editor the
moment this code boots.

This migration inserts, for each resource, the two rows a fresh install
seeds (``seed_default_acls``): Allow ``manage-*`` for the admins group,
Deny ``*`` for Everyone. It must ship in the same release as the
endpoint moves.

Details:

- The admins group is located by name (``admins``; migration 0020
  renamed legacy ``admin`` rows), matching the seeds' shape.
- The old ``/admin`` rows are left in place untouched: they still serve
  as the instance-administrator wildcard marker (``isAdmin`` in the
  frontend reads ``*`` on ``/admin`` via ``/my-permissions``).
- Positions: each inserted resource gets Allow at 0, Deny at 1,
  mirroring the seed layout. A resource that already has rows (an
  operator pre-staged grants) is skipped entirely — never half-merged
  onto unknown positions.
- Idempotent by construction: the per-resource existence check no-ops
  on re-run, and fresh DBs get the rows from ``seed_default_acls``
  (this migration still inserts them harmlessly before that seeding —
  the seed itself is gated on an empty table and skips).
"""

from klangk.model.acl import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_EVERYONE,
)
from klangk.model.migrations.base import Migration

# resource -> the permission admins get on it
RESOURCES = {
    "/users": "manage-users",
    "/groups": "manage-groups",
    "/invitations": "manage-invitations",
    "/server": "manage-server-schedule",
    "/events": "manage-events",
    "/acl": "manage-acls",
}


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        # Fresh database: the boot seeds (seed_default_acls) own this —
        # inserting here would make the seed gate (empty-table check)
        # skip and lose the /, /workspaces, and /admin rows.
        return

    cursor = await db.execute("SELECT id FROM groups WHERE name = 'admins'")
    row = await cursor.fetchone()
    admins_id = row[0] if row is not None else None

    for resource, permission in RESOURCES.items():
        cursor = await db.execute(
            "SELECT COUNT(*) FROM acl_entries WHERE resource = ?",
            (resource,),
        )
        if (await cursor.fetchone())[0] > 0:
            continue  # operator pre-staged rows here; don't disturb them
        if admins_id is not None:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  group_id, system_principal, permission)"
                " VALUES (?, 0, ?, ?, NULL, ?, NULL, ?)",
                (
                    resource,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    admins_id,
                    permission,
                ),
            )
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES (?, 1, ?, ?, NULL, NULL, ?, '*')",
            (resource, ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE),
        )


migration = Migration(21, "0021_first_class_resource_acls", apply)
