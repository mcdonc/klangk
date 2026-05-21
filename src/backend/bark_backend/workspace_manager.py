import logging
import os
import shutil
from pathlib import Path

from . import container_manager, user_store

logger = logging.getLogger(__name__)

_data_dir = Path(
    os.environ.get("BARK_DATA_DIR", str(Path.home() / ".bark" / "data"))
)
WORKSPACES_ROOT = _data_dir / "workspaces"


async def archive_user_data(user_id: str, email: str) -> Path | None:
    """Archive a user's workspace data to a tar.xz file before deletion.

    Returns the archive path, or None if the user had no data directory.
    The archive is saved to $BARK_DATA_DIR/workspaces/{user_id}-{email}.tar.xz
    """
    import asyncio

    user_dir = WORKSPACES_ROOT / user_id
    if not user_dir.exists():
        return None
    archive_name = f"{user_id}-{email}.tar.xz"
    archive_path = WORKSPACES_ROOT / archive_name
    try:
        proc = await asyncio.create_subprocess_exec(
            "tar",
            "-cJf",
            str(archive_path),
            "-C",
            str(WORKSPACES_ROOT),
            user_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            logger.error(
                "tar failed for user %s: %s",
                email,
                stderr.decode("utf-8", errors="replace"),
            )
            return None
        logger.info("Archived user %s data to %s", email, archive_path)
        # Remove the original directory after successful archive
        shutil.rmtree(user_dir)
        return archive_path
    except (asyncio.TimeoutError, OSError) as e:
        logger.error("Failed to archive user %s data: %s", email, e)
        return None


def _workspace_path(user_id: str, workspace_id: str) -> Path:
    return WORKSPACES_ROOT / user_id / "work" / workspace_id


def _sessions_path(user_id: str, workspace_id: str) -> Path:
    return WORKSPACES_ROOT / user_id / "sessions" / workspace_id


def _home_path(user_id: str, workspace_id: str) -> Path:
    return WORKSPACES_ROOT / user_id / "home" / workspace_id


async def create_workspace(user_id: str, name: str) -> dict:
    workspace = await user_store.create_workspace(user_id, name)
    path = _workspace_path(user_id, workspace["id"])
    path.mkdir(parents=True, exist_ok=True)
    sessions = _sessions_path(user_id, workspace["id"])
    sessions.mkdir(parents=True, exist_ok=True)
    home = _home_path(user_id, workspace["id"])
    home.mkdir(parents=True, exist_ok=True)
    # Allocate ports at creation time so ranges are sequential
    await container_manager.allocate_ports(
        workspace["id"], workspace["num_ports"]
    )
    return workspace


async def list_workspaces(user_id: str) -> list[dict]:
    return await user_store.list_workspaces(user_id)


async def get_workspace(workspace_id: str, user_id: str) -> dict | None:
    return await user_store.get_workspace(workspace_id, user_id)


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    workspace = await user_store.get_workspace(workspace_id, user_id)
    if workspace is None:
        return False

    deleted = await user_store.delete_workspace(workspace_id, user_id)
    if deleted:
        path = _workspace_path(user_id, workspace_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        sessions = _sessions_path(user_id, workspace_id)
        if sessions.exists():
            shutil.rmtree(sessions, ignore_errors=True)
    return deleted


def get_workspace_host_path(user_id: str, workspace_id: str) -> Path:
    path = _workspace_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_sessions_host_path(user_id: str, workspace_id: str) -> Path:
    path = _sessions_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_home_host_path(user_id: str, workspace_id: str) -> Path:
    path = _home_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
