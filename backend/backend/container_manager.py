import asyncio
import logging
import os
import time

import aiodocker

from . import user_store

logger = logging.getLogger(__name__)

IMAGE_NAME = os.environ.get("BARK_IMAGE_NAME", "bark-pi")
INSTANCE_ID = os.environ.get("BARK_INSTANCE_ID", "default")


def _parse_idle_timeout() -> tuple[int, int]:
    """Parse BARK_IDLE_TIMEOUT_SECONDS and compute check interval.

    Returns (idle_timeout_seconds, check_interval_seconds).
    """
    default = 30 * 60
    env_val = os.environ.get("BARK_IDLE_TIMEOUT_SECONDS")
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


IDLE_TIMEOUT_SECONDS, CHECK_INTERVAL_SECONDS = _parse_idle_timeout()

# Port allocation
PORT_RANGE_START = 9000
CONTAINER_PORT_START = 8000
DEFAULT_PORTS_PER_WORKSPACE = 5

# Track active containers: container_id -> {last_activity, workspace_id, ports}
_containers: dict[str, dict] = {}
# Callbacks for idle timeout notifications: workspace_id -> [async_callback]
_idle_callbacks: dict[str, list] = {}
_docker: aiodocker.Docker | None = None
_cleanup_task: asyncio.Task | None = None


async def allocate_ports(workspace_id: str, count: int) -> list[int]:
    """Allocate additional ports for a workspace. Returns the newly allocated port numbers."""
    used = await user_store.get_all_allocated_ports()
    ports = []
    port = PORT_RANGE_START
    while len(ports) < count:
        if port not in used:
            ports.append(port)
        port += 1
    await user_store.add_port_allocations(workspace_id, ports)
    return ports


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
    sessions_path: str,
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
                _track_activity(existing_container_id, workspace_id)
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
                "Could not find container %s, creating new one", existing_container_id
            )

    # Ensure workspace has the right number of ports allocated
    host_ports = await get_workspace_ports(workspace_id)
    if len(host_ports) < num_ports:
        new_ports = await allocate_ports(workspace_id, num_ports - len(host_ports))
        host_ports.extend(new_ports)
    elif len(host_ports) > num_ports:
        excess = host_ports[num_ports:]
        await user_store.remove_port_allocations(workspace_id, excess)
        host_ports = host_ports[:num_ports]

    # Collect API keys from environment to pass into the container
    env_vars = []
    for key in os.environ:
        if key.startswith(
            ("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GROQ_", "MISTRAL_", "OLLAMA_")
        ):
            env_vars.append(f"{key}={os.environ[key]}")
    # Tell the container the port mappings (container_port:host_port pairs)
    mappings = [f"{CONTAINER_PORT_START + i}:{hp}" for i, hp in enumerate(host_ports)]
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
                f"{host_path}:/workspace",
                f"{sessions_path}:/home/bark/.pi/sessions",
            ],
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=256m",
                "/home/bark": "rw,nosuid,size=64m",
                "/run": "rw,noexec,nosuid,size=16m",
                "/var/log": "rw,noexec,nosuid,size=16m",
            },
            "PortBindings": port_bindings,
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
    _track_activity(container_id, workspace_id)

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
    stream = container.attach(stdin=True, stdout=True, stderr=True, stream=True)
    return stream


async def stop_container(container_id: str) -> None:
    """Stop a running container. Ports are kept allocated for restart."""
    docker = await get_docker()
    try:
        container = await docker.containers.get(container_id)
        await container.stop()
        logger.info("Stopped container %s", container_id)
    except aiodocker.exceptions.DockerError as e:
        logger.warning("Failed to stop container %s: %s", container_id, e)

    _containers.pop(container_id, None)


async def remove_container(container_id: str) -> None:
    """Stop and remove a container."""
    docker = await get_docker()
    try:
        container = await docker.containers.get(container_id)
        await container.stop()
        await container.delete(force=True)
        logger.info("Removed container %s", container_id)
    except aiodocker.exceptions.DockerError as e:
        logger.warning("Failed to remove container %s: %s", container_id, e)

    _containers.pop(container_id, None)


async def stop_user_containers(user_id: str) -> None:
    """Stop all containers for a user (called on logout)."""
    workspaces = await user_store.get_user_workspaces_with_containers(user_id)
    for ws in workspaces:
        if ws["container_id"]:
            await stop_container(ws["container_id"])


def _track_activity(container_id: str, workspace_id: str) -> None:
    _containers[container_id] = {
        "last_activity": time.time(),
        "workspace_id": workspace_id,
    }


def record_activity(container_id: str) -> None:
    """Record activity on a container (called on prompt/steer/follow_up)."""
    if container_id in _containers:
        _containers[container_id]["last_activity"] = time.time()


def on_idle_stop(workspace_id: str, callback) -> None:
    """Register a callback to be called when a workspace container is stopped due to idle timeout."""
    _idle_callbacks.setdefault(workspace_id, []).append(callback)


def remove_idle_callback(workspace_id: str, callback) -> None:
    """Remove an idle timeout callback."""
    cbs = _idle_callbacks.get(workspace_id, [])
    if callback in cbs:
        cbs.remove(callback)


async def _cleanup_idle_containers() -> None:
    """Periodically stop idle containers."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        now = time.time()
        to_stop = []
        for cid, info in list(_containers.items()):
            if now - info["last_activity"] > IDLE_TIMEOUT_SECONDS:
                to_stop.append((cid, info["workspace_id"]))

        for cid, wid in to_stop:
            logger.info("Stopping idle container %s (workspace %s)", cid, wid)
            # Notify listeners before stopping
            for cb in list(_idle_callbacks.get(wid, [])):
                try:
                    await cb(wid)
                except Exception as e:
                    logger.error("Idle callback error: %s", e)
            await stop_container(cid)


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
        _cleanup_task = asyncio.create_task(_cleanup_idle_containers())


async def shutdown() -> None:
    """Clean up on app shutdown. Stop all managed containers."""
    global _cleanup_task, _docker
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
    # Stop all tracked containers
    for cid in list(_containers.keys()):
        await stop_container(cid)
    # Also stop any orphaned bark containers (not in _containers but have our label)
    try:
        docker = await get_docker()
        containers = await docker.containers.list(
            all=True,
            filters={"label": [f"bark.instance={INSTANCE_ID}"]},
        )
        for c in containers:
            cid = c.id
            if cid not in _containers:
                logger.info("Stopping orphaned bark container %s", cid)
                try:
                    await c.stop()
                except aiodocker.exceptions.DockerError:
                    pass
    except (aiodocker.exceptions.DockerError, OSError) as e:
        logger.warning("Error cleaning up orphaned containers: %s", e)
    if _docker:
        await _docker.close()
        _docker = None
