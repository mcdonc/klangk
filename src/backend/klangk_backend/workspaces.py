import asyncio
import json
import logging
import os
import random
import re
import shutil
import tempfile
from pathlib import Path

from . import container, model, podman, terminal
from .util import resolve_env_bool, resolve_env_value

logger = logging.getLogger(__name__)

_data_dir = Path(
    resolve_env_value("KLANGK_DATA_DIR", str(Path.home() / ".klangk" / "data"))
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
        "auto_start": ws.get("auto_start", False),
        "mounts": ws.get("mounts"),
        "env": ws.get("env"),
        "health_check": ws.get("health_check"),
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
    user_dir = _safe_path(user_id)  # lgtm[py/path-injection]
    if not user_dir.exists():
        return []
    safe_email = _sanitize_filename(email)

    # Page through every workspace (list_workspaces paginates).
    archives: list[Path] = []
    offset = 0
    while True:
        page = await model.list_workspaces(user_id, offset=offset)
        user_workspaces = page["items"]
        if not user_workspaces:
            break
        for ws in user_workspaces:
            ws_name = _sanitize_filename(ws["name"])
            archive_name = f"{user_id}-{safe_email}-{ws_name}.tar.gz"
            try:
                archive_path = _safe_path(
                    archive_name
                )  # lgtm[py/path-injection]
            except ValueError:
                logger.error(
                    "Archive path traversal blocked for workspace %s",
                    ws["name"],
                )
                continue
            home_dir = home_path(user_id, ws["id"])
            metadata = workspace_metadata(ws)
            if await build_workspace_archive(metadata, home_dir, archive_path):
                # lgtm[py/clear-text-logging-sensitive-data]
                logger.info(
                    "Archived workspace %s to %s", ws["name"], archive_path
                )
                archives.append(archive_path)
        if not page["has_more"]:
            break
        offset = page["next_offset"]

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


async def create_workspace(
    user_id: str,
    name: str,
    image: str | None = None,
    default_command: str | None = None,
    auto_start: bool = False,
    mounts: list[str] | None = None,
    env: dict[str, str] | None = None,
    setup_state: str | None = None,
    health_check: str | None = None,
) -> dict:
    workspace = await model.create_workspace(
        user_id,
        name,
        image=image,
        default_command=default_command,
        auto_start=auto_start,
        mounts=mounts,
        env=env,
        setup_state=setup_state or model.SETUP_STATE_COMPLETE,
        health_check=health_check,
    )
    home = home_path(user_id, workspace["id"])
    home.mkdir(parents=True, exist_ok=True)
    work = workspace_path(user_id, workspace["id"])
    work.mkdir(parents=True, exist_ok=True)
    users_dir = home / ".users"
    users_dir.mkdir(exist_ok=True)
    terminals_dir = home / ".terminals"
    terminals_dir.mkdir(exist_ok=True)
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


async def list_workspaces(
    user_id: str,
    limit: int = 10,
    offset: int = 0,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
) -> dict:
    return await model.list_workspaces(user_id, limit, offset, sort, order, q)


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


def ensure_home_symlink(
    workspace_home: Path,
    handle: str,
    user_id: str,
) -> tuple[str, bool]:
    """Ensure the ``/home/{handle} -> .users/{user_id}`` symlink exists.

    Creates the ``.users/{user_id}`` directory and symlink if missing.
    If the symlink exists and already points to the right target, no-op.
    If the handle changed (old symlink for this user_id exists), removes
    the old one and creates the new one.

    Returns ``(container_home_path, created)`` where *created* is True
    when a new user directory was created (caller should populate it
    with skeleton files).
    """
    users_dir = workspace_home / ".users"
    users_dir.mkdir(parents=True, exist_ok=True)

    user_dir = users_dir / user_id
    created = not user_dir.exists()
    user_dir.mkdir(exist_ok=True)

    symlink = workspace_home / handle
    target = f".users/{user_id}"

    if symlink.is_symlink() and os.readlink(symlink) == target:
        return f"/home/{handle}", created

    # Remove any existing symlink for this user (handle rename).
    for entry in workspace_home.iterdir():
        if entry.is_symlink() and os.readlink(entry) == target:
            entry.unlink()
            break

    # The handle path may already exist — e.g., a symlink pointing to a
    # different user's directory from a workspace import.  Adopt the old
    # user's files into the new user directory so the importer gets the
    # exported content, then replace the symlink.
    if symlink.is_symlink():
        old_target = symlink.resolve()
        if old_target.is_dir() and old_target != user_dir:
            # Move files from the old user dir into the new one.
            for child in old_target.iterdir():
                dest = user_dir / child.name
                if not dest.exists():
                    child.rename(dest)
            created = False  # content adopted, no skel needed
        symlink.unlink()

    symlink.symlink_to(target)
    return f"/home/{handle}", created


async def populate_home_skel(
    container_id: str,
    user_id: str,
) -> None:
    """Copy /etc/skel into a new user's home directory inside the container.

    This gives the user the standard skeleton files (.profile, .bashrc,
    etc.) so login shells source ~/.bashrc as users expect.  /etc/skel
    is part of the shadow-utils/login package and is present on most
    Linux distributions (notable exception: NixOS).  The workspace
    container runs Ubuntu, which always has it.
    """
    home = f"/home/.users/{user_id}"
    try:
        await podman.exec_container(
            container_id,
            ["/opt/klangk/bin/klangk-setup-home", home],
            user="klangk",
            timeout=10,
        )
    except Exception:
        logger.warning(
            "Failed to populate skel for user %s in %s",
            user_id,
            container_id,
            exc_info=True,
        )


async def eager_start_workspace(
    ws: dict, *, run_default_command: bool = True
) -> tuple[str, str]:
    """Start a container for a workspace immediately.

    Sets ``idle_timeout = 0`` so the container does not idle out.

    There are two callers with different needs:

    * **Server boot** (``auto_start_workspaces``) leaves
      *run_default_command* at its default ``True`` — the workspace's
      software is already installed in the persisted volume, so the
      default command is safe to run now.
    * **Workspace creation** (``api/workspaces.create_workspace``)
      passes ``run_default_command=False`` — ``setup.sh`` has not run
      yet, so the default command would fail.  The CLI sandbox driver
      sends ``terminal_start`` after setup completes to trigger it.

    Returns ``(container_id, status)``.
    """
    owner_id = ws["user_id"]
    workspace_id = ws["id"]
    host_path = str(get_workspace_host_path(owner_id, workspace_id))
    h_path = str(get_home_host_path(owner_id, workspace_id))
    cfg_path = str(get_config_host_path(owner_id, workspace_id))
    cid, status = await container.registry.start_container(
        workspace_id,
        host_path,
        h_path,
        ws.get("container_id"),
        num_ports=ws.get("num_ports", container.DEFAULT_PORTS_PER_WORKSPACE),
        image=ws.get("image"),
        config_path=cfg_path,
        extra_mounts=ws.get("mounts"),
        extra_env=ws.get("env"),
        user_id=owner_id,
        health_check=ws.get("health_check"),
        setup_state=ws.get("setup_state"),
    )
    state = container.registry.states.get(workspace_id)
    if state:
        state.idle_timeout = 0

    # If the workspace has a default command, create a tmux session
    # and run it now so it's already running when a user connects.
    default_command = ws.get("default_command")
    if default_command and status == "created" and run_default_command:
        handle = await model.get_user_handle(owner_id)
        if handle:
            ws_home = home_path(owner_id, workspace_id)
            user_home, created = ensure_home_symlink(ws_home, handle, owner_id)
            if created:
                await populate_home_skel(cid, owner_id)
            await terminal._ensure_base_session(
                cid,
                owner_id,
                user_home=user_home,
                default_command=default_command,
                setup_state=ws.get("setup_state"),
            )

    return cid, status


async def auto_start_workspaces() -> int:
    """Start containers for all workspaces with auto_start enabled.

    Skipped entirely if ``KLANGK_ALLOW_AUTOSTART`` is not set.
    Returns the number of containers started.
    """
    if not resolve_env_bool("KLANGK_ALLOW_AUTOSTART"):
        return 0

    ws_list = await model.list_auto_start_workspaces()
    started = 0
    for i, ws in enumerate(ws_list):
        if i > 0:
            await asyncio.sleep(random.uniform(0.5, 2.0))
        try:
            cid, status = await eager_start_workspace(ws)
            logger.info(
                "Auto-started workspace %s (%s): %s",
                ws["name"],
                cid[:12],
                status,
            )
            started += 1
        except Exception:
            logger.warning(
                "Failed to auto-start workspace %s (%s)",
                ws["name"],
                ws["id"],
                exc_info=True,
            )
    return started
