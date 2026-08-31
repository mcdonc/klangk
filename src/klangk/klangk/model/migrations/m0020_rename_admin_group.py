"""Migration 0020: rename the seeded ``admin`` group to ``admins`` (#2934).

The administrative group is now named ``admins`` (plural), matching the
other role groups (collaborators, coders, spectators). New deployments
seed ``admins`` directly (``ensure_admin_group``); this migration
renames the row on existing deployments so the upgrade keeps
memberships, ACLs, and the #1622 reseed gate intact.

The rename is by row id (``UPDATE groups SET name``), so every foreign
key that tracks the group — ``user_groups`` memberships and
``acl_entries`` principals — keeps pointing at the same id and survives
untouched. In particular the reseed gate (``seed_default_user`` empties
check) still finds a non-empty admin group after the rename, so an
upgraded deployment with admins does not accidentally re-seed the
default admin user.

Collision: a deployment may already have a manually created group named
``admins``. Merging the two (memberships + ACE re-pointing across
``UNIQUE(resource, position)``) is not safely automatable, so the
migration fails fast — the server is down at that point (the error
propagates through ``init_db`` before uvicorn serves), so recovery is
direct SQLite surgery with klangkd stopped, then restart to retry
(a failed migration is rolled back and retried on next boot, so this
is safe to hit repeatedly).

Idempotent by construction: no ``admin`` row (fresh DB, already-migrated
DB, or an operator who renamed the group to something custom) is a
no-op. On fresh DBs this migration runs at ``init_db`` time, before any
seeding, so there is nothing to rename.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    cursor = await db.execute("SELECT id FROM groups WHERE name = 'admin'")
    row = await cursor.fetchone()
    if row is None:
        return  # fresh / already-migrated / custom-renamed deployment
    admin_id = row[0]

    cursor = await db.execute("SELECT id FROM groups WHERE name = 'admins'")
    if await cursor.fetchone() is not None:
        raise RuntimeError(
            "Cannot rename the 'admin' group to 'admins': a group named"
            " 'admins' already exists (probably created manually)."
            " The server cannot start until this is resolved — klangkd"
            " must be stopped, the manual group renamed directly in the"
            " SQLite database, then klangkd restarted, e.g.:"
            " sqlite3 <data-dir>/klangk.db"
            " \"UPDATE groups SET name = 'admins-manual'"
            " WHERE name = 'admins';\""
            " (the data dir is KLANGKD_DATA_DIR, '.' by default)."
        )

    await db.execute(
        "UPDATE groups SET name = 'admins' WHERE id = ?", (admin_id,)
    )


migration = Migration(20, "0020_rename_admin_group", apply)
