"""Migration 0014: tighten the seeded ``create`` ACE on ``/groups`` (#2770).

``seed_default_acls`` used to grant ``Allow /groups create →
system:authenticated``, so every logged-in user could create groups (and
received a full ``*`` ACE on the new group, letting them add/remove any
user as a member without that user's consent). Nothing in any client
(frontend, CLI, TUI, e2e) relies on that default — group management runs
through the admin-gated ``/admin/groups`` endpoints — and the documented
permission model says groups are managed by admins. The seed now grants
``create`` on ``/groups`` to the admin group instead, matching the
``/workspaces`` tightening (#2569).

Because ``seed_default_acls`` only runs on a database with **no** ACL
entries at all, deployments that already carry the open ACE keep it
across upgrades. This migration rewrites it to the tightened shape —
but only when the ``/groups`` entry set is **exactly** the single seeded
ACE (position 0, Allow, ``create``, system:authenticated). Any other
shape means an operator customized the resource (loosened deliberately,
added entries, reordered): the migration leaves it untouched, and the
changelog documents the manual tightening step for that case.

The replacement inserts an Allow ``create`` ACE for the ``admin`` group
at position 0, so a migrated deployment's ``/groups`` ACL matches a
freshly seeded one. If no ``admin`` group row exists (a pathological
state — ``ensure_admin_group`` recreates it at boot), the seeded ACE is
simply deleted with no replacement: admins manage groups through
``/admin/groups`` regardless.
"""

from klangk.model.acl import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
)
from klangk.model.migrations.base import Migration

_RESOURCE = "/groups"


async def apply(db) -> None:
    cursor = await db.execute(
        "SELECT position, action, principal_type, user_id, group_id,"
        " system_principal, permission FROM acl_entries"
        " WHERE resource = ?",
        (_RESOURCE,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return  # nothing seeded on /groups — fresh/pre-seed deployment
    seeded_shape = (
        len(rows) == 1
        and rows[0][0] == 0  # position
        and rows[0][1] == ACTION_ALLOW
        and rows[0][2] == PRINCIPAL_SYSTEM
        and rows[0][3] is None  # user_id
        and rows[0][4] is None  # group_id
        and rows[0][5] == SYSTEM_AUTHENTICATED
        and rows[0][6] == "create"
    )
    if not seeded_shape:
        return  # operator-customized — leave for the manual step
    await db.execute(
        "DELETE FROM acl_entries WHERE resource = ?", (_RESOURCE,)
    )
    cursor = await db.execute("SELECT id FROM groups WHERE name = 'admin'")
    row = await cursor.fetchone()
    if row is None:
        return
    await db.execute(
        "INSERT INTO acl_entries"
        " (resource, position, action, principal_type, user_id,"
        "  group_id, system_principal, permission)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _RESOURCE,
            0,
            ACTION_ALLOW,
            PRINCIPAL_GROUP,
            None,
            row[0],
            None,
            "create",
        ),
    )


migration = Migration(14, "0014_groups_create_admin", apply)
