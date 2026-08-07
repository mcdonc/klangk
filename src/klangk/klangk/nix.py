"""Per-workspace nix store via btrfs snapshots (#2201, #2208).

When a workspace has the per-workspace ``nix`` setting enabled and
``KLANGKD_NIX_BTRFS_SUBVOLUME`` names a seed btrfs subvolume, the workspace
gets a writable, isolated ``/nix`` as a **btrfs snapshot** of the seed. The
seed is built by #2200 (``build-nix-seed``) and loaded into a btrfs subvolume
by ``scripts/load-nix-seed-btrfs.sh``. ``ContainerRegistry`` binds the
snapshot's ``/nix`` (and ``nix.conf``) into the workspace container; on
workspace delete, ``Workspaces`` removes the snapshot.

btrfs (unlike zfs) lets a non-root user snapshot a subvolume it can write to,
and a snapshot is reachable through the parent mount (no separate mount), so
this needs **no privileged helper** — unlike the zfs-clone path, which would
need a ``cap_sys_admin`` mount helper (see #2210 and openzfs/zfs#10648: non-root
zfs mount is impossible on Linux). Requires the btrfs filesystem be mounted
with ``user_subvol_rm_allowed`` so the same non-root user can delete snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class NixError(RuntimeError):
    """A btrfs operation or configuration problem in the nix subsystem."""


class Nix:
    """Owns the per-workspace btrfs-snapshot lifecycle for ``/nix``.

    Constructed once in ``main.build_app`` as ``app.state.nix``. btrfs calls go
    through ``asyncio.create_subprocess_exec``; existence checks are plain
    ``os.path`` queries (a snapshot is a directory reachable through the parent
    btrfs mount, so there is no mount step and no separate mountpoint lookup).
    """

    def __init__(self, app):
        self.app = app

    @property
    def btrfs_configured(self) -> bool:
        """Whether the btrfs-snapshot path is available (a seed subvolume is set).

        The per-workspace ``nix`` flag (checked by the caller) decides whether a
        *given* workspace opts in; this only says the deploy can serve it.
        """
        return bool(self.app.state.settings.nix_btrfs_subvolume)

    @property
    def seed(self) -> str:
        # Path to the seed btrfs subvolume, e.g. /steam2/btrfs/klangk-nix/seed.
        # ``btrfs_configured`` already requires this set.
        return self.app.state.settings.nix_btrfs_subvolume or ""

    def _ws_path(self, workspace_id: str) -> str:
        # Sibling of the seed: <seed's parent>/ws-<workspace_id>.
        return os.path.join(os.path.dirname(self.seed), f"ws-{workspace_id}")

    async def _btrfs(
        self, args: list[str], *, check: bool = True
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "btrfs",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode if proc.returncode is not None else 1
        out_s, err_s = out.decode(), err.decode()
        if check and rc != 0:
            raise NixError(
                f"btrfs {' '.join(args)} failed (rc={rc}): {err_s.strip()}"
            )
        return rc, out_s, err_s

    async def ensure_workspace_nix(self, workspace_id: str) -> str | None:
        """Ensure a writable ``/nix`` btrfs snapshot for *workspace_id*.

        Returns the snapshot's path (bind-mounted into the container as /nix),
        or ``None`` when btrfs is not configured. Idempotent: reuses an existing
        snapshot across restarts (it lives at the path, reachable via the parent
        mount — no separate mount needed).
        """
        if not self.btrfs_configured:
            return None
        if not os.path.isdir(self.seed):
            raise NixError(
                f"seed subvolume {self.seed} not found — build the seed "
                f"(devenv shell -- build-nix-seed) and load it "
                f"(scripts/load-nix-seed-btrfs.sh) first"
            )
        ws = self._ws_path(workspace_id)
        if not os.path.exists(ws):
            logger.info(
                "nix: snapshotting seed for workspace %s -> %s",
                workspace_id,
                ws,
            )
            await self._btrfs(["subvolume", "snapshot", self.seed, ws])
        return ws

    async def destroy_workspace_nix(self, workspace_id: str) -> None:
        """Delete the per-workspace snapshot (on workspace delete). No-op if absent."""
        if not self.btrfs_configured:
            return
        ws = self._ws_path(workspace_id)
        if not os.path.exists(ws):
            return
        rc, _, err = await self._btrfs(
            ["subvolume", "delete", ws], check=False
        )
        if rc != 0:
            logger.warning(
                "nix: btrfs subvolume delete %s failed (rc=%s): %s",
                ws,
                rc,
                err.strip(),
            )
