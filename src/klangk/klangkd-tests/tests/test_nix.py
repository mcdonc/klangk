"""Unit tests for ``klangk.nix`` — the per-workspace btrfs-snapshot lifecycle (#2201).

The btrfs subprocess calls and filesystem existence checks are faked (no real
btrfs needed); these cover the btrfs-configured gating, snapshot/delete
idempotency, and error paths. The per-workspace ``nix`` flag is the caller's
gate (container.py) — the module only cares whether btrfs is configured.
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


def _app(subvol=None):
    env = {"KLANGKD_NIX_BTRFS_SUBVOLUME": subvol} if subvol else {}
    settings = make_settings(env=env)
    return SimpleNamespace(state=SimpleNamespace(settings=settings))


def _patch(
    monkeypatch,
    *,
    seed_exists=True,
    ws_exists=False,
    btrfs_rc=0,
    btrfs_err=b"",
):
    """Fake the btrfs subprocess + os.path existence checks. Returns call list."""
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _Proc(btrfs_rc, b"", btrfs_err)

    monkeypatch.setattr(nix.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(nix.os.path, "isdir", lambda p: seed_exists)
    monkeypatch.setattr(nix.os.path, "exists", lambda p: ws_exists)
    return calls


# --- gating -----------------------------------------------------------------


async def test_no_op_when_btrfs_not_configured(monkeypatch):
    calls = _patch(monkeypatch)
    n = Nix(_app())
    assert n.btrfs_configured is False
    assert await n.ensure_workspace_nix("ws1") is None
    await n.destroy_workspace_nix("ws1")
    assert calls == []


# --- ensure_workspace_nix ---------------------------------------------------


async def test_ensure_snapshots_when_missing(monkeypatch):
    calls = _patch(monkeypatch, seed_exists=True, ws_exists=False)
    n = Nix(_app(SEED))
    assert n.btrfs_configured is True
    assert await n.ensure_workspace_nix("ws1") == WS
    assert any(a[1:3] == ("subvolume", "snapshot") for a in calls)


async def test_ensure_reuses_existing(monkeypatch):
    calls = _patch(monkeypatch, seed_exists=True, ws_exists=True)
    n = Nix(_app(SEED))
    assert await n.ensure_workspace_nix("ws1") == WS
    assert not any(a[1:3] == ("subvolume", "snapshot") for a in calls)


async def test_ensure_raises_when_seed_missing(monkeypatch):
    _patch(monkeypatch, seed_exists=False)
    n = Nix(_app(SEED))
    with pytest.raises(NixError, match="seed subvolume"):
        await n.ensure_workspace_nix("ws1")


async def test_ensure_raises_when_snapshot_fails(monkeypatch):
    _patch(
        monkeypatch,
        seed_exists=True,
        ws_exists=False,
        btrfs_rc=1,
        btrfs_err=b"read-only filesystem",
    )
    n = Nix(_app(SEED))
    with pytest.raises(NixError, match="read-only filesystem"):
        await n.ensure_workspace_nix("ws1")


# --- destroy_workspace_nix --------------------------------------------------


async def test_destroy_succeeds(monkeypatch):
    calls = _patch(monkeypatch, ws_exists=True, btrfs_rc=0)
    n = Nix(_app(SEED))
    await n.destroy_workspace_nix("ws1")
    assert any(a[1:3] == ("subvolume", "delete") for a in calls)


async def test_destroy_noop_when_missing(monkeypatch):
    calls = _patch(monkeypatch, ws_exists=False)
    n = Nix(_app(SEED))
    await n.destroy_workspace_nix("ws1")
    assert calls == []  # no btrfs call


async def test_destroy_warns_on_error(monkeypatch, caplog):
    _patch(monkeypatch, ws_exists=True, btrfs_rc=1, btrfs_err=b"busy")
    n = Nix(_app(SEED))
    with caplog.at_level("WARNING", logger="klangk.nix"):
        await n.destroy_workspace_nix("ws1")
    assert any("btrfs subvolume delete" in r.message for r in caplog.records)
