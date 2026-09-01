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
  mirroring the seed layout.
- ``/groups`` carries a well-known legacy row on every deployment
  first-booted before #2943: migration 0014 rewrote the seed to a
  single ``Allow create`` row for the admin group. That row matches no
  ``manage-groups`` check, so leaving it would lock admins out of
  group management — the exact failure this migration exists to
  prevent. When ``/groups`` holds **exactly** that legacy shape it is
  replaced with the standard pair (the m0014 precedent). Any other
  non-empty shape is an operator customization: skipped untouched,
  with the manual step documented in the changelog.
- A fresh database (entirely empty ``acl_entries``) is a no-op — the
  boot seeds own it; inserting here would trip the seed's
  empty-table gate and lose the ``/``, ``/workspaces``, and ``/admin``
  rows.
- Idempotent by construction: the per-resource existence check no-ops
  on re-run.
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


def _is_legacy_groups_shape(rows) -> bool:
    """Whether /groups holds exactly the m0014-rewritten seed: a single
    ``Allow create`` row for a group principal at position 0."""
    return bool(
        len(rows) == 1
        and rows[0][0] == 0  # position
        and rows[0][1] == ACTION_ALLOW
        and rows[0][2] == PRINCIPAL_GROUP
        and rows[0][3] is None  # user_id
        and rows[0][4] is not None  # group_id
        and rows[0][5] is None  # system_principal
        and rows[0][6] == "create"
    )


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
            "SELECT position, action, principal_type, user_id, group_id,"
            " system_principal, permission FROM acl_entries"
            " WHERE resource = ?",
            (resource,),
        )
        rows = list(await cursor.fetchall())
        if rows:
            if resource == "/groups" and _is_legacy_groups_shape(rows):
                # The well-known m0014 seed: replace it — nothing checks
                # `create` on /groups anymore, so leaving it would lock
                # admins out of group management (#2945 review).
                await db.execute(
                    "DELETE FROM acl_entries WHERE resource = ?",
                    (resource,),
                )
            else:
                # Operator pre-staged/customized rows; don't disturb
                # them (manual step documented in the changelog).
                continue
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
