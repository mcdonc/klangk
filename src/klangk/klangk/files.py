"""File operations inside workspace containers via ``podman exec``.

All path-accepting methods validate that paths are absolute and
normalized.  Operations run as the ``klangk`` user inside the container
so OS-level permissions apply.  The container boundary is the primary
sandbox; ``validate_path`` provides defense-in-depth.

The stateful operations live on the :class:`Files` class, constructed
once in :func:`klangk.main.build_app` and stored on
``app.state.files`` (#1566) — the same ``X(app_state)`` pattern every
other owned subsystem uses (``Workspaces``, ``Terminal``, ...). The
class owns the ``podman`` reference instead of threading it through
every call. ``validate_path`` is a pure path-normalization helper with
no podman/settings dependency, so it stays module-level.
"""

import logging
import posixpath
import re
from collections.abc import AsyncGenerator

EXEC_USER = "klangk"

logger = logging.getLogger(__name__)

# find's stderr diagnostics name the path they failed on:
# ``find: '<path>': <reason>``. Used to tell a start-point failure
# (whole listing fails) from a child-entry failure (one unreadable
# entry; the rest of the listing is still good).
_FIND_ERROR_PATH_RE = re.compile(r"^find: '(.*)': ")

# 255 is the common Linux NAME_MAX; reading at import time is fine.
NAME_MAX = 255


def _single_leading_slash(normalized: str) -> str:
    """Force one leading slash: ``normpath("//foo")`` keeps the double
    slash on POSIX (implementation-defined)."""
    if normalized.startswith("//") and not normalized.startswith("///"):
        return normalized[1:]
    return normalized


def _reject_oversized_parts(normalized: str) -> None:
    """Reject any path component over the NAME_MAX byte limit."""
    for part in normalized.split("/"):
        if len(part.encode("utf-8")) > NAME_MAX:
            raise ValueError(f"Filename exceeds {NAME_MAX}-byte limit")


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
    normalized = _single_leading_slash(posixpath.normpath(path))
    _reject_oversized_parts(normalized)
    return normalized


def _split_find_errors(err: str, path: str) -> tuple[list[str], list[str]]:
    """Partition find's stderr lines into (start-point, child-entry)
    diagnostics for *path*."""
    start_errors = []
    child_errors = []
    for line in err.splitlines():
        if not line.strip():
            continue
        m = _FIND_ERROR_PATH_RE.match(line)
        if m and m.group(1) == path:
            start_errors.append(line)
        else:
            child_errors.append(line)
    return start_errors, child_errors


def _warn_child_errors(
    container_id: str, path: str, child_errors: list[str]
) -> None:
    """Child-entry failures only warn (the readable entries survive)."""
    if child_errors:
        logger.warning(
            "list_files: skipped unreadable entries under %s in "
            "container %s: %s",
            path,
            container_id,
            " | ".join(child_errors),
        )


def _raise_for_start_error(message: str) -> None:
    """A surfaced start-point failure: permission-denied raises
    PermissionError (a denied volume root must not render as a
    mysterious "Empty directory", #2766); anything else a generic
    OSError."""
    if "Permission denied" in message:
        raise PermissionError(message)
    raise OSError(message)


def classify_find_errors(
    container_id: str, path: str, err: str, rc: int
) -> None:
    """Act on find's stderr diagnostics: start-point failures raise (or
    list empty for ENOENT), child-entry failures only warn (the readable
    entries survive), and a total lack of diagnostics surfaces as a
    generic OSError."""
    start_errors, child_errors = _split_find_errors(err, path)
    _warn_child_errors(container_id, path, child_errors)
    if start_errors:
        message = " ".join(" ".join(start_errors).split())
        if "No such file or directory" in message:
            # ENOENT lists as empty (a missing directory is not
            # an error — matches stat_path/read_file).
            return
        logger.warning(
            "list_files failed for %s in container %s: %s",
            path,
            container_id,
            message,
        )
        _raise_for_start_error(message)
    if not child_errors:
        # rc != 0 with no diagnostics at all: cannot classify —
        # surface it rather than guess.
        raise OSError(f"find exited with status {rc}")


def _float_or(text: str, fallback: float) -> float:
    """A ``find -printf`` timestamp, or *fallback* when malformed."""
    try:
        return float(text)
    except ValueError:
        return fallback


def _parse_find_line(line: str, path: str) -> dict | None:
    """One ``find -printf`` line as an entry dict, or None for a
    malformed/short line."""
    parts = line.split("\t")
    if len(parts) != 5:
        return None
    name, ftype, size_str, mtime_str, ctime_str = parts
    is_dir = ftype == "d"
    entry_path = path.rstrip("/") + "/" + name if path != "/" else "/" + name
    try:
        size = int(size_str) if not is_dir else None
    except ValueError:
        size = None
    return {
        "name": name,
        "path": entry_path,
        "is_dir": is_dir,
        "size": size,
        "mtime": _float_or(mtime_str, 0.0),
        "ctime": _float_or(ctime_str, 0.0),
    }


