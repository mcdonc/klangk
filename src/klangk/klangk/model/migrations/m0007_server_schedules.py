"""Migration 0007: host_schedules -> server_schedules (#2661).

The #2661 scope change renamed the feature from "host" to "server"
terminology; the table follows. Migration *names* are frozen once
shipped (see the 0006 runner check), so the rename is a new migration,
not an edit of 0006.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    # Defensive drop: a dev DB that ran the intermediate (unreleased)
    # commit may already carry a server_schedules table.
    await db.execute("DROP TABLE IF EXISTS server_schedules")
    await db.execute("ALTER TABLE host_schedules RENAME TO server_schedules")


migration = Migration(7, "0007_server_schedules", apply)
