"""Unit tests for ``klangk.nix`` — the per-workspace /nix lifecycle (#2201, #2220).

Two backends (btrfs-snapshot, fuse-overlayfs) selected by ``nix_seed.type``.
The btrfs/fuse subprocess calls and filesystem existence checks are faked (no
real btrfs/fuse needed); the fuse FS ops (makedirs/copyfile/rmtree) run on a
real tmp_path. The per-workspace ``nix`` flag is the caller's gate
(container.py); the module cares whether ``nix_seed.path`` is set and whether
``nix_enabled`` arms it (#2560 — off by default; ``_app`` arms it explicitly).
"""

from types import SimpleNamespace

import pytest

from klangk import nix
from klangk.nix import Nix, NixError

from _helpers import make_settings

SEED = "/steam2/btrfs/klangk-nix/seed"
WS = "/steam2/btrfs/klangk-nix/ws-ws1"


class _Proc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


def _app(seed=None, type=None, enabled=True):
    """A KlangkSettings-backed app with nix_seed configured (or not).

    ``type`` omitted -> the fuse-overlayfs default. ``seed`` omitted ->
    nix_seed unset (feature disabled). ``enabled`` arms ``nix_enabled``
    (#2560); it defaults to True so the backend tests exercise provisioning —
    the flag-off paths are tested explicitly below.
    """
    env = {"KLANGKD_NIX_ENABLED": "1" if enabled else "0"}
    if seed:
        env["KLANGKD_NIX_SEED__PATH"] = seed
    if type:
        env["KLANGKD_NIX_SEED__TYPE"] = type
    settings = make_settings(env=env)
    return SimpleNamespace(state=SimpleNamespace(settings=settings))


def _patch_btrfs(
    monkeypatch,
    *,
    seed_exists=True,
    ws_exists=False,
    fstype="btrfs",
    btrfs_rc=0,
    btrfs_err=b"",
):
    """Fake the btrfs/stat subprocess + os.path existence. Returns call list."""
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        if args[0] == "stat":  # the fstype check (stat -f -c %T <seed>)
            return _Proc(0, fstype.encode(), b"")
        return _Proc(btrfs_rc, b"", btrfs_err)  # btrfs ...

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nix.os.path, "isdir", lambda p: seed_exists)
    monkeypatch.setattr(nix.os.path, "exists", lambda p: ws_exists)
    return calls


# --- gating ----------------------------------------------------------------


async def test_no_op_when_not_configured(monkeypatch):
    calls = _patch_btrfs(monkeypatch)
    n = Nix(_app())  # no nix_seed
    assert n.configured is False
    assert n.available is False
    assert await n.ensure_workspace_nix("ws1") is None
    await n.destroy_workspace_nix("ws1")
    assert calls == []


# --- the nix_enabled master switch (#2560) ---------------------------------


def test_available_is_switch_and_backend():
    """available = nix_enabled AND nix_seed.path — the resolved armed status
    reported by /api/v1/images nix_available."""
    assert Nix(_app(SEED)).available is True
    assert Nix(_app(SEED, enabled=False)).available is False
    assert Nix(_app(None, enabled=False)).available is False
    # the seed alone still counts as "configured" — the switch gates arming
    assert Nix(_app(SEED, enabled=False)).configured is True
    assert Nix(_app()).configured is False


async def test_ensure_skips_and_logs_once_when_disabled(monkeypatch, caplog):
    """Flag off + seed set: ensure is a no-op (start proceeds without /nix),
    logged once per workspace at info — not once per start."""
    calls = _patch_btrfs(monkeypatch, seed_exists=True)
    n = Nix(_app(SEED, type="btrfs-snapshot", enabled=False))
    with caplog.at_level("INFO", logger="klangk.nix"):
        assert await n.ensure_workspace_nix("ws1") is None
        assert await n.ensure_workspace_nix("ws1") is None
    assert calls == []  # no snapshot attempt
    skipped = [r for r in caplog.records if "nix_enabled off" in r.message]
    assert len(skipped) == 1


async def test_destroy_still_runs_when_disabled(monkeypatch):
    """Teardown is gated on the backend, not the switch (#2560): workspace
    delete keeps cleaning up layers while the feature is off."""
    calls = _patch_btrfs(monkeypatch, ws_exists=True)
    n = Nix(_app(SEED, type="btrfs-snapshot", enabled=False))
    await n.destroy_workspace_nix("ws1")
    assert any(a[1:3] == ("subvolume", "delete") for a in calls)


