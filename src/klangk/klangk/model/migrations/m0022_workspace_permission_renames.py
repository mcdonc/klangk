"""Migration 0022: rename workspace-sphere permissions (#2946).

The generic verb names retired by #2946 — ``create`` / ``edit`` /
``delete`` / ``monitor`` / ``export`` / ``share`` / ``change-acls`` /
``admin`` / ``files`` — are renamed to the specific, self-describing
names the API now checks. Stored ACEs carry the old names; without this
migration every existing workspace's grants (including the per-workspace
role groups seeded at creation time) silently stop matching and users
lock themselves out of their own workspaces the moment this code boots.

Scope rules (an old name maps differently by resource):

- On the collection resource ``/workspaces``: ``create`` checks the
  workspace-creation gate — renamed to ``create-workspace``.
- On a workspace resource ``/workspaces/{id}``:

  ==============  =======================
  old             new
  ==============  =======================
  ``create``      ``duplicate-workspace``
  ``edit``        ``edit-workspace``
  ``delete``      ``delete-workspace``
  ``monitor``     ``monitor-workspace``
  ``export``      ``export-workspace``
  ``share``       ``share-workspace``
  ``change-acls`` ``share-advanced``
  ``admin``       ``transfer-workspace``
  ``files``       ``files-view``
  ==============  =======================

Names left untouched: ``view``, ``terminal``, ``files-download``,
``files-write``, ``egress-consent``, ``code-in-isolation``,
``exec-and-sync``, ``spectate-on-shared-terminals``,
``code-in-shared-terminals``, ``share-terminals`` — still checked
under the same names.

Intentional behavior change (changelogged): lifecycle control is no
longer implied by ``terminal``. Role groups whose grant list included
``terminal`` could previously start/stop/restart the workspace; the
three new lifecycle permissions are granted by the *seed* only to
coders and collaborators — so this migration also inserts the trio
for every existing ``coders-*`` / ``collaborators-*`` role group
(identified by group name; the rename alone would silently strip
lifecycle control from every upgraded workspace's operating roles).
An operator who wants a spectator group to keep lifecycle control
adds ``start-workspace`` / ``stop-workspace`` / ``restart-workspace``
to it in the ACL editor after upgrading.

Rows that do not match a mapping (e.g. a legacy ``admin`` row someone
placed on ``/workspaces`` itself) are left untouched — the editor still
shows them; they simply match no check.

Idempotent by construction: plain UPDATEs keyed on the old names; a
re-run matches nothing. A fresh database is a no-op (empty table), and
unlike m0021 there is no INSERT, so the boot seed's empty-table gate is
never involved.
"""

from klangk.model.acl import ACTION_ALLOW, GROUP_SOURCE_WORKSPACE_ROLE
from klangk.model.migrations.base import Migration

# resource-exact renames (collection)
COLLECTION_RENAMES = {"/workspaces": {"create": "create-workspace"}}

# per-workspace renames (resource GLOB '/workspaces/?*')
WORKSPACE_RENAMES = {
    "create": "duplicate-workspace",
    "edit": "edit-workspace",
    "delete": "delete-workspace",
    "monitor": "monitor-workspace",
    "export": "export-workspace",
    "share": "share-workspace",
    "change-acls": "share-advanced",
    "admin": "transfer-workspace",
    "files": "files-view",
}


async def apply(db) -> None:
    for resource, renames in COLLECTION_RENAMES.items():
        for old, new in renames.items():
            await db.execute(
                "UPDATE acl_entries SET permission = ?"
                " WHERE resource = ? AND permission = ?",
                (new, resource, old),
            )
    for old, new in WORKSPACE_RENAMES.items():
        await db.execute(
            "UPDATE acl_entries SET permission = ?"
            " WHERE resource GLOB '/workspaces/?*' AND permission = ?",
            (new, old),
        )
    await _grant_lifecycle_trio(db)


async def _grant_lifecycle_trio(db) -> None:
    """Insert start/stop/restart-workspace for every existing coders-*
    / collaborators-* role group's workspace resource (seeded only for
    new workspaces otherwise). Idempotent: a group that already holds
    the permission on that resource is skipped."""
    args = (GROUP_SOURCE_WORKSPACE_ROLE,)
    cursor = await db.execute(
        "SELECT DISTINCT a.resource, a.group_id FROM acl_entries a"
        " JOIN groups g ON g.id = a.group_id"
        " WHERE a.resource GLOB '/workspaces/?*'"
        "   AND g.source = ?"
        "   AND (g.name LIKE 'coders-%' OR g.name LIKE 'collaborators-%')",
        args,
    )
    for resource, group_id in await cursor.fetchall():
        existing = await db.execute(
            "SELECT permission FROM acl_entries"
            " WHERE resource = ? AND group_id = ?",
            (resource, group_id),
        )
        held = {r[0] for r in await existing.fetchall()}
        cursor2 = await db.execute(
            "SELECT COALESCE(MAX(position), -1) FROM acl_entries"
            " WHERE resource = ?",
            (resource,),
        )
        pos = (await cursor2.fetchone())[0]
        for perm in (
            "start-workspace",
            "stop-workspace",
            "restart-workspace",
        ):
            if perm in held or "*" in held:
                continue
            pos += 1
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES (?, ?, ?, 2, ?, ?)",
                (resource, pos, ACTION_ALLOW, group_id, perm),
            )


migration = Migration(
    id=22,
    name="0022_workspace_permission_renames",
    apply=apply,
)
