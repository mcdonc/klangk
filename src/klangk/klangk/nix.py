"""Per-workspace nix store via zfs clones (#2201).

When a workspace has the per-workspace ``nix`` setting enabled and
``KLANGKD_NIX_ZFS_DATASET`` names a zfs dataset holding the seed, the workspace
gets a writable, isolated ``/nix`` as a **zfs clone** of a shared, snapshotted
seed dataset. The seed is built by #2200 (``scripts/build-nix-seed.sh``) and
loaded into a zfs dataset + snapshotted at ``@base`` by
``scripts/load-nix-seed-zfs.sh``. ``ContainerRegistry`` binds the clone's
``/nix`` (and ``nix.conf``) into the workspace container; on workspace
delete, ``Workspaces`` destroys the clone.

zfs clone/destroy/mount need privilege. Delegate just those operations to the
klangkd user — no full root — with::

    zfs allow <klangkd-user> create,destroy,clone,mount,list <dataset>

Why zfs clones (not the overlayfs the #2201 issue body describes): the #2201
spike (PR #2205) found rootless podman rejects ``--mount type=overlay``, and
in-container overlay needs single-bind + ``--cap-add SYS_ADMIN``; a zfs clone
is instant (~0.5 s), block-shared, plain-bind (no extra caps), and fully
isolated — and the nix DB copy-up gotcha doesn't apply (a clone is a real
filesystem copy, not an overlay). Where no zfs pool is available, leave the workspace's ``nix`` setting unset and the
workspace uses the nix image's baked /nix (no clone).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class NixError(RuntimeError):
    """A zfs operation or configuration problem in the nix subsystem."""


class Nix:
    """Owns the per-workspace zfs-clone lifecycle for ``/nix``.

    Constructed once in ``main.build_app`` as ``app.state.nix`` (#1426
    ownership: takes ``app``, reads settings live). All zfs calls go through
    ``asyncio.create_subprocess_exec`` so they don't block the event loop.
    """

    def __init__(self, app):
        self.app = app

    @property
    def zfs_configured(self) -> bool:
        """Whether the zfs-clone path is available (a seed dataset is configured).

        The per-workspace ``nix`` flag (checked by the caller) decides whether
        a *given* workspace opts into the clone; this only says the deploy can
        serve it.
        """
        return bool(self.app.state.settings.nix_zfs_dataset)

    @property
    def dataset(self) -> str:
        # ``enabled`` already requires this to be set, so callers reach here only
        # with a real value; return it directly (no defensive raise to keep
        # coverage honest).
        return self.app.state.settings.nix_zfs_dataset or ""

    def _seed(self) -> str:
        return f"{self.dataset}/seed"

    def _seed_snapshot(self) -> str:
        return f"{self._seed()}@base"

    def _ws(self, workspace_id: str) -> str:
        return f"{self.dataset}/ws-{workspace_id}"

    async def _zfs(
        self, args: list[str], *, check: bool = True
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "zfs",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else 1
        out_s, err_s = out.decode(), err.decode()
        if check and rc != 0:
            raise NixError(
                f"zfs {' '.join(args)} failed (rc={rc}): {err_s.strip()}"
            )
        return rc, out_s, err_s

    async def _exists(self, name: str) -> bool:
        rc, _, _ = await self._zfs(
            ["list", "-H", "-o", "name", name], check=False
        )
        return rc == 0

    async def ensure_workspace_nix(self, workspace_id: str) -> str | None:
        """Ensure a writable ``/nix`` clone for *workspace_id*.

        Returns the clone's mountpoint (to be bind-mounted into the container),
        or ``None`` when nix is disabled (so callers can call unconditionally).
        Idempotent: reuses an existing clone across container restarts.
        """
        if not self.zfs_configured:
            return None
        if not await self._exists(self._seed_snapshot()):
            raise NixError(
                f"seed snapshot {self._seed_snapshot()} not found — build the "
                f"seed (devenv run build-nix-seed) and load it "
                f"(scripts/load-nix-seed-zfs.sh {self.dataset}) first"
            )
        ws = self._ws(workspace_id)
        if not await self._exists(ws):
            logger.info(
                "nix: cloning seed for workspace %s -> %s", workspace_id, ws
            )
            await self._zfs(["clone", self._seed_snapshot(), ws])
        _, out, _ = await self._zfs(["list", "-H", "-o", "mountpoint", ws])
        mountpoint = out.strip()
        if not mountpoint or mountpoint == "none":
            raise NixError(
                f"workspace nix clone {ws} has no mountpoint "
                f"(set canmount=on on the seed dataset)"
            )
        return mountpoint

    async def destroy_workspace_nix(self, workspace_id: str) -> None:
        """Destroy the per-workspace clone (on workspace delete). No-op if absent."""
        if not self.zfs_configured:
            return
        ws = self._ws(workspace_id)
        rc, _, err = await self._zfs(["destroy", "-r", ws], check=False)
        if rc != 0 and "does not exist" not in err:
            logger.warning(
                "nix: zfs destroy %s failed (rc=%s): %s",
                ws,
                rc,
                err.strip(),
            )