async def test_path_set_without_type_is_fuse(monkeypatch, tmp_path):
    # nix_seed.path set, type omitted -> default fuse-overlayfs (not btrfs).
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "nix").mkdir()
    (seed / "nix.conf").write_text("x")
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _Proc(0)

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nix.os.path, "ismount", lambda p: False)
    n = Nix(_app(str(seed)))
    assert n._type == "fuse-overlayfs"
    await n.ensure_workspace_nix("ws1")
    assert any(a[0] == "fuse-overlayfs" for a in calls)
    assert not any(a[0] == "btrfs" for a in calls)


# --- btrfs-snapshot backend -------------------------------------------------


async def test_btrfs_ensure_snapshots_when_missing(monkeypatch):
    calls = _patch_btrfs(monkeypatch, seed_exists=True, ws_exists=False)
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    assert n.configured is True
    assert await n.ensure_workspace_nix("ws1") == WS
    snap = [a for a in calls if a[1:3] == ("subvolume", "snapshot")]
    # source=seed, dest=ws — a swap would overwrite the shared seed.
    assert snap and snap[0][3] == SEED and snap[0][4] == WS


async def test_btrfs_ensure_reuses_existing(monkeypatch):
    calls = _patch_btrfs(monkeypatch, seed_exists=True, ws_exists=True)
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    assert await n.ensure_workspace_nix("ws1") == WS
    assert not any(a[1:3] == ("subvolume", "snapshot") for a in calls)


async def test_btrfs_ensure_raises_when_seed_missing(monkeypatch):
    _patch_btrfs(monkeypatch, seed_exists=False)
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    with pytest.raises(NixError, match="seed subvolume"):
        await n.ensure_workspace_nix("ws1")


async def test_btrfs_ensure_raises_when_seed_not_on_btrfs(monkeypatch):
    # type is btrfs-snapshot but the seed path is on a non-btrfs filesystem.
    _patch_btrfs(monkeypatch, seed_exists=True, fstype="ext4")
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    with pytest.raises(NixError, match="not btrfs"):
        await n.ensure_workspace_nix("ws1")


async def test_btrfs_ensure_raises_when_snapshot_fails(monkeypatch):
    _patch_btrfs(
        monkeypatch,
        seed_exists=True,
        ws_exists=False,
        btrfs_rc=1,
        btrfs_err=b"read-only filesystem",
    )
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    with pytest.raises(NixError, match="read-only filesystem"):
        await n.ensure_workspace_nix("ws1")


async def test_btrfs_destroy_succeeds(monkeypatch):
    calls = _patch_btrfs(monkeypatch, ws_exists=True, btrfs_rc=0)
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    await n.destroy_workspace_nix("ws1")
    delete = [a for a in calls if a[1:3] == ("subvolume", "delete")]
    assert (
        delete and delete[0][3] == WS
    )  # deletes the ws snapshot, never the seed


async def test_btrfs_destroy_noop_when_missing(monkeypatch):
    calls = _patch_btrfs(monkeypatch, ws_exists=False)
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    await n.destroy_workspace_nix("ws1")
    assert calls == []  # no btrfs call


async def test_btrfs_destroy_warns_on_error(monkeypatch, caplog):
    _patch_btrfs(monkeypatch, ws_exists=True, btrfs_rc=1, btrfs_err=b"busy")
    n = Nix(_app(SEED, type="btrfs-snapshot"))
    with caplog.at_level("WARNING", logger="klangk.nix"):
        await n.destroy_workspace_nix("ws1")
    assert any("btrfs subvolume delete" in r.message for r in caplog.records)


# --- fuse-overlayfs backend -------------------------------------------------
#
# The fuse subprocess calls (fuse-overlayfs / fusermount3) are faked; the
# filesystem ops (makedirs/copyfile/rmtree) run on a real tmp_path. ismount is
# patched because a tmp_path dir is never a real mount.


def _fuse_seed(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "nix").mkdir()
    (seed / "nix.conf").write_text(
        "experimental-features = nix-command flakes\n"
    )
    return seed


def _patch_fuse(monkeypatch, *, mounted=False, stale=False, rc=0, err=b""):
    """Fake the fuse subprocess + ismount. Returns the call list."""
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _Proc(rc, b"", err)

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nix.os.path, "ismount", lambda p: mounted)
    if stale:

        def _boom(p):
            raise OSError("stale fuse handle")

        monkeypatch.setattr(nix.os, "listdir", _boom)
    return calls