class Files:
    """File operations inside workspace containers via ``podman exec``.

    Constructed once in :func:`klangk.main.build_app` and stored
    on ``app.state.files`` (#1566). The class owns the ``podman``
    reference (previously threaded through every call as a trailing
    argument), the same way ``Workspaces`` / ``Terminal`` own theirs.
    """

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def list_files(
        self, container_id: str, path: str = "/"
    ) -> list[dict]:
        """List files and directories at the given path inside the container."""
        path = validate_path(path)
        rc, out, err = await self.app.state.podman.exec_container(
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
            # C locale keeps find's diagnostics in the exact
            # ``find: '<path>': <reason>`` form regardless of the
            # container's or a workspace env override's locale — the
            # classification below depends on it (#2769 review).
            extra_env={"LC_ALL": "C"},
        )
        if rc != 0:
            # find exits 1 for BOTH start-point failures and individual
            # child-entry failures (a stat-denied entry — e.g. a symlink
            # into a 0700 dir — a raced-away file, ELOOP), and still
            # prints the readable entries on stdout. Only a start-point
            # failure fails the listing; child failures degrade to a
            # logged warning plus the surviving entries (#2769 review).
            classify_find_errors(container_id, path, err, rc)
        entries = [
            e
            for e in (
                _parse_find_line(line, path)
                for line in out.strip().splitlines()
            )
            if e is not None
        ]
        entries.sort(key=lambda e: e["name"])
        return entries

    async def stat_path(self, container_id: str, path: str) -> dict | None:
        """Stat a single path.  Returns ``{"is_dir": bool, "size": int}``
        or ``None`` if the path does not exist."""
        path = validate_path(path)
        rc, out, _err = await self.app.state.podman.exec_container(
            container_id,
            ["stat", "-L", "--format", "%F\t%s", "--", path],
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

    async def read_file(self, container_id: str, path: str) -> str | None:
        """Read file contents as text.  Returns None if missing or > 1 MB."""
        path = validate_path(path)
        info = await self.stat_path(container_id, path)
        if info is None or info["is_dir"]:
            return None
        if info["size"] > 1_000_000:
            return None
        rc, out, _err = await self.app.state.podman.exec_container(
            container_id,
            ["cat", "--", path],
            user=EXEC_USER,
        )
        if rc != 0:
            return None
        return out

    def stream_file(
        self, container_id: str, path: str
    ) -> AsyncGenerator[bytes, None]:
        """Stream file contents as raw bytes for download."""
        path = validate_path(path)
        return self.app.state.podman.exec_container_stream(
            container_id,
            ["cat", "--", path],
            user=EXEC_USER,
        )

    def stream_dir_tar(
        self, container_id: str, path: str
    ) -> AsyncGenerator[bytes, None]:
        """Stream a directory as a tar.gz archive for download."""
        path = validate_path(path)
        # Use sh -c with readlink to resolve symlinks before tar -C,
        # because tar -C does not follow symlinks on all implementations.
        return self.app.state.podman.exec_container_stream(
            container_id,
            [
                "sh",
                "-c",
                'dir="$(readlink -f "$1")" && tar -czf - -C "$dir" .',
                "sh",
                path,
            ],
            user=EXEC_USER,
        )

    async def delete_path(self, container_id: str, path: str) -> str:
        """Delete a file or directory.  Returns the path deleted."""
        path = validate_path(path)
        # Check existence first
        rc, _out, _err = await self.app.state.podman.exec_container(
            container_id,
            ["test", "-e", path],
            user=EXEC_USER,
        )
        if rc != 0:
            raise FileNotFoundError("Path not found")
        rc, _out, err = await self.app.state.podman.exec_container(
            container_id,
            ["rm", "-rf", "--", path],
            user=EXEC_USER,
        )
        if rc != 0:
            raise OSError(f"Delete failed: {err.strip()}")
        return path

    async def rename_path(
        self, container_id: str, old_path: str, new_path: str
    ) -> str:
        """Rename/move a file or directory.  Returns the new path."""
        old_path = validate_path(old_path)
        new_path = validate_path(new_path)
        # Check source exists
        rc, _out, _err = await self.app.state.podman.exec_container(
            container_id,
            ["test", "-e", old_path],
            user=EXEC_USER,
        )
        if rc != 0:
            raise FileNotFoundError("Source path not found")
        # Check dest does not exist
        rc, _out, _err = await self.app.state.podman.exec_container(
            container_id,
            ["test", "-e", new_path],
            user=EXEC_USER,
        )
        if rc == 0:
            raise FileExistsError("Destination already exists")
        # Create parent directory
        parent = posixpath.dirname(new_path)
        await self.app.state.podman.exec_container(
            container_id,
            ["mkdir", "-p", "--", parent],
            user=EXEC_USER,
        )
        # Move
        rc, _out, err = await self.app.state.podman.exec_container(
            container_id,
            ["mv", "--", old_path, new_path],
            user=EXEC_USER,
        )
        if rc != 0:
            raise OSError(f"Rename failed: {err.strip()}")
        return new_path

    async def write_file(
        self, container_id: str, path: str, content: bytes
    ) -> str:
        """Write file contents.  Returns the path written."""
        path = validate_path(path)
        # mkdir -p + cat > file in one sh invocation.
        # Path is passed as $1 (positional arg), never interpolated into the
        # command string, so shell metacharacters in the path are harmless.
        rc, _out, err = await self.app.state.podman.exec_container(
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
