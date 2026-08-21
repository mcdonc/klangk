"""Migration 0006: the ``server_state`` key-value table (#2527).

Node-level operator state that must survive a klangkd restart — today
the cordon flag (k8s cordon/drain, #2527): a cordoned node refuses new
workspace starts and suppresses boot auto-start, and a crash-looping
service must not silently un-cordon itself, so the flag lives in the
DB rather than process memory.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        """
        CREATE TABLE server_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


migration = Migration(6, "0006_server_state", apply)
