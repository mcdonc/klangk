"""Container lifecycle management: start, stop, idle timeout, port allocation."""

import asyncio
import logging
import os
import socket
import time
import uuid

from . import model, podman, util

logger = logging.getLogger(__name__)


def _container_dns_config() -> dict:
    """Return Docker DNS config from KLANGK_DNS_SERVERS env var.

    Set KLANGK_DNS_SERVERS to a comma-separated list of DNS server IPs
    (e.g., "100.100.100.100,8.8.8.8" for Tailscale MagicDNS + Google).
    Returns an empty dict if not configured.
    """
    raw = util.resolve_env_secret("KLANGK_DNS_SERVERS", "")
    servers = [s.strip() for s in raw.split(",") if s.strip()]
    if servers:
        return {"Dns": servers}
    return {}


def _resolve_add_host_target(hostname: str) -> str:
    """Return an IP for --add-host, or the 'host-gateway' special token.

    When hostname resolves (e.g. a Docker service name like 'nginx' visible
    via the embedded DNS 127.0.0.11), returns the IP so workspace containers
    can route to it via slirp4netns through the backend's network stack.
    Falls back to the podman/docker 'host-gateway' token for names that don't
    resolve locally (e.g. 'host.docker.internal' outside Docker).
    """
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return "host-gateway"


IMAGE_NAME = util.resolve_env_secret("KLANGK_IMAGE_NAME", "klangk")
INSTANCE_ID = util.resolve_env_secret("KLANGK_INSTANCE_ID", "default")

_allowed_images_env = util.resolve_env_secret("KLANGK_ALLOWED_IMAGES", "")
ALLOWED_IMAGES: set[str] = {
    img.strip() for img in _allowed_images_env.split(",") if img.strip()
}
ALLOWED_IMAGES.add(IMAGE_NAME)  # default image is always allowed

# podman --pull policies; controls where workspace images come from.
_VALID_PULL_POLICIES = {"never", "missing", "always", "newer"}


