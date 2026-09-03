"""Migration 0026: /volumes becomes an admin-surface resource (#2993).

(Renumbered from 0025 during rebase: the sibling /images PR, #2996,
took id 25 on main — migration ids are contiguous and frozen once
shipped.)

The #2946 seed granted ``manage-volumes`` to every authenticated user
with a trailing Deny ``*`` Everyone row. #2993 splits the surface per
endpoint — ``GET /volumes`` checks ``view-volumes``, ``POST``/``DELETE``
keep ``manage-volumes`` — both granted to the admins group only, like
every other admin tab's first-class resource.

This migration rewrites a deployed database to the new seed shape:

- The two #2946 rows are removed: the Allow-Authenticated row (a
  ``manage-*`` name on a grant every authenticated user holds
  contradicts the #2944 convention) and the Deny ``*`` Everyone row
  (dead weight — the JWT middleware rejects unauthenticated requests
  before any ACL check, and default-deny covers the no-match case).
- Allow ``view-volumes`` + Allow ``manage-volumes`` rows for the admins
  group are inserted at positions 0/1 (the fresh-seed layout), with any
  surviving rows shifted down so first-match-wins order keeps the
  admins' grants ahead of operator rows (an operator-staged Deny must
  not shadow what the old Allow-Authenticated row gave admins).

Operator-customized rows that do not match the #2946 shapes (a
scoped self-service grant, an explicit Deny) survive untouched, below
the inserted admin rows. An operator row that happens to repeat a
#2946 shape exactly (e.g. a deliberately re-staged Allow
manage-volumes for Authenticated) is indistinguishable from the seed
and goes with it. A deploy that wants self-service volumes back adds
an Allow row via the ACL editor.

A fresh database (entirely empty ``acl_entries``) is a no-op — the
boot seeds own it (the m0021 precedent).

Idempotent by construction: the deletes match only the exact #2946
shapes, and the admin-row insert is guarded by an existence check.
"""

from klangk.model.acl import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
from klangk.model.migrations.base import Migration

# The exact #2946 seed rows on /volumes: (action, principal_type,
# system_principal, permission).
OLD_SEED_ROWS = (
    (ACTION_ALLOW, PRINCIPAL_SYSTEM, SYSTEM_AUTHENTICATED, "manage-volumes"),
    (ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE, "*"),
)

# The #2993 admin rows, in seed-position order.
ADMIN_PERMISSIONS = ("view-volumes", "manage-volumes")


async def _delete_old_seed_rows(db) -> None:
    """Remove the exact #2946 seed shapes on /volumes."""
    for action, principal_type, system_principal, permission in OLD_SEED_ROWS:
        await db.execute(
            "DELETE FROM acl_entries WHERE resource = '/volumes'"
            " AND action = ? AND principal_type = ?"
            " AND system_principal = ? AND permission = ?",
            (action, principal_type, system_principal, permission),
        )


async def _admins_group_id(db) -> int | None:
    """The ``admins`` group's id, or None when absent (m0021 posture)."""
    cursor = await db.execute("SELECT id FROM groups WHERE name = 'admins'")
    row = await cursor.fetchone()
    return None if row is None else row[0]


async def _missing_admin_permissions(db, admins_id: int) -> list[str]:
    """Seed-position-ordered admin permissions the group lacks."""
    cursor = await db.execute(
        "SELECT permission FROM acl_entries WHERE resource = '/volumes'"
        " AND action = ? AND principal_type = ? AND group_id = ?",
        (ACTION_ALLOW, PRINCIPAL_GROUP, admins_id),
    )
    held = {r[0] for r in await cursor.fetchall()}
    return [p for p in ADMIN_PERMISSIONS if p not in held]


async def _insert_admin_rows(db, admins_id: int, missing: list[str]) -> None:
    """Insert the admin rows at seed positions 0/1, parking survivors."""
    # Park the surviving rows two slots down so positions 0/1 are free.
    # UNIQUE(resource, position) forbids an in-place +2 shift past a
    # neighbor, so rows first jump far above the occupied range and
    # then settle back (m0023's two-step offset). Every parked row is
    # >= offset, so the settle matches >= offset — a strict > would
    # strand the row parked at exactly offset (the original position
    # 0) up there forever.
    offset = 1_000_000
    await db.execute(
        "UPDATE acl_entries SET position = position + ?"
        " WHERE resource = '/volumes'",
        (offset,),
    )
    await db.execute(
        "UPDATE acl_entries SET position = position - ? + 2"
        " WHERE resource = '/volumes' AND position >= ?",
        (offset, offset),
    )
    for permission in missing:
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES ('/volumes', ?, ?, ?, NULL, ?, NULL, ?)",
            (
                ADMIN_PERMISSIONS.index(permission),
                ACTION_ALLOW,
                PRINCIPAL_GROUP,
                admins_id,
                permission,
            ),
        )


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it

    await _delete_old_seed_rows(db)

    admins_id = await _admins_group_id(db)
    if admins_id is None:
        return  # no admins group: nothing to grant

    missing = await _missing_admin_permissions(db, admins_id)
    if missing:
        await _insert_admin_rows(db, admins_id, missing)


migration = Migration(
    id=26,
    name="0026_volumes_admin_surface",
    apply=apply,
)
