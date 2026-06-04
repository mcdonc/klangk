import asyncio
import json
import logging
import shutil
import tempfile
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


def workspace_metadata(ws: dict) -> dict:
    """Extract export metadata from a workspace dict."""
    return {
        "name": ws["name"],
        "image": ws.get("image"),
        "default_command": ws.get("default_command"),
        "mounts": ws.get("mounts"),
        "env": ws.get("env"),
        "num_ports": ws.get("num_ports", 5),
    }


async def build_workspace_archive(
    metadata: dict, home_dir: Path, archive_path: Path
) -> bool:
    """Build a .tar.gz archive in the export/import format.

    The archive contains workspace.json (metadata) and home/ (the home
    directory tree).  Returns True on success, False on failure.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        meta_file = Path(tmpdir) / "workspace.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        tar_args = [
            "tar",
            "-czf",
            str(archive_path),
            "-C",
            tmpdir,
            "workspace.json",
        ]
        if home_dir.exists():
            ws_dir_name = home_dir.name
            tar_args.extend(
                [
                    f"--transform=s/^{ws_dir_name}/home/",
                    "-C",
                    str(home_dir.parent),
                    ws_dir_name,
                ]
            )

        proc = await asyncio.create_subprocess_exec(
            *tar_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            logger.error(
                "tar failed for %s: %s",
                archive_path.name,
                stderr.decode("utf-8", errors="replace"),
            )
            return False
        return True
    except (asyncio.TimeoutError, OSError) as e:
        logger.error("Failed to build archive %s: %s", archive_path.name, e)
        return False
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, True)


async def archive_user_data(user_id: str, email: str) -> list[Path]:
    """Archive each workspace to a .tar.gz in the export/import format.

    Creates one archive per workspace containing workspace.json (metadata)
    and home/ (the workspace home directory).  These archives can be
    re-imported via ``POST /workspaces/import``.

    Returns the list of created archive paths (may be empty).
    After successful archival the user's data directory is removed.
    """
    user_workspaces = await model.list_workspaces(user_id)
    user_dir = WORKSPACES_ROOT / user_id
    if not user_dir.exists():
        return []

    safe_email = email.replace("/", "_").replace("\\", "_").replace("..", "_")
    archives: list[Path] = []

    for ws in user_workspaces:
        ws_name = ws["name"].replace("/", "_").replace("\\", "_")
        archive_name = f"{user_id}-{safe_email}-{ws_name}.tar.gz"
        archive_path = WORKSPACES_ROOT / archive_name
        if not archive_path.resolve().is_relative_to(
            WORKSPACES_ROOT.resolve()
        ):
            logger.error(
                "Archive path traversal blocked for workspace %s", ws["name"]
            )
            continue

        home_dir = home_path(user_id, ws["id"])
        metadata = workspace_metadata(ws)

        if await build_workspace_archive(metadata, home_dir, archive_path):
            logger.info(
                "Archived workspace %s to %s", ws["name"], archive_path
            )
            archives.append(archive_path)

    # Remove the user's data directory after all archives are created.
    if archives:
        await asyncio.to_thread(shutil.rmtree, user_dir, True)
    return archives


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