def image_pull_policy() -> str:
    """Resolve the workspace-image pull policy from the environment.

    Default ``never`` keeps the airgapped behavior (use only the local +
    additional image stores). Set ``KLANGK_IMAGE_PULL_POLICY=missing`` to pull
    from a registry when the image isn't present locally; ``always``/``newer``
    are also accepted. An unrecognized value falls back to ``never``.
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


def validate_mount_spec(spec: str) -> str | None:
    """Validate a Docker mount spec string.

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
    """Singleton managing all container state and Docker interactions."""

    def __init__(self):
        self.states: dict[str, ContainerState] = {}
        # Reverse lookup: container_id → workspace_id
        self._cid_to_wsid: dict[str, str] = {}
        # Bridge token → (workspace_id, sock_or_none) for browser-delegate auth.
        # sock is None for workspace-level fallback tokens, or a specific
        # SafeWebSocket for per-connection tokens.
        self._bridge_tokens: dict[str, tuple[str, object | None]] = {}
        self.cleanup_task: asyncio.Task | None = None
        self.port_lock: asyncio.Lock = asyncio.Lock()
        self.on_workspace_killed = None
        self._cleanup_wake: asyncio.Event | None = None

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
    ) -> tuple[str, str]:
        """Start (or restart) a Pi container for a workspace.

        Returns (container_id, status) where status is one of:
        'connected' (already running), 'restarted', or 'created'.
        """
        resolved_image = image or IMAGE_NAME
        if resolved_image not in ALLOWED_IMAGES:
            raise ValueError(
                f"Image {resolved_image!r} is not in the allowed list: "
                f"{sorted(ALLOWED_IMAGES)}"
            )

        if existing_container_id:
            info = await podman.inspect_container(existing_container_id)
            if info is None:
                logger.info(
                    "Could not find container %s, creating new one",
                    existing_container_id,
                )
            elif info["State"]["Running"]:
                self.track_activity(existing_container_id, workspace_id)
                return existing_container_id, "connected"
            else:
                await podman.remove_container(existing_container_id)
                logger.info(
                    "Removed stopped container %s for workspace %s, "
                    "will recreate",
                    existing_container_id,
                    workspace_id,
                )
        else:
            # DB has no container_id (e.g. after a backend restart that lost
            # in-memory state).  Scan by label so we can adopt a running
            # container rather than blindly --replace it (which hangs trying
            # to stop it).
            containers = await podman.list_containers(
                f"klangk.workspace-id={workspace_id}"
            )
            if containers:
                found_id = containers[0].get("Id") or containers[0].get("ID", "")
                info = await podman.inspect_container(found_id)
                if info and info["State"]["Running"]:
                    logger.info(
                        "Adopted running container %s for workspace %s",
                        found_id,
                        workspace_id,
                    )
                    await model.update_workspace_container(workspace_id, found_id)
                    self.track_activity(found_id, workspace_id)
                    return found_id, "connected"
                elif info:
                    await podman.remove_container(found_id)
                    logger.info(
                        "Removed stopped orphan container %s for workspace %s",
                        found_id,
                        workspace_id,
                    )

        # Lock the entire read+allocate sequence to prevent
        # concurrent start_container calls from double-allocating.
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

        env_vars = []
        nginx_port = util.resolve_env_secret("KLANGK_NGINX_PORT", "8995")
        # Hostname a workspace container uses to reach the nginx front
        # (LLM-proxy + bridge).  Default `host.docker.internal` preserves
        # the host-process / devenv behavior; under the Podman-in-Docker
        # stack with isolated networks this is set to podman's native
        # `host.containers.internal` (or another alias resolvable to the
        # host gateway).  The same name is registered via `--add-host
        # <gateway>:host-gateway` below so it resolves inside the container.
        host_gateway = util.resolve_env_secret(
            "KLANGK_HOST_GATEWAY", "host.docker.internal"
        )
        proxy_url = f"http://{host_gateway}:{nginx_port}/llm-proxy"
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
        # KLANGK_BRIDGE_URL is set at container level so all exec sessions
        # know the backend URL.  KLANGK_BRIDGE_TOKEN is set per-exec
        # session (in terminal.py) so each connection has its own token.
        env_vars.append(
            f"KLANGK_BRIDGE_URL=http://{host_gateway}:{nginx_port}"
        )
        env_vars.append(f"KLANGK_HOSTING_HOSTNAME={hosting_hostname}")
        env_vars.append(f"KLANGK_HOSTING_PROTO={hosting_proto}")
        env_vars.append(f"KLANGK_HOSTING_BASE_PATH={hosting_base_path}")

        # Forward host SSH agent if available
        ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK", "")
        if ssh_auth_sock and os.path.exists(ssh_auth_sock):
            env_vars.append("SSH_AUTH_SOCK=/run/ssh-agent.sock")

        if extra_env:
            for k, v in extra_env.items():
                env_vars.append(f"{k}={v}")

        # Ensure named volumes in extra_mounts exist with klangk labels.
        # Named volumes don't contain / or start with .
        # Everything else is a bind mount, passed through as-is.
        if extra_mounts:
            for mount_spec in extra_mounts:
                source = mount_spec.split(":")[0]
                if "/" not in source and not source.startswith("."):
                    # Named volume — ensure it exists with klangk labels
                    if await podman.inspect_volume(source) is None:
                        await podman.create_volume(
                            source,
                            {
                                "klangk.managed": "true",
                                "klangk.instance": INSTANCE_ID,
                            },
                        )

        binds = [
            f"{home_path}:/home/klangk",
        ]
        if ssh_auth_sock and os.path.exists(ssh_auth_sock):
            binds.append(f"{ssh_auth_sock}:/run/ssh-agent.sock:ro")
        if config_path:
            binds.append(f"{config_path}:/opt/klangk/config:ro")
        binds += extra_mounts or []

        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]

        # Create, persist, and start as one cancellation-shielded unit.
        # The connecting client's websocket can drop mid-startup (idle
        # ping-timeout, navigation), which cancels this coroutine. Without
        # the shield, a cancel landing between `create`/`start` and the DB
        # write orphans a running container with no `container_id` on record
        # — the workspace then appears "stuck" until the adopt-by-label path
        # recovers it on a later connect. Persisting the id immediately after
        # create (before start) also means the container is never lost even
        # if start raises: the next connect inspects it via existing_id and
        # recreates if needed.
        async def _create_and_start() -> str:
            cid = await podman.create_container(
                f"klangk-{INSTANCE_ID}-{workspace_id[:12]}",
                resolved_image,
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
                add_hosts=[
                    f"{host_gateway}:{_resolve_add_host_target(host_gateway)}"
                ],
                dns=_container_dns_config().get("Dns"),
                env=env_vars,
                init=True,
                interactive=True,
                pull=image_pull_policy(),
            )
            await model.update_workspace_container(workspace_id, cid)
            self.track_activity(cid, workspace_id)
            await podman.start_container(cid)
            return cid

        container_id = await asyncio.shield(_create_and_start())

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

    # --- Orphan adoption ---

    async def adopt_orphaned_containers(self) -> None:
        try:
            containers = await podman.list_containers(
                f"klangk.instance={INSTANCE_ID}"
            )
            for c in containers:
                cid = c["Id"]
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
        # (developing klangk in klangk — don't kill our own container).
        if os.path.exists("/.dockerenv"):
            logger.info("Running inside container, skipping container cleanup")
            return
        tracked_ids = set(self._cid_to_wsid.keys())
        tasks = [self.stop_and_remove_container(cid) for cid in tracked_ids]
        try:
            containers = await podman.list_containers(
                f"klangk.instance={INSTANCE_ID}"
            )
            for c in containers:
                cid = c["Id"]
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
