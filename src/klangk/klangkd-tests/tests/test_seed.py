"""Tests for the wheel-installed nix-seed builder (#2225)."""

from __future__ import annotations

import asyncio
import importlib.resources as _ires
import io
import os
import tarfile
from pathlib import Path

import pytest

from klangk import seed

_PODMAN = "podman"


# --- helpers ---------------------------------------------------------------


def _aprocs(proc):
    """Wrap a fake proc so create_subprocess_exec returns it (awaitable)."""

    async def _f(*a, **k):
        return proc

    return _f


class _Proc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._o = out
        self._e = err

    async def communicate(self):
        return self._o, self._e


class _Slow:
    """A proc whose communicate() never resolves — for the run timeout test."""

    def __init__(self):
        self.killed = False
        self.returncode = None

    async def communicate(self):
        await asyncio.sleep(999)

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def _tar_bytes(entries):
    """Build an in-memory tar: entries = list of (name, kind, data)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name=name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            else:
                payload = data or b""
                info.size = len(payload)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf


class _FakePopen:
    """Stands in for subprocess.Popen for the export_to_dir_sync tests."""

    def __init__(self, tar_buf, rc=0, err=b""):
        self.stdout = tar_buf
        self.stderr = io.BytesIO(err)
        self.returncode = rc

    def wait(self):
        return self.returncode


# --- resolve_podman_bin ----------------------------------------------------


def test_resolve_podman_bin_default():
    assert seed.resolve_podman_bin({}) == _PODMAN


def test_resolve_podman_bin_env_override():
    assert (
        seed.resolve_podman_bin({"KLANGKD_PODMAN_BIN": "/x/podman"})
        == "/x/podman"
    )


# --- sig_policy_args ------------------------------------------------------


def test_sig_policy_unset(monkeypatch):
    monkeypatch.delenv("CONTAINERS_SIGNATURE_POLICY", raising=False)
    assert seed.sig_policy_args() == []


def test_sig_policy_existing(tmp_path, monkeypatch):
    pol = tmp_path / "pol.json"
    pol.write_text("{}")
    monkeypatch.setenv("CONTAINERS_SIGNATURE_POLICY", str(pol))
    assert seed.sig_policy_args() == ["--signature-policy", str(pol)]


def test_sig_policy_creates_file(tmp_path, monkeypatch):
    pol = tmp_path / "sub" / "pol.json"
    monkeypatch.setenv("CONTAINERS_SIGNATURE_POLICY", str(pol))
    assert seed.sig_policy_args() == ["--signature-policy", str(pol)]
    assert (
        pol.read_text() == '{"default": [{"type": "insecureAcceptAnything"}]}'
    )


# --- run ------------------------------------------------------------------


async def test_run_success(monkeypatch):
    monkeypatch.setattr(
        seed.asyncio, "create_subprocess_exec", _aprocs(_Proc(0, b"ok", b""))
    )
    rc, out, _ = await seed.run(_PODMAN, ["ps"])
    assert (rc, out) == (0, "ok")


async def test_run_binary_missing(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError(2, "no such file", _PODMAN)

    monkeypatch.setattr(seed.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(seed.SeedError, match="podman not found"):
        await seed.run(_PODMAN, ["ps"])


async def test_run_timeout(monkeypatch):
    p = _Slow()
    monkeypatch.setattr(seed.asyncio, "create_subprocess_exec", _aprocs(p))
    with pytest.raises(seed.SeedError, match="timed out"):
        await seed.run(_PODMAN, ["build"], timeout=0.05)
    assert p.killed


async def test_run_rc_nonzero_raises(monkeypatch):
    monkeypatch.setattr(
        seed.asyncio,
        "create_subprocess_exec",
        _aprocs(_Proc(1, b"", b"broken")),
    )
    with pytest.raises(seed.SeedError, match="failed"):
        await seed.run(_PODMAN, ["ps"])


async def test_run_rc_nonzero_no_check(monkeypatch):
    monkeypatch.setattr(
        seed.asyncio,
        "create_subprocess_exec",
        _aprocs(_Proc(1, b"", b"broken")),
    )
    rc, _, err = await seed.run(_PODMAN, ["ps"], check=False)
    assert rc == 1
    assert "broken" in err


# --- build_image ----------------------------------------------------------


async def test_build_image_args(tmp_path, monkeypatch):
    captured = {}

    async def fake_run(podman, args, **kw):
        captured["args"] = args
        captured["kw"] = kw
        return (0, "", "")

    monkeypatch.setattr(seed, "run", fake_run)
    monkeypatch.setenv("KLANGKBUILD_PLATFORM", "linux/arm64")
    monkeypatch.setenv(
        "CONTAINERS_SIGNATURE_POLICY", str(tmp_path / "pol.json")
    )
    # nix CI runner signal: --security-opt unmask=ALL must be applied
    # (d66ec5cc) — set explicitly so the branch is covered on every
    # runner, not only where the env happens to carry it.
    monkeypatch.setenv("CONTAINERS_STORAGE_CONF", "/tmp/storage.conf")
    await seed.build_image(_PODMAN, "FROM x\n", no_cache=True)
    a = captured["args"]
    assert a[0] == "build"
    assert "--signature-policy" in a
    assert "--platform" in a and "linux/arm64" in a
    assert "--no-cache" in a
    assert "--security-opt" in a and "unmask=ALL" in a
    assert "-t" in a and seed.IMAGE in a
    assert captured["kw"]["timeout"] == 2400.0


async def test_build_image_minimal(monkeypatch):
    """No platform, no sig policy, no --no-cache: minimal build args."""
    captured = {}

    async def fake_run(podman, args, **kw):
        captured["args"] = args
        return (0, "", "")

    monkeypatch.setattr(seed, "run", fake_run)
    monkeypatch.delenv("KLANGKBUILD_PLATFORM", raising=False)
    monkeypatch.delenv("CONTAINERS_SIGNATURE_POLICY", raising=False)
    monkeypatch.delenv("CONTAINERS_STORAGE_CONF", raising=False)
    await seed.build_image(_PODMAN, "FROM x\n", no_cache=False)
    a = captured["args"]
    assert "--platform" not in a
    assert "--signature-policy" not in a
    assert "--no-cache" not in a
    assert "--security-opt" not in a


# --- export_to_dir_sync (the streaming tarfile extraction) ----------------


def test_export_to_dir_sync_extracts_nix_and_conf(tmp_path, monkeypatch):
    buf = _tar_bytes(
        [
            ("nix", "dir", None),
            ("nix/store", "dir", None),
            ("etc/other", "file", b"skip"),  # not whitelisted
            ("etc/nix/nix.conf", "file", b"experimental = "),
        ]
    )
    monkeypatch.setattr(
        seed.subprocess, "Popen", lambda *a, **k: _FakePopen(buf)
    )
    out = tmp_path / "seed"
    out.mkdir()
    seed.export_to_dir_sync(_PODMAN, "cid", str(out))
    assert (out / "nix").is_dir()
    assert (out / "nix" / "store").is_dir()
    assert (
        out / "etc" / "nix" / "nix.conf"
    ).read_bytes() == b"experimental = "
    assert not (out / "etc" / "other").exists()  # non-whitelisted skipped


def test_export_to_dir_sync_strips_leading_dot_slash(tmp_path, monkeypatch):
    buf = _tar_bytes(
        [("./nix", "dir", None), ("./etc/nix/nix.conf", "file", b"x")]
    )
    monkeypatch.setattr(
        seed.subprocess, "Popen", lambda *a, **k: _FakePopen(buf)
    )
    out = tmp_path / "seed"
    out.mkdir()
    seed.export_to_dir_sync(_PODMAN, "cid", str(out))
    assert (out / "nix").is_dir()
    assert (out / "etc" / "nix" / "nix.conf").read_bytes() == b"x"


def test_export_to_dir_sync_rc_fail(tmp_path, monkeypatch):
    # empty stream + non-zero returncode -> "podman export failed"
    monkeypatch.setattr(
        seed.subprocess,
        "Popen",
        lambda *a, **k: _FakePopen(io.BytesIO(), rc=1, err=b"export boom"),
    )
    with pytest.raises(seed.SeedError, match="podman export failed"):
        seed.export_to_dir_sync(_PODMAN, "cid", str(tmp_path / "seed"))


def test_export_to_dir_sync_empty_stream_rc_zero(tmp_path, monkeypatch):
    # rc 0 but no readable members -> "no readable tar stream"
    monkeypatch.setattr(
        seed.subprocess,
        "Popen",
        lambda *a, **k: _FakePopen(io.BytesIO(), rc=0),
    )
    with pytest.raises(seed.SeedError, match="no readable tar stream"):
        seed.export_to_dir_sync(_PODMAN, "cid", str(tmp_path / "seed"))


# --- export_and_extract (orchestration) ----------------------------------


def _wire_export(monkeypatch, *, cid="cid123", sync=None):
    """Patch run for create+rm and export_to_dir_sync for the extraction."""

    async def fake_run(podman, args, **kw):
        if args[:1] == ["create"]:
            return (0, f"{cid}\n", "")
        if args[:1] == ["rm"]:
            return (0, "", "")
        raise AssertionError(f"unexpected run: {args}")

    monkeypatch.setattr(seed, "run", fake_run)
    if sync is not None:
        monkeypatch.setattr(seed, "export_to_dir_sync", sync)


async def test_export_and_extract(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    out.mkdir()

    def fake_sync(podman, cid, o):
        os.makedirs(os.path.join(o, "etc", "nix"))
        os.makedirs(os.path.join(o, "nix"))
        with open(os.path.join(o, "etc", "nix", "nix.conf"), "w") as f:
            f.write("experimental-features = ")

    _wire_export(monkeypatch, sync=fake_sync)
    await seed.export_and_extract(_PODMAN, "img", out)
    assert (out / "nix.conf").read_text() == "experimental-features = "
    assert not (out / "etc").exists()  # flattened + rmdir'd


async def test_export_and_extract_no_cid(tmp_path, monkeypatch):
    async def fake_run(podman, args, **kw):
        return (0, "   \n", "")  # blank container id

    monkeypatch.setattr(seed, "run", fake_run)
    with pytest.raises(seed.SeedError, match="no container id"):
        await seed.export_and_extract(_PODMAN, "img", tmp_path)


async def test_export_and_extract_propagates_sync_error(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    out.mkdir()
    rm = []

    async def fake_run(podman, args, **kw):
        if args[:1] == ["create"]:
            return (0, "cid\n", "")
        if args[:1] == ["rm"]:
            rm.append(args)
            return (0, "", "")
        raise AssertionError

    monkeypatch.setattr(seed, "run", fake_run)

    def boom(podman, cid, o):
        raise seed.SeedError("export broken")

    monkeypatch.setattr(seed, "export_to_dir_sync", boom)
    with pytest.raises(seed.SeedError, match="export broken"):
        await seed.export_and_extract(_PODMAN, "img", out)
    assert rm  # the throwaway container is rm'd in the finally


async def test_export_and_extract_missing_conf(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    out.mkdir()

    def fake_sync(podman, cid, o):
        os.makedirs(os.path.join(o, "nix"))  # but no etc/nix/nix.conf

    _wire_export(monkeypatch, sync=fake_sync)
    with pytest.raises(seed.SeedError, match="nix.conf missing"):
        await seed.export_and_extract(_PODMAN, "img", out)


async def test_export_and_extract_overwrites_existing_conf(
    tmp_path, monkeypatch
):
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix.conf").write_text("OLD")  # pre-existing

    def fake_sync(podman, cid, o):
        os.makedirs(os.path.join(o, "etc", "nix"))
        with open(os.path.join(o, "etc", "nix", "nix.conf"), "w") as f:
            f.write("NEW")

    _wire_export(monkeypatch, sync=fake_sync)
    await seed.export_and_extract(_PODMAN, "img", out)
    assert (out / "nix.conf").read_text() == "NEW"


async def test_export_and_extract_leaves_nonempty_etc(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    out.mkdir()

    def fake_sync(podman, cid, o):
        os.makedirs(os.path.join(o, "etc", "nix"))
        with open(os.path.join(o, "etc", "nix", "nix.conf"), "w") as f:
            f.write("x")
        with open(os.path.join(o, "etc", "extra"), "w") as f:  # etc stays full
            f.write("y")

    _wire_export(monkeypatch, sync=fake_sync)
    await seed.export_and_extract(_PODMAN, "img", out)
    assert (out / "nix.conf").exists()
    assert (out / "etc").exists()  # rmdir(etc) failed (non-empty) -> kept


# --- verify ---------------------------------------------------------------


async def test_verify_args(tmp_path, monkeypatch):
    captured = {}
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix").mkdir()
    (out / "nix.conf").write_text("x")

    async def fake_run(podman, args, **kw):
        captured["args"] = args
        captured["kw"] = kw
        return (0, "nix 2.x\n", "")

    monkeypatch.setattr(seed, "run", fake_run)
    await seed.verify(_PODMAN, "img", out)
    a = captured["args"]
    assert a[0] == "run" and "--rm" in a
    assert f"{out / 'nix'}:/nix:ro" in a
    assert f"{out / 'nix.conf'}:/etc/nix/nix.conf:ro" in a
    assert "nix --version" in a[-1]
    assert "devenv --version" in a[-1]
    assert captured["kw"]["timeout"] == 300.0


# --- tree helpers ----------------------------------------------------------


def test_chmod_w(tmp_path):
    d = tmp_path / "nix"
    d.mkdir()
    f = d / "f"
    f.write_text("x")
    f.chmod(0o444)
    sub = d / "sub"
    sub.mkdir()
    g = sub / "g"
    g.write_text("y")
    g.chmod(0o444)
    sub.chmod(0o555)
    seed.chmod_w(d)
    assert os.access(f, os.W_OK)
    assert os.access(g, os.W_OK)


def test_chmod_w_swallows_chmod_errors(tmp_path, monkeypatch):
    """os.chmod failures don't crash chmod_w."""
    d = tmp_path / "nix"
    d.mkdir()
    (d / "f").write_text("x")

    def boom(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr(seed.os, "chmod", boom)
    seed.chmod_w(d)  # no raise


def test_clear_seed_dir(tmp_path):
    out = tmp_path / "seed"
    out.mkdir()
    nix = out / "nix"
    nix.mkdir()
    (nix / "store").mkdir()
    f = nix / "store" / "x"
    f.write_text("x")
    f.chmod(0o444)
    (nix / "store").chmod(0o555)
    nix.chmod(0o555)
    (out / "nix.conf").write_text("old")
    seed.clear_seed_dir(out)
    assert not (out / "nix").exists()
    assert not (out / "nix.conf").exists()


def test_clear_seed_dir_skips_absent(tmp_path):
    """An absent entry (no etc/) is skipped via `continue`."""
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix.conf").write_text("old")
    seed.clear_seed_dir(out)
    assert not (out / "nix.conf").exists()


def test_clear_seed_dir_swallows_unlink_error(tmp_path, monkeypatch):
    """A file whose unlink fails is skipped."""
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix.conf").write_text("old")

    def boom(*a, **k):
        raise OSError("busy")

    monkeypatch.setattr(seed.os, "unlink", boom)
    seed.clear_seed_dir(out)  # no raise
    assert (out / "nix.conf").exists()  # unlink failed -> kept


# --- build_nix_seed orchestration -----------------------------------------


async def test_build_nix_seed_refuses_existing(tmp_path):
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix").mkdir()
    with pytest.raises(seed.SeedError, match="already contains"):
        await seed.build_nix_seed(
            out, podman_bin=_PODMAN, dockerfile_text="FROM x"
        )


async def test_build_nix_seed_happy(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    calls = []

    async def fake_build_img(podman, text, **k):
        calls.append(("build_img", k))

    async def fake_export(podman, image, o):
        calls.append(("export",))
        (out / "nix").mkdir()  # simulate extraction

    async def fake_verify(podman, image, o):
        calls.append(("verify",))

    monkeypatch.setattr(seed, "build_image", fake_build_img)
    monkeypatch.setattr(seed, "export_and_extract", fake_export)
    monkeypatch.setattr(seed, "verify", fake_verify)
    await seed.build_nix_seed(
        out, podman_bin=_PODMAN, dockerfile_text="FROM x", no_cache=True
    )
    assert [c[0] for c in calls] == ["build_img", "export", "verify"]
    assert calls[0][1]["no_cache"] is True


async def test_build_nix_seed_update_clears(tmp_path, monkeypatch):
    out = tmp_path / "seed"
    out.mkdir()
    (out / "nix").mkdir()
    (out / "nix" / "f").write_text("x")
    (out / "nix.conf").write_text("old")
    cleared = {}
    monkeypatch.setattr(
        seed, "clear_seed_dir", lambda o: cleared.__setitem__("o", o)
    )

    async def noop(*a, **k):
        pass

    monkeypatch.setattr(seed, "build_image", noop)
    monkeypatch.setattr(seed, "export_and_extract", noop)
    monkeypatch.setattr(seed, "verify", noop)
    await seed.build_nix_seed(
        out, podman_bin=_PODMAN, dockerfile_text="FROM x", update=True
    )
    assert cleared["o"] == out


# --- read_dockerfile / finders --------------------------------------------


def test_bundled_dockerfile_present(tmp_path, monkeypatch):
    (tmp_path / "nix-seed").mkdir()
    (tmp_path / "nix-seed" / "Dockerfile").write_text("FROM x")
    monkeypatch.setattr(_ires, "files", lambda pkg: tmp_path)
    assert seed.bundled_dockerfile() == tmp_path / "nix-seed" / "Dockerfile"


def test_bundled_dockerfile_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(_ires, "files", lambda pkg: tmp_path)  # no nix-seed/
    assert seed.bundled_dockerfile() is None


def test_bundled_dockerfile_resource_error(monkeypatch):
    """If importlib.resources.files itself raises, return None."""

    def boom(pkg):
        raise FileNotFoundError("no resources")

    monkeypatch.setattr(_ires, "files", boom)
    assert seed.bundled_dockerfile() is None


def test_source_dockerfile_found():
    p = seed.source_dockerfile()
    assert p is not None and p.is_file()  # the worktree's committed Dockerfile


def test_source_dockerfile_absent(tmp_path, monkeypatch):
    fake = tmp_path / "a" / "b" / "c" / "seed.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.setattr(seed, "__file__", str(fake))
    assert seed.source_dockerfile() is None


def test_read_dockerfile_bundled(tmp_path, monkeypatch):
    (tmp_path / "nix-seed").mkdir()
    (tmp_path / "nix-seed" / "Dockerfile").write_text("BUNDLED")
    monkeypatch.setattr(_ires, "files", lambda pkg: tmp_path)
    assert seed.read_dockerfile() == "BUNDLED"


def test_read_dockerfile_source_fallback(monkeypatch):
    monkeypatch.setattr(seed, "bundled_dockerfile", lambda: None)
    real = seed.source_dockerfile()
    assert real is not None  # exists in this worktree
    monkeypatch.setattr(seed, "source_dockerfile", lambda: real)
    assert seed.read_dockerfile() == real.read_text()


def test_read_dockerfile_missing(monkeypatch):
    monkeypatch.setattr(seed, "bundled_dockerfile", lambda: None)
    monkeypatch.setattr(seed, "source_dockerfile", lambda: None)
    with pytest.raises(seed.SeedError, match="could not locate"):
        seed.read_dockerfile()


# --- main ------------------------------------------------------------------


def _wire_main(monkeypatch, build=None, dockerfile="FROM x"):
    monkeypatch.setattr(seed.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(seed, "resolve_podman_bin", lambda: _PODMAN)
    monkeypatch.setattr(seed, "read_dockerfile", lambda: dockerfile)
    if build is not None:
        monkeypatch.setattr(seed, "build_nix_seed", build)


def test_main_missing_podman(monkeypatch):
    monkeypatch.setattr(
        seed.shutil,
        "which",
        lambda b: None if b == "/bad/podman" else "/usr/bin/x",
    )
    monkeypatch.setattr(seed, "resolve_podman_bin", lambda: "/bad/podman")
    assert seed.main(["/out"]) == 2


def test_main_read_dockerfile_error(monkeypatch):
    def boom():
        raise seed.SeedError("no dockerfile")

    monkeypatch.setattr(seed.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(seed, "resolve_podman_bin", lambda: _PODMAN)
    monkeypatch.setattr(seed, "read_dockerfile", boom)
    assert seed.main(["/out"]) == 1


def test_main_happy(tmp_path, monkeypatch):
    seen = {}

    async def fake_build(out, **kw):
        seen.update(kw)
        seen["out"] = out

    _wire_main(monkeypatch, build=fake_build)
    assert seed.main([str(tmp_path / "out")]) == 0
    assert seen["out"] == str(tmp_path / "out")
    assert seen["podman_bin"] == _PODMAN


def test_main_seed_error(tmp_path, monkeypatch):
    async def boom(out, **kw):
        raise seed.SeedError("nope")

    _wire_main(monkeypatch, build=boom)
    assert seed.main([str(tmp_path / "out")]) == 1


def test_main_passes_update_and_nocache(tmp_path, monkeypatch):
    seen = {}

    async def fake_build(out, **kw):
        seen.update(kw)

    _wire_main(monkeypatch, build=fake_build)
    seed.main([str(tmp_path / "out"), "--update", "--no-cache"])
    assert seen["update"] is True
    assert seen["no_cache"] is True


# --- btrfs loader (cp_a, load_nix_seed_btrfs, load_main) ------------------


def test_cp_a_dir_preserves_symlinks(tmp_path):
    src = tmp_path / "nix"
    src.mkdir()
    (src / "f").write_text("x")
    (src / "link").symlink_to("f")
    dst = tmp_path / "out" / "nix"
    seed.cp_a(src, dst)
    assert (dst / "f").read_text() == "x"
    assert (dst / "link").is_symlink()


def test_cp_a_file(tmp_path):
    src = tmp_path / "nix.conf"
    src.write_text("experimental = ")
    (tmp_path / "out").mkdir()
    dst = tmp_path / "out" / "nix.conf"
    seed.cp_a(src, dst)
    assert dst.read_text() == "experimental = "


async def test_load_btrfs_happy(tmp_path, monkeypatch):
    tree = tmp_path / "seed-tree"
    (tree / "nix" / "store").mkdir(parents=True)
    f = tree / "nix" / "store" / "x"
    f.write_text("x")
    f.chmod(0o444)  # read-only store file
    (tree / "nix.conf").write_text("experimental = ")
    calls = []

    async def fake_run(binary, args, **kw):
        calls.append(args)
        if args[:2] == ["subvolume", "create"]:
            Path(args[2]).mkdir(parents=True, exist_ok=True)
            return (0, "", "")
        raise AssertionError(f"unexpected run: {args}")

    monkeypatch.setattr(seed, "run", fake_run)
    parent = tmp_path / "btrfs"
    result = await seed.load_nix_seed_btrfs(tree, parent)
    assert result == parent / "seed"
    assert calls[0][:2] == ["subvolume", "create"]
    assert str(parent / "seed") in calls[0][2]
    assert (parent / "seed" / "nix" / "store" / "x").is_file()
    assert (parent / "seed" / "nix.conf").read_text() == "experimental = "


async def test_load_btrfs_missing_nix(tmp_path):
    tree = tmp_path / "seed-tree"
    tree.mkdir()
    (tree / "nix.conf").write_text("x")  # but no nix/
    with pytest.raises(seed.SeedError, match="/nix not found"):
        await seed.load_nix_seed_btrfs(tree, tmp_path / "btrfs")


async def test_load_btrfs_missing_conf(tmp_path):
    tree = tmp_path / "seed-tree"
    (tree / "nix").mkdir(parents=True)  # but no nix.conf
    with pytest.raises(seed.SeedError, match="nix.conf not found"):
        await seed.load_nix_seed_btrfs(tree, tmp_path / "btrfs")


async def test_load_btrfs_seed_exists(tmp_path):
    tree = tmp_path / "seed-tree"
    (tree / "nix").mkdir(parents=True)
    (tree / "nix.conf").write_text("x")
    parent = tmp_path / "btrfs"
    (parent / "seed").mkdir(parents=True)  # already there
    with pytest.raises(seed.SeedError, match="already exists"):
        await seed.load_nix_seed_btrfs(tree, parent)


def test_load_main_missing_btrfs(monkeypatch):
    monkeypatch.setattr(
        seed.shutil, "which", lambda b: None if b == "btrfs" else "/usr/bin/x"
    )
    assert seed.load_main(["/seed-tree", "/btrfs-parent"]) == 2


def test_load_main_happy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(seed.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def fake_load(tree, parent, **kw):
        return Path(str(parent)) / "seed"

    monkeypatch.setattr(seed, "load_nix_seed_btrfs", fake_load)
    rc = seed.load_main([str(tmp_path / "tree"), str(tmp_path / "parent")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nix_seed:" in out
    assert "btrfs-snapshot" in out
    assert str(tmp_path / "parent" / "seed") in out


def test_load_main_seed_error(monkeypatch):
    monkeypatch.setattr(seed.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def boom(tree, parent, **kw):
        raise seed.SeedError("nope")

    monkeypatch.setattr(seed, "load_nix_seed_btrfs", boom)
    assert seed.load_main(["/tree", "/parent"]) == 1
