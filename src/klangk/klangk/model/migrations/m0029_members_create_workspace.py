"""Migration 0029: grant ``create-workspace`` to the members group (#3137).

The seed used to grant ``create-workspace`` on ``/workspaces`` to the
admins group only (#2569); a deployer who wanted self-service
workspaces had to grant it to ``members`` by hand in the ACL editor.
#3137 flips the default: every member can create workspaces on a stock
deploy — bounded by the admission/quota controls (memory margin,
per-user running-workspace and volume quotas) and by
``allowed_images`` / ``allowed_mount_roots`` / egress filtering. A
deploy that wants the old admin-only posture stages an explicit Deny
on ``/workspaces`` ahead of the grant (ordered first-match-wins).

This migration brings existing deployments to the new seed shape: an
Allow ``create-workspace`` row for the ``members`` group is **appended**
after any existing rows on ``/workspaces``. Appending — never inserting
at a seed position — keeps every operator-staged row ahead of the new
grant: an explicit Deny (the recommended old-posture recipe) keeps
answering first, so no deployment's posture silently loosens beyond
what the seed itself granted. On the stock shape (Allow admins @0,
nothing else) the append lands at position 1 — exactly the fresh-seed
layout.

The ``members`` group is created when missing (a pre-#2569 database
that never rebooted on a #2569+ build): the boot's
``ensure_members_group`` would create it moments later anyway, but the
ACL row needs a group id now. Shape matches ``ensure_members_group``
(name ``members``, description ``All regular users``, default source).

Edge shape: a database whose ``/workspaces`` rows were deleted
entirely (a deliberate deny-all posture) gets the Allow at position 0
as the resource's only row — members gain creation while admins not
in ``members`` do not. Defensible outcome of the deliberate flip on a
pathological shape; stage a Deny to restore any stricter posture.

A fresh database (entirely empty ``acl_entries``) is a no-op — the
boot seeds own it (the m0021/m0023 precedent).

Idempotent by construction: the insert is guarded by an existence
check for an identical row (Allow, group:members,
``create-workspace`` on ``/workspaces``), so a re-run inserts nothing
— including on deployments where the operator already granted the
permission by hand.
"""

import uuid

from klangk.model.acl import ACTION_ALLOW, PRINCIPAL_GROUP
from klangk.model.migrations.base import Migration

MEMBERS_GROUP_NAME = "members"
MEMBERS_GROUP_DESCRIPTION = "All regular users"
RESOURCE = "/workspaces"
PERMISSION = "create-workspace"


async def _members_group_id(db) -> str:
    """The ``members`` group's id, creating the group when missing."""
    cursor = await db.execute(
        "SELECT id FROM groups WHERE name = ?", (MEMBERS_GROUP_NAME,)
    )
    row = await cursor.fetchone()
    if row is not None:
        return row[0]
    gid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO groups (id, name, description) VALUES (?, ?, ?)",
        (gid, MEMBERS_GROUP_NAME, MEMBERS_GROUP_DESCRIPTION),
    )
    return gid


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it

    members_id = await _members_group_id(db)

    # Idempotency: an operator (or a re-run) already granted this.
    cursor = await db.execute(
        "SELECT 1 FROM acl_entries"
        " WHERE resource = ? AND permission = ?"
        " AND action = ? AND principal_type = ? AND group_id = ?",
        (RESOURCE, PERMISSION, ACTION_ALLOW, PRINCIPAL_GROUP, members_id),
    )
    if await cursor.fetchone() is not None:
        return

    # Append after the last row so operator-staged rows (an explicit
    # Deny, a scoped grant) keep first-match-wins priority.
    cursor = await db.execute(
        "SELECT COALESCE(MAX(position), -1) FROM acl_entries"
        " WHERE resource = ?",
        (RESOURCE,),
    )
    next_position = (await cursor.fetchone())[0] + 1
    await db.execute(
        "INSERT INTO acl_entries"
        " (resource, position, action, principal_type, user_id,"
        "  group_id, system_principal, permission)"
        " VALUES (?, ?, ?, ?, NULL, ?, NULL, ?)",
        (
            RESOURCE,
            next_position,
            ACTION_ALLOW,
            PRINCIPAL_GROUP,
            members_id,
            PERMISSION,
        ),
    )


migration = Migration(
    id=29,
    name="0029_members_create_workspace",
    apply=apply,
)
