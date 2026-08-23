"""Migration 0006: server_schedules (#2661).

Persisted pending server stop/recycle schedules. Rows are deleted once
fired or cancelled, so the table only ever holds *pending* actions — the
scheduler treats its contents as authoritative on boot (a schedule must
survive a klangkd restart).

The table shipped as ``host_schedules`` in an unreleased build of this
branch; it was renamed before release, so the migration drops the old
name if a dev DB carries it.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS server_schedules (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


async def _drop_old_name(db) -> None:
    # Pre-rename dev DBs only; the table never shipped in a release.
    await db.execute("DROP TABLE IF EXISTS host_schedules")


async def apply_with_rename(db) -> None:
    await apply(db)
    await _drop_old_name(db)


migration = Migration(6, "0006_server_schedules", apply_with_rename)
