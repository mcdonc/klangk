"""Migration 0013: grant ``exec-and-sync`` to existing role groups.

The one-shot exec channel is now gated on the dedicated ``exec-and-sync``
permission (#2706/#2712) — enforced in ``ExecController.start`` for
every exec session, which is also what ``klangk sync`` rides on (its
rsync transport is ``klangk exec --raw``), so both sync directions are
covered by the same gate. New workspaces seed the permission into their
``coders``/``collaborators`` role groups via
``_ROLE_GROUP_PERMISSIONS``; this migration backfills existing
workspaces so the upgrade does not silently break ``klangk exec`` /
``klangk sync`` / ``klangk sandbox`` for members who already had it
(preserving prior behavior: revoking ``exec-and-sync`` is an admin
choice, not
the new default).

For every ``coders-``/``collaborators-<workspace_id>`` role group, an
``Allow`` ACE for ``exec-and-sync`` is appended at the end of that resource's
ACL (max position + 1) unless one already exists. Owners need nothing
(their seeded ACE is the ``*`` wildcard); spectators never had exec.
Admins who granted ``code-in-isolation`` to other principals via custom
ACEs must add ``exec-and-sync`` explicitly to preserve one-shot exec
for them —
terminals are unaffected.
"""

from klangk.model.migrations.base import Migration
from klangk.model.migrations.shared import grant_role_group_permission

_EXEC_AND_SYNC = "exec-and-sync"


async def apply(db) -> None:
    await grant_role_group_permission(db, _EXEC_AND_SYNC)


migration = Migration(13, "0013_exec_and_sync_permission", apply)
