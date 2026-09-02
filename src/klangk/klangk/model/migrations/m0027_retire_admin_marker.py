"""Migration 0027: drop the retired ``/admin`` ACL tree (#2995).

Instance-admin status now derives from its source of truth — membership
in the ``admins`` group — surfaced as the explicit ``is_admin`` flag on
``/my-permissions``. The ``/admin`` rows (Allow ``*`` admins, Deny
``*`` Everyone, seeded since the resource tree existed) survive only as
that marker: no permission check consults them (#2944 moved every
governed surface to a first-class resource whose walk never passes
through ``/admin``). Nothing else matches them either — the ancestor
walk reaches ``/admin`` only from ``/admin``-prefixed paths, and no
endpoint maps to one anymore — so every stored row at ``/admin`` or
under it is inert and can simply be deleted. A deployment whose only
use of ``/admin`` was the marker is repaired by this deletion; one that
staged custom rows there loses nothing functional (they answered no
check — m0021 already re-granted the live shapes on the first-class
resources).

The seed (``seed_default_acls``) and ``STATIC_RESOURCES`` drop
``/admin`` in the same release, so no new rows appear after the
migration runs.

Idempotent by construction: a ``DELETE`` with no matching rows is a
no-op.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        "DELETE FROM acl_entries"
        " WHERE resource = '/admin' OR resource LIKE '/admin/%'"
    )


migration = Migration(
    id=27,
    name="0027_retire_admin_marker",
    apply=apply,
)
