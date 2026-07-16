"""TCP port allocation tracking.

OS-level socket probes (``port_in_use``, ``free_port``, ``scan_free_ports``)
live in :mod:`klangk_backend.util` now (#1547); they are re-imported below so
the historical ``model.ports.*`` / ``model.*`` import paths keep working.

:class:`PortsModel` is the ``app_state``-owned form (DB-backed allocation)
reached via ``app_state.model.ports`` (#1563 / #1572). The module-level free
functions are the pre-existing ``_current_db`` ContextVar delegates, kept as
the backstop until #1578 dissolves the ContextVar.
"""

import asyncio

from ..util import (  # moved to util (#1547); re-exported via __all__
    MAX_PORT,
    free_port,
    port_in_use,
    scan_free_ports,
)


__all__ = [
    # OS-level socket probes — moved to klangk_backend.util (#1547);
    # re-exported here so the historical model.ports.* / model.* import
    # paths keep working.
    "MAX_PORT",
    "port_in_use",
    "free_port",
    "scan_free_ports",
    # DB-backed allocation tracking — methods live on PortsModel
    # (reached via app_state.model.ports); no module-level free fns remain
    # (#1578).
    "PortsModel",
]


class PortsModel:
    """DB-backed port-allocation tracking, through ``app_state.db``.

    Reached via ``app_state.model.ports``. Reaches the DB through
    ``self.app_state.db`` (the single DB instance for the whole app). The
    OS-level socket probes (``port_in_use`` etc.) stay in ``util`` — only
    the DB-backed allocation tracking lives here.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    async def add_port_allocations(
        self, workspace_id: str, ports: list[int]
    ) -> None:
        """Allocate ports to a workspace. Raises IntegrityError on conflict."""
        async with self.app_state.db.transaction() as db:
            for port in ports:
                await db.execute(
                    "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                    (port, workspace_id),
                )

    async def find_and_allocate_ports(
        self, workspace_id: str, count: int, start: int
    ) -> list[int]:
        """Atomically find free ports and allocate them in a single transaction."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute("SELECT port FROM port_allocations")
            rows = await cursor.fetchall()
            used = {row["port"] for row in rows}

            # The socket.bind() probe inside scan_free_ports blocks, so run
            # the scan in the default executor to avoid stalling the loop.
            loop = asyncio.get_running_loop()
            ports = await loop.run_in_executor(
                None, scan_free_ports, start, count, used
            )

            for p in ports:
                await db.execute(
                    "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                    (p, workspace_id),
                )
            return ports

    async def remove_port_allocations(
        self, workspace_id: str, ports: list[int]
    ) -> None:
        """Remove specific port allocations from a workspace."""
        async with self.app_state.db.transaction() as db:
            for port in ports:
                await db.execute(
                    "DELETE FROM port_allocations WHERE port = ? AND workspace_id = ?",
                    (port, workspace_id),
                )

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        """Return all allocated ports for a workspace, sorted."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT port FROM port_allocations WHERE workspace_id = ? ORDER BY port",
                (workspace_id,),
            )
            rows = await cursor.fetchall()
            return [row["port"] for row in rows]

    async def get_all_allocated_ports(self) -> set[int]:
        """Return all allocated port numbers across all workspaces."""
        async with self.app_state.db.transaction() as db:
            cursor = await db.execute("SELECT port FROM port_allocations")
            rows = await cursor.fetchall()
            return {row["port"] for row in rows}
