"""Migration 0010: groups.source (#2750).

Every workspace seeds four role groups (``owners-``/``coders-``/
``collaborators-``/``spectators-<workspace_id>``) into the same global
``groups`` table used for human-managed groups, and the global group
lists return every row — so long-lived deploys accumulate hundreds of
machine-generated UUID-suffixed names that bury the human-created ones.

This adds a ``source`` marker column (mirroring ``user_groups.source``,
which already distinguishes manual from OIDC-synced memberships):
``'manual'`` (default — human-created and OIDC-synced groups) vs
``'workspace-role'`` (seeded per-workspace role group). Seeding writes
the marker, teardown finds role groups by it, and the list endpoints
filter on it.

Backfill: existing rows are classified by the
``^(owners|coders|collaborators|spectators)-<uuid>$`` name pattern. The
suffix must parse as a UUID **and** match an existing workspace row —
the seed's naming scheme, which ``delete_workspace`` teardown has always
relied on. A human-created group that merely looks like a role group
(valid-UUID suffix, no such workspace) is left untouched (no
reclassification; the collision is expected to never happen in
practice). Matched rows also get their descriptions normalized to
``Workspace role group: <role> of workspace <name>``.
"""

import re
import uuid as uuid_mod

from klangk.model.migrations.base import Migration

_ROLE_GROUP_NAME_RE = re.compile(
    r"^(owners|coders|collaborators|spectators)-(.+)$"
)

_WORKSPACE_ROLE = "workspace-role"


async def apply(db) -> None:
    await db.execute(
        "ALTER TABLE groups ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
    )
    cursor = await db.execute("SELECT id, name FROM groups")
    rows = await cursor.fetchall()
    if not rows:
        return
    # Workspace existence check: only rows whose UUID suffix is a real
    # workspace are reclassified (collision skip, #2750).
    ws_cursor = await db.execute("SELECT id, name FROM workspaces")
    ws_names = {row[0]: row[1] for row in await ws_cursor.fetchall()}
    for row in rows:
        group_id, name = row[0], row[1]
        m = _ROLE_GROUP_NAME_RE.match(name)
        if m is None:
            continue
        role, ws_id = m.group(1), m.group(2)
        try:
            uuid_mod.UUID(ws_id)
        except ValueError:
            continue  # suffix is not a UUID — human-named, skip
        if ws_id not in ws_names:
            continue  # collision: pattern-matching human group, skip
        await db.execute(
            "UPDATE groups SET source = ?, description = ? WHERE id = ?",
            (
                _WORKSPACE_ROLE,
                f"Workspace role group: {role} of workspace {ws_names[ws_id]}",
                group_id,
            ),
        )


migration = Migration(10, "0010_groups_source", apply)
