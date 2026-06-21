"""File operations on workspace home directories (host-side mount).

All path-accepting functions in this module delegate to ``resolve_path``
which validates that the resolved path stays within the workspace root
via ``Path.is_relative_to``.  CodeQL cannot trace this inter-procedural
validation; the corresponding alerts are dismissed as false positives.
"""

import os
import shutil
from pathlib import Path

from . import workspaces

NAME_MAX = os.pathconf("/", "PC_NAME_MAX") if hasattr(os, "pathconf") else 255


def resolve_path(user_id: str, workspace_id: str, relative_path: str) -> Path:
    """Resolve a relative path within a workspace, preventing traversal."""
    for part in Path(relative_path).parts:
        if len(part.encode()) > NAME_MAX:
            raise ValueError(f"Filename exceeds {NAME_MAX}-byte limit")
    root = workspaces.get_home_host_path(user_id, workspace_id).resolve()
    resolved = (root / relative_path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Path traversal not allowed")
    return resolved


def list_files(
    user_id: str, workspace_id: str, relative_path: str = "."
) -> list[dict]:
    """List files and directories at the given path."""
    path = resolve_path(user_id, workspace_id, relative_path)
    if not path.exists() or not path.is_dir():
        return []

    entries = []
    home = workspaces.get_home_host_path(user_id, workspace_id)
    for entry in sorted(path.iterdir()):
        st = entry.stat()
        entries.append(
            {
                "name": entry.name,
                "path": str(entry.relative_to(home)),
                "is_dir": entry.is_dir(),
                "size": st.st_size if entry.is_file() else None,
                "mtime": st.st_mtime,
                "ctime": st.st_ctime,
            }
        )
    return entries


def read_file(
    user_id: str, workspace_id: str, relative_path: str
) -> str | None:
    """Read file contents. Returns None if file doesn't exist or is too large."""
    path = resolve_path(user_id, workspace_id, relative_path)
    if not path.exists() or not path.is_file():
        return None

    # Limit to 1MB
    if path.stat().st_size > 1_000_000:
        return None

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def delete_path(user_id: str, workspace_id: str, relative_path: str) -> str:
    """Delete a file or directory. Returns the relative path deleted."""
    path = resolve_path(user_id, workspace_id, relative_path)
    if not path.exists():
        raise FileNotFoundError("Path not found")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return str(
        path.relative_to(workspaces.get_home_host_path(user_id, workspace_id))
    )


def rename_path(
    user_id: str, workspace_id: str, old_path: str, new_path: str
) -> str:
    """Rename/move a file or directory. Returns the new relative path."""
    src = resolve_path(user_id, workspace_id, old_path)
    dst = resolve_path(user_id, workspace_id, new_path)
    if not src.exists():
        raise FileNotFoundError("Source path not found")
    if dst.exists():
        raise FileExistsError("Destination already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return str(
        dst.relative_to(workspaces.get_home_host_path(user_id, workspace_id))
    )


def write_file(
    user_id: str, workspace_id: str, relative_path: str, content: bytes
) -> str:
    """Write file contents. Returns the resolved relative path."""
    path = resolve_path(user_id, workspace_id, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(
        path.relative_to(workspaces.get_home_host_path(user_id, workspace_id))
    )


def write_file_path(
    user_id: str, workspace_id: str, relative_path: str
) -> Path:
    """Resolve and prepare a file path for writing.

    Creates parent directories and returns the resolved ``Path``.
    Callers are responsible for writing data to the returned path.
    """
    path = resolve_path(user_id, workspace_id, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
