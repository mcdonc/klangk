"""Node-level operator state persisted in the ``server_state`` table.

The k8s cordon/drain pair for klangkd (#2527): the cordon flag must
survive a klangkd restart (a crash-looping service must not silently
un-cordon itself and re-start user workspaces while an operator
investigates), so it lives in the DB, not process memory.

The table is a generic key-value store (``key TEXT PRIMARY KEY``,
``value TEXT NOT NULL``) so future node-level state slots in without
another migration. Only the cordon key exists today.

:class:`ServerStateModel` is the ``app_state``-owned form reached via
``app.state.model.server_state``. The flag is read at container-start
time (not cached at boot), so a cordon set by one request path is
honored immediately by every other path, across SIGHUP restarts, and
after crashes.
"""

from __future__ import annotations

CORDON_KEY = "cordoned"


class ServerStateModel:
    """Persisted node-level state, resolved through ``app_state.db``."""

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def get(self, key: str, default: str | None = None) -> str | None:
        """Return the stored value for *key*, or *default*."""
        row = await self.app.state.db.fetchone(
            "SELECT value FROM server_state WHERE key = ?", (key,)
        )
        return row[0] if row else default

    async def set(self, key: str, value: str) -> None:
        """Upsert *key* = *value*."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                """
                INSERT INTO server_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # --- Cordon (#2527) ---

    async def is_cordoned(self) -> bool:
        """True when the node is cordoned (new starts refused)."""
        return (await self.get(CORDON_KEY)) == "1"

    async def set_cordoned(self, cordoned: bool) -> None:
        """Set/clear the cordon flag."""
        await self.set(CORDON_KEY, "1" if cordoned else "0")
