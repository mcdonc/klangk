import asyncio
import json
import logging
import os
import re
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

# Characters allowed in sanitized filenames (alphanumeric, dash,
# underscore, dot, @).  Everything else is replaced with underscore.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._@-]")


def _sanitize_filename(name: str) -> str:
    """Replace unsafe characters in a filename component."""
    return _SAFE_FILENAME_RE.sub("_", name)


def _safe_path(*segments: str) -> Path:
    """Build a path under WORKSPACES_ROOT, raising ValueError on traversal."""
    path = WORKSPACES_ROOT.joinpath(*segments)
    if not path.resolve().is_relative_to(WORKSPACES_ROOT.resolve()):
        raise ValueError(f"Path traversal blocked: {'/'.join(segments)}")
    return path


def _rmtree(path: Path | str, label: str = "") -> None:
    """Remove a directory tree, logging individual errors."""

    def _on_error(fn, fpath, exc):
        logger.warning(
            "rmtree %s: %s(%s) failed: %s",
            label or path,
            fn.__name__,
            fpath,
            exc,
        )

    shutil.rmtree(path, onexc=_on_error)


async def _async_rmtree(path: Path | str, label: str = "") -> None:
    """Remove a directory tree in a thread, logging errors."""
    await asyncio.to_thread(_rmtree, path, label)


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


def _build_export_tar_args(
    output: str,
    tmpdir: str,
    home_dir: Path | None,
) -> list[str]:
    """Build GNU tar arguments for workspace export.

    GNU tar stores symlinks as symlinks (not contents), so external
    symlinks (e.g., -> /usr/bin/python3) are harmless in the archive —
    they resolve correctly inside the container. No symlink filtering.

    Args:
        output: tar output path, or "-" for stdout.
        tmpdir: directory containing workspace.json.
        home_dir: workspace home directory, or None if it doesn't exist.
    """
    args = [
        "tar",
        "-czf",
        output,
        "-C",
        tmpdir,
        "workspace.json",
    ]
    if home_dir is not None and home_dir.exists():
        ws_dir_name = home_dir.name
        escaped = re.escape(ws_dir_name)
        args.extend(
            [
                f"--transform=s/^{escaped}/home/",
                "-C",
                str(home_dir.parent),
                ws_dir_name,
            ]
        )
    return args


async def build_workspace_archive(
    metadata: dict, home_dir: Path, archive_path: Path
) -> bool:
    """Build a .tar.gz archive in the export/import format.

    The archive contains workspace.json (metadata) and home/ (the home
    directory tree).  Symlinks are stored as symlinks (not dereferenced).
    Both home_dir and archive_path must resolve under WORKSPACES_ROOT.
    Returns True on success, False on failure.
    """
    # Validate that paths stay within the data directory.
    for label, p in [("home_dir", home_dir), ("archive_path", archive_path)]:
        if p.exists() or p.parent.exists():
            resolved = (p if p.exists() else p.parent).resolve()
            if not resolved.is_relative_to(WORKSPACES_ROOT.resolve()):
                logger.error("Path validation failed for %s: %s", label, p)
                return False

    tmpdir = tempfile.mkdtemp()
    try:
        meta_file = Path(tmpdir) / "workspace.json"
        meta_file.write_text(json.dumps(metadata, indent=2))

        tar_args = _build_export_tar_args(str(archive_path), tmpdir, home_dir)

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
        await _async_rmtree(tmpdir, "build_workspace_archive tmpdir")


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

    safe_email = _sanitize_filename(email)
    archives: list[Path] = []

    for ws in user_workspaces:
        ws_name = _sanitize_filename(ws["name"])
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
        await _async_rmtree(user_dir, f"user data {user_id}")
    return archives


def workspace_path(user_id: str, workspace_id: str) -> Path:
    return home_path(user_id, workspace_id) / "work"


def home_path(user_id: str, workspace_id: str) -> Path:
    return _safe_path(user_id, "home", workspace_id)


def config_path(user_id: str, workspace_id: str) -> Path:
    return _safe_path(user_id, "config", workspace_id)


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
    users_dir = home / ".users"
    users_dir.mkdir(exist_ok=True)
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
        await _async_rmtree(home, f"workspace {workspace['id']} rollback")
        raise
    return workspace


