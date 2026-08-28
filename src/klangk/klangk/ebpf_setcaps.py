"""``klangk-ebpf-setcaps`` — grant the eBPF watcher its file capabilities.

Deployment helper for the process-ledger eBPF backend (#2520): applies
``cap_bpf,cap_perfmon+ep`` to the ``procleddy-ebpf`` binary, resolving
"the right binary" the same way klangkd does — an explicit
``KLANGKD_PROCESS_LEDGER_WATCHER`` (or ``--path``) wins; otherwise the
wheel-adjacent default next to this module.

Why a helper: the grant must be re-applied after every rebuild/upgrade
(file capabilities live on the inode, so a new binary starts bare), the
path depends on how the deployment installed the wheel, and the caps
must be exactly ``cap_bpf,cap_perfmon`` — no more (see the security
warning in docs/features/process-ledger.md). Run it as root (or via a
passwordless-setcap sudoers rule); it needs ``CAP_SETFCAP`` and the
``setcap``/``getcap`` tools from libcap.

Usage::

    sudo klangk-ebpf-setcaps                     # wheel-adjacent default
    sudo klangk-ebpf-setcaps --config /etc/klangk/klangkd.yaml
    sudo klangk-ebpf-setcaps --path /opt/bin/procleddy-ebpf
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .settings import KlangkSettings

CAPS = "cap_bpf,cap_perfmon+ep"


def resolve_binary(path: str | None, config: str | None) -> Path:
    """Resolve the watcher binary the way klangkd's ledger would.

    ``--path`` wins; then the explicit watcher setting (honoring
    ``--config``/env like klangkd); then the wheel-adjacent
    ``procleddy-ebpf`` default.
    """
    if path:
        return Path(path)
    settings = KlangkSettings(os.environ, config_file=config or "none")
    if settings.process_ledger_watcher:
        return Path(settings.process_ledger_watcher)
    return Path(__file__).parent / "procleddy-ebpf"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="klangk-ebpf-setcaps",
        description=(
            "Apply cap_bpf,cap_perfmon+ep to the process-ledger eBPF "
            "watcher binary (run as root; see "
            "docs/features/process-ledger.md)."
        ),
    )
    parser.add_argument(
        "--path",
        help="Explicit watcher binary path (overrides config/env).",
    )
    parser.add_argument(
        "--config",
        help=(
            "klangkd config file to read KLANGKD_PROCESS_LEDGER_WATCHER "
            "from (default: env vars only, like --config=none)."
        ),
    )
    args = parser.parse_args(argv)

    binary = resolve_binary(args.path, args.config)
    if not binary.exists():
        print(
            f"klangk-ebpf-setcaps: watcher binary not found at {binary} — "
            "build/install it first (scripts/procleddy-ebpf, or the "
            "klangk:build-procleddy-ebpf devenv task), or pass --path.",
            file=sys.stderr,
        )
        return 1

    setcap = shutil.which("setcap")
    if setcap is None:
        print(
            "klangk-ebpf-setcaps: setcap not found — install libcap "
            "(provides setcap/getcap).",
            file=sys.stderr,
        )
        return 1

    proc = subprocess.run(
        [setcap, CAPS, str(binary)], capture_output=True, text=True
    )
    if proc.returncode != 0:
        print(
            f"klangk-ebpf-setcaps: setcap failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()} "
            "(this needs root / CAP_SETFCAP).",
            file=sys.stderr,
        )
        return 1

    getcap = shutil.which("getcap")
    if getcap is not None:
        verify = subprocess.run(
            [getcap, str(binary)], capture_output=True, text=True
        )
        print(verify.stdout.strip() or f"{binary}: {CAPS}")
    else:
        print(f"klangk-ebpf-setcaps: applied {CAPS} to {binary}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint glue
    sys.exit(main())
