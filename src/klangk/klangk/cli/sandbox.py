"""Sandbox config loading and path resolution for ``klangk sandbox``."""

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
    service_command: str | None = None
    auto_start: bool = False
    health_check: str | None = None
    # sandbox
    mount_at: str = "~/work"
    setup: str | None = None
    setup_timeout: int = 300
    # lists
    copy: list[str] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)


def parse_setup_timeout(sandbox: dict) -> int:
    """The sandbox section's setup-timeout, or 300 when absent."""
    setup_timeout = sandbox.get(
        "setup-timeout", sandbox.get("setup_timeout", 300)
    )
    try:
        return int(setup_timeout)
    except (TypeError, ValueError):
        raise ValueError(
            f"setup-timeout must be an integer, got {setup_timeout!r}"
        )


def list_field(raw: dict, name: str) -> list[str]:
    """A top-level config list, defaulting to empty."""
    return raw.get(name) or []


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

    copy_specs = list_field(raw, "copy")
    # Reject malformed copy specs up front so they surface as config
    # errors (via load_config_or_exit) instead of being silently
    # mangled at setup time (#3119).
    validate_copy_specs(copy_specs)

    return SandboxConfig(
        image=workspace.get("image"),
        service_command=workspace.get(
            "service-command", workspace.get("service_command")
        ),
        auto_start=bool(
            workspace.get("auto-start", workspace.get("auto_start", False))
        ),
        health_check=workspace.get(
            "health-check", workspace.get("health_check")
        ),
        mount_at=sandbox.get("mount-at", sandbox.get("mount_at", "~/work")),
        setup=sandbox.get("setup"),
        setup_timeout=parse_setup_timeout(sandbox),
        copy=copy_specs,
        mounts=list_field(raw, "mounts"),
        volumes=list_field(raw, "volumes"),
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

    #3118: only the leading ``~`` is special. Any other shell-like
    syntax — ``~user`` tildes, ``$VAR`` references — passes through
    literally; the copy path quotes the result (#3093), so no
    container-side expansion happens there either: sandbox copy
    destinations are literal container paths by design.
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


def parse_copy_spec(spec: str) -> tuple[str, str]:
    """Split a copy spec into ``(source, dest)``.

    Copy specs are strictly ``source:dest`` — exactly one colon,
    both halves non-empty. A spec with no colon, with extra
    colon-separated segments (a mount-style ``:ro`` option, say),
    or with an empty half is rejected: copy specs take no options
    segment, a path containing a colon cannot be expressed, and
    silently dropping or defaulting the bad parts would hide the
    mistake (#3119).
    """
    src, sep, dest = spec.partition(":")
    if not sep or ":" in dest:
        raise ValueError(
            f"Invalid copy spec: {spec!r} (need source:dest with"
            " exactly one colon; copy specs take no options)"
        )
    if not src or not dest:
        raise ValueError(
            f"Invalid copy spec: {spec!r} (source and destination"
            " must both be non-empty)"
        )
    return src, dest


def validate_copy_specs(specs: list[str]) -> None:
    """Raise ``ValueError`` on any malformed copy spec (#3119)."""
    for spec in specs:
        if not isinstance(spec, str):
            raise ValueError(f"Invalid copy spec: {spec!r} (must be a string)")
        parse_copy_spec(spec)


def build_copy_pairs(
    config: SandboxConfig,
    sandbox_root: Path,
    handle: str,
) -> list[tuple[str, str]]:
    """Return ``(host_path, container_path)`` pairs from the copy list.

    #3118: destinations are literal container paths — only a leading
    ``~`` expands (via expand_container_path). A ``~user`` or ``$VAR``
    destination passes through literally; copy_sandbox_files quotes it
    (#3093), so the container writes it as a literal filename. A
    relative destination likewise passes through unresolved — the
    container's shell runs it against the exec working directory (the
    container user's home), not against ``mount_at``.
    """
    pairs = []
    for spec in config.copy:
        src_part, dest_part = parse_copy_spec(spec)
        src = expand_host_path(src_part, sandbox_root)
        dest = expand_container_path(dest_part, handle)
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
