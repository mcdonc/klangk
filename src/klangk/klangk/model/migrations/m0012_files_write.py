"""Migration 0012: ``files-write`` permission.

Companion to 0011 (#2705): the mutating files routes —
``POST /files/upload``, ``POST /files/rename``, ``DELETE /files`` — now
require the new ``files-write`` permission **in addition to** ``files``.
Upload and download are both raw byte-transfer channels, so by default
the same principals keep both: new grants (role-group seeding,
member/group share endpoints) include ``files-write`` alongside
``files``/``files-download``.

This migration mirrors every **Allow** ``files-download`` ACE as an Allow
``files-write`` ACE for the same principal at the same resource, at the
adjacent position. Mirroring ``files-download`` (not ``files``) is
deliberate: an operator who deleted a ``files-download`` mirror after 0011
— deliberately withholding download from some member — does not silently
regain the write channel. On an upgrade straight from a pre-0011
database, 0011 runs first and materializes the ``files-download`` rows
this migration copies, so the result is identical to mirroring ``files``.

Known asymmetry: the mirror works at the **ACE level**, not the
*effective-outcome* level. An operator who withheld download by inserting
a ``Deny files-download`` (rather than deleting the Allow mirror) keeps
upload: the Deny is not mirrored, and the Allow it shadows still is, so
effective ``files-download`` is False while ``files-write`` is True.
That is not a security regression — such a principal could upload before
the migration, via ``files`` — but it means "grants upload to every
principal holding download" holds for deletion-based withholding only.
Deny-based withholding withholds download alone; delete the Allow mirror
to withhold both.

Position/ordering rationale, parking trick, and failure semantics are the
same as 0011 — see ``m0011_files_download.py``; this module only changes
the source and target permission strings.
"""

from klangk.model.migrations.base import Migration

_SOURCE = "files-download"
_TARGET = "files-write"
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
        _TARGET,
    )


async def apply(db) -> None:
    cursor = await db.execute(
        "SELECT DISTINCT resource FROM acl_entries"
        " WHERE action = ? AND permission = ?",
        (_ALLOW, _SOURCE),
    )
    resources = [row[0] for row in await cursor.fetchall()]
    for resource in resources:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        rows = await cursor.fetchall()
        # Park existing rows at unique negative positions (ids are
        # unique, so -1 - id never collides) before re-sequencing.
        for row in rows:
            await db.execute(
                "UPDATE acl_entries SET position = ? WHERE id = ?",
                (-1 - row[0], row[0]),
            )
        # Rewrite the sequence, inserting each mirror directly after
        # its source entry so evaluation order vs. `*` ACEs is kept.
        position = 0
        for row in rows:
            await db.execute(
                "UPDATE acl_entries SET position = ? WHERE id = ?",
                (position, row[0]),
            )
            position += 1
            if row[3] == _ALLOW and row[8] == _SOURCE:  # action, permission
                await db.execute(
                    "INSERT INTO acl_entries"
                    " (resource, position, action, principal_type,"
                    "  user_id, group_id, system_principal, permission)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    _mirror_row(row, position),
                )
                position += 1


migration = Migration(12, "0012_files_write", apply)
