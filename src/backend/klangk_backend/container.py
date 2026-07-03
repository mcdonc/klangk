"""Container lifecycle management: start, stop, idle timeout, port allocation."""

import asyncio
import logging
import os
import time

from . import auth, model, plugins, podman, terminal, util

logger = logging.getLogger(__name__)


def container_dns_config() -> list[str]:
    """Return DNS server list from KLANGK_DNS_SERVERS env var.

    Set KLANGK_DNS_SERVERS to a comma-separated list of DNS server IPs
    (e.g., "100.100.100.100,8.8.8.8" for Tailscale MagicDNS + Google).
    Returns an empty list if not configured.
    """
    raw = util.resolve_env_value("KLANGK_DNS_SERVERS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


IMAGE_NAME = util.resolve_env_value("KLANGK_IMAGE_NAME", "klangk-workspace")
INSTANCE_ID = util.resolve_env_value("KLANGK_INSTANCE_ID", "default")

TERMINAL_BANNER = util.resolve_env_value("KLANGK_TERMINAL_BANNER", "")

_allowed_images_env = util.resolve_env_value("KLANGK_ALLOWED_IMAGES", "")
ALLOWED_IMAGES: set[str] = {
    img.strip() for img in _allowed_images_env.split(",") if img.strip()
}
ALLOWED_IMAGES.add(IMAGE_NAME)  # default image is always allowed

_VALID_PULL_POLICIES = {"never", "missing", "always", "newer"}


def image_pull_policy() -> str:
    """Resolve the workspace-image pull policy from the environment.

    Default ``never`` keeps airgapped behavior (image must already exist
    locally).  Set ``KLANGK_IMAGE_PULL_POLICY=missing`` to pull from a
    registry when the image isn't present.
    """
    policy = util.resolve_env_value("KLANGK_IMAGE_PULL_POLICY", "never")
    if policy not in _VALID_PULL_POLICIES:
        logger.warning(
            "Invalid KLANGK_IMAGE_PULL_POLICY=%r (valid: %s); using 'never'.",
            policy,
            ", ".join(sorted(_VALID_PULL_POLICIES)),
        )
        return "never"
    return policy


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


_allowed_mount_roots_env = util.resolve_env_value(
    "KLANGK_ALLOWED_MOUNT_ROOTS", ""
)
ALLOWED_MOUNT_ROOTS: list[str] = [
    os.path.realpath(p.strip())
    for p in _allowed_mount_roots_env.split(",")
    if p.strip()
]

_PROTECTED_PATHS = [
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
]


def _is_named_volume(source: str) -> bool:
    """A mount source with no '/' that doesn't start with '.' is a volume."""
    return "/" not in source and not source.startswith(".")


def _is_protected(source: str) -> bool:
    """True if source is a protected host path that must never be mounted.

    Uses ``os.path.realpath`` to resolve symlinks — a symlink pointing
    to a protected path (e.g. Docker socket) is still blocked.
    """
    resolved = os.path.realpath(source)
    data_dir = os.path.realpath(
        util.resolve_env_value(
            "KLANGK_DATA_DIR", os.path.expanduser("~/.klangk/data")
        )
        or os.path.expanduser("~/.klangk/data")
    )
    for blocked in [*_PROTECTED_PATHS, data_dir]:
        blocked = os.path.realpath(blocked)
        if resolved == blocked or resolved.startswith(blocked + "/"):
            return True
    return False


def validate_mount_spec(spec: str) -> str | None:
    """Validate a container mount spec string.

    Returns None if valid, or an error message string if invalid.
    Valid forms: source:dest or source:dest:options
    The container path (dest) must be absolute.
    """
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
        if _is_protected(source):
            return f"Invalid mount {spec!r}: source is a protected host path"
        if ALLOWED_MOUNT_ROOTS:
            resolved = os.path.realpath(source)
            if not any(
                resolved == root or resolved.startswith(root + "/")
                for root in ALLOWED_MOUNT_ROOTS
            ):
                allowed = ", ".join(ALLOWED_MOUNT_ROOTS)
                return (
                    f"Invalid mount {spec!r}: bind mount source must be "
                    f"under an allowed root ({allowed})"
                )
    return None


def validate_mounts(mounts: list[str]) -> str | None:
    """Validate a list of mount specs. Returns first error or None."""
    for spec in mounts:
        error = validate_mount_spec(spec)
        if error:
            return error
    return None


def parse_idle_timeout() -> tuple[int, int]:
    default = 30 * 60
    env_val = util.resolve_env_value("KLANGK_IDLE_TIMEOUT_SECONDS")
    if env_val is not None:
        try:
            timeout = int(env_val)
        except ValueError:
            logger.warning(
                "KLANGK_IDLE_TIMEOUT_SECONDS=%r is not a valid integer, "
                "using default %d",
                env_val,
                default,
            )
            timeout = default
    else:
        timeout = default
    interval = max(10, min(60, timeout // 3))
    return timeout, interval


IDLE_TIMEOUT_SECONDS, CHECK_INTERVAL_SECONDS = parse_idle_timeout()


# --- Runtime SSL/CA certificate injection (#1181) -------------------------
# The detection (ssl_cert_dir), container env vars (ssl_env_vars) and the
# in-container mount/bundle paths live in :mod:`ssl_trust`, which also owns
# the backend-process trust path.  See that module for the full design.
from .ssl_trust import (  # noqa: E402
    SSL_MOUNT_DEST as _SSL_MOUNT_DEST,
    ssl_cert_dir,
    ssl_env_vars,
)

PORT_RANGE_START = int(
    util.resolve_env_value("KLANGK_PORT_RANGE_START") or "9000"
)
CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5

# Health-check polling interval (seconds) for HealthMonitor.  See #1015.
HEALTH_CHECK_INTERVAL_SECONDS = int(
    util.resolve_env_value("KLANGK_HEALTH_CHECK_INTERVAL") or "30"
)
# Per-invocation timeout for a single `podman exec` health check.
HEALTH_CHECK_TIMEOUT_SECONDS = float(
    util.resolve_env_value("KLANGK_HEALTH_CHECK_TIMEOUT") or "10"
)
# Startup grace period (seconds).  After a service begins starting,
# failing health checks do NOT count as unhealthy until this much time
# has elapsed -- mirroring Docker's HEALTHCHECK `--start-period`.  The
# service command (a dev server, AI gateway, daemon) needs time to boot
# after it's launched; without a grace window the very first poll fires
# while it's still coming up and false-flags the workspace unhealthy
# (e.g. "Gateway not yet ready to accept connections").  A *healthy*
# result is still recorded immediately, so a fast-booting service shows
# up as healthy as soon as it actually responds.  The window is anchored
# to when the service command fires (see ``mark_service_started``) and
# falls back to when the container state was first tracked, so
# health-checked workspaces with no service command also get a grace.
HEALTH_CHECK_STARTUP_GRACE_SECONDS = float(
    util.resolve_env_value("KLANGK_HEALTH_CHECK_STARTUP_GRACE") or "30"
)
# Maximum bytes of check output retained as the failure reason (#1088).
# A bounded tail keeps a verbose check from growing memory unbounded
# across many workspaces while still capturing *why* it failed.
HEALTH_MESSAGE_MAX_BYTES = 512


class ContainerState:
    """Per-workspace container lifecycle state."""

    def __init__(self, workspace_id: str, container_id: str):
        self.workspace_id = workspace_id
        self.container_id = container_id
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
        return IDLE_TIMEOUT_SECONDS


class PortAllocator:
    """Port allocation for workspace containers.

    Owns the ``port_lock`` and delegates to ``model`` for DB-backed
    port tracking.  Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self) -> None:
        self.port_lock: asyncio.Lock = asyncio.Lock()

    async def allocate_ports(self, workspace_id: str, count: int) -> list[int]:
        async with self.port_lock:
            return await model.find_and_allocate_ports(
                workspace_id, count, PORT_RANGE_START
            )

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        return await model.get_workspace_ports(workspace_id)


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

    def __init__(self, registry: "ContainerRegistry") -> None:
        self._registry = registry
        self.cleanup_task: asyncio.Task | None = None
        self._cleanup_wake: asyncio.Event | None = None

    def get_cleanup_wake(self) -> asyncio.Event:
        if self._cleanup_wake is None:
            self._cleanup_wake = asyncio.Event()
        return self._cleanup_wake

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        state = self._registry.states.get(workspace_id)
        if state:
            state.idle_callbacks.append(callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        state = self._registry.states.get(workspace_id)
        if state and callback in state.idle_callbacks:
            state.idle_callbacks.remove(callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        state = self._registry.states.get(workspace_id)
        if state:
            state.idle_timeout = seconds
            self.get_cleanup_wake().set()

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        state = self._registry.states.get(workspace_id)
        if state:
            return state.get_idle_timeout()
        return IDLE_TIMEOUT_SECONDS

    async def cleanup_idle_containers(self) -> None:
        while True:
            timeouts = [
                s.idle_timeout
                for s in self._registry.states.values()
                if s.idle_timeout is not None
            ]
            if timeouts:
                interval = max(2, min(timeouts) // 2)
            else:
                interval = CHECK_INTERVAL_SECONDS
            wake = self.get_cleanup_wake()
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            now = time.time()
            to_stop = []
            for ws_id, state in list(self._registry.states.items()):
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
                state = self._registry.states.get(wid)
                if state:
                    for cb in list(state.idle_callbacks):
                        try:
                            await cb(wid)
                        except Exception as e:
                            logger.error("Idle callback error: %s", e)
                await self._registry.stop_and_remove_container(cid)
                await self._registry.notify_workspace_killed(wid)

    def start_cleanup_loop(self) -> None:
        logger.info(
            "Instance: %s, idle timeout: %ds, check interval: %ds",
            INSTANCE_ID,
            IDLE_TIMEOUT_SECONDS,
            CHECK_INTERVAL_SECONDS,
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

    def __init__(self, registry: "ContainerRegistry") -> None:
        self._registry = registry
        self.health_task: asyncio.Task | None = None

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
            < HEALTH_CHECK_STARTUP_GRACE_SECONDS
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
        ``eager_start_workspace``) and invokes the check via
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
        handle = await model.get_user_handle(owner_id)
        if not handle:
            return "unhealthy", f"owner {owner_id} has no handle"
        # Resolve the owner's container home the same way
        # eager_start_workspace does, so the check runs in the right
        # HOME rather than as root in /.
        from . import workspaces as _wm  # noqa: allow-deferred-import

        ws_home = _wm.home_path(owner_id, state.workspace_id)
        user_home, _created = _wm.ensure_home_symlink(
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
            rc, out, err = await podman.exec_container(
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
                timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
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
            self._broadcast(
                state.workspace_id, new_status, state.health_message
            )

    def _broadcast(
        self,
        workspace_id: str,
        status: str,
        message: str | None = None,
    ) -> None:
        """Emit a ``service_health`` event to all connections.

        Fanned out via :meth:`WsState.notify_service_health` so the
        workspace list page learns about health transitions for
        auto-started services even when nobody is connected to the
        workspace's terminal session (#1015).  The failure *reason*
        rides along as ``health_message`` so operators can see *why*
        it's unhealthy without digging through logs (#1088).
        """
        # Imported lazily to avoid an import cycle with wshandler.
        from .wshandler import state as _ws_state  # noqa: allow-deferred-import

        _ws_state.notify_service_health(
            workspace_id, healthy=status == "healthy", message=message
        )

    async def run_health_loop(self) -> None:
        """Background loop: every interval, poll eligible workspaces."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
            for state in list(self._registry.states.values()):
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

    def start_health_loop(self) -> None:
        if self.health_task is None:
            self.health_task = asyncio.create_task(self.run_health_loop())


class ContainerRegistry:
    """Singleton managing all container state and podman interactions.

    Composes :class:`PortAllocator`, :class:`BrowserRouter`,
    :class:`IdleMonitor`, and :class:`HealthMonitor` as collaborators.
    Backward-compatible proxy methods delegate to the collaborators so
    existing callers are unchanged.
    """

    def __init__(self):
        self.states: dict[str, ContainerState] = {}
        # Reverse lookup: container_id -> workspace_id
        self._cid_to_wsid: dict[str, str] = {}
        # Per-workspace locks to serialize container creation.
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self.on_workspace_killed = None
        self.on_container_status_changed = None

        # Collaborators
        self.ports = PortAllocator()
        self.browsers = BrowserRouter()
        self.idle = IdleMonitor(self)
        self.health = HealthMonitor(self)

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
            state = ContainerState(workspace_id, container_id)
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

    def remove_state(self, workspace_id: str) -> None:
        state = self.states.pop(workspace_id, None)
        if state:
            self._cid_to_wsid.pop(state.container_id, None)
            # Drop the per-container service-firing lock (#1188).
            terminal.clear_service_session_lock(state.container_id)
        self._workspace_locks.pop(workspace_id, None)

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
    def _cleanup_wake(self, value: asyncio.Event | None) -> None:
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

    async def start_container(
        self,
        workspace_id: str,
        host_path: str,
        home_path: str,
        existing_container_id: str | None = None,
        num_ports: int = DEFAULT_PORTS_PER_WORKSPACE,
        hosting_hostname: str = "localhost",
        hosting_proto: str = "http",
        hosting_base_path: str = "",
        image: str | None = None,
        config_path: str | None = None,
        extra_mounts: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        user_id: str | None = None,
        health_check: str | None = None,
        setup_state: str | None = None,
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
        info = await podman.inspect_container(existing_container_id)
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
        await podman.remove_container(existing_container_id)
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
        """Allocate or trim host ports under the port lock."""
        async with self.port_lock:
            host_ports = await model.get_workspace_ports(workspace_id)
            if len(host_ports) < num_ports:
                new_ports = await model.find_and_allocate_ports(
                    workspace_id,
                    num_ports - len(host_ports),
                    PORT_RANGE_START,
                )
                host_ports.extend(new_ports)
            elif len(host_ports) > num_ports:
                excess = host_ports[num_ports:]
                await model.remove_port_allocations(workspace_id, excess)
                host_ports = host_ports[:num_ports]
        return host_ports

    def _build_env(
        self,
        workspace_id: str,
        host_ports: list[int],
        hosting_hostname: str,
        hosting_proto: str,
        hosting_base_path: str,
        agent_home: str,
        extra_env: dict[str, str] | None,
        ssl_dir: str | None = None,
    ) -> list[str]:
        """Build the container environment variable list."""
        env_vars: list[str] = []
        nginx_port = util.resolve_env_value("KLANGK_NGINX_PORT", "8995")
        proxy_url = f"http://host.containers.internal:{nginx_port}/llm-proxy"
        llm_model = util.resolve_env_value("KLANGK_LLM_MODEL", "")
        env_vars.append(f"KLANGK_LLM_PROXY_URL={proxy_url}")
        if llm_model:
            env_vars.append(f"KLANGK_LLM_MODEL={llm_model}")
        env_vars.append("PI_SKIP_VERSION_CHECK=1")
        logger.info(
            "Container LLM proxy: %s (model: %s)",
            proxy_url,
            llm_model,
        )

        mappings = [
            f"{CONTAINER_PORT_START + i}:{hp}"
            for i, hp in enumerate(host_ports)
        ]
        env_vars.append(f"KLANGK_PORT_MAPPINGS={','.join(mappings)}")
        env_vars.append(f"KLANGK_WORKSPACE_ID={workspace_id}")
        env_vars.append(f"KLANGK_AGENT_HOME={agent_home}")
        env_vars.append(
            f"KLANGK_BRIDGE_URL=http://host.containers.internal:{nginx_port}"
        )
        env_vars.append(f"KLANGK_HOSTING_HOSTNAME={hosting_hostname}")
        env_vars.append(f"KLANGK_HOSTING_PROTO={hosting_proto}")
        env_vars.append(f"KLANGK_HOSTING_BASE_PATH={hosting_base_path}")
        if TERMINAL_BANNER:
            env_vars.append(f"KLANGK_TERMINAL_BANNER={TERMINAL_BANNER}")

        # Runtime SSL/CA trust (#1181): point OpenSSL/Python/curl/Node
        # at the bundle the entrypoint builds from the mounted certs.
        # Appended before plugin/extra env so a deployer can override if
        # ever needed. Emitted only when a trustable cert dir is present.
        env_vars.extend(ssl_env_vars(ssl_dir))

        for k, v in plugins.container_env().items():
            env_vars.append(f"{k}={v}")

        if extra_env:
            for k, v in extra_env.items():
                env_vars.append(f"{k}={v}")

        return env_vars

    @staticmethod
    async def _ensure_volumes(
        extra_mounts: list[str] | None,
        user_id: str | None,
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
                        "klangk.instance": INSTANCE_ID,
                    }
                    if user_id:
                        labels["klangk.user-id"] = user_id
                    await podman.create_volume(source, labels)
                else:
                    vol_labels = info.get("Labels") or {}
                    if vol_labels.get("klangk.instance") != INSTANCE_ID:
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
        cid = await podman.create_container(
            container_name, resolved_image, **create_kwargs
        )
        logger.info(
            "workspace-open: create container image (podman create): %.3fs",
            time.monotonic() - t_create,
        )
        await model.update_workspace_container(workspace_id, cid)
        self.track_activity(
            cid,
            workspace_id,
            health_check=health_check,
            owner_id=owner_id,
            setup_state=setup_state,
        )
        t_podman_start = time.monotonic()
        try:
            await podman.start_container(cid)
        except podman.PodmanError as exc:
            if "port is already allocated" not in exc.message:
                raise
            await self._resolve_port_conflict(cid, container_name, publish)
            await podman.start_container(cid)
        logger.info(
            "workspace-open: boot container (podman start): %.3fs",
            time.monotonic() - t_podman_start,
        )

        # Configure sudo inside the container.
        if allow_sudo:
            sudoers_rule = "klangk ALL=(ALL) NOPASSWD:ALL"
        else:
            sudoers_rule = "klangk ALL=(ALL) !ALL"
        await podman.exec_container(
            cid,
            ["klangk-configure-sudo", sudoers_rule],
            user="root",
        )

        # Write the workspace token so container processes can
        # authenticate without an env-var restart.
        workspace_token = auth.create_workspace_token(workspace_id)
        await terminal.set_workspace_token(cid, workspace_token)

        # Block until the entrypoint's one-time setup is done. ``podman
        # start`` returns when the entrypoint has *begun*, not finished;
        # the sentinel below is created only after the on-entrypoint hooks
        # complete. Waiting here means every caller of start_container —
        # terminals, exec, agent, health check — gets a genuine readiness
        # guarantee regardless of shell, closing the race that previously
        # only the in-bashrc gate covered (and only for bash).
        await podman.wait_for_container_ready(cid)

        return cid

    @staticmethod
    async def _resolve_port_conflict(
        cid: str,
        container_name: str,
        publish: list[tuple[int, int]],
    ) -> None:
        """Remove stale containers holding conflicting ports."""
        logger.warning(
            "Port conflict starting %s, cleaning stale containers",
            container_name,
        )
        wanted_ports = {hp for hp, _cp in publish}
        stale = await podman.list_containers(f"klangk.instance={INSTANCE_ID}")
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
                except podman.PodmanError as del_exc:
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
        hosting_hostname: str = "localhost",
        hosting_proto: str = "http",
        hosting_base_path: str = "",
        image: str | None = None,
        config_path: str | None = None,
        extra_mounts: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        user_id: str | None = None,
        health_check: str | None = None,
        setup_state: str | None = None,
    ) -> tuple[str, str]:
        """Inner implementation of start_container (called under lock)."""
        t_start = time.monotonic()
        resolved_image = image or IMAGE_NAME
        if resolved_image not in ALLOWED_IMAGES:
            raise ValueError(
                f"Image {resolved_image!r} is not in the allowed "
                f"list: {sorted(ALLOWED_IMAGES)}"
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
        # sync) so every exec process inherits KLANGK_AGENT_HOME (#1157).
        agent_home = f"/home/{await model.agent_handle()}"
        ssl_dir = ssl_cert_dir()
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
        await self._ensure_volumes(extra_mounts, user_id)
        binds = self._build_mounts(
            home_path, config_path, extra_mounts, ssl_dir
        )

        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]
        container_name = f"klangk-{INSTANCE_ID}-{workspace_id[:12]}"
        allow_sudo = util.resolve_env_bool("KLANGK_ALLOW_SUDO")

        create_kwargs = dict(
            labels={
                "klangk.managed": "true",
                "klangk.instance": INSTANCE_ID,
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
            dns=container_dns_config() or None,
            env=env_vars,
            init=True,
            interactive=True,
            userns=util.resolve_env_value(
                "KLANGK_USERNS", "keep-id:uid=1000,gid=1000"
            ),
            pull=image_pull_policy(),
        )

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
        """Stop and remove a container."""
        try:
            await podman.remove_container(container_id)
            logger.info("Stopped container %s", container_id)
        except podman.PodmanError as e:
            logger.warning(
                "Failed to stop container %s: %s",
                container_id,
                e,
            )
        ws_id = self._cid_to_wsid.pop(container_id, None)
        if ws_id:
            self.revoke_workspace_browsers(ws_id)
            self.states.pop(ws_id, None)
            self._workspace_locks.pop(ws_id, None)
        # Drop the per-container service-firing lock (#1188).
        terminal.clear_service_session_lock(container_id)

    async def notify_workspace_killed(self, workspace_id: str) -> None:
        """Call the on_workspace_killed callback, logging any errors."""
        self._notify_status_changed(workspace_id, False)
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
        workspaces = await model.get_user_workspaces_with_containers(user_id)
        for ws in workspaces:
            if ws["container_id"]:
                await self.stop_and_remove_container(ws["container_id"])
                await self.notify_workspace_killed(ws["id"])

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
            cid = await podman.create_container(
                "klangk-prewarm",
                IMAGE_NAME,
                pull="never",
                userns=util.resolve_env_value(
                    "KLANGK_USERNS", "keep-id:uid=1000,gid=1000"
                ),
            )
            await podman.remove_container(cid)
            logger.info("Podman pre-warmed in %.3fs", time.monotonic() - t0)
        except podman.PodmanError as e:
            logger.warning(
                "Podman pre-warm failed (%.3fs): %s", time.monotonic() - t0, e
            )

    # --- Orphan adoption ---

    async def adopt_orphaned_containers(self) -> None:
        try:
            containers = await podman.list_containers(
                f"klangk.instance={INSTANCE_ID}"
            )
            for c in containers:
                cid = c.get("Id") or c.get("ID", "")
                if cid not in self._cid_to_wsid:
                    logger.info(
                        "Removing orphaned container %s on startup",
                        cid[:12],
                    )
                    try:
                        await podman.remove_container(cid)
                    except podman.PodmanError as e:
                        logger.warning(
                            "Failed to remove orphaned container %s: %s",
                            cid[:12],
                            e,
                        )
        except (
            podman.PodmanError,
            OSError,
        ) as e:
            logger.warning("Error scanning for orphaned containers: %s", e)

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
        # Skip container cleanup when running inside a container
        # (developing klangk in klangk -- don't kill our own container).
        if os.path.exists("/.dockerenv") or os.path.exists(
            "/run/.containerenv"
        ):
            logger.info("Running inside container, skipping container cleanup")
            return
        tracked_ids = set(self._cid_to_wsid.keys())
        tasks = [self.stop_and_remove_container(cid) for cid in tracked_ids]
        try:
            containers = await podman.list_containers(
                f"klangk.instance={INSTANCE_ID}"
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


# Module-level singleton
registry = ContainerRegistry()
