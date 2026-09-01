"""Helpers shared by the frozen schema migrations.

Each helper consolidates a pattern two or more migrations repeat
verbatim (the migration-clone residuals of the #2904 jscpd scan,
consolidated during the #2845 ratchet). The SQL statements and their
ordering are exactly what the migrations shipped with — parameterized
only by the strings the migrations differ in — so the upgrade outcome
for a deployed database is unchanged.
"""

import re

from klangk.model.acl import ACTION_ALLOW, PRINCIPAL_GROUP

# The operating role groups whose per-workspace grants get backfilled
# (owners hold the ``*`` wildcard; spectators never had these channels).
_ROLE_GROUP_RE = re.compile(r"^(coders|collaborators)-(.+)$")


def _mirror_row(row: tuple, position: int, target: str) -> tuple:
    """Build an INSERT tuple mirroring *row* (minus id) for *target*.

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
        target,
    )


async def _resequence_with_mirrors(db, rows, source: str, target: str) -> None:
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
        if row[3] == ACTION_ALLOW and row[8] == source:  # action, permission
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  user_id, group_id, system_principal, permission)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _mirror_row(row, position, target),
            )
            position += 1


async def mirror_permission_aces(db, source: str, target: str) -> None:
    """Mirror every Allow ``source`` ACE as an Allow ``target`` ACE for
    the same principal at the same resource, at the adjacent position.

    ``acl_entries`` has ``UNIQUE(resource, position)`` and SQLite
    enforces it per-statement (no deferring), so re-sequencing a
    resource's positions can transiently collide: each resource's rows
    are first parked at unique negative positions, then the final
    sequence (with the mirrors interleaved) is written back (m0011's
    rationale). Adjacency, not appending, is load-bearing: a ``*`` ACE
    matches every check, so a mirror appended after a later ``Deny *``
    would be shadowed.
    """
    cursor = await db.execute(
        "SELECT DISTINCT resource FROM acl_entries"
        " WHERE action = ? AND permission = ?",
        (ACTION_ALLOW, source),
    )
    resources = [row[0] for row in await cursor.fetchall()]
    for resource in resources:
        cursor = await db.execute(
            "SELECT id, resource, position, action, principal_type,"
            " user_id, group_id, system_principal, permission"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        await _resequence_with_mirrors(
            db, await cursor.fetchall(), source, target
        )


async def grant_role_group_permission(db, permission: str) -> None:
    """Append an Allow *permission* ACE for every ``coders-``/
    ``collaborators-<workspace_id>`` role group's workspace resource
    (max position + 1) unless one already exists.

    The match is by role-group name + ``workspace-role`` source marker,
    not by ACEs: a coders/collaborators group an admin deliberately
    stripped still gains the permission (the backfill covers the role
    groups of every existing workspace; stripping the grant afterwards
    is then an owner edit).
    """
    cursor = await db.execute(
        "SELECT name FROM groups WHERE source = 'workspace-role'"
    )
    role_groups = [row[0] for row in await cursor.fetchall()]
    for name in role_groups:
        m = _ROLE_GROUP_RE.match(name)
        if m is None:
            continue  # owners/spectators (and unknown suffixes) untouched
        ws_id = m.group(2)
        resource = f"/workspaces/{ws_id}"
        existing = await db.execute(
            "SELECT 1 FROM acl_entries"
            " WHERE resource = ? AND group_id = ("
            "   SELECT id FROM groups WHERE name = ?)"
            " AND permission = ? LIMIT 1",
            (resource, name, permission),
        )
        if await existing.fetchone() is not None:
            continue
        max_pos = await db.execute(
            "SELECT COALESCE(MAX(position), -1) FROM acl_entries"
            " WHERE resource = ?",
            (resource,),
        )
        pos = (await max_pos.fetchone())[0] + 1
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            "  user_id, group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, NULL,"
            "  (SELECT id FROM groups WHERE name = ?), NULL,"
            "  ?)",
            (resource, pos, ACTION_ALLOW, PRINCIPAL_GROUP, name, permission),
        )
