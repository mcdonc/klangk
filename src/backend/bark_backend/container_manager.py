import asyncio
import logging
import time

import aiodocker

from . import env_util, user_store

logger = logging.getLogger(__name__)

IMAGE_NAME = env_util.resolve_env_secret("BARK_IMAGE_NAME", "bark-pi")
INSTANCE_ID = env_util.resolve_env_secret("BARK_INSTANCE_ID", "default")


def parse_idle_timeout() -> tuple[int, int]:
    """Parse BARK_IDLE_TIMEOUT_SECONDS and compute check interval.

    Returns (idle_timeout_seconds, check_interval_seconds).
    """
    default = 30 * 60
    env_val = env_util.resolve_env_secret("BARK_IDLE_TIMEOUT_SECONDS")
    if env_val is not None:
        try:
            timeout = int(env_val)
        except ValueError:
            logger.warning(
                "BARK_IDLE_TIMEOUT_SECONDS=%r is not a valid integer, using default %d",
                env_val,
                default,
            )
            timeout = default
    else:
        timeout = default
    interval = max(10, min(60, timeout // 3))
    return timeout, interval


IDLE_TIMEOUT_SECONDS, CHECK_INTERVAL_SECONDS = parse_idle_timeout()

# Port allocation
PORT_RANGE_START = 9000
CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5

# Track active containers: container_id -> {last_activity, workspace_id, ports}
_containers: dict[str, dict] = {}
# Callbacks for idle timeout notifications: workspace_id -> [async_callback]
_idle_callbacks: dict[str, list] = {}
# Per-workspace idle timeout overrides: workspace_id -> seconds
_workspace_idle_timeouts: dict[str, int] = {}
_docker: aiodocker.Docker | None = None
_cleanup_task: asyncio.Task | None = None
# Serializes port allocation to prevent races between concurrent workspace
# creations within this process. Sufficient because each Bark instance has
# its own BARK_DATA_DIR/bark.db. If multiple processes ever shared a DB,
# this would need a database-level lock instead.
_port_lock: asyncio.Lock = asyncio.Lock()
# Connection refcount per workspace: workspace_id -> count
# Tracks how many WebSocket connections are using each workspace's container.
_workspace_connections: dict[str, int] = {}
# Called when a workspace's container is killed by idle timeout.
# Set by ws_handler to clean up Pi state. Signature: async (workspace_id) -> None
_on_workspace_killed: object = None
# Signals the cleanup loop to wake up early when a short timeout is set.
# Created lazily to avoid binding to the wrong event loop at import time.
_cleanup_wake: asyncio.Event | None = None


def get_cleanup_wake() -> asyncio.Event:
    global _cleanup_wake
    if _cleanup_wake is None:
        _cleanup_wake = asyncio.Event()
    return _cleanup_wake


async def allocate_ports(workspace_id: str, count: int) -> list[int]:
    """Allocate additional ports for a workspace. Returns the newly allocated port numbers."""
    async with _port_lock:
        return await user_store.find_and_allocate_ports(
            workspace_id, count, PORT_RANGE_START
        )


async def get_workspace_ports(workspace_id: str) -> list[int]:
    """Get allocated ports for a workspace."""
    return await user_store.get_workspace_ports(workspace_id)


async def get_docker() -> aiodocker.Docker:  # pragma: no cover
    global _docker
    if _docker is None:
        _docker = aiodocker.Docker()
    return _docker


async def start_container(
    workspace_id: str,
    host_path: str,
    home_path: str,
    existing_container_id: str | None = None,
    resume_session: str | None = None,
    num_ports: int = DEFAULT_PORTS_PER_WORKSPACE,
    hosting_hostname: str = "localhost",
    hosting_proto: str = "http",
    hosting_base_path: str = "",
) -> tuple[str, str]:
    """Start (or restart) a Pi container for a workspace.

    Returns (container_id, status) where status is one of:
    'connected' (already running), 'restarted', or 'created'.
    """
    docker = await get_docker()

    # Check existing container — if running, reuse; if stopped, remove and recreate
    # (recreate ensures the entrypoint runs fresh, picking up new extensions/AGENTS.md)
    if existing_container_id:
        try:
            container = await docker.containers.get(existing_container_id)
            info = await container.show()
            if info["State"]["Running"]:
                track_activity(existing_container_id, workspace_id)
                return existing_container_id, "connected"
            # Stopped container: remove it so we recreate with fresh entrypoint
            await container.delete(force=True)
            logger.info(
                "Removed stopped container %s for workspace %s, will recreate",
                existing_container_id,
                workspace_id,
            )
        except aiodocker.exceptions.DockerError:
            logger.info(
                "Could not find container %s, creating new one",
                existing_container_id,
            )

    # Ensure workspace has the right number of ports allocated
    host_ports = await get_workspace_ports(workspace_id)
    if len(host_ports) < num_ports:
        new_ports = await allocate_ports(
            workspace_id, num_ports - len(host_ports)
        )
        host_ports.extend(new_ports)
    elif len(host_ports) > num_ports:
        excess = host_ports[num_ports:]
        await user_store.remove_port_allocations(workspace_id, excess)
        host_ports = host_ports[:num_ports]

    # Pass LLM config to the container via the nginx proxy.
    # The proxy injects the API key, so containers never see it.
    env_vars = []
    nginx_port = env_util.resolve_env_secret("BARK_NGINX_PORT", "8995")
    proxy_url = f"http://host.docker.internal:{nginx_port}/llm-proxy"
    llm_model = env_util.resolve_env_secret("LLM_MODEL", "")
    env_vars.append(f"LLM_PROXY_URL={proxy_url}")
    if llm_model:
        env_vars.append(f"LLM_MODEL={llm_model}")
    env_vars.append("PI_SKIP_VERSION_CHECK=1")
    logger.info(
        "Container LLM proxy: %s (model: %s)",
        proxy_url,
        llm_model,
    )
    # Pass Logfire/OTEL config so the Pi otel-telemetry extension can
    # send traces to the same Logfire project as the backend.
    logfire_token = env_util.resolve_env_secret("LOGFIRE_TOKEN")
    if logfire_token:
        logfire_base = env_util.resolve_env_secret(
            "LOGFIRE_BASE_URL", "https://logfire-api.pydantic.dev"
        )
        env_vars.append(f"OTEL_EXPORTER_OTLP_ENDPOINT={logfire_base}")
        env_vars.append(
            f"OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer {logfire_token}"
        )
        env_vars.append("OTEL_SERVICE_NAME=bark-pi-agent")
        logfire_env = env_util.resolve_env_secret("LOGFIRE_ENVIRONMENT")
        if logfire_env:
            env_vars.append(
                f"OTEL_RESOURCE_ATTRIBUTES="
                f"deployment.environment={logfire_env}"
            )

    # Tell the container the port mappings (container_port:host_port pairs)
    mappings = [
        f"{CONTAINER_PORT_START + i}:{hp}" for i, hp in enumerate(host_ports)
    ]
    env_vars.append(f"BARK_PORT_MAPPINGS={','.join(mappings)}")
    env_vars.append(f"BARK_WORKSPACE_ID={workspace_id}")
    env_vars.append(f"BARK_HOSTING_HOSTNAME={hosting_hostname}")
    env_vars.append(f"BARK_HOSTING_PROTO={hosting_proto}")
    env_vars.append(f"BARK_HOSTING_BASE_PATH={hosting_base_path}")
    if resume_session:
        env_vars.append(f"BARK_RESUME_SESSION={resume_session}")

    # Build port bindings: map well-known container ports to allocated host ports
    port_bindings = {}
    exposed_ports = {}
    for i, host_port in enumerate(host_ports):
        container_port = CONTAINER_PORT_START + i
        port_key = f"{container_port}/tcp"
        exposed_ports[port_key] = {}
        port_bindings[port_key] = [{"HostPort": str(host_port)}]

    config = {
        "Image": IMAGE_NAME,
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

    await user_store.update_workspace_container(workspace_id, container_id)
    track_activity(container_id, workspace_id)

    logger.info(
        "Started container %s for workspace %s (ports %s)",
        container_id,
        workspace_id,
        host_ports,
    )
    return container_id, "created"


async def attach_container(container_id: str) -> aiodocker.stream.Stream:
    """Attach to container stdin/stdout for Pi RPC communication."""
    docker = await get_docker()
    container = await docker.containers.get(container_id)
    stream = container.attach(
        stdin=True, stdout=True, stderr=True, stream=True
    )
    return stream


async def stop_and_remove_container(container_id: str) -> None:
    """Stop and remove a container. Ports are kept allocated for restart."""
    import traceback

    caller = "".join(traceback.format_stack()[-3:-1])
    logger.info(
        "stop_and_remove_container(%s) called from:\n%s",
        container_id[:12],
        caller,
    )
    docker = await get_docker()
    try:
        container = await docker.containers.get(container_id)
        # force=True sends SIGKILL immediately — no need for a
        # separate stop() which would SIGTERM + wait up to 10s.
        await container.delete(force=True)
        logger.info("Stopped container %s", container_id)
    except aiodocker.exceptions.DockerError as e:
        logger.warning("Failed to stop container %s: %s", container_id, e)

    _containers.pop(container_id, None)


async def stop_user_containers(user_id: str) -> None:
    """Stop all containers for a user (called on logout)."""
    workspaces = await user_store.get_user_workspaces_with_containers(user_id)
    for ws in workspaces:
        if ws["container_id"]:
            await stop_and_remove_container(ws["container_id"])


def track_activity(container_id: str, workspace_id: str) -> None:
    _containers[container_id] = {
        "last_activity": time.time(),
        "workspace_id": workspace_id,
    }


def add_connection(workspace_id: str) -> int:
    """Increment and return the connection count for a workspace."""
    _workspace_connections[workspace_id] = (
        _workspace_connections.get(workspace_id, 0) + 1
    )
    return _workspace_connections[workspace_id]


def remove_connection(workspace_id: str) -> int:
    """Decrement and return the connection count for a workspace."""
    count = _workspace_connections.get(workspace_id, 0)
    if count <= 1:
        _workspace_connections.pop(workspace_id, None)
        return 0
    _workspace_connections[workspace_id] = count - 1
    return count - 1


def connection_count(workspace_id: str) -> int:
    """Return the current connection count for a workspace."""
    return _workspace_connections.get(workspace_id, 0)


def record_activity(container_id: str) -> None:
    """Record activity on a container (called on prompt/steer/follow_up)."""
    if container_id in _containers:
        _containers[container_id]["last_activity"] = time.time()


def set_workspace_idle_timeout(workspace_id: str, seconds: int) -> None:
    """Set a per-workspace idle timeout override."""
    _workspace_idle_timeouts[workspace_id] = seconds
    # Wake the cleanup loop so it picks up the new short timeout immediately
    # instead of waiting for its current (potentially long) sleep to finish.
    get_cleanup_wake().set()


def get_workspace_idle_timeout(workspace_id: str) -> int:
    """Get the idle timeout for a workspace (per-workspace override or global default)."""
    return _workspace_idle_timeouts.get(workspace_id, IDLE_TIMEOUT_SECONDS)


def set_on_workspace_killed(callback) -> None:
    """Register a callback for when a container is killed by idle timeout."""
    global _on_workspace_killed
    _on_workspace_killed = callback


def on_idle_stop(workspace_id: str, callback) -> None:
    """Register a callback to be called when a workspace container is stopped due to idle timeout."""
    _idle_callbacks.setdefault(workspace_id, []).append(callback)


def remove_idle_callback(workspace_id: str, callback) -> None:
    """Remove an idle timeout callback."""
    cbs = _idle_callbacks.get(workspace_id, [])
    if callback in cbs:
        cbs.remove(callback)


async def cleanup_idle_containers() -> None:
    """Periodically stop idle containers."""
    while True:
        # Sleep interval adapts to the shortest active per-workspace timeout
        # so containers with short timeouts are cleaned up promptly without
        # polling too aggressively when only long timeouts are active.
        if _workspace_idle_timeouts:
            min_timeout = min(_workspace_idle_timeouts.values())
            interval = max(2, min_timeout // 2)
        else:
            interval = CHECK_INTERVAL_SECONDS
        # Use Event-based wait so set_workspace_idle_timeout can wake us
        # immediately, even if we're in the middle of a long default sleep.
        wake = get_cleanup_wake()
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        now = time.time()
        to_stop = []
        for cid, info in list(_containers.items()):
            timeout = get_workspace_idle_timeout(info["workspace_id"])
            idle_secs = now - info["last_activity"]
            logger.info(
                "Idle check: %s idle %.0fs / %ds",
                cid[:12],
                idle_secs,
                timeout,
            )
            if idle_secs > timeout:
                to_stop.append((cid, info["workspace_id"]))

        for cid, wid in to_stop:
            logger.info("Stopping idle container %s (workspace %s)", cid, wid)
            # Notify listeners before stopping
            for cb in list(_idle_callbacks.get(wid, [])):
                try:
                    await cb(wid)
                except Exception as e:
                    logger.error("Idle callback error: %s", e)
            await stop_and_remove_container(cid)
            # Clean up shared workspace state (Pi client, refcount, etc.)
            if _on_workspace_killed:
                try:
                    await _on_workspace_killed(wid)
                except Exception as e:
                    logger.error(
                        "Workspace killed callback error for %s: %s", wid, e
                    )


async def adopt_orphaned_containers() -> None:
    """Register any running bark containers left from a previous process.

    After a crash (SIGKILL), containers survive but the in-memory
    tracking is lost.  This scans Docker for containers with our
    instance label and registers them so the idle timeout loop will
    eventually clean them up.
    """
    try:
        docker = await get_docker()
        containers = await docker.containers.list(
            filters={"label": [f"bark.instance={INSTANCE_ID}"]},
        )
        for c in containers:
            if c.id not in _containers:
                labels = (await c.show())["Config"]["Labels"]
                workspace_id = labels.get("bark.workspace-id", "unknown")
                track_activity(c.id, workspace_id)
                logger.info(
                    "Adopted orphaned container %s (workspace %s)",
                    c.id[:12],
                    workspace_id,
                )
    except (aiodocker.exceptions.DockerError, OSError) as e:
        logger.warning("Error scanning for orphaned containers: %s", e)


def start_cleanup_loop() -> None:
    """Start the background cleanup task."""
    global _cleanup_task
    logger.info(
        "Instance: %s, idle timeout: %ds, check interval: %ds",
        INSTANCE_ID,
        IDLE_TIMEOUT_SECONDS,
        CHECK_INTERVAL_SECONDS,
    )
    if _cleanup_task is None:
        _cleanup_task = asyncio.create_task(cleanup_idle_containers())


async def shutdown() -> None:
    """Clean up on app shutdown. Stop all managed containers."""
    global _cleanup_task, _docker
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
    # Stop all tracked containers in parallel
    tasks = [
        stop_and_remove_container(cid) for cid in list(_containers.keys())
    ]
    # Also stop any orphaned bark containers (not in _containers but have our label)
    try:
        docker = await get_docker()
        containers = await docker.containers.list(
            filters={"label": [f"bark.instance={INSTANCE_ID}"]},
        )
        for c in containers:
            if c.id not in _containers:
                logger.info("Removing orphaned bark container %s", c.id)
                tasks.append(c.delete(force=True))
    except (aiodocker.exceptions.DockerError, OSError) as e:
        logger.warning("Error listing orphaned containers: %s", e)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _docker:
        await _docker.close()
        _docker = None
