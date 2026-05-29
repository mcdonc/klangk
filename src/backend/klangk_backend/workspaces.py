import asyncio
import logging
import shutil
from pathlib import Path

from . import container, model
from .util import resolve_env_secret

logger = logging.getLogger(__name__)

_data_dir = Path(
    resolve_env_secret(
        "KLANGK_DATA_DIR", str(Path.home() / ".klangk" / "data")
    )
)
WORKSPACES_ROOT = _data_dir / "workspaces"


async def archive_user_data(user_id: str, email: str) -> Path | None:
    """Archive a user's workspace data to a tar.xz file before deletion.

    Returns the archive path, or None if the user had no data directory.
    The archive is saved to $KLANGK_DATA_DIR/workspaces/{user_id}-{email}.tar.xz
    """
    user_dir = WORKSPACES_ROOT / user_id
    if not user_dir.exists():
        return None
    # Sanitize email for use in filename — replace path separators
    # and other unsafe characters to prevent path traversal.
    safe_email = email.replace("/", "_").replace("\\", "_").replace("..", "_")
    archive_name = f"{user_id}-{safe_email}.tar.xz"
    archive_path = WORKSPACES_ROOT / archive_name
    # Verify the resolved path is still under WORKSPACES_ROOT
    if not archive_path.resolve().is_relative_to(  # pragma: no cover
        WORKSPACES_ROOT.resolve()
    ):
        logger.error("Archive path traversal blocked for email %s", email)
        return None
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
        await asyncio.to_thread(shutil.rmtree, user_dir)
        return archive_path
    except (asyncio.TimeoutError, OSError) as e:
        logger.error("Failed to archive user %s data: %s", email, e)
        return None


def workspace_path(user_id: str, workspace_id: str) -> Path:
    return home_path(user_id, workspace_id) / "work"


def home_path(user_id: str, workspace_id: str) -> Path:
    return WORKSPACES_ROOT / user_id / "home" / workspace_id


def config_path(user_id: str, workspace_id: str) -> Path:
    return WORKSPACES_ROOT / user_id / "config" / workspace_id


def get_config_host_path(user_id: str, workspace_id: str) -> Path:
    path = config_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_default_command(
    user_id: str, workspace_id: str, command: str | None
) -> None:
    """Write the default command file to the config directory."""
    path = config_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    cmd_file = path / "default-command"
    if command:
        cmd_file.write_text(command)
    elif cmd_file.exists():
        cmd_file.unlink()


async def create_workspace(
    user_id: str,
    name: str,
    image: str | None = None,
    default_command: str | None = None,
    mounts: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    workspace = await model.create_workspace(
        user_id,
        name,
        image=image,
        default_command=default_command,
        mounts=mounts,
        env=env,
    )
    home = home_path(user_id, workspace["id"])
    home.mkdir(parents=True, exist_ok=True)
    work = workspace_path(user_id, workspace["id"])
    work.mkdir(parents=True, exist_ok=True)
    if default_command:
        write_default_command(user_id, workspace["id"], default_command)
    # Allocate ports at creation time so ranges are sequential
    try:
        await container.registry.allocate_ports(
            workspace["id"], workspace["num_ports"]
        )
    except Exception:
        # Clean up the DB record and directories on port allocation failure
        await model.delete_workspace(workspace["id"], user_id)
        await asyncio.to_thread(shutil.rmtree, home, True)
        raise
    return workspace


async def list_workspaces(user_id: str) -> list[dict]:
    return await model.list_workspaces(user_id)


async def get_workspace(workspace_id: str, user_id: str) -> dict | None:
    return await model.get_workspace(workspace_id, user_id)


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    workspace = await model.get_workspace(workspace_id, user_id)
    if workspace is None:
        return False

    deleted = await model.delete_workspace(workspace_id, user_id)
    if deleted:
        p = home_path(user_id, workspace_id)
        await asyncio.to_thread(shutil.rmtree, p, True)
    return deleted


def get_workspace_host_path(user_id: str, workspace_id: str) -> Path:
    path = workspace_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_home_host_path(user_id: str, workspace_id: str) -> Path:
    path = home_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
