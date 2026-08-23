"""Migration 0007: host_schedules -> server_schedules (#2661).

The #2661 scope change renamed the feature from "host" to "server"
terminology; the table follows. Migration *names* are frozen once
shipped (see the 0006 runner check), so the rename is a new migration,
not an edit of 0006.

Dev-DB note: a database that ran the intermediate (never-released)
commit that renamed 0006 itself recorded ``0006_server_schedules`` and
will be refused at boot ("Migration 6 is recorded as
'0006_host_schedules' but the code says '0006_server_schedules'")
before this migration ever runs. Manual recovery for such dev DBs::

    DELETE FROM schema_migrations WHERE id = 6;

then restart — 0006 re-runs (``CREATE TABLE IF NOT EXISTS``, no-op on
the existing table), 0007 renames it, and history is consistent again.
Released databases never see this; only 0006's original name exists.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    # Defensive drop: a dev DB that ran the intermediate (unreleased)
    # commit may already carry a server_schedules table.
    await db.execute("DROP TABLE IF EXISTS server_schedules")
    await db.execute("ALTER TABLE host_schedules RENAME TO server_schedules")


migration = Migration(7, "0007_server_schedules", apply)
