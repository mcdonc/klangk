"""Migration 0024: grant ``join-workspace`` to existing terminal holders.

The ``workspace_connect`` handshake — the gate for opening a workspace
at all — now checks ``join-workspace`` instead of ``terminal`` (#2975):
``terminal`` becomes the Terminal-tab visibility signal the frontend
reads from my-permissions, and the connect gate gets a self-describing
name. Stored ACEs carry no ``join-workspace`` rows; without this
migration every existing workspace's grants (including the per-workspace
role groups seeded at creation time, and direct user/group shares) fall
short of the new gate and members lock themselves out of their own
workspaces the moment this code boots.

Scope: every ``Allow`` ACE for ``terminal`` on a workspace resource
(GLOB ``/workspaces/?*``) gains a sibling ``Allow`` ACE for
``join-workspace`` appended at the end of that resource's ACL (max
position + 1) unless the principal already holds ``join-workspace`` or
the ``*`` wildcard on that resource (owners need nothing). This is a
copy, not a rename — the ``terminal`` rows stay untouched, so custom
ACLs and scripts that grant/check ``terminal`` keep working, and every
grant path that ever carried ``terminal`` is covered (role groups and
direct user/group shares alike). The seed and both share flows
(member, group) grant ``join-workspace`` alongside ``terminal`` for
fresh rows; this migration backfills existing deployments so the
upgrade does not silently stop anyone who could already connect.

Idempotent by construction: the already-covered check matches on a
re-run, so a second apply inserts nothing.
"""

from klangk.model.acl import ACTION_ALLOW
from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    # Distinct principals holding an Allow terminal ACE on a workspace.
    cursor = await db.execute(
        "SELECT DISTINCT resource, principal_type, user_id, group_id,"
        " system_principal FROM acl_entries"
        " WHERE action = ? AND permission = 'terminal'"
        "   AND resource GLOB '/workspaces/?*'",
        (ACTION_ALLOW,),
    )
    holders = await cursor.fetchall()
    for (
        resource,
        principal_type,
        user_id,
        group_id,
        system_principal,
    ) in holders:
        # Already covered: an existing join-workspace (or wildcard)
        # Allow ACE for the same principal on the same resource.
        covered = await db.execute(
            "SELECT 1 FROM acl_entries"
            " WHERE resource = ? AND action = ? AND principal_type = ?"
            " AND user_id IS ? AND group_id IS ? AND system_principal IS ?"
            " AND permission IN ('join-workspace', '*') LIMIT 1",
            (
                resource,
                ACTION_ALLOW,
                principal_type,
                user_id,
                group_id,
                system_principal,
            ),
        )
        if await covered.fetchone() is not None:
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
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'join-workspace')",
            (
                resource,
                pos,
                ACTION_ALLOW,
                principal_type,
                user_id,
                group_id,
                system_principal,
            ),
        )


migration = Migration(
    id=24,
    name="0024_join_workspace_permission",
    apply=apply,
)
