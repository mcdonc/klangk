"""Migration 0013: grant ``exec-and-sync`` to existing role groups.

The one-shot exec channel is now gated on the dedicated ``exec-and-sync``
permission (#2706/#2712) — enforced in ``ExecController.start`` for
every exec session, which is also what ``klangk sync`` rides on (its
rsync transport is ``klangk exec --raw``), so both sync directions are
covered by the same gate. New workspaces seed the permission into their
``coders``/``collaborators`` role groups via
``_ROLE_GROUP_PERMISSIONS``; this migration backfills existing
workspaces so the upgrade does not silently break ``klangk exec`` /
``klangk sync`` / ``klangk sandbox`` for members who already had it
(preserving prior behavior: revoking ``exec-and-sync`` is an admin
choice, not
the new default).

For every ``coders-``/``collaborators-<workspace_id>`` role group, an
``Allow`` ACE for ``exec-and-sync`` is appended at the end of that resource's
ACL (max position + 1) unless one already exists. Owners need nothing
(their seeded ACE is the ``*`` wildcard); spectators never had exec.
Admins who granted ``code-in-isolation`` to other principals via custom
ACEs must add ``exec-and-sync`` explicitly to preserve one-shot exec
for them —
terminals are unaffected.
"""

import re

from klangk.model.acl import ACTION_ALLOW, PRINCIPAL_GROUP
from klangk.model.migrations.base import Migration

_SYNC_ROLES = ("coders", "collaborators")
_ROLE_GROUP_RE = re.compile(r"^(%s)-(.+)$" % "|".join(_SYNC_ROLES))


async def apply(db) -> None:
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
            " AND permission = 'exec-and-sync' LIMIT 1",
            (resource, name),
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
            "  'exec-and-sync')",
            (resource, pos, ACTION_ALLOW, PRINCIPAL_GROUP, name),
        )


migration = Migration(13, "0013_exec_and_sync_permission", apply)