async def list_workspaces(user_id: str) -> list[dict]:
    return await model.list_workspaces(user_id)


async def get_workspace(
    workspace_id: str, user_id: str | None = None
) -> dict | None:
    return await model.get_workspace(workspace_id, user_id)


async def delete_workspace(workspace_id: str, user_id: str) -> bool:
    workspace = await model.get_workspace(workspace_id, user_id)
    if workspace is None:
        return False

    deleted = await model.delete_workspace(workspace_id, user_id)
    if deleted:
        p = home_path(user_id, workspace_id)
        await _async_rmtree(p, f"workspace {workspace_id}")
    return deleted


def get_workspace_host_path(user_id: str, workspace_id: str) -> Path:
    path = workspace_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_home_host_path(user_id: str, workspace_id: str) -> Path:
    path = home_path(user_id, workspace_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- Handle management ---

_HANDLE_RE = re.compile(r"^[a-z0-9._-]+$")
_RESERVED_NAMES = frozenset({"work", ".users"})
_MAX_HANDLE_LEN = 32


def suggest_handle(email: str) -> str:
    """Derive a handle from an email address local part."""
    local = email.split("@")[0] if "@" in email else email
    handle = re.sub(r"[^a-z0-9._-]", "", local.lower())
    if not handle:
        handle = "user"
    return handle[:_MAX_HANDLE_LEN]


def _validate_handle(handle: str) -> str | None:
    """Return an error message if the handle is invalid, else None."""
    if not handle:
        return "Handle cannot be empty"
    if len(handle) > _MAX_HANDLE_LEN:
        return f"Handle must be {_MAX_HANDLE_LEN} characters or fewer"
    if handle.startswith("."):
        return "Handle cannot start with a dot"
    if handle in _RESERVED_NAMES:
        return f"'{handle}' is reserved"
    if not _HANDLE_RE.match(handle):
        return "Handle may only contain lowercase letters, digits, dots, dashes, and underscores"
    return None


def get_user_handle(
    user_id: str, workspace_id: str, connecting_user_id: str
) -> str | None:
    """Find the handle symlink for a user in a workspace, or None."""
    home = home_path(user_id, workspace_id)
    if not home.exists():
        return None
    target = f".users/{connecting_user_id}"
    for entry in home.iterdir():
        if entry.is_symlink() and os.readlink(entry) == target:
            return entry.name
    return None


def set_user_handle(
    user_id: str,
    workspace_id: str,
    connecting_user_id: str,
    handle: str,
) -> str:
    """Create or update a user's handle symlink.

    Returns the container-side home path (e.g. ``/home/alice``).
    Raises ``ValueError`` on validation failure or conflict.
    """
    error = _validate_handle(handle)
    if error:
        raise ValueError(error)

    home = home_path(user_id, workspace_id)
    users_dir = home / ".users"
    users_dir.mkdir(parents=True, exist_ok=True)

    # Create the uuid directory.
    user_dir = users_dir / connecting_user_id
    user_dir.mkdir(exist_ok=True)

    symlink = home / handle
    target = f".users/{connecting_user_id}"

    # If symlink already exists, check ownership.
    if symlink.is_symlink():
        if os.readlink(symlink) == target:
            return f"/home/{handle}"  # already correct
        raise ValueError(f"'{handle}' is already taken")
    elif symlink.exists():
        raise ValueError(f"'{handle}' conflicts with an existing file")

    # Remove any existing symlink for this user (handle rename).
    old_handle = get_user_handle(user_id, workspace_id, connecting_user_id)
    if old_handle is not None:
        old_symlink = home / old_handle
        if old_symlink.is_symlink():
            old_symlink.unlink()

    symlink.symlink_to(target)
    return f"/home/{handle}"


def suggest_alternative(user_id: str, workspace_id: str, handle: str) -> str:
    """Return the first available suffixed variant of a handle."""
    home = home_path(user_id, workspace_id)
    for i in range(2, 100):
        candidate = f"{handle}-{i}"
        if len(candidate) > _MAX_HANDLE_LEN:
            candidate = f"{handle[: _MAX_HANDLE_LEN - len(str(i)) - 1]}-{i}"
        if not (home / candidate).exists():
            return candidate
    return f"{handle}-{id(handle) % 10000}"  # pragma: no cover
