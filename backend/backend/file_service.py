"""File operations on workspace directories (host-side mount)."""

import os
from pathlib import Path

from . import workspace_manager


def _resolve_path(user_id: str, workspace_name: str, relative_path: str) -> Path:
    """Resolve a relative path within a workspace, preventing directory traversal."""
    root = workspace_manager.get_workspace_host_path(user_id, workspace_name)
    resolved = (root / relative_path).resolve()
    if not str(resolved).startswith(str(root.resolve())):
        raise ValueError("Path traversal not allowed")
    return resolved


def list_files(user_id: str, workspace_name: str, relative_path: str = ".") -> list[dict]:
    """List files and directories at the given path."""
    path = _resolve_path(user_id, workspace_name, relative_path)
    if not path.exists() or not path.is_dir():
        return []

    entries = []
    for entry in sorted(path.iterdir()):
        entries.append({
            "name": entry.name,
            "path": str(entry.relative_to(
                workspace_manager.get_workspace_host_path(user_id, workspace_name)
            )),
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if entry.is_file() else None,
        })
    return entries


def read_file(user_id: str, workspace_name: str, relative_path: str) -> str | None:
    """Read file contents. Returns None if file doesn't exist or is too large."""
    path = _resolve_path(user_id, workspace_name, relative_path)
    if not path.exists() or not path.is_file():
        return None

    # Limit to 1MB
    if path.stat().st_size > 1_000_000:
        return None

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def write_file(user_id: str, workspace_name: str, relative_path: str, content: bytes) -> str:
    """Write file contents. Returns the resolved relative path."""
    path = _resolve_path(user_id, workspace_name, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path.relative_to(
        workspace_manager.get_workspace_host_path(user_id, workspace_name)
    ))
