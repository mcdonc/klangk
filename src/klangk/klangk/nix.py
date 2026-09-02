"""Per-workspace nix store (#2201, #2208, #2220).

A workspace with the per-workspace ``nix`` setting gets a writable, isolated
``/nix`` derived from a shared seed, via one of two interchangeable backends
selected by ``nix_seed.type`` (see :class:`klangk.settings.NixSeedConfig`):

- **btrfs-snapshot** — the seed is a btrfs subvolume; each workspace gets a CoW
  snapshot. A snapshot is reachable through the parent mount and btrfs lets a
  non-root user snapshot a subvolume it can write to, so this needs **no
  privileged helper** (unlike zfs, whose non-root mount is impossible on Linux
  — openzfs/zfs#10648). Requires a btrfs filesystem mounted with
  ``user_subvol_rm_allowed``. The CoW-optimised choice.
- **fuse-overlayfs** (the default) — the seed is a plain directory overlaid per
  workspace via ``fuse-overlayfs`` (seed = read-only lower, per-workspace upper
  captures writes). Works on **any** filesystem, no privileged helper (needs
  ``fuse-overlayfs`` + ``fusermount3`` + ``/dev/fuse``).

Both return a *mountpoint* — a directory holding ``nix/`` and ``nix.conf`` —
that ``ContainerRegistry`` binds into the container as ``/nix`` (+
``/etc/nix/nix.conf``). On workspace delete, ``Workspaces`` tears it down. The
per-workspace ``nix`` flag (checked by the caller) opts a given workspace in;
this module only decides whether a backend is configured (``nix_seed.path``
set) and armed (``nix_enabled``, #2560 — off by default; ``available`` is the
resolved armed status). Omit ``nix_seed`` entirely to disable the feature
(nix is then image-only). While ``nix_enabled`` is off, provisioning is a
no-op (a stored workspace flag is skipped with a one-time info log); teardown
still runs so workspace delete keeps cleaning up layers.

Caveat: the fuse backend suits a bare-metal Linux host (podman runs on the host,
no userns nesting between the FUSE mount and the workspace container). It does
**not** work where podman is nested — the rootless runtime can't bind a
process-owned FUSE mount into a workspace container's userns (host-container and
macOS deployments; see #2221). There, fall back to the nix image's baked ``/nix``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat

logger = logging.getLogger(__name__)


class NixError(RuntimeError):
    """A btrfs/fuse operation or configuration problem in the nix subsystem."""


def rmtree_rw(path: str) -> None:
    """``shutil.rmtree`` that survives read-only store files/dirs.

    nix store paths are 0444 files inside 0555 dirs; a plain ``rmtree`` aborts
    because unlinking a file needs write on its parent dir. The ``onerror``
    makes the failing entry's parent (and the entry) user-writable, then retries
    the failed op. If it still can't (genuinely stuck), it logs and moves on so
    cleanup of a half-deleted workspace is best-effort.
    """

    def _onerror(func, p: str, _exc) -> None:
        try:
            # chmod the failing entry's parent so unlink/rmdir can proceed.
            # Skip when p is the rmtree root: its parent is the shared seed
            # parent dir (sibling to every ws-*), which is not ours to chmod.
            if p != path:
                os.chmod(os.path.dirname(p), stat.S_IRWXU)
            os.chmod(p, stat.S_IRWXU)
            func(p)
        except OSError as exc:
            logger.warning(
                "nix: could not remove %s during cleanup: %s", p, exc
            )

    shutil.rmtree(path, onerror=_onerror)


class Nix:
    """Owns the per-workspace ``/nix`` lifecycle.

    Constructed once in ``main.build_app`` as ``app.state.nix``. Subprocess
    calls (``btrfs`` / ``fuse-overlayfs`` / ``fusermount3`` / ``stat``) go
    through ``asyncio.create_subprocess_exec``; existence/mount checks are plain
    ``os.path`` queries. ``ensure_workspace_nix`` / ``destroy_workspace_nix``
    dispatch to the configured backend (``nix_seed.type``).
    """

    def __init__(self, app):
        self.app = app
        # Workspaces already info-logged as skipped-by-nix_enabled (#2560) —
        # one notice per workspace per process, not one per start.
        self._skip_notified: set[str] = set()

    # --- configuration / dispatch -------------------------------------------

    @property
    def configured(self) -> bool:
        """Whether a nix backend is configured (``nix_seed.path`` is set)."""
        return bool(self.app.state.settings.nix_seed.path)

    @property
    def available(self) -> bool:
        """Resolved armed status (#2560): the switch AND a backend.

        ``nix_enabled`` (off by default) AND ``nix_seed.path`` set. This is
        what the ``/api/v1/images`` ``nix_available`` field reports — all
        three create/edit surfaces hide the toggle while false.
        """
        return self.configured and bool(self.app.state.settings.nix_enabled)

    @property
    def _seed(self) -> str:
        return self.app.state.settings.nix_seed.path or ""

    @property
    def _type(self) -> str:
        return self.app.state.settings.nix_seed.type

    def _ws_path(self, workspace_id: str) -> str:
        # Sibling of the seed: <seed's parent>/ws-<workspace_id>. Same layout
        # for both backends (a btrfs snapshot or a fuse overlay work-dir).
        return os.path.join(os.path.dirname(self._seed), f"ws-{workspace_id}")

    async def ensure_workspace_nix(self, workspace_id: str) -> str | None:
        """Ensure a writable ``/nix`` for *workspace_id*; return its mountpoint.

        The mountpoint holds ``nix/`` (bind-mounted into the container as
        ``/nix``) and ``nix.conf``. Returns ``None`` when no backend is
        configured, or when the feature is switched off (``nix_enabled``,
        #2560 — the skip is logged once per workspace at info; re-enabling
        resumes the mount, the per-workspace layer persists). Idempotent:
        reuses an existing snapshot/fuse mount.
        """
        if not self.configured:
            return None
        if not self.app.state.settings.nix_enabled:
            if workspace_id not in self._skip_notified:
                self._skip_notified.add(workspace_id)
                logger.info(
                    "nix: workspace %s has the nix setting but the feature "
                    "is disabled (nix_enabled off, #2560) — starting "
                    "without the /nix mount",
                    workspace_id,
                )
            return None
        if self._type == "btrfs-snapshot":
            return await self.ensure_btrfs(workspace_id)
        return await self._ensure_fuse(workspace_id)

    async def destroy_workspace_nix(self, workspace_id: str) -> None:
        """Tear down the per-workspace ``/nix``. No-op if absent/unconfigured."""
        if not self.configured:
            return
        if self._type == "btrfs-snapshot":
            await self._destroy_btrfs(workspace_id)
        else:
            await self._destroy_fuse(workspace_id)

    # --- subprocess helper ---------------------------------------------------

    async def _spawn(self, args: list[str]):
        """Spawn a backend tool with captured output. A missing binary
        (fuse-overlayfs / fusermount3 / btrfs / stat) surfaces as a
        clear NixError, not a raw FileNotFoundError out of container
        start."""
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise NixError(
                f"{args[0]} not found on PATH — install the nix backend "
                f"tooling and re-run `klangkd doctor`"
            ) from exc

    async def _communicate(
        self, proc, args: list[str], timeout: float
    ) -> tuple[bytes, bytes]:
        """``communicate()`` bounded by *timeout*; a hang is killed and
        raises a clear NixError."""
        try:
            return await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise NixError(f"{' '.join(args)} timed out after {timeout}s")

    async def run(
        self, args: list[str], *, check: bool = True, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        proc = await self._spawn(args)
        out, err = await self._communicate(proc, args, timeout)
        rc = proc.returncode if proc.returncode is not None else 1
        out_s, err_s = out.decode(), err.decode()
        if check and rc != 0:
            raise NixError(
                f"{' '.join(args)} failed (rc={rc}): {err_s.strip()}"
            )
        return rc, out_s, err_s

    # --- btrfs-snapshot backend ---------------------------------------------

    async def ensure_btrfs(self, workspace_id: str) -> str:
        seed = self._seed
        if not os.path.isdir(seed):
            raise NixError(
                f"seed subvolume {seed} not found — build the seed "
                f"(klangk-build-nix-seed) and load it "
                f"(klangk-load-nix-seed-btrfs) first"
            )
        fstype = (await self.run(["stat", "-f", "-c", "%T", seed]))[1].strip()
        if fstype != "btrfs":
            raise NixError(
                f"nix_seed.type is btrfs-snapshot but the seed {seed} is on "
                f"{fstype}, not btrfs — either load the seed into a btrfs "
                f"subvolume (klangk-load-nix-seed-btrfs) or switch to "
                f"type: fuse-overlayfs"
            )
        ws = self._ws_path(workspace_id)
        if not os.path.exists(ws):
            logger.info(
                "nix: snapshotting seed for workspace %s -> %s",
                workspace_id,
                ws,
            )
            await self.run(["btrfs", "subvolume", "snapshot", seed, ws])
        return ws

    async def _destroy_btrfs(self, workspace_id: str) -> str | None:
        ws = self._ws_path(workspace_id)
        if not os.path.exists(ws):
            return None
        rc, _, err = await self.run(
            ["btrfs", "subvolume", "delete", ws], check=False
        )
        if rc != 0:
            logger.warning(
                "nix: btrfs subvolume delete %s failed (rc=%s): %s",
                ws,
                rc,
                err.strip(),
            )
        return None

    # --- fuse-overlayfs backend ---------------------------------------------

    async def _ensure_fuse(self, workspace_id: str) -> str:
        seed = self._seed
        if not os.path.isdir(os.path.join(seed, "nix")):
            raise NixError(
                f"seed directory {seed}/nix not found — build the seed "
                f"(klangk-build-nix-seed) and point nix_seed.path "
                f"at its output first"
            )
        ws = self._ws_path(workspace_id)
        merged = os.path.join(ws, "nix")
        if os.path.ismount(merged):
            # A live fuse mount is reused — but a prior klangkd crash can leave
            # a stale mount that's still "mounted" yet unusable. Probe it and
            # re-mount if the probe fails.
            try:
                os.listdir(merged)
            except OSError:
                logger.warning(
                    "nix: stale fuse mount at %s; re-mounting", merged
                )
                await self.run(["fusermount3", "-u", merged], check=False)
                await self._mount_fuse(seed, ws, merged)
        else:
            await self._mount_fuse(seed, ws, merged)
        return ws

    async def _mount_fuse(self, seed: str, ws: str, merged: str) -> None:
        os.makedirs(os.path.join(ws, "upper"), exist_ok=True)
        os.makedirs(os.path.join(ws, "work"), exist_ok=True)
        os.makedirs(merged, exist_ok=True)
        await self.run(
            [
                "fuse-overlayfs",
                merged,
                "-o",
                f"lowerdir={seed}/nix,upperdir={ws}/upper,workdir={ws}/work",
            ]
        )
        # The container binds <mountpoint>/nix.conf; seed provides it. Copy so
        # the mountpoint is self-contained (a symlink would resolve through the
        # seed path, which need not be visible inside the container's view).
        shutil.copyfile(
            os.path.join(seed, "nix.conf"), os.path.join(ws, "nix.conf")
        )

    async def _destroy_fuse(self, workspace_id: str) -> None:
        ws = self._ws_path(workspace_id)
        if not os.path.exists(ws):
            return
        merged = os.path.join(ws, "nix")
        if os.path.ismount(merged):
            rc, _, err = await self.run(
                ["fusermount3", "-u", merged], check=False
            )
            if rc != 0:
                logger.warning(
                    "nix: fusermount3 -u %s failed (rc=%s): %s; trying lazy",
                    merged,
                    rc,
                    err.strip(),
                )
                # Lazy unmount so a busy mount doesn't pin the ws dir.
                rc2, _, err2 = await self.run(
                    ["fusermount3", "-u", "-z", merged], check=False
                )
                if rc2 != 0:
                    logger.warning(
                        "nix: lazy fusermount3 -u %s failed (rc=%s): %s; "
                        "ws dir left as an orphan for the operator",
                        merged,
                        rc2,
                        err2.strip(),
                    )
        # upper holds read-only store paths; rmtree_rw chmods them away. If
        # the mount is still live (both unmounts failed), rmtree best-effort-
        # skips the busy mountpoint (logged via _onerror) — the ws dir is then
        # an orphan the operator must clean up (umount + rm).
        rmtree_rw(ws)
