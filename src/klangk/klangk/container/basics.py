"""Container basics: port allocation, lifecycle state, identity.

Merges the three #2542 vocabulary fragments — ``ports``, ``state``,
``identity`` — into one module (#2908).  Each was a tiny standalone
concept (port constants + :class:`PortAllocator`; the per-workspace
:class:`ContainerState`; container-name helpers + :class:`BrowserRouter`),
consumed almost entirely by :mod:`.registry` and each other.  Keeping
them apart was accidental micro-fragmentation, not separation of
concerns — the big win of the #2542 split (``registry.py`` staying lean)
is untouched.
"""

import asyncio
import re
import time


# ---------------------------------------------------------------------------
# Port allocation (former ``ports`` submodule).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Per-workspace lifecycle state (former ``state`` submodule).
# ---------------------------------------------------------------------------


class ContainerState:
    """Per-workspace container lifecycle state."""

    def __init__(self, workspace_id: str, container_id: str, app):
        self.workspace_id = workspace_id
        self.container_id = container_id
        self.app = app
        self.last_activity = time.time()
        self.idle_timeout: int | None = None
        self.idle_callbacks: list = []
        # Health-monitoring state (#1015).  Populated at container start
        # time so HealthMonitor can poll without a DB lookup per tick.
        self.health_status: str | None = None  # "healthy" | "unhealthy"
        self.health_checked_at: float | None = None  # time.time() of last
        # Short, human-readable reason for the last unhealthy result
        # (stderr/stdout tail or exception text).  None when healthy or
        # not yet checked.  Surfaced via the status API + service_health
        # event so an unhealthy workspace isn't a black box (#1088).
        self.health_message: str | None = None
        # Per-workspace monotonic counter carried on every service_health
        # frame so a reconnecting consumer can detect a missed transition
        # against the connect-time snapshot (#1175 item 4).  Increments on
        # each emitted frame (transition and death); resets when the state
        # is recreated (container restart) -- the snapshot reconciles.
        self.health_seq: int = 0
        self.health_check: str | None = None  # shell command, None = disabled
        self.owner_id: str | None = None
        self.setup_state: str | None = None
        # Home layout (#2169 chunk 2, #2720): the health check resolves
        # the owner's per-handle HOME only when this is True; False means
        # the shared /home/klangk. Default True (the pre-#2720 layout and
        # the safe fallback if a start path forgets to set it).
        self.per_handle_home: bool = True
        # Anchor for the startup grace window
        # (HEALTH_CHECK_STARTUP_GRACE_SECONDS): the moment the monitored
        # service began starting.  Defaults to now (container-state
        # creation) so health-checked workspaces with no service command
        # still get a grace window; reset to "now" by
        # ``mark_service_started`` when the service command actually
        # fires, which is the precise point the service begins booting.
        self.service_started_at: float = time.time()

    def record_activity(self) -> None:
        self.last_activity = time.time()

    def mark_service_started(self) -> None:
        """Record that the service command just fired.

        Resets the startup-grace anchor to now.  Called from
        ``terminal.ensure_service_session`` right after it launches the
        service command, so the grace window is measured from the real
        start of the service rather than from the (earlier) container
        creation -- important for the per-connection fire path where a
        freshly-created workspace launches its service command on first
        ``terminal_start``, possibly long after the container started.
        """
        self.service_started_at = time.time()

    def get_idle_timeout(self) -> int:
        if self.idle_timeout is not None:
            return self.idle_timeout
        return self.app.state.container_registry.idle_timeout_seconds


# ---------------------------------------------------------------------------
# Container identity (former ``identity`` submodule, itself the #2858
# merge of the naming and browsers submodules): name construction and
# browser-delegate routing.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Container names (moved verbatim from the former naming submodule).
# ---------------------------------------------------------------------------


def workspace_name_slug(name: str, *, limit: int = 24) -> str:
    """Sanitize a workspace name for embedding in a container name (#2286).

    Podman container names must match ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``. Lowercase,
    collapse runs of non-``[a-z0-9]`` to a single ``-``, trim the ends, and cap
    the length. Returns ``""`` for names that reduce to nothing (empty,
    all-symbols) — callers fall back to an id-only name. The slug is decorative:
    uniqueness always comes from the instance id + workspace id, never the slug.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().strip())
    return slug[:limit].strip("-")


def workspace_container_name(iid: str, workspace_id: str, slug: str) -> str:
    """The workspace container name: iid + slugified name + id[:8] (#2286).

    Falls back to an id-only name when the slug is empty (all-symbol / missing
    name). Uniqueness is iid + id; the slug is decorative. The network sidecar
    name (:meth:`ContainerRegistry.network_sidecar_name`) shares the id[:8]
    tail so an id-prefix grep matches the pair.
    """
    if slug:
        return f"klangk-{iid}-{slug}-{workspace_id[:8]}"
    return f"klangk-{iid}-{workspace_id[:8]}"


# ---------------------------------------------------------------------------
# Browser-delegate routing (moved verbatim from the former browsers
# submodule).
# ---------------------------------------------------------------------------


class BrowserRouter:
    """Browser-delegate routing: browser_id → (workspace_id, sock).

    Browser IDs are browser-generated UUIDs (sessionStorage) sent
    with terminal_start.  Unlike the old bridge tokens they survive
    browser refresh because the same sessionStorage UUID re-registers
    with the new WebSocket.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self) -> None:
        self.browsers: dict[str, tuple[str, object | None]] = {}

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        """Register a browser ID for bridge routing.

        Idempotent: the same *browser_id* can re-register with a new
        *sock* after a browser refresh (sessionStorage keeps the ID).
        """
        self.browsers[browser_id] = (workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        """Look up (workspace_id, sock) for a browser ID."""
        return self.browsers.get(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        """Remove ALL browser registrations for a workspace.

        Called when a container is recreated or stopped.
        """
        to_remove = [
            bid
            for bid, (ws, _s) in self.browsers.items()
            if ws == workspace_id
        ]
        for bid in to_remove:
            del self.browsers[bid]

    def revoke_browser(self, sock: object) -> None:
        """Remove all browser registrations bound to a specific socket."""
        to_remove = [
            bid for bid, (_ws, s) in self.browsers.items() if s is sock
        ]
        for bid in to_remove:
            del self.browsers[bid]
