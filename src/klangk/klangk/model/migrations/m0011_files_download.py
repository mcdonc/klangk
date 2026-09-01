"""Migration 0011: ``files-download`` permission (#2705).

``GET /files/download`` previously required only the ``files``
permission, so anyone who could browse/read in the viewer could also
pull raw bytes out of the container — an exfil avenue for members who
should only ever view files in-app. The route now requires the new
``files-download`` permission **in addition to** ``files`` (and ``*``
still matches both).

New grants (role-group seeding, member/group share endpoints) include
``files-download`` alongside ``files``. This migration preserves the
behavior of existing deployments: every ``Allow`` ACE with permission
``files`` gets a mirrored ``Allow`` ``files-download`` ACE for the same
principal at the same resource, inserted at the adjacent position.

Why adjacent (not appended): ACEs evaluate in position order and a
``*`` ACE matches every permission check. If a ``Deny *`` sits *after*
an ``Allow files`` entry, the ``files`` check passes but an
appended-at-the-end ``files-download`` mirror would be shadowed by the
deny — silently breaking downloads the operator never touched. The
mirror takes the very next position so it answers every check exactly
as its source ``files`` entry did.

Renumbering: ``acl_entries`` has ``UNIQUE(resource, position)`` and
SQLite enforces it per-statement (no deferring), so re-sequencing a
resource's positions can transiently collide. Existing rows are first
parked at unique negative positions, then the final sequence (with the
mirrors interleaved) is written back. The whole migration runs inside
the runner's ``BEGIN IMMEDIATE`` transaction.
"""

from klangk.model.migrations.base import Migration

_FILES = "files"
_FILES_DOWNLOAD = "files-download"
_ALLOW = 1


def _mirror_row(row: tuple, position: int) -> tuple:
    """Build an INSERT tuple mirroring *row* (minus id) for the new perm.

    ``row`` is ``(id, resource, position, action, principal_type,
    user_id, group_id, system_principal, permission)``.
    """
    return (
        row[1],  # resource
        position,
        row[3],  # action (Allow)
        row[4],  # principal_type
        row[5],  # user_id
        row[6],  # group_id
        row[7],  # system_principal
        _FILES_DOWNLOAD,
    )


async def apply(db) -> None:
    cursor = await db.execute(
        "SELECT DISTINCT resource FROM acl_entries"
        " WHERE action = ? AND permission = ?",
        (_ALLOW, _FILES),
    )
    resources = [row[0] for row in await cursor.fetchall()]
    for resource in resources:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        await _resequence_with_mirrors(db, await cursor.fetchall())


async def _resequence_with_mirrors(db, rows) -> None:
    """Park existing rows at unique negative positions (ids are unique,
    so -1 - id never collides), then rewrite the sequence inserting each
    mirror directly after its source entry so evaluation order vs. `*`
    ACEs is kept."""
    for row in rows:
        await db.execute(
            "UPDATE acl_entries SET position = ? WHERE id = ?",
            (-1 - row[0], row[0]),
        )
    position = 0
    for row in rows:
        await db.execute(
            "UPDATE acl_entries SET position = ? WHERE id = ?",
            (position, row[0]),
        )
        position += 1
        if row[3] == _ALLOW and row[8] == _FILES:  # action, permission
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  user_id, group_id, system_principal, permission)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _mirror_row(row, position),
            )
            position += 1


migration = Migration(11, "0011_files_download", apply)
