import asyncio
import json
import logging
import os
import random
import re
import shutil
import tempfile
from pathlib import Path

from . import container, model

logger = logging.getLogger(__name__)

# Characters allowed in sanitized filenames (alphanumeric, dash,
# underscore, dot, @).  Everything else is replaced with underscore.
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._@-]")


def sanitize_filename(name: str) -> str:
    """Replace unsafe characters in a filename component."""
    return _SAFE_FILENAME_RE.sub("_", name)


def rmtree(path: Path | str, label: str = "") -> None:
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
    await asyncio.to_thread(rmtree, path, label)


def _ensure_home_symlink_sync(
    workspace_home: Path,
    handle: str,
    user_id: str,
) -> tuple[str, bool]:
    """Synchronous ``ensure_home_symlink`` implementation.

    Performs blocking filesystem calls (``Path.exists``, ``readlink``,
    ``symlink_to``, ``Path.iterdir``, …) and must not run on the event
    loop — callers go through ``ensure_home_symlink``, which offloads
    this to a worker thread via ``asyncio.to_thread`` (#1262).
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
    podman=None,
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


class Workspaces:
    """Workspace filesystem paths, CRUD, and lifecycle.

    Constructed once in :func:`build_app` and stored on
    ``app.state.workspaces`` (#1484). The workspace root (``WORKSPACES_ROOT``)
    and ``data_dir`` are computed at construction from settings — no
    import-time env read (the frozen-at-import hazard).
    """

    def __init__(self, app_state):
        self.app_state = app_state
        self.settings = app_state.settings
        raw = self.settings.data_dir
        self.data_dir = Path(raw)
        self.root = self.data_dir / "workspaces"

    # --- export/import helpers ---

    def build_export_tar_args(
        self,
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

    def workspace_metadata(self, ws: dict) -> dict:
        """Extract export metadata from a workspace dict.

        Includes the instance ID so that imports can verify the archive
        came from the same Klangk instance.
        """
        return {
            "name": ws["name"],
            "instance_id": self.app_state.util.instance_id(),
            "image": ws.get("image"),
            "service_command": ws.get("service_command"),
            "auto_start": ws.get("auto_start", False),
            "mounts": ws.get("mounts"),
            "env": ws.get("env"),
            "health_check": ws.get("health_check"),
            "num_ports": ws.get("num_ports", 5),
        }

    # --- path helpers (close over root) ---

    def safe_path(self, *segments: str) -> Path:
        """Build a path under the workspace root, raising ValueError on traversal."""
        path = self.root.joinpath(*segments)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"Path traversal blocked: {'/'.join(segments)}")
        return path

    def workspace_path(self, workspace_id: str) -> Path:
        """Build path to /{root}/{workspace_id}/home/work"""
        return self.home_path(workspace_id) / "work"

    def home_path(self, workspace_id: str) -> Path:
        """Build path to /{root}/{workspace_id}/home"""
        return self.safe_path(workspace_id, "home")

    def config_path(self, workspace_id: str) -> Path:
        """Build path to /{root}/{workspace_id}/config"""
        return self.safe_path(workspace_id, "config")

    def get_config_host_path(self, workspace_id: str) -> Path:
        path = self.config_path(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_workspace_host_path(self, workspace_id: str) -> Path:
        path = self.workspace_path(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_home_host_path(self, workspace_id: str) -> Path:
        path = self.home_path(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- archive ---

    async def build_workspace_archive(
        self, metadata: dict, home_dir: Path, archive_path: Path
    ) -> bool:
        """Build a .tar.gz archive in the export/import format.

        The archive contains workspace.json (metadata) and home/ (the home
        directory tree).  Symlinks are stored as symlinks (not dereferenced).
        Both home_dir and archive_path must resolve under the workspace root.
        Returns True on success, False on failure.
        """
        # Validate that paths stay within the data directory.
        for label, p in [
            ("home_dir", home_dir),
            ("archive_path", archive_path),
        ]:
            if p.exists() or p.parent.exists():
                resolved = (p if p.exists() else p.parent).resolve()
                if not resolved.is_relative_to(self.root.resolve()):
                    logger.error("Path validation failed for %s: %s", label, p)
                    return False

        tmpdir = tempfile.mkdtemp()
        try:
            meta_file = Path(tmpdir) / "workspace.json"
            meta_file.write_text(json.dumps(metadata, indent=2))

            tar_args = self.build_export_tar_args(
                str(archive_path), tmpdir, home_dir
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
            logger.error(
                "Failed to build archive %s: %s", archive_path.name, e
            )
            return False
        finally:
            await _async_rmtree(tmpdir, "build_workspace_archive tmpdir")

    async def archive_user_data(self, user_id: str, email: str) -> list[Path]:
        """Archive each workspace to a .tar.gz in the export/import format.

        Creates one archive per workspace containing workspace.json (metadata)
        and home/ (the workspace home directory).  These archives can be
        re-imported via ``POST /workspaces/import``.

        Returns the list of created archive paths (may be empty).
        After successful archival each workspace's data directory is removed.
        """
        safe_email = sanitize_filename(email)

        # Page through every workspace (list_workspaces paginates).
        archives: list[Path] = []
        archived_ws_ids: list[str] = []
        offset = 0
        while True:
            page = await self.app_state.model.workspaces.list_workspaces(
                user_id, offset=offset
            )
            user_workspaces = page["items"]
            if not user_workspaces:
                break
            for ws in user_workspaces:
                ws_name = sanitize_filename(ws["name"])
                archive_name = f"{user_id}-{safe_email}-{ws_name}.tar.gz"
                try:
                    archive_path = self.safe_path(
                        archive_name
                    )  # lgtm[py/path-injection]
                except ValueError:
                    logger.error(
                        "Archive path traversal blocked for workspace %s",
                        ws["name"],
                    )
                    continue
                home_dir = self.home_path(ws["id"])
                metadata = self.workspace_metadata(ws)
                if await self.build_workspace_archive(
                    metadata, home_dir, archive_path
                ):
                    # lgtm[py/clear-text-logging-sensitive-data]
                    logger.info(
                        "Archived workspace %s to %s", ws["name"], archive_path
                    )
                    archives.append(archive_path)
                    archived_ws_ids.append(ws["id"])
            if not page["has_more"]:
                break
            offset = page["next_offset"]

        # Remove each archived workspace's data directory.
        for ws_id in archived_ws_ids:
            ws_dir = self.safe_path(ws_id)
            if ws_dir.exists():
                await _async_rmtree(ws_dir, f"workspace data {ws_id}")
        return archives

    # --- CRUD ---

    async def create_workspace(
        self,
        user_id: str,
        name: str,
        image: str | None = None,
        service_command: str | None = None,
        auto_start: bool = False,
        mounts: list[str] | None = None,
        env: dict[str, str] | None = None,
        setup_state: str | None = None,
        health_check: str | None = None,
    ) -> dict:
        workspace = (
            await self.app_state.model.workspaces.create_workspace_with_acl(
                user_id,
                name,
                image=image,
                service_command=service_command,
                auto_start=auto_start,
                mounts=mounts,
                env=env,
                setup_state=setup_state or model.SETUP_STATE_COMPLETE,
                health_check=health_check,
            )
        )
        home = self.home_path(workspace["id"])
        home.mkdir(parents=True, exist_ok=True)
        work = self.workspace_path(workspace["id"])
        work.mkdir(parents=True, exist_ok=True)
        users_dir = home / ".users"
        users_dir.mkdir(exist_ok=True)
        terminals_dir = home / ".terminals"
        terminals_dir.mkdir(exist_ok=True)
        # Allocate ports at creation time so ranges are sequential
        try:
            await self.app_state.container_registry.allocate_ports(
                workspace["id"], workspace["num_ports"]
            )
        except Exception:
            # Clean up the DB record and directories on port allocation failure
            await self.app_state.model.workspaces.delete_workspace(
                workspace["id"], user_id
            )
            await _async_rmtree(home, f"workspace {workspace['id']} rollback")
            raise
        return workspace

    async def list_workspaces(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
        sort: str = "created",
        order: str = "desc",
        q: str | None = None,
    ) -> dict:
        return await self.app_state.model.workspaces.list_workspaces(
            user_id, limit, offset, sort, order, q
        )

    async def get_workspace(
        self, workspace_id: str, user_id: str | None = None
    ) -> dict | None:
        return await self.app_state.model.workspaces.get_workspace(
            workspace_id, user_id
        )

    async def delete_workspace(self, workspace_id: str, user_id: str) -> bool:
        workspace = await self.app_state.model.workspaces.get_workspace(
            workspace_id, user_id
        )
        if workspace is None:
            return False

        deleted = await self.app_state.model.workspaces.delete_workspace(
            workspace_id, user_id
        )
        if deleted:
            ws_dir = self.safe_path(workspace_id)
            await _async_rmtree(ws_dir, f"workspace {workspace_id}")
        return deleted

    # --- home symlink ---

    async def ensure_home_symlink(
        self,
        workspace_home: Path,
        handle: str,
        user_id: str,
    ) -> tuple[str, bool]:
        """Ensure the ``/home/{handle} -> .users/{user_id}`` symlink exists.

        Creates the ``.users/{user_id}`` directory and symlink if missing.
        If the symlink exists and already points to the right target, no-op.
        If the handle changed (old symlink for this user_id exists), removes
        the old one and creates the new one.

        The blocking filesystem work runs in a worker thread via
        ``asyncio.to_thread`` so connect / container-start paths do not
        stall the event loop on disk latency (#1262).

        Returns ``(container_home_path, created)`` where *created* is True
        when a new user directory was created (caller should populate it
        with skeleton files).
        """
        return await asyncio.to_thread(
            _ensure_home_symlink_sync, workspace_home, handle, user_id
        )

    async def populate_home_skel(
        self,
        container_id: str,
        user_id: str,
    ) -> None:
        """Copy /etc/skel into a new user's home directory inside the container."""
        await populate_home_skel(container_id, user_id, self.app_state.podman)

    # --- lifecycle ---

    async def start_workspace(self, ws: dict) -> tuple[str, str]:
        """Start a container for a workspace immediately.

        Thin wrapper around ``self.app_state.container_registry.start_container``
        that unpacks the workspace dict. The agent home provisioning and the
        service command firing happen at the single create choke point
        inside ``start_container`` (see ``ContainerRegistry._bringup``, #1244),
        so they no longer live here.

        ``idle_timeout`` is left at its default; only
        ``auto_start_workspaces`` (the boot path) pins it to 0.

        Returns ``(container_id, status)``.
        """
        owner_id = ws["user_id"]
        workspace_id = ws["id"]
        host_path = str(self.get_workspace_host_path(workspace_id))
        h_path = str(self.get_home_host_path(workspace_id))
        cfg_path = str(self.get_config_host_path(workspace_id))
        cid, status = await self.app_state.container_registry.start_container(
            workspace_id,
            host_path,
            h_path,
            ws.get("container_id"),
            num_ports=ws.get(
                "num_ports", container.DEFAULT_PORTS_PER_WORKSPACE
            ),
            image=ws.get("image"),
            config_path=cfg_path,
            extra_mounts=ws.get("mounts"),
            extra_env=ws.get("env"),
            user_id=owner_id,
            health_check=ws.get("health_check"),
            setup_state=ws.get("setup_state"),
            service_command=ws.get("service_command"),
        )
        return cid, status

    async def auto_start_workspaces(self) -> int:
        """Start containers for all workspaces with auto_start enabled.

        Skipped entirely if ``KLANGK_ALLOW_AUTOSTART`` is not set.
        Returns the number of containers started.
        """
        if not self.settings.allow_autostart:
            return 0

        ws_list = (
            await self.app_state.model.workspaces.list_auto_start_workspaces()
        )
        started = 0
        for i, ws in enumerate(ws_list):
            if i > 0:
                await asyncio.sleep(random.uniform(0.5, 2.0))
            try:
                cid, status = await self.start_workspace(ws)
                # Auto-started containers are boot services: pin them alive
                # so they do not idle out between user connections. Only the
                # boot path does this -- create/restart use the default idle
                # timeout (#1244).
                state = self.app_state.container_registry.states.get(ws["id"])
                if state:
                    state.idle_timeout = 0
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
