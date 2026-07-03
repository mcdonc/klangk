"""TCP port allocation tracking and free-port discovery."""

import asyncio
import socket

from ._core import transaction


async def add_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Allocate ports to a workspace. Raises IntegrityError on conflict."""
    async with transaction() as db:
        for port in ports:
            await db.execute(
                "INSERT INTO port_allocations (port, workspace_id) VALUES (?, ?)",
                (port, workspace_id),
            )


# Highest valid TCP port.  find_and_allocate_ports will not scan past
# this, so an exhausted range fails fast instead of looping forever.
MAX_PORT = 65535


def port_in_use(port: int) -> bool:
    """Check if a port is bound at the OS level."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def scan_free_ports(start: int, count: int, used: set[int]) -> list[int]:
    """Find ``count`` free ports at or after ``start``.

    Skips ports already in ``used`` (DB-allocated) and ports reported as
    bound by the OS.  This is synchronous because it performs blocking
    ``socket.bind()`` checks; ``find_and_allocate_ports`` runs it in an
    executor so the event loop is not stalled.  Raises ``ValueError`` if
    fewer than ``count`` free ports are available before ``MAX_PORT``.
    """
    ports: list[int] = []
    port = start
    while len(ports) < count:
        if port > MAX_PORT:
            raise ValueError(
                f"Could not allocate {count} free ports starting at "
                f"{start}: exhausted at {MAX_PORT}"
            )
        if port not in used and not port_in_use(port):
            ports.append(port)
        port += 1
    return ports


async def find_and_allocate_ports(
    workspace_id: str, count: int, start: int
) -> list[int]:
    """Atomically find free ports and allocate them in a single transaction."""
    async with transaction() as db:
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


async def remove_port_allocations(workspace_id: str, ports: list[int]) -> None:
    """Remove specific port allocations from a workspace."""
    async with transaction() as db:
        for port in ports:
            await db.execute(
                "DELETE FROM port_allocations WHERE port = ? AND workspace_id = ?",
                (port, workspace_id),
            )


async def get_workspace_ports(workspace_id: str) -> list[int]:
    """Return all allocated ports for a workspace, sorted."""
    async with transaction() as db:
        cursor = await db.execute(
            "SELECT port FROM port_allocations WHERE workspace_id = ? ORDER BY port",
            (workspace_id,),
        )
        rows = await cursor.fetchall()
        return [row["port"] for row in rows]


async def get_all_allocated_ports() -> set[int]:
    """Return all allocated port numbers across all workspaces."""
    async with transaction() as db:
        cursor = await db.execute("SELECT port FROM port_allocations")
        rows = await cursor.fetchall()
        return {row["port"] for row in rows}
