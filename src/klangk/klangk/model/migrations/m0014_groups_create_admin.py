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

The replacement inserts an Allow ``create`` ACE for the ``admin``
group at position 0, so a migrated deployment's ``/groups`` ACL matches a
freshly seeded one. If no ``admin`` group row exists, the seeded ACE is
simply deleted with no replacement: admins manage groups through
``/admin/groups`` regardless. That state is reachable without anything
pathological — the admin group can be renamed (``update_group`` has no
admin guard), and this migration runs before ``ensure_admin_group``
recreates the row — so the branch is a deliberate fallback, not a
should-never-happen.
"""

from klangk.model.acl import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
)
from klangk.model.migrations.base import Migration

_RESOURCE = "/groups"


def _is_seeded_shape(rows) -> bool:
    """Whether the /groups ACL still holds exactly the single
    system-authenticated create entry the seed wrote."""
    if len(rows) != 1:
        return False
    row = rows[0]
    # Integer-indexed compare: rows may be the db.Row wrapper (no slice
    # support) on the production connection path.
    return (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
    ) == (
        0,  # position
        ACTION_ALLOW,
        PRINCIPAL_SYSTEM,
        None,  # user_id
        None,  # group_id
        SYSTEM_AUTHENTICATED,
        "create",
    )


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
    if not _is_seeded_shape(rows):
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
