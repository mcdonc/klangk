"""Sandbox config loading and path resolution for ``klangkc sandbox``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SandboxConfig:
    """Parsed .klangk-sandbox.yaml."""

    # workspace
    image: str | None = None
    # sandbox
    mount_at: str = "~/work"
    setup: str | None = None
    # lists
    copy: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)


def load_sandbox_config(sandbox_root: Path) -> SandboxConfig:
    """Parse ``.klangk-sandbox.yaml`` under *sandbox_root*.

    Raises ``FileNotFoundError`` if the config file doesn't exist.
    Raises ``ValueError`` on invalid config.
    """
    config_path = sandbox_root / ".klangk-sandbox.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"No sandbox config found at {config_path}")
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("Invalid sandbox config: expected a mapping")

    workspace = raw.get("workspace") or {}
    sandbox = raw.get("sandbox") or {}

    return SandboxConfig(
        image=workspace.get("image"),
        mount_at=sandbox.get("mount_at", "~/work"),
        setup=sandbox.get("setup"),
        copy=raw.get("copy") or [],
        mounts=raw.get("mounts") or [],
        volumes=raw.get("volumes") or [],
    )


def expand_host_path(path: str, sandbox_root: Path) -> str:
    """Expand ``~`` and resolve relative paths against *sandbox_root*.

    Returns an absolute host path.
    """
    expanded = os.path.expanduser(path)
    p = Path(expanded)
    if not p.is_absolute():
        p = (sandbox_root / p).resolve()
    return str(p)


def expand_container_path(
    path: str, handle: str, mount_at: str | None = None
) -> str:
    """Expand container path.

    - ``~`` or ``~/...`` → ``/home/{handle}/...``
    - Absolute paths pass through unchanged
    - Relative paths are resolved against *mount_at* (which must
      already be expanded)
    """
    if path.startswith("~/"):
        return f"/home/{handle}/{path[2:]}"
    if path == "~":
        return f"/home/{handle}"
    if not path.startswith("/") and mount_at is not None:
        return f"{mount_at}/{path}"
    return path


def _expand_spec(
    spec: str,
    sandbox_root: Path,
    handle: str,
    expand_source: bool = True,
    mount_at: str | None = None,
) -> str:
    """Expand a ``source:dest[:options]`` spec.

    *expand_source* controls whether the source side gets host-path
    expansion (True for bind mounts, False for named volumes).
    *mount_at* is the resolved container mount point — relative
    destination paths are resolved against it.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid mount spec: {spec!r} (need source:dest)")
    src = parts[0]
    dest = parts[1]
    opts = parts[2:]
    if expand_source:
        src = expand_host_path(src, sandbox_root)
    dest = expand_container_path(dest, handle, mount_at=mount_at)
    result = f"{src}:{dest}"
    if opts:
        result += ":" + ":".join(opts)
    return result


def build_all_mounts(
    config: SandboxConfig,
    sandbox_root: Path,
    handle: str,
) -> list[str]:
    """Build the full mount list for ``create_workspace()``.

    Includes:
    - The implicit sandbox root mount at ``mount_at``
    - Explicit mounts from config (with host path expansion)
    - Volumes from config (no host path expansion on source)
    """
    resolved_mount_at = expand_container_path(config.mount_at, handle)
    mounts = [f"{sandbox_root.resolve()}:{resolved_mount_at}"]
    for spec in config.mounts:
        mounts.append(
            _expand_spec(
                spec, sandbox_root, handle, mount_at=resolved_mount_at
            )
        )
    for spec in config.volumes:
        mounts.append(
            _expand_spec(
                spec,
                sandbox_root,
                handle,
                expand_source=False,
                mount_at=resolved_mount_at,
            )
        )
    return mounts


def build_copy_pairs(
    config: SandboxConfig,
    sandbox_root: Path,
    handle: str,
) -> list[tuple[str, str]]:
    """Return ``(host_path, container_path)`` pairs from the copy list."""
    pairs = []
    for spec in config.copy:
        parts = spec.split(":")
        if len(parts) < 2:
            raise ValueError(f"Invalid copy spec: {spec!r} (need source:dest)")
        src = expand_host_path(parts[0], sandbox_root)
        dest = expand_container_path(parts[1], handle)
        pairs.append((src, dest))
    return pairs


def resolve_setup_command(config: SandboxConfig, handle: str) -> str | None:
    """Return the absolute container path for the setup script, or None."""
    if not config.setup:
        return None
    if config.setup.startswith("/"):
        return config.setup
    mount_at = expand_container_path(config.mount_at, handle)
    return f"{mount_at}/{config.setup}"
