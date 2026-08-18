"""Port allocation for workspace containers (#2542 split).

Owns the ``port_lock`` and delegates to ``model`` for DB-backed port
tracking.  Extracted from ``ContainerRegistry`` (issue #972).
"""

import asyncio

CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5


class PortAllocator:
    """Port allocation for workspace containers.

    Owns the ``port_lock`` and delegates to ``model`` for DB-backed
    port tracking.  Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self, app) -> None:
        self.port_lock: asyncio.Lock = asyncio.Lock()
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def allocate_ports(self, workspace_id: str, count: int) -> list[int]:
        # Clamp to the server-wide cap (KLANGKD_HOSTED_PORTS_PER_WORKSPACE)
        # so creation never allocates ports the deployer has disabled —
        # otherwise a cap of 0 would still leave orphan allocations
        # until the container's first start reconcile (#1237).
        count = min(
            count, self.app.state.container_registry.ports_per_workspace_cap()
        )
        async with self.port_lock:
            return await self.app.state.model.ports.find_and_allocate_ports(
                workspace_id,
                count,
                self.app.state.container_registry.port_range_start,
            )

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        return await self.app.state.model.ports.get_workspace_ports(
            workspace_id
        )
