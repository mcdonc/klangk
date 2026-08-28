"""eBPF process-ledger watcher tests (#2520 spike).

Two layers:

- **Compile-only** (runs everywhere devenv runs): the BPF object and the
  loader build with the unwrapped clang + libbpf from the devenv env.
- **Runtime** (skips without CAP_BPF + CAP_PERFMON): loads the real
  monitor, scopes it at a root pid, forks+execs a child under that root,
  and asserts the NDJSON birth event arrives — the exact capture the
  /proc poller cannot do for short-lived processes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "scripts" / "procleddy-ebpf"
OUT = REPO / "src" / "klangk" / "klangk"


def _have_clang() -> bool:
    return shutil.which("clang") is not None


@pytest.fixture(scope="module")
def ebpf_bin(tmp_path_factory):
    """Build the BPF object + loader the way the devenv task does."""
    if os.uname().sysname != "Linux":
        pytest.skip("Linux only")
    if not _have_clang():
        pytest.skip("no clang in this environment")
    outdir = tmp_path_factory.mktemp("procleddy-ebpf")
    bpf_o = outdir / "procleddy-ebpf.bpf.o"
    loader = outdir / "procleddy-ebpf"
    # Unwrapped clang (see devenv.nix task): the cc-wrapper's hardening
    # flags are rejected by the bpf backend. Resolve it from the wrapper's
    # package via nix-style paths, or fall back to any clang and strip
    # the incompatible flags by targeting bpf with -fno-* disables.
    clang = shutil.which("clang")
    bpf_cc = [
        clang,
        # NIX_HARDENING_ENABLE= disables the cc-wrapper's injected
        # hardening flags, which the bpf backend rejects.
        "-target",
        "bpf",
        "-O2",
        "-g",
        "-I",
        str(SRC),
    ]
    import os as _os

    env = dict(_os.environ, NIX_HARDENING_ENABLE="")
    r = subprocess.run(
        bpf_cc + ["-c", str(SRC / "procleddy_ebpf.bpf.c"), "-o", str(bpf_o)],
        capture_output=True,
        text=True,
        env=env,
    )
    if r.returncode != 0 and ("not supported" in r.stderr or "unsupported option" in r.stderr):
        pytest.skip(f"this clang cannot target bpf: {r.stderr[:200]}")
    assert r.returncode == 0, r.stderr
    cflags = subprocess.run(
        ["pkg-config", "--cflags", "--libs", "libbpf"],
        capture_output=True,
        text=True,
    )
    assert cflags.returncode == 0, cflags.stderr
    r = subprocess.run(
        [
            shutil.which("cc"),
            "-O2",
            "-I",
            str(SRC),
            "-o",
            str(loader),
            str(SRC / "procleddy-ebpf.c"),
            *cflags.stdout.split(),
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return loader, bpf_o


def test_compiles(ebpf_bin):
    loader, bpf_o = ebpf_bin
    assert loader.exists() and loader.stat().st_mode & 0o111
    assert bpf_o.exists()


def _can_load_bpf(loader, bpf_o) -> bool:
    """True when the monitor can load BPF programs in this context."""
    p = subprocess.run(
        [str(loader)],
        input=b"",
        capture_output=True,
        timeout=10,
        cwd=str(bpf_o.parent),
    )
    return b"BPF load failed" not in p.stderr


def test_runtime_birth_event(ebpf_bin):
    loader, bpf_o = ebpf_bin
    if not _can_load_bpf(loader, bpf_o):
        pytest.skip("no CAP_BPF/CAP_PERFMON in this environment")
    root = os.getpid()
    proc = subprocess.Popen(
        [str(loader)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(bpf_o.parent),
    )
    try:
        proc.stdin.write(
            json.dumps({"type": "scope", "roots": [root]}).encode() + b"\n"
        )
        proc.stdin.flush()
        # Fork+exec under the root: the monitor's parents map links the
        # child to us and the exec event must arrive immediately.
        subprocess.run(
            ["/bin/sh", "-c", "true"],
            check=True,
            capture_output=True,
        )
        deadline = time.time() + 5
        events = []
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
            if any(
                e.get("type") == "birth" and e.get("ppid") == root
                for e in events
            ):
                break
        births = [e for e in events if e.get("type") == "birth"]
        assert any(
            b["ppid"] == root and b["argv"].startswith("/bin/sh")
            for b in births
        ), events
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
