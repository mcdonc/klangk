"""Migration 0017: grant ``change-acls`` to existing ``share`` holders (#2764).

Raw ACL editing moved from the ``share`` permission to the dedicated
``change-acls`` permission — enforced on ``GET``/``PUT
/workspaces/{id}/acl`` and (in addition to site ``admin``) on ``PUT
/admin/acl/resource`` when the target is an individual workspace. The
simple sharing surface (member and group shares) stays on ``share``;
role-group writes additionally require ``change-acls`` (they can mint
an owner): a member who can invite collaborators does not thereby gain
the power to rewrite the whole ACE list.

New workspaces need no seeding — owners hold ``change-acls`` through
their ``*`` wildcard ACE (the m0013 precedent), and no other role group
gets it by default. This migration backfills existing deployments so
the upgrade does not silently take raw ACL editing away from principals
who had it: every principal gets an ``Allow`` ACE for
``change-acls`` appended at the end of that resource's ACL (max
position + 1) unless it already has ``change-acls`` or the ``*``
wildcard. Principals whose ``Allow`` share never took effect — an
earlier-positioned ``Deny`` share (or wildcard) ACE shadows it under the
first-match-wins walk — are skipped: backfilling them would grant a new
power, not preserve one. Revoking it afterwards is an admin choice, not
the new default.
"""

from klangk.model.acl import ACTION_ALLOW
from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    # Distinct principals holding an Allow share ACE, on any resource.
    cursor = await db.execute(
        "SELECT DISTINCT resource, principal_type, user_id, group_id,"
        " system_principal FROM acl_entries"
        " WHERE action = ? AND permission = 'share'",
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
        # Skip principals whose share was already shadowed on this
        # resource by their own earlier ACE: the walk is first-match-wins,
        # so a Deny share (or wildcard) at a lower position means the
        # Allow row never took effect and there is no prior behavior to
        # preserve — backfilling them would GRANT a new power, not
        # preserve one.
        effective = await db.execute(
            "SELECT action FROM acl_entries"
            " WHERE resource = ? AND principal_type = ?"
            " AND user_id IS ? AND group_id IS ? AND system_principal IS ?"
            " AND permission IN ('share', '*')"
            " ORDER BY position LIMIT 1",
            (
                resource,
                principal_type,
                user_id,
                group_id,
                system_principal,
            ),
        )
        row = await effective.fetchone()
        if row is None or row[0] != ACTION_ALLOW:
            continue
        # Already covered: an existing change-acls (or wildcard) Allow
        # ACE for the same principal on the same resource.
        covered = await db.execute(
            "SELECT 1 FROM acl_entries"
            " WHERE resource = ? AND action = ? AND principal_type = ?"
            " AND user_id IS ? AND group_id IS ? AND system_principal IS ?"
            " AND permission IN ('change-acls', '*') LIMIT 1",
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
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'change-acls')",
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


migration = Migration(17, "0017_change_acls_permission", apply)
