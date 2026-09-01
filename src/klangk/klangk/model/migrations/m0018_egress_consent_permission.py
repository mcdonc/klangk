"""Migration 0018: grant ``egress-consent`` to existing role groups.

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
had consent. The match is by role-group name + source marker, not by
ACEs: a coders/collaborators group whose ``terminal`` grant an admin
deliberately stripped still gains ``egress-consent`` (matching the
issue's wording: the backfill covers the role groups of every existing
workspace; stripping the group's deciding is then an owner edit).
Admins who granted ``terminal`` to other principals via custom ACEs
(e.g. simple member shares) must add ``egress-consent`` explicitly to
preserve deciding for them.
"""

from klangk.model.migrations.base import Migration
from klangk.model.migrations.shared import grant_role_group_permission

_EGRESS_CONSENT = "egress-consent"


async def apply(db) -> None:
    await grant_role_group_permission(db, _EGRESS_CONSENT)


migration = Migration(18, "0018_egress_consent_permission", apply)
