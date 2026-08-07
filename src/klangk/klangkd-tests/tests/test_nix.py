"""Unit tests for ``klangk.nix`` — the per-workspace zfs-clone lifecycle (#2201).

The zfs subprocess calls are faked (no real zfs/pool needed); these cover the
gating, clone/destroy idempotency, and error paths.
"""

from types import SimpleNamespace

import pytest

from klangk import nix
from klangk.nix import Nix, NixError

from _helpers import make_settings


class _Proc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


def _app(nix_enabled="", nix_zfs_dataset=None):
    env = {}
    if nix_enabled:
        env["KLANGKD_NIX_ENABLED"] = nix_enabled
    if nix_zfs_dataset:
        env["KLANGKD_NIX_ZFS_DATASET"] = nix_zfs_dataset
    settings = make_settings(env=env)
    return SimpleNamespace(state=SimpleNamespace(settings=settings))


def _patch_zfs(monkeypatch, resolver):
    """Patch ``asyncio.create_subprocess_exec``; resolver(args) -> (rc, out, err).

    *args* excludes the leading ``zfs``. Returns the recorded call list.
    """

    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        rc, out, err = resolver(args[1:])
        return _Proc(rc, out, err)

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    return calls


# --- gating -----------------------------------------------------------------


async def test_disabled_when_flag_unset(monkeypatch):
    calls = _patch_zfs(monkeypatch, lambda a: (1, b"", b""))
    n = Nix(_app())
    assert n.enabled is False
    assert await n.ensure_workspace_nix("ws1") is None
    await n.destroy_workspace_nix("ws1")  # no-op
    assert calls == []


async def test_disabled_when_dataset_missing(monkeypatch):
    _patch_zfs(monkeypatch, lambda a: (1, b"", b""))
    n = Nix(_app(nix_enabled="true"))
    assert n.enabled is False
    assert await n.ensure_workspace_nix("ws1") is None


# --- ensure_workspace_nix ---------------------------------------------------


def _resolver(seed_snap_exists, ws_exists, mountpoint="/tank/nix/ws-ws1"):
    def resolve(args):
        if args[0] == "list":
            field, target = args[3], args[4]
            if field == "name":
                if target.endswith("@base"):
                    return (0 if seed_snap_exists else 1, b"", b"nope")
                # ws dataset existence probe
                return (0 if ws_exists else 1, b"", b"does not exist")
            if field == "mountpoint":
                return 0, mountpoint.encode() + b"\n", b""
        if args[0] == "clone":
            return 0, b"", b""
        return 0, b"", b""

    return resolve


async def test_ensure_clones_when_missing(monkeypatch):
    calls = _patch_zfs(
        monkeypatch, _resolver(seed_snap_exists=True, ws_exists=False)
    )
    n = Nix(_app("true", "tank/nix"))
    assert n.enabled is True
    mp = await n.ensure_workspace_nix("ws1")
    assert mp == "/tank/nix/ws-ws1"
    assert any(c[1:][0] == "clone" for c in calls)


async def test_ensure_reuses_existing_clone(monkeypatch):
    calls = _patch_zfs(
        monkeypatch, _resolver(seed_snap_exists=True, ws_exists=True)
    )
    n = Nix(_app("true", "tank/nix"))
    mp = await n.ensure_workspace_nix("ws1")
    assert mp == "/tank/nix/ws-ws1"
    assert not any(c[1:][0] == "clone" for c in calls)  # no clone call


async def test_ensure_raises_when_seed_snapshot_missing(monkeypatch):
    _patch_zfs(monkeypatch, _resolver(seed_snap_exists=False, ws_exists=False))
    n = Nix(_app("true", "tank/nix"))
    with pytest.raises(NixError, match="seed snapshot"):
        await n.ensure_workspace_nix("ws1")


async def test_ensure_raises_when_mountpoint_none(monkeypatch):
    _patch_zfs(
        monkeypatch,
        _resolver(seed_snap_exists=True, ws_exists=True, mountpoint="none"),
    )
    n = Nix(_app("true", "tank/nix"))
    with pytest.raises(NixError, match="mountpoint"):
        await n.ensure_workspace_nix("ws1")


async def test_ensure_raises_when_clone_fails(monkeypatch):
    def resolve(args):
        if args[0] == "list":
            if args[4].endswith("@base"):
                return 0, b"", b""
            return 1, b"", b"does not exist"  # ws missing
        if args[0] == "clone":
            return 1, b"", b"permission denied"
        return 0, b"", b""

    _patch_zfs(monkeypatch, resolve)
    n = Nix(_app("true", "tank/nix"))
    with pytest.raises(NixError, match="permission denied"):
        await n.ensure_workspace_nix("ws1")


# --- destroy_workspace_nix --------------------------------------------------


def _destroy_resolver(ws_dataset, destroy_rc=0, destroy_err=b""):
    def resolve(args):
        if args[0] == "destroy":
            assert args[2] == ws_dataset
            return destroy_rc, b"", destroy_err
        return 0, b"", b""

    return resolve


async def test_destroy_succeeds(monkeypatch):
    calls = _patch_zfs(
        monkeypatch, _destroy_resolver("tank/nix/ws-ws1", destroy_rc=0)
    )
    n = Nix(_app("true", "tank/nix"))
    await n.destroy_workspace_nix("ws1")
    assert any(c[1:][0] == "destroy" for c in calls)


async def test_destroy_ignores_missing(monkeypatch):
    calls = _patch_zfs(
        monkeypatch,
        _destroy_resolver(
            "tank/nix/ws-ws1",
            destroy_rc=1,
            destroy_err=b"dataset does not exist",
        ),
    )
    n = Nix(_app("true", "tank/nix"))
    await n.destroy_workspace_nix("ws1")  # no warning, no raise
    assert any(c[1:][0] == "destroy" for c in calls)


async def test_destroy_warns_on_other_error(monkeypatch, caplog):
    _patch_zfs(
        monkeypatch,
        _destroy_resolver(
            "tank/nix/ws-ws1", destroy_rc=1, destroy_err=b"busy"
        ),
    )
    n = Nix(_app("true", "tank/nix"))
    with caplog.at_level("WARNING", logger="klangk.nix"):
        await n.destroy_workspace_nix("ws1")
    assert any("zfs destroy" in r.message for r in caplog.records)
