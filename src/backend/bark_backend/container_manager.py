"""Container lifecycle management: start, stop, idle timeout, port allocation."""

import asyncio
import logging
import time
import uuid

import aiodocker

from . import util, model

logger = logging.getLogger(__name__)

IMAGE_NAME = util.resolve_env_secret("BARK_IMAGE_NAME", "bark-pi")
INSTANCE_ID = util.resolve_env_secret("BARK_INSTANCE_ID", "default")

_allowed_images_env = util.resolve_env_secret("BARK_ALLOWED_IMAGES", "")
ALLOWED_IMAGES: set[str] = {
    img.strip() for img in _allowed_images_env.split(",") if img.strip()
}
ALLOWED_IMAGES.add(IMAGE_NAME)  # default image is always allowed


def parse_idle_timeout() -> tuple[int, int]:
    default = 30 * 60
    env_val = util.resolve_env_secret("BARK_IDLE_TIMEOUT_SECONDS")
    if env_val is not None:
        try:
            timeout = int(env_val)
        except ValueError:
            logger.warning(
                "BARK_IDLE_TIMEOUT_SECONDS=%r is not a valid integer, "
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

PORT_RANGE_START = 9000
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
        self.connection_count = 0

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
        # Bridge token → workspace_id (for browser-delegate auth)
        self._bridge_tokens: dict[str, str] = {}
        self.docker: aiodocker.Docker | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.port_lock: asyncio.Lock = asyncio.Lock()
        self.on_workspace_killed = None
        self._cleanup_wake: asyncio.Event | None = None

    def get_cleanup_wake(self) -> asyncio.Event:
        if self._cleanup_wake is None:
            self._cleanup_wake = asyncio.Event()
        return self._cleanup_wake

    async def get_docker(self) -> aiodocker.Docker:  # pragma: no cover
        if self.docker is None:
            self.docker = aiodocker.Docker()
        return self.docker

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

    def create_bridge_token(self, workspace_id: str) -> str:
        """Generate a unique token that maps to a workspace_id.

        Revokes any existing tokens for this workspace first.
        """
        self.revoke_bridge_token(workspace_id)
        token = str(uuid.uuid4())
        self._bridge_tokens[token] = workspace_id
        return token

    def resolve_bridge_token(self, token: str) -> str | None:
        """Look up the workspace_id for a bridge token."""
        return self._bridge_tokens.get(token)

    def revoke_bridge_token(self, workspace_id: str) -> None:
        """Remove all bridge tokens for a workspace."""
        to_remove = [
            t for t, ws in self._bridge_tokens.items() if ws == workspace_id
        ]
        for t in to_remove:
            del self._bridge_tokens[t]

    def get_state(self, workspace_id: str) -> ContainerState | None:
        return self.states.get(workspace_id)

    def add_connection(self, workspace_id: str) -> int:
        state = self.states.get(workspace_id)
        if state:
            state.connection_count += 1
            return state.connection_count
        return 1  # pragma: no cover

    def remove_connection(self, workspace_id: str) -> int:
        state = self.states.get(workspace_id)
        if state:
            state.connection_count = max(0, state.connection_count - 1)
            return state.connection_count
        return 0

    def connection_count(self, workspace_id: str) -> int:
        state = self.states.get(workspace_id)
        return state.connection_count if state else 0

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
        resume_session: str | None = None,
        num_ports: int = DEFAULT_PORTS_PER_WORKSPACE,
        hosting_hostname: str = "localhost",
        hosting_proto: str = "http",
        hosting_base_path: str = "",
        image: str | None = None,
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

        docker = await self.get_docker()

        if existing_container_id:
            try:
                container = await docker.containers.get(existing_container_id)
                info = await container.show()
                if info["State"]["Running"]:
                    self.track_activity(existing_container_id, workspace_id)
                    return existing_container_id, "connected"
                await container.delete(force=True)
                logger.info(
                    "Removed stopped container %s for workspace %s, "
                    "will recreate",
                    existing_container_id,
                    workspace_id,
                )
            except aiodocker.exceptions.DockerError:
                logger.info(
                    "Could not find container %s, creating new one",
                    existing_container_id,
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
        nginx_port = util.resolve_env_secret("BARK_NGINX_PORT", "8995")
        proxy_url = f"http://host.docker.internal:{nginx_port}/llm-proxy"
        llm_model = util.resolve_env_secret("LLM_MODEL", "")
        env_vars.append(f"LLM_PROXY_URL={proxy_url}")
        if llm_model:
            env_vars.append(f"LLM_MODEL={llm_model}")
        env_vars.append("PI_SKIP_VERSION_CHECK=1")
        logger.info(
            "Container LLM proxy: %s (model: %s)",
            proxy_url,
            llm_model,
        )

        logfire_token = util.resolve_env_secret("LOGFIRE_TOKEN")
        if logfire_token:
            logfire_base = util.resolve_env_secret(
                "LOGFIRE_BASE_URL",
                "https://logfire-api.pydantic.dev",
            )
            env_vars.append(f"OTEL_EXPORTER_OTLP_ENDPOINT={logfire_base}")
            env_vars.append(
                "OTEL_EXPORTER_OTLP_HEADERS="
                f"Authorization=Bearer {logfire_token}"
            )
            env_vars.append("OTEL_SERVICE_NAME=bark-pi-agent")
            logfire_env = util.resolve_env_secret("LOGFIRE_ENVIRONMENT")
            if logfire_env:
                env_vars.append(
                    "OTEL_RESOURCE_ATTRIBUTES="
                    f"deployment.environment={logfire_env}"
                )

        mappings = [
            f"{CONTAINER_PORT_START + i}:{hp}"
            for i, hp in enumerate(host_ports)
        ]
        env_vars.append(f"BARK_PORT_MAPPINGS={','.join(mappings)}")
        env_vars.append(f"BARK_WORKSPACE_ID={workspace_id}")
        bridge_token = self.create_bridge_token(workspace_id)
        env_vars.append(
            f"BARK_BRIDGE_URL=http://host.docker.internal:{nginx_port}"
        )
        env_vars.append(f"BARK_BRIDGE_TOKEN={bridge_token}")
        env_vars.append(f"BARK_HOSTING_HOSTNAME={hosting_hostname}")
        env_vars.append(f"BARK_HOSTING_PROTO={hosting_proto}")
        env_vars.append(f"BARK_HOSTING_BASE_PATH={hosting_base_path}")
        if resume_session:
            env_vars.append(f"BARK_RESUME_SESSION={resume_session}")

        port_bindings = {}
        exposed_ports = {}
        for i, host_port in enumerate(host_ports):
            container_port = CONTAINER_PORT_START + i
            port_key = f"{container_port}/tcp"
            exposed_ports[port_key] = {}
            port_bindings[port_key] = [{"HostPort": str(host_port)}]

        config = {
            "Image": resolved_image,
            "Labels": {
                "bark.managed": "true",
                "bark.instance": INSTANCE_ID,
                "bark.workspace-id": workspace_id,
            },
            "HostConfig": {
                "Init": True,
                "ReadonlyRootfs": True,
                "Binds": [
                    f"{host_path}:/work",
                    f"{home_path}:/home/bark",
                ],
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,size=256m",
                    "/run": "rw,noexec,nosuid,size=16m",
                    "/var/log": "rw,noexec,nosuid,size=16m",
                },
                "PortBindings": port_bindings,
                "ExtraHosts": ["host.docker.internal:host-gateway"],
            },
            "ExposedPorts": exposed_ports,
            "Env": env_vars,
            "OpenStdin": True,
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
        }

        container = await docker.containers.create_or_replace(
            name=f"bark-{INSTANCE_ID}-{workspace_id[:12]}",
            config=config,
        )
        await container.start()
        container_id = container.id

        await model.update_workspace_container(workspace_id, container_id)
        self.track_activity(container_id, workspace_id)

        logger.info(
            "Started container %s for workspace %s (ports %s)",
            container_id,
            workspace_id,
            host_ports,
        )
        return container_id, "created"

    async def attach_container(
        self, container_id: str
    ) -> aiodocker.stream.Stream:
        """Attach to container stdin/stdout for Pi RPC communication."""
        docker = await self.get_docker()
        container = await docker.containers.get(container_id)
        stream = container.attach(
            stdin=True, stdout=True, stderr=True, stream=True
        )
        return stream

    async def stop_and_remove_container(self, container_id: str) -> None:
        """Stop and remove a container."""
        docker = await self.get_docker()
        try:
            container = await docker.containers.get(container_id)
            await container.delete(force=True)
            logger.info("Stopped container %s", container_id)
        except aiodocker.exceptions.DockerError as e:
            logger.warning(
                "Failed to stop container %s: %s",
                container_id,
                e,
            )
        ws_id = self._cid_to_wsid.pop(container_id, None)
        if ws_id:
            self.revoke_bridge_token(ws_id)
            self.states.pop(ws_id, None)

    async def stop_user_containers(self, user_id: str) -> None:
        """Stop all containers for a user (called on logout)."""
        workspaces = await model.get_user_workspaces_with_containers(user_id)
        for ws in workspaces:
            if ws["container_id"]:
                await self.stop_and_remove_container(ws["container_id"])
                if self.on_workspace_killed:
                    try:
                        await self.on_workspace_killed(ws["id"])
                    except Exception as e:  # pragma: no cover
                        logger.error(
                            "Workspace killed callback error for %s: %s",
                            ws["id"],
                            e,
                        )

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
                logger.info(
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
                if self.on_workspace_killed:
                    try:
                        await self.on_workspace_killed(wid)
                    except Exception as e:
                        logger.error(
                            "Workspace killed callback error for %s: %s",
                            wid,
                            e,
                        )

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
            docker = await self.get_docker()
            containers = await docker.containers.list(
                filters={"label": [f"bark.instance={INSTANCE_ID}"]},
            )
            for c in containers:
                if c.id not in self._cid_to_wsid:
                    labels = (await c.show())["Config"]["Labels"]
                    workspace_id = labels.get("bark.workspace-id", "unknown")
                    self.track_activity(c.id, workspace_id)
                    logger.info(
                        "Adopted orphaned container %s (workspace %s)",
                        c.id[:12],
                        workspace_id,
                    )
        except (
            aiodocker.exceptions.DockerError,
            OSError,
        ) as e:
            logger.warning("Error scanning for orphaned containers: %s", e)

    # --- Shutdown ---

    async def shutdown(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
            self.cleanup_task = None
        tracked_ids = set(self._cid_to_wsid.keys())
        tasks = [self.stop_and_remove_container(cid) for cid in tracked_ids]
        try:
            docker = await self.get_docker()
            containers = await docker.containers.list(
                filters={"label": [f"bark.instance={INSTANCE_ID}"]},
            )
            for c in containers:
                if c.id not in tracked_ids:
                    logger.info(
                        "Removing orphaned bark container %s",
                        c.id,
                    )
                    tasks.append(c.delete(force=True))
        except (
            aiodocker.exceptions.DockerError,
            OSError,
        ) as e:
            logger.warning("Error listing orphaned containers: %s", e)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.docker:
            await self.docker.close()
            self.docker = None


# Module-level singleton
registry = ContainerRegistry()
