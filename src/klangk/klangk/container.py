"""Container lifecycle management: start, stop, idle timeout, port allocation."""

import asyncio
import logging
import os
import time

from . import podman
from .podman import PodmanError

logger = logging.getLogger(__name__)


# --- Runtime SSL/CA certificate injection (#1181) -------------------------
from .ssl_trust import (  # noqa: E402
    SSL_MOUNT_DEST as _SSL_MOUNT_DEST,
    ssl_env_vars,
)


# ---------------------------------------------------------------------------
# Pure module-level constants (no settings reads — #1487 carve-out)
# ---------------------------------------------------------------------------

CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5

HEALTH_MESSAGE_MAX_BYTES = 512

_VALID_PULL_POLICIES = {"never", "missing", "always", "newer"}

_VALID_MOUNT_OPTIONS = {
    "ro",
    "rw",
    "z",
    "Z",
    "nocopy",
    "consistent",
    "cached",
    "delegated",
}

_PROTECTED_PATHS = [
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
]


def _is_named_volume(source: str) -> bool:
    """A mount source with no '/' that doesn't start with '.' is a volume."""
    return "/" not in source and not source.startswith(".")


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


class BrowserRouter:
    """Browser-delegate routing: browser_id → (workspace_id, sock).

    Browser IDs are browser-generated UUIDs (sessionStorage) sent
    with terminal_start.  Unlike the old bridge tokens they survive
    browser refresh because the same sessionStorage UUID re-registers
    with the new WebSocket.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self) -> None:
        self._browsers: dict[str, tuple[str, object | None]] = {}

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        """Register a browser ID for bridge routing.

        Idempotent: the same *browser_id* can re-register with a new
        *sock* after a browser refresh (sessionStorage keeps the ID).
        """
        self._browsers[browser_id] = (workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        """Look up (workspace_id, sock) for a browser ID."""
        return self._browsers.get(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        """Remove ALL browser registrations for a workspace.

        Called when a container is recreated or stopped.
        """
        to_remove = [
            bid
            for bid, (ws, _s) in self._browsers.items()
            if ws == workspace_id
        ]
        for bid in to_remove:
            del self._browsers[bid]

    def revoke_browser(self, sock: object) -> None:
        """Remove all browser registrations bound to a specific socket."""
        to_remove = [
            bid for bid, (_ws, s) in self._browsers.items() if s is sock
        ]
        for bid in to_remove:
            del self._browsers[bid]


class IdleMonitor:
    """Idle-timeout tracking and cleanup loop.

    Monitors ``ContainerState.last_activity`` for all tracked
    workspaces and kills containers that exceed their idle timeout.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self, app) -> None:
        self.app = app
        self.cleanup_task: asyncio.Task | None = None
        self._cleanup_wake: asyncio.Event | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    def get_cleanup_wake(self) -> asyncio.Event:
        if self._cleanup_wake is None:
            self._cleanup_wake = asyncio.Event()
        return self._cleanup_wake

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            state.idle_callbacks.append(callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state and callback in state.idle_callbacks:
            state.idle_callbacks.remove(callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            state.idle_timeout = seconds
            self.get_cleanup_wake().set()

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            return state.get_idle_timeout()
        return self.app.state.container_registry.idle_timeout_seconds

    async def cleanup_idle_containers(self) -> None:
        registry = self.app.state.container_registry
        while True:
            timeouts = [
                s.idle_timeout
                for s in registry.states.values()
                if s.idle_timeout is not None
            ]
            if timeouts:
                interval = max(2, min(timeouts) // 2)
            else:
                interval = registry.check_interval_seconds
            wake = self.get_cleanup_wake()
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            now = time.time()
            to_stop = []
            for ws_id, state in list(registry.states.items()):
                timeout = state.get_idle_timeout()
                idle_secs = now - state.last_activity
                logger.debug(
                    "Idle check: %s idle %.0fs / %ds",
                    state.container_id[:12],
                    idle_secs,
                    timeout,
                )
                if timeout > 0 and idle_secs > timeout:
                    to_stop.append((state.container_id, ws_id))

            for cid, wid in to_stop:
                logger.info(
                    "Stopping idle container %s (workspace %s)",
                    cid,
                    wid,
                )
                state = registry.states.get(wid)
                if state:
                    for cb in list(state.idle_callbacks):
                        try:
                            await cb(wid)
                        except Exception as e:
                            logger.error("Idle callback error: %s", e)
                await registry.notify_workspace_killed(wid)
                await registry.stop_and_remove_container(cid)

    def start_cleanup_loop(self) -> None:
        registry = self.app.state.container_registry
        logger.info(
            "Instance: %s, idle timeout: %ds, check interval: %ds",
            self.app.state.util.instance_id(),
            registry.idle_timeout_seconds,
            registry.check_interval_seconds,
        )
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                self.cleanup_idle_containers()
            )


def unhealthy_message(rc: int, out: str, err: str) -> str:
    """Build a bounded failure reason from a check's exit code/output.

    Prefers stderr (where shells/diagnostics write their failures);
    falls back to a tail of stdout; if both are empty, reports just
    the exit code.  Truncated to ``HEALTH_MESSAGE_MAX_BYTES`` so a
    verbose check can't grow memory unbounded across workspaces --
    the goal is "why did it fail", not a full transcript (#1088).
    """
    body = (err or "").strip() or (out or "").strip()
    if body and len(body) > HEALTH_MESSAGE_MAX_BYTES:
        body = "..." + body[-HEALTH_MESSAGE_MAX_BYTES:]
    return f"exited {rc}: {body}" if body else f"exited {rc}"


class HealthMonitor:
    """Periodically poll container service health via ``podman exec``.

    Mirrors :class:`IdleMonitor` in shape.  For each workspace with a
    running container and a configured ``health_check`` command, runs
    the command inside the container as the creating user with their
    HOME set.  Exit 0 -> healthy; anything else (non-zero, timeout,
    error) -> unhealthy.  Status transitions are broadcast to
    connected clients as ``service_health`` events.

    See #1015 for the design rationale (external polling beats a
    container-side WS agent here).
    """

    def __init__(self, app) -> None:
        self.app = app
        self.health_task: asyncio.Task | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def connections(self):
        """The WebSocketState instance the monitor broadcasts through (#1464).

        Reached via ``app_state.sockets`` (owned instance, #1426) — no
        post-construction wiring needed.
        """
        return self.app.state.sockets

    def _setup_complete(self, state: ContainerState) -> bool:
        """True if health checks may run for this workspace.

        Checks are skipped until setup has finished (setup_state ==
        "complete"); running them during setup would report false
        negatives (the service isn't running yet because setup.sh
        hasn't installed it).
        """
        return state.setup_state == "complete"

    def _in_startup_grace(self, state: ContainerState) -> bool:
        """True while the service is still within its startup grace window.

        Mirrors Docker's HEALTHCHECK ``--start-period``: while the
        service command is booting, a failing check is expected rather
        than a real outage, so :meth:`_check_workspace` ignores
        unhealthy results here (but still records a *healthy* result so
        a fast-booting service is marked up immediately).  Anchored to
        ``service_started_at`` (when the command fired, or container
        creation as a fallback).
        """
        return (
            time.time() - state.service_started_at
            < self.app.state.container_registry.health_check_startup_grace
        )

    async def _run_one(self, state: ContainerState) -> tuple[str, str]:
        """Run a single workspace's health check.

        Returns ``(status, message)`` where *status* is ``"healthy"`` or
        ``"unhealthy"`` and *message* is a short, human-readable reason
        for an unhealthy result (a bounded tail of the check's
        stderr/stdout, or the exception text) -- empty when healthy.
        Surfacing the reason turns an ``unhealthy`` status from a black
        box into a diagnosable failure instead of "good luck" (#1088).

        Resolves the owner's container home (same logic as
        ``start_workspace``) and invokes the check via
        ``podman exec`` as the creating user with HOME set.  The check
        runs as a **non-login** bash shell (``bash -c``) on purpose: it
        is an operational probe, not a user session, so it deliberately
        sources *no* startup file -- not ``~/.profile``, ``~/.bashrc``,
        nor ``/etc/profile.d/*``.  This keeps the probe deterministic
        and decoupled from the owning user's interactive setup: a slow
        ``nvm`` load, a broken ``~/.profile`` edit, or a stray ``read``
        prompt must never make an unattended 30s poll flap "unhealthy".

        The flip side is that the check command must not rely on the
        user's PATH or env.  It inherits only the container's image
        ``PATH`` (so ``/opt/klangk/bin`` and system tools like
        ``grep``/``curl`` resolve) plus ``HOME``.  Anything the checked
        service needs -- a sandbox-installed binary, ``OPENCLAW_HOME`` /
        ``HERMES_HOME``, a custom ``PATH`` -- must be referenced by
        **absolute path** in the check command, or wrapped in an
        executable script whose shebang and ``export`` lines bake those
        in (the recommended pattern for non-trivial checks; see
        ``docs/features/health-check.md``).  Errors and timeouts count
        as ``"unhealthy"``.
        """
        owner_id = state.owner_id
        if owner_id is None:
            return "unhealthy", "no owner recorded for workspace"
        handle = await self.app.state.model.users.get_user_handle(owner_id)
        if not handle:
            return "unhealthy", f"owner {owner_id} has no handle"
        # Resolve the owner's container home the same way
        # start_workspace does, so the check runs in the right
        # HOME rather than as root in /.
        ws = self.app.state.workspaces
        ws_home = ws.home_path(state.workspace_id)
        user_home, _created = await ws.ensure_home_symlink(
            ws_home, handle, owner_id
        )
        cid_short = state.container_id[:12]
        logger.debug(
            "Health check: container %s (workspace %s) running %r",
            cid_short,
            state.workspace_id,
            state.health_check,
        )
        try:
            rc, out, err = await self.app.state.podman.exec_container(
                state.container_id,
                # bash -c (NON-login): sources nothing, so the probe is
                # deterministic and insulated from the user's interactive
                # shell setup. Only the image PATH + HOME are visible,
                # so health_check commands must use absolute paths (or a
                # wrapper script). See docs/features/health-check.md.
                # Skipped until setup_state == complete.
                ["bash", "-c", state.health_check],
                user="klangk",
                extra_env={"HOME": user_home},
                timeout=self.app.state.container_registry.health_check_timeout,
            )
        except (podman.PodmanError, asyncio.TimeoutError, OSError) as e:
            return "unhealthy", f"{type(e).__name__}: {e}"
        if rc == 0:
            return "healthy", ""
        return "unhealthy", unhealthy_message(rc, out, err)

    async def _check_workspace(self, state: ContainerState) -> None:
        """Poll one workspace, record the reason, and broadcast on change."""
        new_status, message = await self._run_one(state)
        # Startup grace window: the service command may still be
        # booting, so an unhealthy result here is expected, not a real
        # outage.  Don't transition to unhealthy, broadcast, or log a
        # failure (mirrors Docker HEALTHCHECK --start-period).  A
        # healthy result is still recorded below so a fast-booting
        # service is marked up the moment it actually responds.
        if new_status == "unhealthy" and self._in_startup_grace(state):
            logger.debug(
                "Health check for workspace %s (container %s) failing "
                "but within startup grace (%.0fs elapsed); not flagging "
                "unhealthy",
                state.workspace_id,
                state.container_id[:12],
                time.time() - state.service_started_at,
            )
            return
        old_status = state.health_status
        state.health_status = new_status
        # Clear the reason once healthy again so a stale failure message
        # can't linger next to a "healthy" status (#1088).
        state.health_message = message if new_status == "unhealthy" else None
        state.health_checked_at = time.time()
        if new_status == "unhealthy":
            # Log the reason at info on a fresh transition (so it's
            # visible without debug logs), debug on steady-state polls
            # so a persistently-broken check doesn't spam at info (#1088).
            log = logger.info if old_status != "unhealthy" else logger.debug
            log(
                "Health check for workspace %s (container %s) unhealthy: %s",
                state.workspace_id,
                state.container_id[:12],
                message,
            )
        if new_status != old_status:
            self._broadcast(state, new_status, state.health_message)

    def _broadcast(
        self,
        state: ContainerState,
        status: str,
        message: str | None = None,
    ) -> None:
        """Emit a ``service_health`` transition event to all connections.

        Fanned out via :meth:`WsState.notify_service_health` so the
        workspace list page learns about health transitions for
        auto-started services even when nobody is connected to the
        workspace's terminal session (#1015).  The failure *reason*
        rides along as ``health_message`` so operators can see *why*
        it's unhealthy without digging through logs (#1088).

        Also forwards the additive contract fields (#1175):
        ``running=True`` (this is a live-container frame), the last
        ``health_checked_at`` (#1175 item 3a), and a per-workspace
        ``seq`` (#1175 item 4) bumped on every emit so a reconnecting
        consumer can detect a missed transition.
        """
        state.health_seq += 1
        self.connections.notify_service_health(
            state.workspace_id,
            healthy=status == "healthy",
            message=message,
            running=True,
            health_checked_at=state.health_checked_at,
            seq=state.health_seq,
        )

    def broadcast_death(self, state: ContainerState) -> None:
        """Emit the terminal ``service_health`` frame for a dying container.

        When a container dies the server emits
        ``container_status{running: false}`` and then *silence* on the
        ``service_health`` stream, because the health loop only polls
        ``registry.states`` and a dead container's state is removed.  A
        consumer watching ``service_health`` therefore believes the
        last-known status (possibly healthy) still holds while the
        container is gone (#1175 item 2).  This closes the hole by
        emitting one unambiguous terminal frame with ``running=False``
        and ``healthy=False`` *before* the state is dropped, so a single
        stream is a single source of truth.
        """
        state.health_seq += 1
        self.connections.notify_service_health(
            state.workspace_id,
            healthy=False,
            message=None,
            running=False,
            health_checked_at=state.health_checked_at,
            seq=state.health_seq,
        )

    async def run_health_loop(self) -> None:
        """Background loop: every interval, poll eligible workspaces.

        After each poll sweep, emits liveness heartbeats to connections
        that opted in (#1175 item 3b).  Emitting from *this* loop (rather
        than a standalone task) ties heartbeat presence to the health
        loop being alive -- if the loop stalls, the heartbeats stop.
        """
        while True:
            registry = self.app.state.container_registry
            await asyncio.sleep(registry.health_check_interval)
            for state in list(registry.states.values()):
                if not state.health_check:
                    continue
                if not self._setup_complete(state):
                    continue
                try:
                    await self._check_workspace(state)
                except Exception as e:  # pragma: no cover - defensive
                    logger.error(
                        "Health check error for workspace %s: %s",
                        state.workspace_id,
                        e,
                    )
            self._send_heartbeats()

    def _send_heartbeats(self) -> None:
        """Fan health heartbeats to opt-in connections."""
        self.connections.send_health_heartbeats()

    def start_health_loop(self) -> None:
        if self.health_task is None:
            self.health_task = asyncio.create_task(self.run_health_loop())


class ContainerRegistry:
    """Manages all container state and podman interactions.

    Composes :class:`PortAllocator`, :class:`BrowserRouter`,
    :class:`IdleMonitor`, and :class:`HealthMonitor` as collaborators.
    Backward-compatible proxy methods delegate to the collaborators so
    existing callers are unchanged.

    Constructed once in :func:`build_app` and stored on
    ``app.state.container_registry`` (#1426). The module-level ``registry``
    is a transitional shim for callers not yet migrated to explicit
    threading.
    """

    def __init__(self, app):
        self.app = app

        # Runtime-mutable state (initialized from settings but overridable
        # at runtime via set_idle_timeout — NOT a live settings read).
        self.idle_timeout_seconds, self.check_interval_seconds = (
            self._parse_idle_timeout()
        )

        self.states: dict[str, ContainerState] = {}
        self._cid_to_wsid: dict[str, str] = {}
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._service_session_locks: dict[str, asyncio.Lock] = {}
        self.on_workspace_killed = None
        self.on_container_status_changed = None

        # Collaborators
        self.ports = PortAllocator(app)
        self.browsers = BrowserRouter()
        self.idle = IdleMonitor(app)
        self.health = HealthMonitor(app)

        # The Podman instance is reached via self.app.state.podman (owned
        # instance, #1426) — no post-construction wiring needed.

    def reconfigure(self, app) -> None:
        self.app = app
        self.ports.reconfigure(app)
        self.idle.reconfigure(app)
        self.health.reconfigure(app)

    # --- settings-derived config (read live off app_state, #1608) ---

    @property
    def image_name(self) -> str:
        return self.app.state.settings.image_name or "klangk-workspace"

    @property
    def terminal_banner(self) -> str:
        return self.app.state.settings.terminal_banner or ""

    @property
    def allowed_images(self) -> set[str]:
        imgs: set[str] = set()
        raw = self.app.state.settings.allowed_images
        if raw:
            imgs = {i.strip() for i in raw.split(",") if i.strip()}
        imgs.add(self.image_name)
        return imgs

    @property
    def allowed_mount_roots(self) -> list[str]:
        raw = self.app.state.settings.allowed_mount_roots
        if not raw:
            return []
        return [
            os.path.realpath(p.strip()) for p in raw.split(",") if p.strip()
        ]

    @property
    def port_range_start(self) -> int:
        return int(self.app.state.settings.port_range_start or "9000")

    @property
    def health_check_interval(self) -> float:
        return float(self.app.state.settings.health_check_interval or "30")

    @property
    def health_check_timeout(self) -> float:
        return float(self.app.state.settings.health_check_timeout or "10")

    @property
    def health_check_startup_grace(self) -> float:
        return float(
            self.app.state.settings.health_check_startup_grace or "30"
        )

    # --- settings-derived methods (were module functions, #1487) ---

    def container_dns_config(self) -> list[str]:
        """Return DNS server list from settings.dns_servers."""
        raw = self.app.state.settings.dns_servers
        return [d.strip() for d in raw.split(",") if d.strip()]

    def image_pull_policy(self) -> str:
        """Resolve the workspace-image pull policy from settings."""
        policy = self.app.state.settings.image_pull_policy
        if policy not in _VALID_PULL_POLICIES:
            logger.warning(
                "Invalid KLANGKD_IMAGE_PULL_POLICY=%r (valid: %s); using 'never'.",
                policy,
                ", ".join(sorted(_VALID_PULL_POLICIES)),
            )
            return "never"
        return policy

    def _is_protected(self, source: str) -> bool:
        """True if source is a protected host path that must never be mounted."""
        resolved = os.path.realpath(source)
        data_dir = os.path.realpath(self.app.state.settings.data_dir)
        for blocked in [*_PROTECTED_PATHS, data_dir]:
            blocked = os.path.realpath(blocked)
            if resolved == blocked or resolved.startswith(blocked + "/"):
                return True
        return False

    def validate_mount_spec(self, spec: str) -> str | None:
        """Validate a container mount spec string."""
        parts = spec.split(":")
        if len(parts) < 2 or len(parts) > 3:
            return f"Invalid mount {spec!r}: expected source:dest or source:dest:options"
        source, dest = parts[0], parts[1]
        if not source:
            return f"Invalid mount {spec!r}: source is empty"
        if not dest.startswith("/"):
            return f"Invalid mount {spec!r}: container path must be absolute (start with /)"
        if len(parts) == 3:
            options = parts[2]
            for opt in options.split(","):
                if opt and opt not in _VALID_MOUNT_OPTIONS:
                    return f"Invalid mount {spec!r}: unknown option {opt!r}"
        if not _is_named_volume(source):
            if self._is_protected(source):
                return (
                    f"Invalid mount {spec!r}: source is a protected host path"
                )
            if self.allowed_mount_roots:
                resolved = os.path.realpath(source)
                if not any(
                    resolved == root or resolved.startswith(root + "/")
                    for root in self.allowed_mount_roots
                ):
                    allowed = ", ".join(self.allowed_mount_roots)
                    return (
                        f"Invalid mount {spec!r}: bind mount source must be "
                        f"under an allowed root ({allowed})"
                    )
        return None

    def validate_mounts(self, mounts: list[str]) -> str | None:
        """Validate a list of mount specs. Returns first error or None."""
        for spec in mounts:
            error = self.validate_mount_spec(spec)
            if error:
                return error
        return None

    def _parse_idle_timeout(self) -> tuple[int, int]:
        default = 30 * 60
        env_val = self.app.state.settings.idle_timeout_seconds
        if env_val is not None:
            try:
                timeout = int(env_val)
            except ValueError:
                logger.warning(
                    "KLANGKD_IDLE_TIMEOUT_SECONDS=%r is not a valid integer, "
                    "using default %d",
                    env_val,
                    default,
                )
                timeout = default
        else:
            timeout = default
        interval = max(10, min(60, timeout // 3))
        return timeout, interval

    def ports_per_workspace_cap(self) -> int:
        """Server-wide ceiling on hosted-app ports per workspace."""
        raw = self.app.state.settings.hosted_ports_per_workspace
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning(
                "KLANGKD_HOSTED_PORTS_PER_WORKSPACE=%r is not an int; "
                "using default %d",
                raw,
                DEFAULT_PORTS_PER_WORKSPACE,
            )
            return DEFAULT_PORTS_PER_WORKSPACE

    def set_idle_timeout(self, seconds: int) -> None:
        """Set the global idle timeout (replaces api mutating module globals)."""
        self.idle_timeout_seconds = seconds
        self.check_interval_seconds = max(10, min(60, seconds // 3))

    # --- Service-session locks (#1188, #1478) ---
    # The per-container firing-lock dict lives here on the registry. It used
    # to live at module scope in terminal.py (terminal.py couldn't import
    # container — circular); #1477 removed that constraint by threading
    # app_state through ensure_service_session, so the registry now owns the
    # dict and terminal reaches it via app_state.container_registry.

    def get_service_session_lock(self, container_id: str) -> asyncio.Lock:
        """Get or create the per-container lock for service-command firing."""
        if container_id not in self._service_session_locks:
            self._service_session_locks[container_id] = asyncio.Lock()
        return self._service_session_locks[container_id]

    def clear_service_session_lock(self, container_id: str) -> None:
        """Drop the per-container firing lock for a torn-down container.

        Called from container teardown so the lock dict does not grow
        unbounded with container churn. Safe to call when no lock exists
        for the id.
        """
        self._service_session_locks.pop(container_id, None)

    def prune_service_session_locks(
        self, active_container_ids: set[str]
    ) -> int:
        """Remove lock entries for containers no longer tracked (#1351).

        Bounds the ``_service_session_locks`` dict against unbounded growth
        from container churn: explicit :func:`clear_service_session_lock`
        calls cover the normal teardown path, but a racing re-bind in
        ``stop_and_remove_container`` can leave an entry whose container is
        gone. This opportunistic sweep removes any entry whose container id
        is no longer in *active_container_ids*.

        Entries whose lock is currently held are skipped: recreating a fresh
        ``asyncio.Lock`` for an in-flight service-command fire would not
        serialize against the held one, reopening the duplicate-window race
        the lock exists to prevent. Returns the number of entries pruned.
        """
        stale = [
            cid
            for cid, lock in self._service_session_locks.items()
            if cid not in active_container_ids and not lock.locked()
        ]
        for cid in stale:
            del self._service_session_locks[cid]
        return len(stale)

    def workspace_id_for(self, container_id: str) -> str | None:
        """Return the workspace_id for a container, or None."""
        return self._cid_to_wsid.get(container_id)

    def _get_workspace_lock(self, workspace_id: str) -> asyncio.Lock:
        """Get or create a per-workspace lock for container operations."""
        if workspace_id not in self._workspace_locks:
            self._workspace_locks[workspace_id] = asyncio.Lock()
        return self._workspace_locks[workspace_id]

    # --- State tracking ---

    def track_activity(
        self,
        container_id: str,
        workspace_id: str,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
    ) -> None:
        state = self.states.get(workspace_id)
        was_new = state is None
        if was_new:
            state = ContainerState(workspace_id, container_id, self.app)
            self.states[workspace_id] = state
        else:
            # Remove old reverse mapping if container changed
            if state.container_id != container_id:
                self._cid_to_wsid.pop(state.container_id, None)
            state.container_id = container_id
        self._cid_to_wsid[container_id] = workspace_id
        state.record_activity()
        # Health-monitoring metadata (#1015).  Always refresh so a
        # config change (or a recreated container) is picked up.
        state.health_check = health_check
        if owner_id is not None:
            state.owner_id = owner_id
        if setup_state is not None:
            state.setup_state = setup_state
        if was_new:
            self._notify_status_changed(workspace_id, True)

    def record_activity(self, container_id: str) -> None:
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            state = self.states.get(ws_id)
            if state:
                state.record_activity()

    def mark_service_started(self, container_id: str) -> None:
        """Reset the startup-grace anchor for a container's service.

        Called by ``terminal.ensure_service_session`` right after it
        launches the service command, so the health monitor's grace
        window is measured from the real start of the service.  No-op
        if the container isn't tracked (e.g. the service session fired
        before the state was registered).
        """
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            state = self.states.get(ws_id)
            if state:
                state.mark_service_started()

    def get_state(self, workspace_id: str) -> ContainerState | None:
        return self.states.get(workspace_id)

    def set_on_workspace_killed(self, callback) -> None:
        self.on_workspace_killed = callback

    def set_on_container_status_changed(self, callback) -> None:
        self.on_container_status_changed = callback

    def _notify_status_changed(self, workspace_id: str, running: bool) -> None:
        if self.on_container_status_changed:
            self.on_container_status_changed(workspace_id, running)

    async def remove_state(self, workspace_id: str) -> None:
        """Remove tracked state for a workspace.

        Serialized under the per-workspace lock (the same one
        :meth:`start_container` holds) so a racing start cannot observe a
        half-cleaned registry (#1258). The per-workspace lock entry is
        deliberately *not* removed here -- see :meth:`stop_and_remove_container`
        for why popping it would reopen the race.
        """
        async with self._get_workspace_lock(workspace_id):
            state = self.states.pop(workspace_id, None)
            if state:
                self._cid_to_wsid.pop(state.container_id, None)
                # Drop the per-container service-firing lock (#1188), then
                # sweep any other entries orphaned by container churn (#1351).
                self.clear_service_session_lock(state.container_id)
                self.prune_service_session_locks(set(self._cid_to_wsid))

    # --- Proxy: PortAllocator ---

    @property
    def port_lock(self) -> asyncio.Lock:
        return self.ports.port_lock

    async def allocate_ports(self, workspace_id: str, count: int) -> list[int]:
        return await self.ports.allocate_ports(workspace_id, count)

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        return await self.ports.get_workspace_ports(workspace_id)

    # --- Proxy: BrowserRouter ---

    @property
    def _browsers(self) -> dict:
        return self.browsers._browsers

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        self.browsers.register_browser(browser_id, workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        return self.browsers.resolve_browser(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        self.browsers.revoke_workspace_browsers(workspace_id)

    def revoke_browser(self, sock: object) -> None:
        self.browsers.revoke_browser(sock)

    # --- Proxy: IdleMonitor ---

    @property
    def cleanup_task(self) -> asyncio.Task | None:
        return self.idle.cleanup_task

    @cleanup_task.setter
    def cleanup_task(self, value: asyncio.Task | None) -> None:
        self.idle.cleanup_task = value

    @property
    def _cleanup_wake(self) -> asyncio.Event | None:  # pragma: no cover
        return self.idle._cleanup_wake

    @_cleanup_wake.setter
    def _cleanup_wake(
        self, value: asyncio.Event | None
    ) -> None:  # pragma: no cover
        self.idle._cleanup_wake = value

    def get_cleanup_wake(self) -> asyncio.Event:
        return self.idle.get_cleanup_wake()

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        self.idle.on_idle_stop(workspace_id, callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        self.idle.remove_idle_callback(workspace_id, callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        self.idle.set_workspace_idle_timeout(workspace_id, seconds)

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        return self.idle.get_workspace_idle_timeout(workspace_id)

    async def cleanup_idle_containers(self) -> None:
        await self.idle.cleanup_idle_containers()

    def start_cleanup_loop(self) -> None:
        self.idle.start_cleanup_loop()

    # --- Proxy: HealthMonitor ---

    @property
    def health_task(self) -> asyncio.Task | None:
        return self.health.health_task

    def start_health_loop(self) -> None:
        self.health.start_health_loop()

    # --- Container lifecycle ---

    async def _bringup(
        self,
        workspace_id: str,
        container_id: str,
        service_command: str | None,
        setup_state: str | None,
    ) -> None:
        """Provision the agent home and fire the service command.

        Called at the single choke point: every freshly-created container
        (the tail of :meth:`start_container`). Idempotent via
        :meth:`terminal.ensure_service_session` (per-container lock +
        window-exists check), so calling this on every fresh create is safe:
        after the first fire it is a no-op. The create-time deferral for
        workspaces whose ``setup.sh`` has not run yet is handled by gating
        on ``setup_state`` -- the CLI sandbox driver marks such workspaces
        ``"pending"`` at create, and the fire lands later once setup
        completes and the WS connect path runs.
        """
        agent_home = await self.app.state.agents.ensure_agent_home(
            workspace_id, container_id
        )
        if not service_command:
            return
        await self.app.state.terminal.ensure_service_session(
            container_id,
            agent_home,
            service_command,
            setup_state=setup_state,
        )

    def _egress_filter(
        self, allowed_domains: list[str] | None
    ) -> tuple[dict[str, str] | None, list[str] | None, list[str] | None]:
        """Build ``(annotations, hooks_dirs, cap_drop)`` for egress (#1365).

        ``(None, None, None)`` when unrestricted (no domains, or netfilter
        disabled). Delegates to ``app.state.netfilter``; defensive for
        test app-states that may not wire it.
        """
        nf = getattr(self.app.state, "netfilter", None)
        if nf is None:
            return None, None, None
        return nf.create_kwargs(allowed_domains)

    async def start_container(
        self,
        workspace_id: str,
        host_path: str,
        home_path: str,
        existing_container_id: str | None = None,
        num_ports: int = DEFAULT_PORTS_PER_WORKSPACE,
        hosting_hostname: str | None = None,
        hosting_proto: str | None = None,
        hosting_base_path: str | None = None,
        image: str | None = None,
        config_path: str | None = None,
        extra_mounts: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        user_id: str | None = None,
        health_check: str | None = None,
        setup_state: str | None = None,
        service_command: str | None = None,
        allowed_domains: list[str] | None = None,
    ) -> tuple[str, str]:
        """Start (or restart) a Pi container for a workspace.

        Returns (container_id, status) where status is one of:
        'connected' (already running), 'restarted', or 'created'.

        Serialized per workspace so concurrent WebSocket connections
        don't race to create the same container.
        """
        async with self._get_workspace_lock(workspace_id):
            return await self._start_container_inner(
                workspace_id,
                host_path,
                home_path,
                existing_container_id=existing_container_id,
                num_ports=num_ports,
                hosting_hostname=hosting_hostname,
                hosting_proto=hosting_proto,
                hosting_base_path=hosting_base_path,
                image=image,
                config_path=config_path,
                extra_mounts=extra_mounts,
                extra_env=extra_env,
                user_id=user_id,
                health_check=health_check,
                setup_state=setup_state,
                service_command=service_command,
                allowed_domains=allowed_domains,
            )

    async def _handle_existing_container(
        self,
        existing_container_id: str,
        workspace_id: str,
        t_start: float,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
    ) -> tuple[str, str] | None:
        """Check an existing container and reuse/remove it.

        Returns ``(container_id, "connected")`` if the container is
        still running, or ``None`` if it was removed (or not found)
        and a new one should be created.
        """
        info = await self.app.state.podman.inspect_container(
            existing_container_id
        )
        t_inspect = time.monotonic()
        logger.info(
            "workspace-open: check if old container still exists "
            "(podman inspect): %.3fs",
            t_inspect - t_start,
        )
        if info is None:
            logger.info(
                "Could not find container %s, creating new one",
                existing_container_id,
            )
            return None
        if info["State"]["Running"]:
            self.track_activity(
                existing_container_id,
                workspace_id,
                health_check=health_check,
                owner_id=owner_id,
                setup_state=setup_state,
            )
            logger.info(
                "workspace-open: DONE — container was already running, "
                "no work needed: %.3fs",
                time.monotonic() - t_start,
            )
            return existing_container_id, "connected"
        await self.app.state.podman.remove_container(existing_container_id)
        logger.info(
            "workspace-open: delete old stopped container (podman rm): %.3fs",
            time.monotonic() - t_inspect,
        )
        logger.info(
            "Removed stopped container %s for workspace %s, will recreate",
            existing_container_id,
            workspace_id,
        )
        return None

    async def _reconcile_ports(
        self, workspace_id: str, num_ports: int
    ) -> list[int]:
        """Allocate or trim host ports under the port lock.

        ``num_ports`` is clamped down to the server-wide cap
        (``KLANGKD_HOSTED_PORTS_PER_WORKSPACE``). At cap 0 every workspace
        releases all of its allocations; the returned empty list then
        suppresses the hosting env in :meth:`_build_env` (#1237).
        """
        num_ports = min(num_ports, self.ports_per_workspace_cap())
        async with self.port_lock:
            host_ports = await self.app.state.model.ports.get_workspace_ports(
                workspace_id
            )
            if len(host_ports) < num_ports:
                new_ports = (
                    await self.app.state.model.ports.find_and_allocate_ports(
                        workspace_id,
                        num_ports - len(host_ports),
                        self.port_range_start,
                    )
                )
                host_ports.extend(new_ports)
            elif len(host_ports) > num_ports:
                excess = host_ports[num_ports:]
                await self.app.state.model.ports.remove_port_allocations(
                    workspace_id, excess
                )
                host_ports = host_ports[:num_ports]
        return host_ports

    def _build_env(
        self,
        workspace_id: str,
        host_ports: list[int],
        hosting_hostname: str | None,
        hosting_proto: str | None,
        hosting_base_path: str | None,
        agent_home: str,
        extra_env: dict[str, str] | None,
        ssl_dir: str | None = None,
    ) -> list[str]:
        """Build the container environment variable list.

        ``hosting_hostname``/``hosting_proto``/``hosting_base_path`` are
        optional: callers with a live request pass the values they derived
        from its headers (``wshandler.connection``), and callers without one
        (``start_workspace`` — autostart/create, no connection yet) pass
        ``None``. Resolving the floor here, at the single choke point, means
        no start path can bypass the override: when a caller omits them we
        derive the env / bare-localhost floor via ``derive_hosting_info``
        (the same resolver the request paths use), so a deployer's
        ``KLANGKD_HOSTING_HOSTNAME`` is honored on every start — eager or not.
        """
        if (
            hosting_hostname is None
            or hosting_proto is None
            or hosting_base_path is None
        ):
            h, p, b = self.app.state.util.derive_hosting_info(None, None)
            # Use ``is None`` (not ``or``): an explicit empty base_path
            # (root deployment) is a legitimate value that must survive,
            # not be clobbered by the resolved floor.
            if hosting_hostname is None:
                hosting_hostname = h
            if hosting_proto is None:
                hosting_proto = p
            if hosting_base_path is None:
                hosting_base_path = b
        env_vars: list[str] = []
        egress_port = self.app.state.settings.egress_port
        proxy_url = f"http://host.containers.internal:{egress_port}/llm-proxy"
        llm_model = self.app.state.settings.llm_model
        env_vars.append(f"KLANGKWS_LLM_PROXY_URL={proxy_url}")
        if llm_model:
            env_vars.append(f"KLANGKWS_LLM_MODEL={llm_model}")
        env_vars.append("PI_SKIP_VERSION_CHECK=1")
        logger.info(
            "Container LLM proxy: %s (model: %s)",
            proxy_url,
            llm_model,
        )

        # Hosted-app serving env. Omit entirely when the workspace has
        # no host ports (KLANGKD_HOSTED_PORTS_PER_WORKSPACE=0, or a
        # per-workspace value of 0): KLANGKWS_PORT_MAPPINGS absent makes
        # klangk-hosted-url / get_hosted_url error out cleanly, and the
        # KLANGKWS_HOSTING_* vars are meaningless without hosting. #1237
        if host_ports:
            mappings = [
                f"{CONTAINER_PORT_START + i}:{hp}"
                for i, hp in enumerate(host_ports)
            ]
            env_vars.append(f"KLANGKWS_PORT_MAPPINGS={','.join(mappings)}")
            env_vars.append(f"KLANGKWS_HOSTING_HOSTNAME={hosting_hostname}")
            env_vars.append(f"KLANGKWS_HOSTING_PROTO={hosting_proto}")
            env_vars.append(f"KLANGKWS_HOSTING_BASE_PATH={hosting_base_path}")
        env_vars.append(f"KLANGKWS_WORKSPACE_ID={workspace_id}")
        env_vars.append(f"KLANGKWS_AGENT_HOME={agent_home}")
        env_vars.append(
            f"KLANGKWS_BRIDGE_URL=http://host.containers.internal:{egress_port}"
        )
        if self.terminal_banner:
            env_vars.append(f"KLANGKWS_TERMINAL_BANNER={self.terminal_banner}")

        # Runtime SSL/CA trust (#1181): point OpenSSL/Python/curl/Node
        # at the bundle the entrypoint builds from the mounted certs.
        # Appended before feature/extra env so a deployer can override if
        # ever needed. Emitted only when a trustable cert dir is present.
        env_vars.extend(ssl_env_vars(ssl_dir))

        for k, v in self.app.state.features.container_env().items():
            env_vars.append(f"{k}={v}")

        if extra_env:
            for k, v in extra_env.items():
                env_vars.append(f"{k}={v}")

        return env_vars

    async def _ensure_volumes(
        self,
        extra_mounts: list[str] | None,
        user_id: str | None,
        podman,
    ) -> None:
        """Create named volumes and validate bind-mount sources."""
        if not extra_mounts:
            return
        for mount_spec in extra_mounts:
            source = mount_spec.split(":")[0]
            if _is_named_volume(source):
                info = await podman.inspect_volume(source)
                if info is None:
                    labels = {
                        "klangk.managed": "true",
                        "klangk.instance": self.app.state.util.instance_id(),
                    }
                    if user_id:
                        labels["klangk.user-id"] = user_id
                    await podman.create_volume(source, labels)
                else:
                    vol_labels = info.get("Labels") or {}
                    if (
                        vol_labels.get("klangk.instance")
                        != self.app.state.util.instance_id()
                    ):
                        raise ValueError(
                            f"Volume {source!r} is not managed "
                            "by this klangk instance"
                        )
                    vol_owner = vol_labels.get("klangk.user-id")
                    if vol_owner and user_id and vol_owner != user_id:
                        raise ValueError(
                            f"Volume {source!r} belongs to another user"
                        )
            elif not os.path.exists(source):
                raise ValueError(f"Bind mount source does not exist: {source}")

    @staticmethod
    def _build_mounts(
        home_path: str,
        config_path: str | None,
        extra_mounts: list[str] | None,
        ssl_dir: str | None = None,
    ) -> list[str]:
        """Build the bind-mount list for the container."""
        binds = [f"{home_path}:/home"]
        if config_path:
            binds.append(f"{config_path}:/opt/klangk/config:ro")
        if ssl_dir:
            # Read-only mount of deployer CA certs (#1181).
            binds.append(f"{ssl_dir}:{_SSL_MOUNT_DEST}:ro")
        binds += extra_mounts or []
        return binds

    async def _create_and_start(
        self,
        container_name: str,
        resolved_image: str,
        workspace_id: str,
        publish: list[tuple[int, int]],
        allow_sudo: bool,
        create_kwargs: dict,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
    ) -> str:
        """Create the container, persist it, start it, and configure it.

        Handles port-conflict retries by removing stale containers
        that hold conflicting ports.
        """
        t_create = time.monotonic()
        cid = await self.app.state.podman.create_container(
            container_name, resolved_image, **create_kwargs
        )
        logger.info(
            "workspace-open: create container image (podman create): %.3fs",
            time.monotonic() - t_create,
        )
        await self.app.state.model.workspaces.update_workspace_container(
            workspace_id, cid
        )
        self.track_activity(
            cid,
            workspace_id,
            health_check=health_check,
            owner_id=owner_id,
            setup_state=setup_state,
        )
        t_podman_start = time.monotonic()
        try:
            await self.app.state.podman.start_container(cid)
        except podman.PodmanError as exc:
            if "port is already allocated" not in exc.message:
                raise
            await self._resolve_port_conflict(
                cid, container_name, publish, self.app.state.podman
            )
            await self.app.state.podman.start_container(cid)
        logger.info(
            "workspace-open: boot container (podman start): %.3fs",
            time.monotonic() - t_podman_start,
        )

        # Configure sudo inside the container.
        if allow_sudo:
            sudoers_rule = "klangk ALL=(ALL) NOPASSWD:ALL"
        else:
            sudoers_rule = "klangk ALL=(ALL) !ALL"
        await self.app.state.podman.exec_container(
            cid,
            ["klangk-configure-sudo", sudoers_rule],
            user="root",
        )

        # Write the workspace token so container processes can
        # authenticate without an env-var restart.
        workspace_token = self.app.state.auth.create_workspace_token(
            workspace_id
        )
        await self.app.state.terminal.set_workspace_token(cid, workspace_token)

        # Block until the entrypoint's one-time setup is done. ``podman
        # start`` returns when the entrypoint has *begun*, not finished;
        # the sentinel below is created only after the on-entrypoint hooks
        # complete. Waiting here means every caller of start_container —
        # terminals, exec, agent, health check — gets a genuine readiness
        # guarantee regardless of shell, closing the race that previously
        # only the in-bashrc gate covered (and only for bash).
        await self.app.state.podman.wait_for_container_ready(cid)

        return cid

    async def _resolve_port_conflict(
        self,
        cid: str,
        container_name: str,
        publish: list[tuple[int, int]],
        podman,
    ) -> None:
        """Remove stale containers holding conflicting ports."""
        logger.warning(
            "Port conflict starting %s, cleaning stale containers",
            container_name,
        )
        wanted_ports = {hp for hp, _cp in publish}
        stale = await podman.list_containers(
            f"klangk.instance={self.app.state.util.instance_id()}"
        )
        for c in stale:
            stale_id = c.get("Id") or c.get("ID", "")
            if stale_id == cid:
                continue
            info = await podman.inspect_container(stale_id)
            if info is None:
                continue
            bindings = info.get("HostConfig", {}).get("PortBindings") or {}
            bound = set()
            for ports_list in bindings.values():
                for entry in ports_list or []:
                    try:
                        bound.add(int(entry["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
            if bound & wanted_ports:
                try:
                    await podman.remove_container(stale_id)
                    logger.info(
                        "Removed stale container %s (ports %s)",
                        stale_id[:12],
                        bound & wanted_ports,
                    )
                except PodmanError as del_exc:
                    logger.warning(
                        "Could not remove stale container %s: %s",
                        stale_id[:12],
                        del_exc,
                    )

    async def _start_container_inner(
        self,
        workspace_id: str,
        host_path: str,
        home_path: str,
        existing_container_id: str | None = None,
        num_ports: int = DEFAULT_PORTS_PER_WORKSPACE,
        hosting_hostname: str | None = None,
        hosting_proto: str | None = None,
        hosting_base_path: str | None = None,
        image: str | None = None,
        config_path: str | None = None,
        extra_mounts: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        user_id: str | None = None,
        health_check: str | None = None,
        setup_state: str | None = None,
        service_command: str | None = None,
        allowed_domains: list[str] | None = None,
    ) -> tuple[str, str]:
        """Inner implementation of start_container (called under lock)."""
        t_start = time.monotonic()
        resolved_image = image or self.image_name
        if resolved_image not in self.allowed_images:
            raise ValueError(
                f"Image {resolved_image!r} is not in the allowed "
                f"list: {sorted(self.allowed_images)}"
            )

        # Reuse a running container or remove a stopped one.
        if existing_container_id:
            result = await self._handle_existing_container(
                existing_container_id,
                workspace_id,
                t_start,
                health_check=health_check,
                owner_id=user_id,
                setup_state=setup_state,
            )
            if result is not None:
                return result

        # Allocate host ports.
        t_ports = time.monotonic()
        host_ports = await self._reconcile_ports(workspace_id, num_ports)
        logger.info(
            "workspace-open: allocate host ports from DB: %.3fs",
            time.monotonic() - t_ports,
        )

        # Build environment and mounts.
        t_env = time.monotonic()
        # Resolve the agent home at this async seam (``_build_env`` is
        # sync) so every exec process inherits KLANGKWS_AGENT_HOME (#1157).
        agent_home = f"/home/{await self.app.state.model.users.agent_handle()}"
        ssl_dir = self.app.state.ssl_trust.ssl_cert_dir()
        if ssl_dir:
            logger.info(
                "Runtime SSL trust enabled: mounting %s at %s",
                ssl_dir,
                _SSL_MOUNT_DEST,
            )
        env_vars = self._build_env(
            workspace_id,
            host_ports,
            hosting_hostname,
            hosting_proto,
            hosting_base_path,
            agent_home,
            extra_env,
            ssl_dir,
        )
        await self._ensure_volumes(
            extra_mounts, user_id, self.app.state.podman
        )
        binds = self._build_mounts(
            home_path, config_path, extra_mounts, ssl_dir
        )

        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]
        iid = self.app.state.util.instance_id()
        container_name = f"klangk-{iid}-{workspace_id[:12]}"
        allow_sudo = self.app.state.settings.allow_sudo.strip().lower() in (
            "1",
            "true",
            "yes",
        )

        create_kwargs = dict(
            labels={
                "klangk.managed": "true",
                "klangk.instance": iid,
                "klangk.workspace-id": workspace_id,
            },
            binds=binds,
            tmpfs={
                "/tmp": "rw,exec,nosuid,size=2g",
                "/run": "rw,noexec,nosuid,size=256m",
                "/var/log": "rw,noexec,nosuid,size=256m",
            },
            publish=publish,
            add_hosts=["host.containers.internal:host-gateway"],
            dns=self.container_dns_config() or None,
            env=env_vars,
            init=True,
            interactive=True,
            userns=self.app.state.settings.userns,
            pull=self.image_pull_policy(),
        )

        # Per-workspace egress filtering (#1365): add the OCI annotation
        # + --hooks-dir only when the workspace declares allowed_domains
        # AND netfilter is enabled, so unrestricted workspaces keep
        # podman's default hooks-dir behavior (no behavior change). The
        # klangk hooks dir is passed alongside the standard default hook
        # dirs (#1770 — --hooks-dir overrides, not appends, so the
        # standard dirs are repeated to keep operator createContainer
        # hooks running). The filtered container also drops NET_ADMIN
        # (#1773) so the entrypoint can't flush the ruleset.
        annotations, hooks_dirs, cap_drop = self._egress_filter(
            allowed_domains
        )
        if annotations is not None:
            create_kwargs["annotations"] = annotations
            create_kwargs["hooks_dir"] = hooks_dirs
            create_kwargs["cap_drop"] = cap_drop

        logger.info(
            "workspace-open: build env vars, volumes, and "
            "container config: %.3fs",
            time.monotonic() - t_env,
        )

        # Shield create+start from cancellation so a dropped
        # WebSocket doesn't orphan a running container.
        container_id = await asyncio.shield(
            self._create_and_start(
                container_name,
                resolved_image,
                workspace_id,
                publish,
                allow_sudo,
                create_kwargs,
                health_check=health_check,
                owner_id=user_id,
                setup_state=setup_state,
            )
        )

        # Fresh create: provision the agent home and fire the service
        # command (#1244). This is the single choke point -- every
        # start path (boot autostart, create, connect, klangk restart)
        # routes through start_container, so the bring-up runs once per
        # fresh container regardless of caller. ensure_service_session
        # is idempotent, and setup_state gates the create-time deferral
        # for workspaces whose setup.sh has not run yet.
        await self._bringup(
            workspace_id, container_id, service_command, setup_state
        )
        logger.info(
            "workspace-open: DONE — new container created and started: %.3fs",
            time.monotonic() - t_start,
        )
        logger.info(
            "Started container %s for workspace %s (ports %s)",
            container_id,
            workspace_id,
            host_ports,
        )
        return container_id, "created"

    async def stop_and_remove_container(self, container_id: str) -> None:
        """Stop and remove a container.

        The slow ``self.app.state.podman.remove_container`` call runs *outside* the
        workspace lock; only the registry-state teardown is serialized --
        under the same per-workspace lock :meth:`start_container` uses -- so
        a concurrent start for the same workspace cannot observe a
        half-cleaned registry (#1258). Under the lock we re-check that
        ``container_id`` still maps to this workspace: a racing
        ``start_container`` may already have re-bound the workspace to a
        fresh container, in which case we must not tear down the new state
        (or revoke its browsers).

        The per-workspace lock entry is deliberately *not* popped. Popping
        it while another coroutine is waiting on (or holding) that exact
        ``asyncio.Lock`` would let a subsequent ``_get_workspace_lock``
        create a brand-new lock object that does not serialize against the
        in-flight one -- reopening the very race this method exists to
        prevent. The retained entry is a single small object per workspace
        ever seen and is cleared on process restart.
        """
        try:
            await self.app.state.podman.remove_container(container_id)
            logger.info("Stopped container %s", container_id)
        except podman.PodmanError as e:
            logger.warning(
                "Failed to stop container %s: %s",
                container_id,
                e,
            )
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            async with self._get_workspace_lock(ws_id):
                # Re-verify under the lock: a racing start_container may
                # have re-bound this workspace to a new container while we
                # waited for the lock. Only tear down state we still own.
                if self._cid_to_wsid.get(container_id) == ws_id:
                    self._cid_to_wsid.pop(container_id, None)
                    self.revoke_workspace_browsers(ws_id)
                    self.states.pop(ws_id, None)
        # Drop the per-container service-firing lock (#1188), then sweep any
        # other entries orphaned by container churn (e.g. a racing re-bind
        # that popped this container's mapping before teardown) (#1351).
        self.clear_service_session_lock(container_id)
        self.prune_service_session_locks(set(self._cid_to_wsid))

    async def notify_workspace_killed(self, workspace_id: str) -> None:
        """Call the on_workspace_killed callback, logging any errors.

        Must be called **before** ``stop_and_remove_container`` so that
        ``self.states`` still contains the workspace state needed to emit
        the terminal ``service_health`` death frame.
        """
        self._notify_status_changed(workspace_id, False)
        # Close the container-death hole (#1175 item 2): emit a terminal
        # ``running=False`` frame so consumers watching only service_health
        # learn the service is down.  Only health-checked workspaces ever
        # appeared on the stream, so only those get a terminal frame.
        state = self.states.get(workspace_id)
        if state is not None and state.health_check is not None:
            self.health.broadcast_death(state)
        if self.on_workspace_killed:
            try:
                await self.on_workspace_killed(workspace_id)
            except Exception as e:
                logger.error(
                    "Workspace killed callback error for %s: %s",
                    workspace_id,
                    e,
                )

    async def stop_user_containers(self, user_id: str) -> None:
        """Stop all containers for a user (called on logout)."""
        workspaces = await self.app.state.model.workspaces.get_user_workspaces_with_containers(
            user_id
        )
        for ws in workspaces:
            if ws["container_id"]:
                await self.notify_workspace_killed(ws["id"])
                await self.stop_and_remove_container(ws["container_id"])

    # --- Pre-warm ---

    async def prewarm_podman(self) -> None:
        """Run a throwaway container create+rm to warm podman caches.

        The very first ``podman create`` with ``--userns=keep-id`` in a
        session can take ~20-30s while podman initialises storage,
        user-namespace mappings, and network helpers.  Paying that cost
        here (during backend startup) keeps it off the path where a
        user is waiting.
        """
        t0 = time.monotonic()
        try:
            cid = await self.app.state.podman.create_container(
                "klangk-prewarm",
                self.image_name,
                pull="never",
                userns=self.app.state.settings.userns,
            )
            await self.app.state.podman.remove_container(cid)
            logger.info("Podman pre-warmed in %.3fs", time.monotonic() - t0)
        except podman.PodmanError as e:
            logger.warning(
                "Podman pre-warm failed (%.3fs): %s", time.monotonic() - t0, e
            )

    # --- Startup reap ---

    async def reap_instance_containers(self) -> None:
        """Remove every container labelled with this instance's ID.

        Runs early in :func:`startup`, before any workspace is tracked, so
        every leftover container from a crashed/killed previous run is
        reaped unconditionally -- there is nothing to "adopt" because the
        in-memory registry starts empty.  ``auto_start_workspaces`` then
        recreates the ones that should be running.

        Safe even when klangkd itself runs inside a container: the
        ``klangk.instance`` label filter scopes removal to containers
        *this instance* created, so an unrelated host container (or a
        container created by an outer klangkd with a different instance
        ID) is never touched (#1556).
        """
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
        except (podman.PodmanError, OSError) as e:
            logger.warning("Error scanning for leftover containers: %s", e)
            return
        for c in containers:
            cid = c.get("Id") or c.get("ID", "")
            if not cid:
                continue
            logger.info("Reaping leftover container %s on startup", cid[:12])
            try:
                await self.app.state.podman.remove_container(cid)
            except podman.PodmanError as e:
                logger.warning(
                    "Failed to reap leftover container %s: %s", cid[:12], e
                )

    # --- Shutdown ---

    async def shutdown(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
            self.cleanup_task = None
        if self.health_task:
            self.health_task.cancel()
            try:
                await self.health_task
            except asyncio.CancelledError:
                pass
            self.health.health_task = None
        tracked_ids = set(self._cid_to_wsid.keys())
        tasks = [self.stop_and_remove_container(cid) for cid in tracked_ids]
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
            for c in containers:
                cid = c.get("Id") or c.get("ID", "")
                if cid not in tracked_ids:
                    logger.info(
                        "Removing orphaned klangk container %s",
                        cid,
                    )
                    tasks.append(self.stop_and_remove_container(cid))
        except (
            podman.PodmanError,
            OSError,
        ) as e:
            logger.warning("Error listing orphaned containers: %s", e)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
