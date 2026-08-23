"""Migration 0006: host_schedules (#2661).

Persisted pending server-action schedules. Rows are deleted once
fired or cancelled, so the table only ever holds *pending* actions — the
scheduler treats its contents as authoritative on boot (a schedule must
survive a klangkd restart).

Kept under its recorded name ``host_schedules`` — migration names are
frozen once shipped (a rename forks history); 0007 renames the table
itself to ``server_schedules``.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS host_schedules (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


migration = Migration(6, "0006_host_schedules", apply)
