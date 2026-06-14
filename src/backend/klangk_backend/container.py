"""Container lifecycle management: start, stop, idle timeout, port allocation."""

import asyncio
import logging
import os
import time
import uuid

from . import auth, model, podman, util

logger = logging.getLogger(__name__)


def _container_dns_config() -> list[str]:
    """Return DNS server list from KLANGK_DNS_SERVERS env var.

    Set KLANGK_DNS_SERVERS to a comma-separated list of DNS server IPs
    (e.g., "100.100.100.100,8.8.8.8" for Tailscale MagicDNS + Google).
    Returns an empty list if not configured.
    """
    raw = util.resolve_env_secret("KLANGK_DNS_SERVERS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


IMAGE_NAME = util.resolve_env_secret("KLANGK_IMAGE_NAME", "klangk-workspace")
INSTANCE_ID = util.resolve_env_secret("KLANGK_INSTANCE_ID", "default")

_allowed_images_env = util.resolve_env_secret("KLANGK_ALLOWED_IMAGES", "")
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
    policy = util.resolve_env_secret("KLANGK_IMAGE_PULL_POLICY", "never")
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


_allowed_mount_roots_env = util.resolve_env_secret(
    "KLANGK_ALLOWED_MOUNT_ROOTS", ""
)
ALLOWED_MOUNT_ROOTS: list[str] = [
    os.path.normpath(p)
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
    """True if source is a protected host path that must never be mounted."""
    norm = os.path.normpath(source)
    data_dir = os.path.normpath(
        util.resolve_env_secret(
            "KLANGK_DATA_DIR", os.path.expanduser("~/.klangk/data")
        )
        or os.path.expanduser("~/.klangk/data")
    )
    for blocked in [*_PROTECTED_PATHS, data_dir]:
        blocked = os.path.normpath(blocked)
        if norm == blocked or norm.startswith(blocked + "/"):
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
            norm = os.path.normpath(source)
            if not any(
                norm == root or norm.startswith(root + "/")
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
    env_val = util.resolve_env_secret("KLANGK_IDLE_TIMEOUT_SECONDS")
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

PORT_RANGE_START = int(
    util.resolve_env_secret("KLANGK_PORT_RANGE_START") or "9000"
)
CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5


class ContainerState:
    """Per-workspace container lifecycle state."""

    def __init__(self, workspace_id: str, container_id: str):
        self.workspace_id = workspace_id
        self.container_id = container_id
        self.last_activity = time.time()
        self.idle_timeout: int | None = None
        self.idle_callbacks: list = []

    def record_activity(self) -> None:
        self.last_activity = time.time()

    def get_idle_timeout(self) -> int:
        if self.idle_timeout is not None:
            return self.idle_timeout
        return IDLE_TIMEOUT_SECONDS


class ContainerRegistry:
    """Singleton managing all container state and podman interactions."""

    def __init__(self):
        self.states: dict[str, ContainerState] = {}
        # Reverse lookup: container_id -> workspace_id
        self._cid_to_wsid: dict[str, str] = {}
        # Bridge token -> (workspace_id, sock_or_none) for browser-delegate auth.
        self._bridge_tokens: dict[str, tuple[str, object | None]] = {}
        self.cleanup_task: asyncio.Task | None = None
        self.port_lock: asyncio.Lock = asyncio.Lock()
        # Per-workspace locks to serialize container creation.
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self.on_workspace_killed = None
        self._cleanup_wake: asyncio.Event | None = None

    def _get_workspace_lock(self, workspace_id: str) -> asyncio.Lock:
        """Get or create a per-workspace lock for container operations."""
        if workspace_id not in self._workspace_locks:
            self._workspace_locks[workspace_id] = asyncio.Lock()
        return self._workspace_locks[workspace_id]

    def get_cleanup_wake(self) -> asyncio.Event:
        if self._cleanup_wake is None:
            self._cleanup_wake = asyncio.Event()
        return self._cleanup_wake

    # --- State tracking ---

    def track_activity(self, container_id: str, workspace_id: str) -> None:
        state = self.states.get(workspace_id)
        if state is None:
            state = ContainerState(workspace_id, container_id)
            self.states[workspace_id] = state
        else:
            # Remove old reverse mapping if container changed
            if state.container_id != container_id:  # pragma: no cover
                self._cid_to_wsid.pop(state.container_id, None)
            state.container_id = container_id
        self._cid_to_wsid[container_id] = workspace_id
        state.record_activity()

    def record_activity(self, container_id: str) -> None:
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            state = self.states.get(ws_id)
            if state:
                state.record_activity()

    def create_bridge_token(self, workspace_id: str, sock: object) -> str:
        """Generate a unique token that maps to (workspace_id, sock).

        Each terminal exec session gets its own token so browser-delegate
        requests route to the specific browser connection that owns the
        terminal.
        """
        token = str(uuid.uuid4())
        self._bridge_tokens[token] = (workspace_id, sock)
        return token

    def resolve_bridge_token(self, token: str) -> tuple[str, object] | None:
        """Look up (workspace_id, sock) for a bridge token."""
        return self._bridge_tokens.get(token)

    def revoke_bridge_token(self, workspace_id: str) -> None:
        """Remove ALL bridge tokens for a workspace.

        Called when a container is recreated or stopped.
        """
        to_remove = [
            t
            for t, (ws, _s) in self._bridge_tokens.items()
            if ws == workspace_id
        ]
        for t in to_remove:
            del self._bridge_tokens[t]

    def revoke_connection_token(self, sock: object) -> None:
        """Remove all bridge tokens bound to a specific connection."""
        to_remove = [
            t for t, (_ws, s) in self._bridge_tokens.items() if s is sock
        ]
        for t in to_remove:
            del self._bridge_tokens[t]

    def get_state(self, workspace_id: str) -> ContainerState | None:
        return self.states.get(workspace_id)

    # --- Idle callbacks ---

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        state = self.states.get(workspace_id)
        if state:
            state.idle_callbacks.append(callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        state = self.states.get(workspace_id)
        if state and callback in state.idle_callbacks:
            state.idle_callbacks.remove(callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        state = self.states.get(workspace_id)
        if state:
            state.idle_timeout = seconds
            self.get_cleanup_wake().set()

    def set_on_workspace_killed(self, callback) -> None:
        self.on_workspace_killed = callback

    def remove_state(self, workspace_id: str) -> None:
        state = self.states.pop(workspace_id, None)
        if state:
            self._cid_to_wsid.pop(state.container_id, None)

    # --- Port allocation ---

    async def allocate_ports(self, workspace_id: str, count: int) -> list[int]:
        async with self.port_lock:
            return await model.find_and_allocate_ports(
                workspace_id, count, PORT_RANGE_START
            )

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        state = self.states.get(workspace_id)
        if state:
            return state.get_idle_timeout()
        return IDLE_TIMEOUT_SECONDS

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        return await model.get_workspace_ports(workspace_id)

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
    ) -> tuple[str, str]:
        """Inner implementation of start_container (called under lock)."""
        t_start = time.monotonic()
        resolved_image = image or IMAGE_NAME
        if resolved_image not in ALLOWED_IMAGES:
            raise ValueError(
                f"Image {resolved_image!r} is not in the allowed list: "
                f"{sorted(ALLOWED_IMAGES)}"
            )

        if existing_container_id:
            info = await podman.inspect_container(existing_container_id)
            t_inspect = time.monotonic()
            logger.info(
                "workspace-open: check if old container still exists (podman inspect): %.3fs",
                t_inspect - t_start,
            )
            if info is None:
                logger.info(
                    "Could not find container %s, creating new one",
                    existing_container_id,
                )
            elif info["State"]["Running"]:
                self.track_activity(existing_container_id, workspace_id)
                logger.info(
                    "workspace-open: DONE — container was already running, no work needed: %.3fs",
                    time.monotonic() - t_start,
                )
                return existing_container_id, "connected"
            else:
                await podman.remove_container(existing_container_id)
                logger.info(
                    "workspace-open: delete old stopped container (podman rm): %.3fs",
                    time.monotonic() - t_inspect,
                )
                logger.info(
                    "Removed stopped container %s for workspace %s, "
                    "will recreate",
                    existing_container_id,
                    workspace_id,
                )

        # Lock the entire read+allocate sequence to prevent
        # concurrent start_container calls from double-allocating.
        t_ports_start = time.monotonic()
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

        logger.info(
            "workspace-open: allocate host ports from DB: %.3fs",
            time.monotonic() - t_ports_start,
        )

        t_env_start = time.monotonic()
        env_vars = []
        nginx_port = util.resolve_env_secret("KLANGK_NGINX_PORT", "8995")
        proxy_url = f"http://host.containers.internal:{nginx_port}/llm-proxy"
        llm_model = util.resolve_env_secret("KLANGK_LLM_MODEL", "")
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
        env_vars.append(
            f"KLANGK_BRIDGE_URL=http://host.containers.internal:{nginx_port}"
        )
        env_vars.append(f"KLANGK_HOSTING_HOSTNAME={hosting_hostname}")
        env_vars.append(f"KLANGK_HOSTING_PROTO={hosting_proto}")
        env_vars.append(f"KLANGK_HOSTING_BASE_PATH={hosting_base_path}")
        workspace_token = auth.create_workspace_token(workspace_id)
        env_vars.append(f"KLANGK_WORKSPACE_TOKEN={workspace_token}")

        if extra_env:
            for k, v in extra_env.items():
                env_vars.append(f"{k}={v}")

        # Ensure named volumes in extra_mounts exist with klangk labels.
        # Refuse to mount a volume owned by another instance or user.
        if extra_mounts:
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
                                f"Volume {source!r} is not managed by this "
                                "klangk instance"
                            )
                        vol_owner = vol_labels.get("klangk.user-id")
                        if vol_owner and user_id and vol_owner != user_id:
                            raise ValueError(
                                f"Volume {source!r} belongs to another user"
                            )

        binds = [
            f"{home_path}:/home",
        ]
        if config_path:
            binds.append(f"{config_path}:/opt/klangk/config:ro")
        binds += extra_mounts or []

        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]

        container_name = f"klangk-{INSTANCE_ID}-{workspace_id[:12]}"

        # Shield the create+persist+start sequence from cancellation.
        # The connecting client's websocket can drop mid-startup (idle
        # ping-timeout, navigation), which cancels this coroutine.
        # Without the shield, a cancel landing between create and the
        # DB write orphans a running container with no container_id on
        # record.
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
            dns=_container_dns_config() or None,
            env=env_vars,
            init=True,
            interactive=True,
            userns=util.resolve_env_secret(
                "KLANGK_USERNS", "keep-id:uid=1000,gid=1000"
            ),
            pull=image_pull_policy(),
        )

        logger.info(
            "workspace-open: build env vars, volumes, and container config: %.3fs",
            time.monotonic() - t_env_start,
        )

        async def _create_and_start() -> str:
            t_create = time.monotonic()
            cid = await podman.create_container(
                container_name, resolved_image, **create_kwargs
            )
            logger.info(
                "workspace-open: create container image (podman create): %.3fs",
                time.monotonic() - t_create,
            )
            await model.update_workspace_container(workspace_id, cid)
            self.track_activity(cid, workspace_id)
            t_podman_start = time.monotonic()
            try:
                await podman.start_container(cid)
            except podman.PodmanError as exc:
                if "port is already allocated" not in exc.message:
                    raise
                # A stale container is holding one of our ports.
                # Find managed containers binding conflicting ports
                # and remove them, then retry.
                logger.warning(
                    "Port conflict starting %s, cleaning stale containers",
                    container_name,
                )
                wanted_ports = {hp for hp, _cp in publish}
                stale = await podman.list_containers(
                    f"klangk.instance={INSTANCE_ID}"
                )
                for c in stale:
                    stale_id = c.get("Id") or c.get("ID", "")
                    if stale_id == cid:
                        continue
                    info = await podman.inspect_container(stale_id)
                    if info is None:
                        continue
                    bindings = (
                        info.get("HostConfig", {}).get("PortBindings") or {}
                    )
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
                            logger.info(
                                "Could not remove stale container %s: %s",
                                stale_id[:12],
                                del_exc,
                            )
                await podman.start_container(cid)
            logger.info(
                "workspace-open: boot container (podman start): %.3fs",
                time.monotonic() - t_podman_start,
            )
            return cid

        container_id = await asyncio.shield(_create_and_start())

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
            self.revoke_bridge_token(ws_id)
            self.states.pop(ws_id, None)

    async def _notify_workspace_killed(self, workspace_id: str) -> None:
        """Call the on_workspace_killed callback, logging any errors."""
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
                await self._notify_workspace_killed(ws["id"])

    # --- Idle cleanup loop ---

    async def cleanup_idle_containers(self) -> None:
        while True:
            timeouts = [
                s.idle_timeout
                for s in self.states.values()
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
            for ws_id, state in list(self.states.items()):
                timeout = state.get_idle_timeout()
                idle_secs = now - state.last_activity
                logger.debug(
                    "Idle check: %s idle %.0fs / %ds",
                    state.container_id[:12],
                    idle_secs,
                    timeout,
                )
                if idle_secs > timeout:
                    to_stop.append((state.container_id, ws_id))

            for cid, wid in to_stop:
                logger.info(
                    "Stopping idle container %s (workspace %s)",
                    cid,
                    wid,
                )
                state = self.states.get(wid)
                if state:
                    for cb in list(state.idle_callbacks):
                        try:
                            await cb(wid)
                        except Exception as e:
                            logger.error("Idle callback error: %s", e)
                await self.stop_and_remove_container(cid)
                await self._notify_workspace_killed(wid)

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

    # --- Pre-warm ---

    async def prewarm_podman(self) -> None:
        """Run a throwaway container create+rm to warm podman caches.

        The very first ``podman create`` in a session can take ~20s while
        podman initialises storage, user-namespace mappings, and network
        helpers.  Paying that cost here (during backend startup) keeps it
        off the path where a user is waiting.
        """
        t0 = time.monotonic()
        try:
            cid = await podman.create_container(
                "klangk-prewarm", IMAGE_NAME, pull="never"
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
                    labels = c.get("Labels") or {}
                    workspace_id = labels.get("klangk.workspace-id", "unknown")
                    self.track_activity(cid, workspace_id)
                    logger.info(
                        "Adopted orphaned container %s (workspace %s)",
                        cid[:12],
                        workspace_id,
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
