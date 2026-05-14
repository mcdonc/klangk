import asyncio
import logging
import os
import time

import aiodocker

from . import user_store

logger = logging.getLogger(__name__)

IMAGE_NAME = "bark-pi"
IDLE_TIMEOUT_SECONDS = 15 * 60  # 15 minutes
CHECK_INTERVAL_SECONDS = 60

# Port allocation: each workspace gets PORTS_PER_WORKSPACE ports
# starting from PORT_RANGE_START
PORT_RANGE_START = 9000
PORTS_PER_WORKSPACE = 5

# Track active containers: container_id -> {last_activity, workspace_id, ports}
_containers: dict[str, dict] = {}
# Track allocated port ranges: workspace_id -> (start_port, end_port)
_allocated_ports: dict[str, tuple[int, int]] = {}
# Callbacks for idle timeout notifications: workspace_id -> [async_callback]
_idle_callbacks: dict[str, list] = {}
_docker: aiodocker.Docker | None = None
_cleanup_task: asyncio.Task | None = None


def _allocate_port_range(workspace_id: str) -> tuple[int, int]:
    """Allocate a port range for a workspace. Returns (start_port, end_port) inclusive."""
    if workspace_id in _allocated_ports:
        return _allocated_ports[workspace_id]

    used_ranges = set(_allocated_ports.values())
    port = PORT_RANGE_START
    while True:
        candidate = (port, port + PORTS_PER_WORKSPACE - 1)
        if candidate not in used_ranges:
            _allocated_ports[workspace_id] = candidate
            return candidate
        port += PORTS_PER_WORKSPACE


def _release_port_range(workspace_id: str) -> None:
    _allocated_ports.pop(workspace_id, None)


def get_workspace_ports(workspace_id: str) -> tuple[int, int] | None:
    """Get the allocated port range for a workspace, if any."""
    return _allocated_ports.get(workspace_id)


async def get_docker() -> aiodocker.Docker:
    global _docker
    if _docker is None:
        _docker = aiodocker.Docker()
    return _docker


async def start_container(
    workspace_id: str,
    host_path: str,
    existing_container_id: str | None = None,
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
            logger.info("Removed stopped container %s for workspace %s, will recreate", existing_container_id, workspace_id)
        except aiodocker.exceptions.DockerError:
            logger.info("Could not find container %s, creating new one", existing_container_id)

    # Allocate port range for this workspace
    start_port, end_port = _allocate_port_range(workspace_id)

    # Collect API keys from environment to pass into the container
    env_vars = []
    for key in os.environ:
        if key.startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GROQ_", "MISTRAL_", "OLLAMA_")):
            env_vars.append(f"{key}={os.environ[key]}")
    # Tell the container which ports are available
    env_vars.append(f"BARK_PORT_START={start_port}")
    env_vars.append(f"BARK_PORT_END={end_port}")
    # Pass host UID/GID so the container can create a matching user
    env_vars.append(f"BARK_UID={os.getuid()}")
    env_vars.append(f"BARK_GID={os.getgid()}")

    # Build port bindings: map each container port to the same host port
    port_bindings = {}
    exposed_ports = {}
    for port in range(start_port, end_port + 1):
        port_key = f"{port}/tcp"
        exposed_ports[port_key] = {}
        port_bindings[port_key] = [{"HostPort": str(port)}]

    config = {
        "Image": IMAGE_NAME,
        "HostConfig": {
            "Binds": [f"{host_path}:/workspace"],
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
        name=f"bark-{workspace_id[:12]}",
        config=config,
    )
    await container.start()
    container_id = container.id

    await user_store.update_workspace_container(workspace_id, container_id)
    _track_activity(container_id, workspace_id)

    logger.info(
        "Started container %s for workspace %s (ports %d-%d)",
        container_id, workspace_id, start_port, end_port,
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

    info = _containers.pop(container_id, None)
    if info:
        _release_port_range(info["workspace_id"])


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
                    logger.warning("Idle callback error: %s", e)
            await stop_container(cid)


def start_cleanup_loop() -> None:
    """Start the background cleanup task."""
    global _cleanup_task
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
    if _docker:
        await _docker.close()
        _docker = None
