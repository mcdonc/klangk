"""Migration 0016: grant ``monitor`` to existing terminal holders (#2783).

Health/status reception is now gated on the dedicated ``monitor``
permission instead of ``terminal`` — enforced on the
``GET /workspaces/{id}/status`` endpoint and on the member-scoped
``container_status`` / ``service_health`` / ``workspace_evicted``
WebSocket fan-outs (#1714). New workspaces seed ``monitor`` into every
role group that has ``terminal`` via ``_ROLE_GROUP_PERMISSIONS``, and
the member-share flow grants it alongside ``terminal``; this migration
backfills existing deployments so the upgrade does not silently stop
status delivery to principals who already receive it (preserving prior
behavior: revoking ``monitor`` is an admin choice, not the new default).

For every workspace resource, each principal (user, group, or system)
holding an ``Allow`` ACE for ``terminal`` gets an ``Allow`` ACE for
``monitor`` appended at the end of that resource's ACL (max position +
1) unless it already has ``monitor`` or the ``*`` wildcard (owners need
nothing). Every grant path that ever carried ``terminal`` is covered —
role groups and direct user shares alike.
"""

from klangk.model.acl import ACTION_ALLOW
from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    # Distinct principals holding an Allow terminal ACE, on any resource.
    cursor = await db.execute(
        "SELECT DISTINCT resource, principal_type, user_id, group_id,"
        " system_principal FROM acl_entries"
        " WHERE action = ? AND permission = 'terminal'",
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
        # Already covered: an existing monitor (or wildcard) Allow ACE
        # for the same principal on the same resource.
        covered = await db.execute(
            "SELECT 1 FROM acl_entries"
            " WHERE resource = ? AND action = ? AND principal_type = ?"
            " AND user_id IS ? AND group_id IS ? AND system_principal IS ?"
            " AND permission IN ('monitor', '*') LIMIT 1",
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
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'monitor')",
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


migration = Migration(16, "0016_monitor_permission", apply)
