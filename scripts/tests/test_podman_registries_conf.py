"""Contract tests for the registries.conf provisioning (#286).

Rootless podman from nix ships no ``containers-registries.conf(5)``, so any
image build whose Dockerfile references a short image name
(``alpine:3.21``, ``python:3.14-slim``, ``debian:trixie-slim``,
``node:26-slim``) fails with:

    Error: creating build container: short-name "alpine:3.21" did not
    resolve to an alias and no containers-registries.conf(5) was found

The fix has two halves, both pinned here (grep-style contract tests in the
spirit of ``test_build_script_remote_guard.py`` — a future edit that drops
one silently is loud):

1. ``devenv.nix`` exports ``CONTAINERS_REGISTRIES_CONF`` (empty on macOS,
   where builds run inside the podman VM) and enterShell seeds the file.
   Unlike the signature policy (which podman only honors via the
   ``--signature-policy`` flag), podman reads this env var directly, so no
   CLI flag is threaded through the build scripts.
2. ``scripts/_podman_common.sh`` — sourced by every build/pull script —
   re-creates the file when the env var is set but the file is missing
   (direct script invocation outside the devenv shell). This matters more
   than the policy safety net: podman hard-fails with "loading registries
   configuration ... no such file or directory" when the env var points at
   a nonexistent path.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make sure the scripts directory is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"
_PODMAN_COMMON = _REPO_ROOT / "scripts" / "_podman_common.sh"
# Scripts with their own `podman build`/`pull`/`push` that must source the
# shared helper (dist-smoke-test.sh is exempt: its Dockerfile uses the
# fully-qualified docker.io/library/node:22-slim precisely to sidestep
# short-name resolution).
_HELPER_CONSUMER_SCRIPTS = [
    "build-workspace-image.sh",
    "build-network-sidecar.sh",
    "build-base-image.sh",
    "build-fips-image.sh",
    "pull-base-image.sh",
    "pull-fips-image.sh",
]


def test_build_pull_scripts_source_the_helper():
    """Every build/pull script must source _podman_common.sh.

    The registries.conf (and signature-policy) provisioning lives in the
    shared helper; a script that drops the source line misses it.
    """
    for name in _HELPER_CONSUMER_SCRIPTS:
        text = (_REPO_ROOT / "scripts" / name).read_text()
        assert 'source "$SCRIPT_DIR/_podman_common.sh"' in text, (
            f"{name} no longer sources _podman_common.sh — it misses the "
            f"registries.conf/signature-policy provisioning (#286)"
        )


def test_devenv_exports_registries_conf():
    """devenv.nix must export CONTAINERS_REGISTRIES_CONF on Linux.

    Without the export, every podman invocation inside the shell (including
    ones that don't source _podman_common.sh) resolves short names against
    a config that does not exist on nix-rootless podman.
    """
    text = _DEVENV_NIX.read_text()
    assert "CONTAINERS_REGISTRIES_CONF" in text, (
        "devenv.nix no longer exports CONTAINERS_REGISTRIES_CONF — short "
        "image names fail to resolve (#286)"
    )
    # The export must be gated off on macOS (podman builds run inside the
    # VM, which ships its own registries.conf).
    m = re.search(
        r"env\.CONTAINERS_REGISTRIES_CONF.*?if pkgs\.stdenv\.hostPlatform\.isDarwin",
        text,
        re.DOTALL,
    )
    assert m, (
        "CONTAINERS_REGISTRIES_CONF must be empty on Darwin (the podman VM "
        "has its own registries.conf) — the isDarwin gate is gone (#286)"
    )


def test_enter_shell_seeds_registries_conf():
    """enterShell must seed the registries.conf next to policy.json.

    The env var pointing at a missing file is worse than no env var at all:
    podman hard-fails with "loading registries configuration ... no such
    file or directory" instead of falling back to defaults.
    """
    text = _DEVENV_NIX.read_text()
    assert 'unqualified-search-registries = ["docker.io"]' in text, (
        "enterShell no longer seeds a registries.conf with docker.io in "
        "unqualified-search-registries (#286)"
    )
    assert "$_PODMAN_CONF/registries.conf" in text, (
        "enterShell must write registries.conf into the same state dir as "
        "policy.json ($_PODMAN_CONF) — the seeding block is gone (#286)"
    )


def test_podman_common_recreates_missing_file():
    """_podman_common.sh must recreate the file when the env var is set.

    Covers direct script invocation outside the devenv shell, where
    enterShell never ran. Mirrors the policy.json safety net.
    """
    text = _PODMAN_COMMON.read_text()
    assert re.search(r'\[ -n "\$\{CONTAINERS_REGISTRIES_CONF:-\}" \]', text), (
        "_podman_common.sh no longer checks CONTAINERS_REGISTRIES_CONF — "
        "direct invocation outside devenv fails on short names (#286)"
    )
    assert 'unqualified-search-registries = ["docker.io"]' in text, (
        "_podman_common.sh must write the same docker.io unqualified-search "
        "config as enterShell (#286)"
    )
