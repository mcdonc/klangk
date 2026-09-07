"""Migration 0039: id-only workspace-role-group descriptions (#3283).

The workspace seed wrote each role group's description as
``Workspace role group: <role> of workspace <name>`` — the free-form,
user-chosen workspace name. ``GET /groups`` is an authenticated listing
(#2944), so any signed-in user could page the group list and read every
workspace's name on the deploy, data the workspaces endpoints expose
per-user only.

This migration rewrites the seeded-template descriptions of rows
carrying ``source = 'workspace-role'`` to the id-only form
``Workspace role group: <role>`` (the group name already carries the
workspace id). Descriptions that no longer match the template — an
admin who edited one after seeding — are left untouched: only the
machine-written rows are rewritten. The seed itself now writes the
id-only wording, so this migration exists solely for existing rows.
"""

import re

from klangk.model.migrations.base import Migration

# The pre-#3283 seed template: "Workspace role group: <role> of
# workspace <name>" — the name is the leaked half.
_SEEDED_TEMPLATE = re.compile(
    r"^Workspace role group: "
    r"(owners|coders|collaborators|spectators)"
    r" of workspace "
)


async def apply(db) -> None:
    cursor = await db.execute(
        "SELECT id, description FROM groups WHERE source = 'workspace-role'"
    )
    rows = await cursor.fetchall()
    for group_id, description in rows:
        if description is None:
            continue
        match = _SEEDED_TEMPLATE.match(description)
        if match is None:
            continue  # human-edited description — leave it alone
        await db.execute(
            "UPDATE groups SET description = ? WHERE id = ?",
            (f"Workspace role group: {match.group(1)}", group_id),
        )


migration = Migration(39, "0039_role_group_descriptions", apply)
