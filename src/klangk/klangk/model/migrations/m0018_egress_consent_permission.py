"""Migration 0017: grant ``egress-consent`` to existing role groups.

Egress consent is now gated on the dedicated ``egress-consent``
permission (#2883) instead of ``terminal`` — enforced at the
``/ws/consent-decider`` registration handshake, so a spectator
(watch-only) can no longer register a decider, decide held requests,
revoke verdicts, or pause prompting. New workspaces seed the permission
into their ``coders``/``collaborators`` role groups via
``_ROLE_GROUP_PERMISSIONS``; this migration backfills existing
workspaces so the upgrade does not silently stop members who could
already decide from deciding (preserving prior behavior: withholding
``egress-consent`` is an owner/admin choice, not the new default).

For every ``coders-``/``collaborators-<workspace_id>`` role group, an
``Allow`` ACE for ``egress-consent`` is appended at the end of that
resource's ACL (max position + 1) unless one already exists. Owners
need nothing (their seeded ACE is the ``*`` wildcard); spectators never
had consent. Admins who granted ``terminal`` to other principals via
custom ACEs (e.g. simple member shares) must add ``egress-consent``
explicitly to preserve deciding for them.
"""

import re

from klangk.model.acl import ACTION_ALLOW, PRINCIPAL_GROUP
from klangk.model.migrations.base import Migration

_CONSENT_ROLES = ("coders", "collaborators")
_ROLE_GROUP_RE = re.compile(r"^(%s)-(.+)$" % "|".join(_CONSENT_ROLES))


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
            " AND permission = 'egress-consent' LIMIT 1",
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
            "  'egress-consent')",
            (resource, pos, ACTION_ALLOW, PRINCIPAL_GROUP, name),
        )


migration = Migration(18, "0018_egress_consent_permission", apply)