async def test_fuse_ensure_mounts_when_missing(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    calls = _patch_fuse(monkeypatch, mounted=False)
    n = Nix(_app(str(seed), type="fuse-overlayfs"))
    assert n.configured is True
    ws = await n.ensure_workspace_nix("ws1")
    assert ws == str(tmp_path / "ws-ws1")
    fuse_calls = [a for a in calls if a[0] == "fuse-overlayfs"]
    assert len(fuse_calls) == 1
    assert fuse_calls[0][1] == str(tmp_path / "ws-ws1" / "nix")
    assert f"lowerdir={seed}/nix" in fuse_calls[0][3]
    # mountpoint scaffolded + nix.conf copied alongside the merged mount
    assert (tmp_path / "ws-ws1" / "upper").is_dir()
    assert (tmp_path / "ws-ws1" / "work").is_dir()
    assert (tmp_path / "ws-ws1" / "nix").is_dir()
    assert (
        (tmp_path / "ws-ws1" / "nix.conf")
        .read_text()
        .startswith("experimental-features")
    )


async def test_fuse_ensure_reuses_live_mount(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    merged = tmp_path / "ws-ws1" / "nix"
    merged.mkdir(parents=True)  # an existing (live) mount target
    calls = _patch_fuse(
        monkeypatch, mounted=True
    )  # ismount True; probe listdir OK
    n = Nix(_app(str(seed)))
    ws = await n.ensure_workspace_nix("ws1")
    assert ws == str(tmp_path / "ws-ws1")
    # reused — no remount, no nix.conf re-copy
    assert not any(a[0] == "fuse-overlayfs" for a in calls)


async def test_fuse_ensure_remounts_when_stale(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    merged = tmp_path / "ws-ws1" / "nix"
    merged.mkdir(parents=True)
    calls = _patch_fuse(monkeypatch, mounted=True, stale=True)
    n = Nix(_app(str(seed)))
    await n.ensure_workspace_nix("ws1")
    assert any(a[:2] == ("fusermount3", "-u") for a in calls)  # unmount stale
    assert any(a[0] == "fuse-overlayfs" for a in calls)  # then remount


async def test_fuse_ensure_raises_when_seed_missing(monkeypatch, tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()  # but no seed/nix
    _patch_fuse(monkeypatch)
    n = Nix(_app(str(seed)))
    with pytest.raises(NixError, match="seed directory"):
        await n.ensure_workspace_nix("ws1")


async def test_fuse_ensure_raises_when_mount_fails(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    _patch_fuse(
        monkeypatch, mounted=False, rc=1, err=b"fuse-overlayfs: no /dev/fuse"
    )
    n = Nix(_app(str(seed)))
    with pytest.raises(NixError, match="no /dev/fuse"):
        await n.ensure_workspace_nix("ws1")


async def test_fuse_destroy_unmounts(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    ws = tmp_path / "ws-ws1"
    (ws / "nix").mkdir(parents=True)
    (ws / "upper").mkdir()
    (ws / "nix.conf").write_text("x")
    calls = _patch_fuse(monkeypatch, mounted=True)
    n = Nix(_app(str(seed)))
    await n.destroy_workspace_nix("ws1")
    assert any(a[:2] == ("fusermount3", "-u") for a in calls)
    assert not ws.exists()


async def test_fuse_destroy_not_mounted(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    ws = tmp_path / "ws-ws1"
    (ws / "upper").mkdir(parents=True)
    (ws / "nix.conf").write_text("x")
    calls = _patch_fuse(monkeypatch, mounted=False)
    n = Nix(_app(str(seed)))
    await n.destroy_workspace_nix("ws1")
    assert not any(a[0] == "fusermount3" for a in calls)
    assert not ws.exists()


async def test_fuse_destroy_missing_is_noop(monkeypatch, tmp_path):
    seed = _fuse_seed(tmp_path)
    calls = _patch_fuse(monkeypatch)
    n = Nix(_app(str(seed)))
    await n.destroy_workspace_nix("ws1")  # no ws dir
    assert calls == []


async def test_fuse_destroy_removes_readonly_store_files(
    monkeypatch, tmp_path
):
    # nix store paths are read-only (0444 files / 0555 dirs); rmtree_rw must
    # chmod them away so destroy completes.
    seed = _fuse_seed(tmp_path)
    ws = tmp_path / "ws-ws1"
    ro = ws / "upper" / "store" / "abc-path" / "bin" / "hello"
    ro.parent.mkdir(parents=True)
    ro.write_text("x")
    ro.chmod(0o444)
    ro.parent.chmod(0o555)
    _patch_fuse(monkeypatch, mounted=False)
    n = Nix(_app(str(seed)))
    await n.destroy_workspace_nix("ws1")
    assert not ws.exists()


async def test_fuse_destroy_warns_on_unmount_error(
    monkeypatch, tmp_path, caplog
):
    seed = _fuse_seed(tmp_path)
    ws = tmp_path / "ws-ws1"
    (ws / "nix").mkdir(parents=True)
    (ws / "nix.conf").write_text("x")
    calls = _patch_fuse(monkeypatch, mounted=True, rc=1, err=b"mount busy")
    n = Nix(_app(str(seed)))
    with caplog.at_level("WARNING", logger="klangk.nix"):
        await n.destroy_workspace_nix("ws1")
    assert any("fusermount3 -u" in r.message for r in caplog.records)
    # A failed unmount falls back to a lazy one so a busy mount doesn't pin
    # the ws dir (#2220 review).
    assert any(a[:3] == ("fusermount3", "-u", "-z") for a in calls)


class _SlowProc:
    """A proc whose communicate() never resolves — for the _run timeout test."""

    def __init__(self):
        self.killed = False
        self.returncode = None

    async def communicate(self):
        await nix.asyncio.sleep(999)  # cancelled by wait_for on timeout

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


async def test_run_raises_nixerror_when_binary_missing(monkeypatch, tmp_path):
    """A missing fuse-overlayfs/btrfs/fusermount3 surfaces as NixError, not a
    raw FileNotFoundError out of container start (#2220 review)."""
    seed = _fuse_seed(tmp_path)

    async def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", boom)
    monkeypatch.setattr(nix.os.path, "ismount", lambda p: False)
    n = Nix(_app(str(seed), type="fuse-overlayfs"))
    with pytest.raises(NixError, match="fuse-overlayfs not found"):
        await n.ensure_workspace_nix("ws1")


async def test_run_timeout_raises_nixerror_and_kills(monkeypatch):
    """A wedged fuse-overlayfs/fusermount3 times out + is killed, not hung
    (#2220 review)."""
    proc = _SlowProc()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    n = Nix(_app("/seed", type="fuse-overlayfs"))
    with pytest.raises(NixError, match="timed out"):
        await n._run(["fuse-overlayfs"], timeout=0.05)
    assert proc.killed


async def test_rmtree_rw_logs_when_retry_fails(monkeypatch, tmp_path, caplog):
    # Cover the onerror's except branch: a retry that still fails logs a warning
    # instead of raising (best-effort cleanup). We capture the onerror rmtree_rw
    # hands to shutil.rmtree and invoke it with a failing op.
    target = tmp_path / "tree"
    target.mkdir()
    captured = {}

    def fake_rmtree(path, onerror=None):
        captured["onerror"] = onerror  # don't actually remove

    monkeypatch.setattr(nix.shutil, "rmtree", fake_rmtree)
    with caplog.at_level("WARNING", logger="klangk.nix"):
        nix.rmtree_rw(str(target))
        # Simulate rmtree hitting a stuck entry: onerror retries, the retry fails.

        def _boom(_p):
            raise OSError("stuck")

        captured["onerror"](_boom, str(target / "x"), None)
    assert any("could not remove" in r.message for r in caplog.records)


async def test_fuse_destroy_lazy_unmount_success_no_orphan_warning(
    monkeypatch, tmp_path, caplog
):
    """#2834: the first unmount fails (busy) but the LAZY one succeeds:
    no "left as an orphan" warning, the ws dir is still reaped."""
    seed = _fuse_seed(tmp_path)
    ws = tmp_path / "ws-ws1"
    (ws / "nix").mkdir(parents=True)
    (ws / "nix.conf").write_text("x")
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        # Plain unmount fails; the lazy (-z) retry succeeds.
        rc = 1 if "-z" not in args else 0
        return _Proc(rc, b"", b"mount busy")

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nix.os.path, "ismount", lambda p: True)
    n = Nix(_app(str(seed)))
    with caplog.at_level("WARNING", logger="klangk.nix"):
        await n.destroy_workspace_nix("ws1")
    assert any(a[:3] == ("fusermount3", "-u", "-z") for a in calls)
    assert not any("orphan" in r.message for r in caplog.records)


async def test_rmtree_onerror_root_path_skips_parent_chmod(tmp_path):
    """#2834: the rmtree error handler never chmods the shared seed
    parent when the failing entry IS the rmtree root."""
    import os as os_mod
    import stat as stat_mod

    parent = tmp_path / "seed"
    parent.mkdir()
    root = parent / "ws-x"
    root.mkdir()
    (root / "file").write_text("x")
    # The root's own rmdir fails (parent read-only) -> onerror(p == root)
    # -> the parent chmod is skipped (not ours to touch).
    parent.chmod(0o555)
    try:
        nix.rmtree_rw(str(root))
        # The seed parent was left at its original mode: the guard held.
        assert stat_mod.S_IMODE(parent.stat().st_mode) == 0o555
    finally:
        parent.chmod(0o755)
        os_mod.chmod(root, 0o755)
        for child in root.iterdir():
            child.chmod(0o755)
