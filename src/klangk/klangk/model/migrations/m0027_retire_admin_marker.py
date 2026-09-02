"""Migration 0027: remove the retired /admin marker rows (#2974).

Since #2944 no route checks a permission on ``/admin``; the resource's
rows survived only as the instance-administrator wildcard marker that
``/my-permissions`` consumers read via ``*`` on ``/admin``. #2974
replaces the hack with an explicit ``is_admin`` flag on
``/my-permissions`` derived from admins-group membership — the source
of truth — so the marker rows (and the resource) are retired.

This migration deletes every stored row on ``/admin``: the seeded pair
(Allow ``*`` admins + Deny ``*`` Everyone) and anything an operator
added on the dead tree (it can only ever have served the marker — no
endpoint resolves to ``/admin`` since #2944, so nothing else can be
reading those rows).

A fresh database (entirely empty ``acl_entries``) is a no-op — the boot
seeds own it.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
    if (await cursor.fetchone())[0] == 0:
        return  # fresh database — the boot seeds own it
    await db.execute("DELETE FROM acl_entries WHERE resource = '/admin'")


migration = Migration(
    id=27,
    name="0027_retire_admin_marker",
    apply=apply,
)
