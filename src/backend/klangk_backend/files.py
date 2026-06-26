"""File operations inside workspace containers via ``podman exec``.

All path-accepting functions validate that paths are absolute and
normalized.  Operations run as the ``klangk`` user inside the container
so OS-level permissions apply.  The container boundary is the primary
sandbox; ``validate_path`` provides defense-in-depth.
"""

import posixpath
from collections.abc import AsyncGenerator

from . import podman

EXEC_USER = "klangk"

# 255 is the common Linux NAME_MAX; reading at import time is fine.
NAME_MAX = 255


def validate_path(path: str) -> str:
    """Validate and normalize an absolute container path.

    Raises ``ValueError`` on any suspicious input:

    * null bytes
    * non-absolute paths
    * path components exceeding NAME_MAX
    * paths that still contain ``..`` after normalization (defense-in-depth;
      normpath should collapse them, but we reject them anyway)
    """
    if "\0" in path:
        raise ValueError("Null byte in path")
    if not path.startswith("/"):
        raise ValueError("Path must be absolute")
    normalized = posixpath.normpath(path)
    # normpath("//foo") → "//foo" on POSIX (implementation-defined);
    # force a single leading slash.
    if normalized.startswith("//") and not normalized.startswith("///"):
        normalized = normalized[1:]
    for part in normalized.split("/"):
        if len(part.encode("utf-8")) > NAME_MAX:
            raise ValueError(f"Filename exceeds {NAME_MAX}-byte limit")
    return normalized


async def list_files(container_id: str, path: str = "/") -> list[dict]:
    """List files and directories at the given path inside the container."""
    path = validate_path(path)
    rc, out, _err = await podman.exec_container(
        container_id,
        [
            "find",
            "-L",
            path,
            "-maxdepth",
            "1",
            "-mindepth",
            "1",
            "-printf",
            r"%f\t%Y\t%s\t%T@\t%C@\n",
        ],
        user=EXEC_USER,
    )
    if rc != 0:
        return []
    entries = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, ftype, size_str, mtime_str, ctime_str = parts
        is_dir = ftype == "d"
        entry_path = (
            path.rstrip("/") + "/" + name if path != "/" else "/" + name
        )
        try:
            size = int(size_str) if not is_dir else None
        except ValueError:
            size = None
        try:
            mtime = float(mtime_str)
        except ValueError:
            mtime = 0.0
        try:
            ctime = float(ctime_str)
        except ValueError:
            ctime = 0.0
        entries.append(
            {
                "name": name,
                "path": entry_path,
                "is_dir": is_dir,
                "size": size,
                "mtime": mtime,
                "ctime": ctime,
            }
        )
    entries.sort(key=lambda e: e["name"])
    return entries


async def stat_path(container_id: str, path: str) -> dict | None:
    """Stat a single path.  Returns ``{"is_dir": bool, "size": int}``
    or ``None`` if the path does not exist."""
    path = validate_path(path)
    rc, out, _err = await podman.exec_container(
        container_id,
        ["stat", "--format", "%F\t%s", "--", path],
        user=EXEC_USER,
    )
    if rc != 0:
        return None
    parts = out.strip().split("\t")
    if len(parts) != 2:
        return None
    ftype, size_str = parts
    is_dir = "directory" in ftype
    try:
        size = int(size_str)
    except ValueError:
        size = 0
    return {"is_dir": is_dir, "size": size}


async def read_file(container_id: str, path: str) -> str | None:
    """Read file contents as text.  Returns None if missing or > 1 MB."""
    path = validate_path(path)
    info = await stat_path(container_id, path)
    if info is None or info["is_dir"]:
        return None
    if info["size"] > 1_000_000:
        return None
    rc, out, _err = await podman.exec_container(
        container_id,
        ["cat", "--", path],
        user=EXEC_USER,
    )
    if rc != 0:
        return None
    return out


def stream_file(container_id: str, path: str) -> AsyncGenerator[bytes, None]:
    """Stream file contents as raw bytes for download."""
    path = validate_path(path)
    return podman.exec_container_stream(
        container_id,
        ["cat", "--", path],
        user=EXEC_USER,
    )


def stream_dir_tar(
    container_id: str, path: str
) -> AsyncGenerator[bytes, None]:
    """Stream a directory as a tar.gz archive for download."""
    path = validate_path(path)
    return podman.exec_container_stream(
        container_id,
        ["tar", "-czf", "-", "-C", path, "."],
        user=EXEC_USER,
    )


async def delete_path(container_id: str, path: str) -> str:
    """Delete a file or directory.  Returns the path deleted."""
    path = validate_path(path)
    # Check existence first
    rc, _out, _err = await podman.exec_container(
        container_id,
        ["test", "-e", path],
        user=EXEC_USER,
    )
    if rc != 0:
        raise FileNotFoundError("Path not found")
    rc, _out, err = await podman.exec_container(
        container_id,
        ["rm", "-rf", "--", path],
        user=EXEC_USER,
    )
    if rc != 0:
        raise OSError(f"Delete failed: {err.strip()}")
    return path


async def rename_path(container_id: str, old_path: str, new_path: str) -> str:
    """Rename/move a file or directory.  Returns the new path."""
    old_path = validate_path(old_path)
    new_path = validate_path(new_path)
    # Check source exists
    rc, _out, _err = await podman.exec_container(
        container_id,
        ["test", "-e", old_path],
        user=EXEC_USER,
    )
    if rc != 0:
        raise FileNotFoundError("Source path not found")
    # Check dest does not exist
    rc, _out, _err = await podman.exec_container(
        container_id,
        ["test", "-e", new_path],
        user=EXEC_USER,
    )
    if rc == 0:
        raise FileExistsError("Destination already exists")
    # Create parent directory
    parent = posixpath.dirname(new_path)
    await podman.exec_container(
        container_id,
        ["mkdir", "-p", "--", parent],
        user=EXEC_USER,
    )
    # Move
    rc, _out, err = await podman.exec_container(
        container_id,
        ["mv", "--", old_path, new_path],
        user=EXEC_USER,
    )
    if rc != 0:
        raise OSError(f"Rename failed: {err.strip()}")
    return new_path


async def write_file(container_id: str, path: str, content: bytes) -> str:
    """Write file contents.  Returns the path written."""
    path = validate_path(path)
    # mkdir -p + cat > file in one sh invocation.
    # Path is passed as $1 (positional arg), never interpolated into the
    # command string, so shell metacharacters in the path are harmless.
    rc, _out, err = await podman.exec_container(
        container_id,
        [
            "sh",
            "-c",
            'mkdir -p "$(dirname "$1")" && cat > "$1"',
            "sh",
            path,
        ],
        user=EXEC_USER,
        stdin_data=content,
    )
    if rc != 0:
        raise OSError(f"Write failed: {err.strip()}")
    return path
