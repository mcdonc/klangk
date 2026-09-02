"""Migration 0024: grant ``join-workspace`` alongside stored ``terminal``.

The ``workspace_connect`` handshake — the gate for opening a workspace
at all — now checks ``join-workspace`` instead of ``terminal`` (#2975):
``terminal`` becomes the Terminal-tab visibility signal the frontend
reads from my-permissions, and the connect gate gets a self-describing
name. Stored ACEs carry no ``join-workspace`` rows; without this
migration every existing grant falls short of the new gate and members
lock themselves out of their own workspaces the moment this code boots.

Answer preservation, not just grant copying — the ACL walk
(:func:`klangk.acl.check_permission`) evaluates first-match-wins over
the resource AND its ancestors (``/workspaces/{id}`` → ``/workspaces``
→ ``/``), for Allow and Deny rows alike. So this migration copies every
``terminal`` row — Allow AND Deny — on ANY resource (not just the
workspace GLOB): an Allow ``terminal`` on the collection ``/workspaces``
answered the old gate through the ancestor walk, and a Deny
``terminal`` above a grant blocked it; both must keep answering the new
gate identically (the m0022 rename precedent carried Deny rows along
the same way). Each sibling ``join-workspace`` row is inserted directly
AFTER its source row — positions at and above the insertion point shift
up — so every principal's first matching row answers with the same
action it did before the swap.

This is a copy, not a rename — the ``terminal`` rows stay untouched, so
custom ACLs and scripts that grant/check ``terminal`` keep working. The
seed and both share flows (member, group) grant ``join-workspace``
alongside ``terminal`` for fresh rows.

Idempotent by construction: a source row whose identical
``join-workspace`` sibling (same resource, principal, and action)
already exists is skipped, so a re-run inserts nothing.
"""

from klangk.model.migrations.base import Migration


async def _shift_up(db, resource: str, from_pos: int) -> None:
    """Bump every row at/from ``from_pos`` up by one — highest first.

    Single-row UPDATEs in descending order can never transiently violate
    UNIQUE(resource, position); one bulk UPDATE could (its row
    evaluation order is undefined).
    """
    cursor = await db.execute(
        "SELECT position FROM acl_entries"
        " WHERE resource = ? AND position >= ? ORDER BY position DESC",
        (resource, from_pos),
    )
    for (pos,) in await cursor.fetchall():
        await db.execute(
            "UPDATE acl_entries SET position = position + 1"
            " WHERE resource = ? AND position = ?",
            (resource, pos),
        )


async def apply(db) -> None:
    # Every terminal row on any resource, highest position first within
    # each resource: inserting a sibling shifts only rows ABOVE the
    # source, so sources at lower positions are still where the cursor
    # expects them when their turn comes.
    cursor = await db.execute(
        "SELECT resource, position, action, principal_type, user_id,"
        " group_id, system_principal FROM acl_entries"
        " WHERE permission = 'terminal'"
        " ORDER BY resource, position DESC"
    )
    sources = await cursor.fetchall()
    for (
        resource,
        position,
        action,
        principal_type,
        user_id,
        group_id,
        system_principal,
    ) in sources:
        # Idempotency: an identical join-workspace sibling (same
        # principal and action on the same resource) means this source
        # was already copied.
        covered = await db.execute(
            "SELECT 1 FROM acl_entries"
            " WHERE resource = ? AND permission = 'join-workspace'"
            " AND action = ? AND principal_type = ?"
            " AND user_id IS ? AND group_id IS ? AND system_principal IS ?"
            " LIMIT 1",
            (
                resource,
                action,
                principal_type,
                user_id,
                group_id,
                system_principal,
            ),
        )
        if await covered.fetchone() is not None:
            continue
        await _shift_up(db, resource, position + 1)
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            "  user_id, group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'join-workspace')",
            (
                resource,
                position + 1,
                action,
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
