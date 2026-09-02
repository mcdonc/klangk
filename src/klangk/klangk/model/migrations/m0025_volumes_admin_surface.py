"""Migration 0025: /volumes becomes an admin surface (#2974).

The #2946 seed granted ``manage-volumes`` to every authenticated user —
a ``manage-*`` name on the whole deployment's volume inventory, which
contradicts the #2944 convention (``manage-*`` = admin surface, admins
by default, delegable). #2974 splits the endpoint gates:

- ``GET /api/v1/volumes``      → ``view-volumes``  (the admin tab's read)
- ``POST /api/v1/volumes``     → ``manage-volumes``
- ``DELETE /api/v1/volumes/…`` → ``manage-volumes``

and re-seeds ``/volumes`` to the admins group, view + manage. The
endpoint-level per-user ownership check on DELETE is dropped for
the same reason: a ``manage-volumes`` holder administers every
managed volume (the label survives for provenance; the
``ensure_volumes`` mount-time validation at container assembly is
unaffected, #2974).

This migration rewrites existing deployments to the new shape:

- Deletes rows on ``/volumes`` that match the retired seed exactly
  (Allow ``manage-volumes`` Authenticated, Deny ``*`` Everyone) — they
  are the defaults this change replaces, not operator intent. This
  runs even when the deployment has no ``admins`` group: the
  over-broad Allow Authenticated grant must not survive an upgrade
  just because the grantee group is missing.
- Inserts Allow ``view-volumes`` + Allow ``manage-volumes`` for the
  admins group unless a row already *allows* that permission on
  ``/volumes`` (idempotent; operator-staged Allow rows win — a staged
  Deny does not suppress the insert, so admins keep the surface).
- Custom rows the operator added (anything that is not the retired
  seed pair) are left untouched.

A fresh database (entirely empty ``acl_entries``) is a no-op — the boot
seeds own it (the m0021 precedent; inserting here would trip the seed's
empty-table gate and lose the ``/``, ``/workspaces``, ``/admin`` rows).

The admins group is located by name (``admins``; migration 0020
renamed legacy ``admin`` rows), matching the seeds' shape.
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

# (position, action, principal_type, system_principal, permission) of the
# retired #2946 seed pair on /volumes.
_RETIRED_SEED = {
    (
        0,
        ACTION_ALLOW,
        PRINCIPAL_SYSTEM,
        SYSTEM_AUTHENTICATED,
        "manage-volumes",
    ),
    (1, ACTION_DENY, PRINCIPAL_SYSTEM, SYSTEM_EVERYONE, "*"),
}

_ADMIN_PERMISSIONS = ("view-volumes", "manage-volumes")


async def _admins_group_id(db) -> str | None:
    cursor = await db.execute(
        "SELECT id FROM groups WHERE name = 'admins' LIMIT 1"
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def _rows(db):
    cursor = await db.execute(
        "SELECT position, action, principal_type, system_principal,"
        " permission, rowid FROM acl_entries WHERE resource = '/volumes'"
        " ORDER BY position"
    )
    return list(await cursor.fetchall())


async def _delete_retired_seed_rows(db, rows) -> None:
    for row in rows:
        key = (row[0], row[1], row[2], row[3], row[4])
        if key in _RETIRED_SEED:
            await db.execute(
                "DELETE FROM acl_entries WHERE rowid = ?", (row[5],)
            )


async def _permission_allowed(db, permission: str) -> bool:
    # Allow rows only: a staged Deny must not suppress the admins'
    # Allow insert (first-match-wins still lets the operator's Deny
    # take effect for the principals it covers).
    cursor = await db.execute(
        "SELECT COUNT(*) FROM acl_entries"
        " WHERE resource = '/volumes' AND permission = ?"
        " AND action = ?",
        (permission, ACTION_ALLOW),
    )
    return (await cursor.fetchone())[0] > 0


async def _next_position(db) -> int:
    cursor = await db.execute(
        "SELECT COALESCE(MAX(position), -1) FROM acl_entries"
        " WHERE resource = '/volumes'"
    )
    return (await cursor.fetchone())[0] + 1


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it

    rows = await _rows(db)
    await _delete_retired_seed_rows(db, rows)

    group_id = await _admins_group_id(db)
    if group_id is None:
        # Nothing to grant — but the retired rows are already gone, so
        # /volumes is locked rather than left world-manageable.
        return

    for permission in _ADMIN_PERMISSIONS:
        if await _permission_allowed(db, permission):
            continue  # operator-staged or already migrated
        # Append rather than assume 0/1: a deployment whose retired
        # pair was only half-customized keeps rows at those positions,
        # and a fixed insert would violate UNIQUE(resource, position)
        # (failed migration = boot loop).
        position = await _next_position(db)
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            " group_id, system_principal, permission)"
            " VALUES ('/volumes', ?, ?, ?, NULL, ?, NULL, ?)",
            (position, ACTION_ALLOW, PRINCIPAL_GROUP, group_id, permission),
        )


migration = Migration(
    id=25,
    name="0025_volumes_admin_surface",
    apply=apply,
)
