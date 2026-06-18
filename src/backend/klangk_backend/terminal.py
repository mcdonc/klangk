"""Terminal session: interactive shell via ``podman exec`` over a PTY.

A single local PTY (slave set to raw mode) bridges the ``podman exec``
subprocess's stdio to the container-side PTY allocated by ``-t``.  Raw
mode keeps the local line discipline from consuming escape sequences
(arrow keys, etc.).  Resize sets the master window size and signals
podman with ``SIGWINCH`` so it resizes the container PTY.
"""

import asyncio
import codecs
import fcntl
import logging
import os
import pty
import uuid
import signal
import struct
import termios
import tty
from collections.abc import AsyncGenerator

from . import podman
from .util import BoundedOutputQueue, resolve_env_secret

logger = logging.getLogger(__name__)

_READ_CHUNK = 65536


def terminal_tmux_enabled() -> bool:
    """Whether new terminal sessions are wrapped in tmux.

    Defaults to enabled (the historical behaviour).  Set
    ``KLANGK_DISABLE_TMUX`` to a truthy value (``1``/``true``/``yes``) to
    drop users straight into a plain login shell instead.  Note this only
    affects the default per-user terminal; shared/joined terminals are
    built on tmux session groups and always use tmux regardless.
    """
    val = resolve_env_secret("KLANGK_DISABLE_TMUX", "") or ""
    return val.lower() not in ("1", "true", "yes")


# Backend env vars stripped from the in-container shell.
_SENSITIVE_ENV_PREFIXES = (
    "KLANGK_LLM_API_KEY",
    "ANTHROPIC_",
    "OPENAI_",
    "GOOGLE_",
    "GROQ_",
    "MISTRAL_",
)


def _build_environment(
    command_override: str | None,
    user_home: str | None = None,
    user_id: str | None = None,
    user_handle: str | None = None,
) -> list[str]:
    env = ["TERM=xterm-256color"]
    if user_home is not None:
        env.append(f"HOME={user_home}")
    if user_id is not None:
        env.append(f"KLANGK_USER_ID={user_id}")
    if user_handle is not None:
        env.append(f"KLANGK_USER_HANDLE={user_handle}")
    if command_override is not None:
        env.append(f"KLANGK_CMD_OVERRIDE={command_override}")
    return env


_WORKSPACE_STATE_FILE = ".workspace-state.json"


def _build_shell_command(
    session_name: str | None = None,
    user_home: str | None = None,
    socket_path: str | None = None,
    join_session: str | None = None,
    read_only: bool = False,
    tmux_enabled: bool = True,
) -> tuple[list[str], str | None]:
    """Build the shell command for a terminal session.

    *session_name*: tmux session name (typically the user_id).
    *user_home*: sets ``HOME`` env var inside the session.
    *socket_path*: use ``-S`` for shared terminal sockets.
    *join_session*: join an existing session group (for shared terminals).
    *read_only*: attach with ``-r`` for spy mode.
    *tmux_enabled*: when ``False`` and this is a plain (non-shared)
    session, launch a bare login shell instead of tmux.  Shared/joined
    sessions (``socket_path``/``join_session``) always use tmux.

    Returns ``(command, unique_session_name)``.  *unique_session_name* is
    set only for shared terminal joins so ``stop()`` can kill the tmux
    session inside the container (preventing stale clients that deadlock
    the tmux server).
    """
    unset_args: list[str] = []
    for key in os.environ:
        if key.startswith(_SENSITIVE_ENV_PREFIXES):
            unset_args.extend(["-u", key])

    # Plain-shell mode: drop straight into a login shell (sources
    # /etc/profile -> /etc/bash.bashrc, same init path tmux's login shell
    # uses).  Only applies to the default session — sharing needs tmux.
    if not tmux_enabled and socket_path is None and join_session is None:
        cmd = ["env", *unset_args, "bash", "-l"]
        return cmd, None

    tmux_env: list[str] = []
    session_args: list[str] = []
    socket_args: list[str] = []
    unique: str | None = None
    if socket_path is not None:
        socket_args = ["-S", socket_path]
    if user_home is not None:
        tmux_env = ["-e", f"HOME={user_home}"]
    if session_name is not None:
        if join_session is not None:
            # Join an existing session group.  Use a unique session name
            # so rapid re-joins don't collide with a stale session.
            unique = f"{session_name}-{uuid.uuid4().hex[:8]}"
            session_args = ["-t", join_session, "-s", unique]
        else:
            # Create or reattach to own session.
            session_args = ["-A", "-s", session_name]
    cmd = [
        "env",
        *unset_args,
        "tmux",
        *socket_args,
        "new-session",
        *session_args,
        *tmux_env,
    ]
    # Respawn the shell on exit only for owner sessions — NOT for
    # session-group joiners, where it creates zombie panes on teardown.
    if join_session is None and socket_path is None:
        cmd += [
            "\\;",
            "set-option",
            "remain-on-exit",
            "on",
            "\\;",
            "set-hook",
            "pane-died",
            "respawn-pane",
        ]
    # Note: no refresh-client here for joins — the caller selects the
    # target window first, then triggers a refresh via resize.
    # Read-only is enforced in handle_terminal_input (wshandler.py),
    # which drops input when session.read_only is True.  tmux's
    # switch-client -r is not used because it caused display issues.
    return cmd, unique


def _build_exec_argv(
    container_id: str,
    env: list[str],
    shell_cmd: list[str],
    work_dir: str = "/home/work",
) -> list[str]:
    argv = ["exec", "-t", "-i", "-u", "klangk", "-w", work_dir]
    for entry in env:
        argv += ["-e", entry]
    argv.append(container_id)
    argv += shell_cmd
    return argv


async def attach_browser(container_id: str, browser_id: str) -> None:
    """Run ``klangk-attach-browser <browser_id>`` inside the container.

    This stores the browser ID in the tmux global environment so that
    ``klangk-browser-id`` can read it dynamically.  Called after each
    ``terminal_start`` (including re-attach after browser refresh).
    """
    argv = [
        "exec",
        "-u",
        "klangk",
        container_id,
        "klangk-attach-browser",
        browser_id,
    ]
    proc = await asyncio.create_subprocess_exec(
        podman.PODMAN_BIN,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=podman.subprocess_env(),
    )
    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        logger.warning(
            "klangk-attach-browser failed (rc=%d): %s",
            proc.returncode,
            stderr.decode(errors="replace").strip(),
        )


async def tmux_command(
    container_id: str, session_name: str, args: list[str]
) -> str:
    """Run a tmux command in the container and return stdout.

    Retries up to 3 times on socket-not-found errors, which can occur
    when the tmux server is still starting in a fresh container.
    """
    for attempt in range(3):
        argv = [
            "exec",
            "-u",
            "klangk",
            container_id,
            "tmux",
            *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            podman.PODMAN_BIN,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=podman.subprocess_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if "No such file or directory" in err and attempt < 2:
            await asyncio.sleep(0.5)
            continue
        raise RuntimeError(f"tmux command failed: {err}")
    return ""  # pragma: no cover


async def list_windows(container_id: str, session_name: str) -> list[dict]:
    """List tmux windows for a session. Returns [{index, name}, ...]."""
    output = await tmux_command(
        container_id,
        session_name,
        [
            "list-windows",
            "-t",
            session_name,
            "-F",
            "#{window_id}|||#{window_index}|||#{window_name}|||#{window_active}",
        ],
    )
    windows = []
    for line in output.strip().splitlines():
        parts = line.split("|||")
        if len(parts) >= 4:
            windows.append(
                {
                    "id": parts[0],  # e.g. "@0" — unique, never reused
                    "index": int(parts[1]),
                    "name": parts[2],
                    "active": parts[3] == "1",
                }
            )
    return windows


async def new_window(
    container_id: str, session_name: str, name: str | None = None
) -> list[dict]:
    """Create a new tmux window and return the updated window list.

    If *name* is not provided, auto-generates a unique name.
    Raises ``ValueError`` if *name* duplicates an existing window name.

    Uses a single podman exec with a shell script to minimize
    round-trips (list + create + list in one call).
    """
    if name is not None:
        # Explicit name — check + create + list in one exec.
        script = (
            f"existing=$(tmux list-windows -t {session_name}"
            f" -F '#{{window_name}}' 2>/dev/null);"
            f" echo \"$existing\" | grep -qx '{name}'"
            f" && echo 'DUPLICATE' && exit 1;"
            f" tmux new-window -t {session_name} -n '{name}';"
            f" tmux list-windows -t {session_name}"
            f" -F '#{{window_id}}|||#{{window_index}}|||#{{window_name}}|||#{{window_active}}'"
        )
    else:
        # Auto-name — find next number, create, list.
        script = (
            f"names=$(tmux list-windows -t {session_name}"
            f" -F '#{{window_name}}' 2>/dev/null);"
            f' n=1; while echo "$names" | grep -qx "$n"; do n=$((n+1)); done;'
            f' tmux new-window -t {session_name} -n "$n";'
            f" tmux list-windows -t {session_name}"
            f" -F '#{{window_id}}|||#{{window_index}}|||#{{window_name}}|||#{{window_active}}'"
        )
    argv = [
        "exec",
        "-u",
        "klangk",
        container_id,
        "bash",
        "-c",
        script,
    ]
    proc = await asyncio.create_subprocess_exec(
        podman.PODMAN_BIN,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=podman.subprocess_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        if "DUPLICATE" in output:
            raise ValueError(f"Window name '{name}' already exists")
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"new_window failed: {err}")
    windows = []
    for line in output.strip().splitlines():
        parts = line.split("|||")
        if len(parts) >= 4:
            windows.append(
                {
                    "id": parts[0],
                    "index": int(parts[1]),
                    "name": parts[2],
                    "active": parts[3] == "1",
                }
            )
    return windows


async def rename_window(
    container_id: str, session_name: str, index: int, name: str
) -> None:
    """Rename a tmux window.

    Raises ``ValueError`` if *name* duplicates another window's name.
    """
    existing = await list_windows(container_id, session_name)
    if any(w["name"] == name and w["index"] != index for w in existing):
        raise ValueError(f"Window name '{name}' already exists")
    await tmux_command(
        container_id,
        session_name,
        ["rename-window", "-t", f"{session_name}:{index}", name],
    )


async def select_window(
    container_id: str, session_name: str, target: int | str
) -> None:
    """Switch the active tmux window.

    *target* can be a window index (int), window name (str), or
    window id (``@N`` string — preferred, globally unique).
    """
    # Window IDs (@N) can be used directly as targets without
    # a session prefix.
    if isinstance(target, str) and target.startswith("@"):
        t = target
    else:
        t = f"{session_name}:{target}"
    await tmux_command(
        container_id,
        session_name,
        ["select-window", "-t", t],
    )


async def close_window(
    container_id: str, session_name: str, target: int | str
) -> list[dict]:
    """Close a tmux window and return the updated window list.

    *target* can be a window index (int), window name (str), or
    window id (``@N`` string — preferred, globally unique).
    """
    if isinstance(target, str) and target.startswith("@"):
        t = target
    else:
        t = f"{session_name}:{target}"
    await tmux_command(
        container_id,
        session_name,
        ["kill-window", "-t", t],
    )
    return await list_windows(container_id, session_name)


_STATE_PATH = f"/home/{_WORKSPACE_STATE_FILE}"


async def load_workspace_state(container_id: str) -> dict:
    """Read per-user workspace state from /home/.workspace-state.json.

    Returns a dict keyed by handle, e.g.
    ``{"admin": {"terminal_windows": [...], ...}, ...}``.
    Returns empty dict if the file doesn't exist or is corrupt.
    Used for restoring state after container restart.
    """
    argv = ["exec", "-u", "klangk", container_id, "cat", _STATE_PATH]
    proc = await asyncio.create_subprocess_exec(
        podman.PODMAN_BIN,
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=podman.subprocess_env(),
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        return {}
    try:
        import json

        return json.loads(stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return {}


async def save_workspace_state(container_id: str, state: dict) -> None:
    """Snapshot per-user workspace state.

    Writes atomically to /home/.workspace-state.json via temp + rename.
    Callers should serialize access via WorkspaceSession._save_lock.

    Uses ``mktemp`` inside the container so the temp file is created with
    ``O_EXCL``, preventing symlink-following attacks.
    """
    import json

    data = json.dumps(state, indent=2)
    # mktemp creates a file safely (O_EXCL); cat writes into it; mv renames
    # atomically; trap ensures cleanup on any failure.
    script = (
        "set -e; t=$(mktemp /home/.workspace-state.XXXXXX);"
        " trap 'rm -f \"$t\"' EXIT;"
        f' cat > "$t" && mv "$t" {_STATE_PATH};'
        " trap - EXIT"
    )
    argv = [
        "exec",
        "-i",
        "-u",
        "klangk",
        container_id,
        "bash",
        "-c",
        script,
    ]
    proc = await asyncio.create_subprocess_exec(
        podman.PODMAN_BIN,
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=podman.subprocess_env(),
    )
    await asyncio.wait_for(proc.communicate(input=data.encode()), timeout=10)


async def restore_windows(
    container_id: str, session_name: str, saved_windows: list[dict]
) -> None:
    """Create any missing tmux windows from saved state.

    Compares saved window names against existing windows and creates
    any that are missing.
    """
    existing = await list_windows(container_id, session_name)
    existing_names = {w["name"] for w in existing}
    for win in saved_windows:
        name = win.get("name", "")
        if name and name not in existing_names:
            await new_window(container_id, session_name, name=name)


async def kill_joiner_sessions(container_id: str, owner_handle: str) -> None:
    """Kill all session-group sessions except the owner's own session.

    Used when unsharing to disconnect spectators/collaborators.
    """
    try:
        output = await tmux_command(
            container_id,
            owner_handle,
            [
                "list-sessions",
                "-F",
                "#{session_name}",
            ],
        )
        for session_name in output.strip().splitlines():
            if session_name != owner_handle:
                try:
                    await tmux_command(
                        container_id,
                        owner_handle,
                        ["kill-session", "-t", session_name],
                    )
                except RuntimeError:
                    pass  # Session may have already exited
    except RuntimeError:
        pass  # No sessions


class ShellProcess:
    """Owns the PTY + ``podman exec`` subprocess for one shell.

    The master fd is set to non-blocking mode and registered with the
    asyncio event loop via ``add_reader``.  This avoids the default
    thread-pool executor whose limited threads (typically 6) are
    easily exhausted by blocking PTY I/O, causing cascading stalls
    across all terminal sessions.
    """

    def __init__(self) -> None:
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._read_event: asyncio.Event | None = None

    async def start(  # pragma: no cover
        self, argv: list[str], rows: int, cols: int
    ) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            tty.setraw(slave_fd)
            _set_winsize(master_fd, rows, cols)
            self._proc = await asyncio.create_subprocess_exec(
                podman.PODMAN_BIN,
                *argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=podman.subprocess_env(),
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        # Set non-blocking and register with the event loop so reads
        # and writes never consume a thread-pool slot.
        os.set_blocking(master_fd, False)
        self._read_event = asyncio.Event()
        asyncio.get_running_loop().add_reader(master_fd, self._read_event.set)

    async def read(self) -> bytes:  # pragma: no cover
        try:
            while True:
                try:
                    return os.read(self._master_fd, _READ_CHUNK)
                except BlockingIOError:
                    self._read_event.clear()
                    await self._read_event.wait()
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:  # pragma: no cover
        try:
            os.write(self._master_fd, data)
        except BlockingIOError:
            # Buffer full — run in executor as fallback so we don't
            # spin.  This is rare; normally the buffer accepts input.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.write, self._master_fd, data)

    def resize(self, rows: int, cols: int) -> None:  # pragma: no cover
        _set_winsize(self._master_fd, rows, cols)
        if self._proc is not None:
            os.kill(self._proc.pid, signal.SIGWINCH)

    def close(self) -> None:  # pragma: no cover
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self._master_fd)
            except Exception:
                pass
            if self._read_event is not None:
                self._read_event.set()  # unblock any pending read
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


def _set_winsize(fd: int, rows: int, cols: int) -> None:  # pragma: no cover
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _make_shell_process() -> ShellProcess:
    return ShellProcess()


class TerminalSession:
    """Manages an interactive shell session over a PTY."""

    def __init__(
        self,
        container_id: str,
        session_name: str | None = None,
        user_home: str | None = None,
        socket_path: str | None = None,
        join_session: str | None = None,
        read_only: bool = False,
        user_id: str | None = None,
        user_handle: str | None = None,
    ):
        self.container_id = container_id
        self.session_name = session_name
        self.user_home = user_home
        self.socket_path = socket_path
        self.join_session = join_session
        self.read_only = read_only
        self.user_id = user_id
        self.user_handle = user_handle
        self._shell: ShellProcess | None = None
        self._output_queue: BoundedOutputQueue[str] = BoundedOutputQueue(
            maxsize=64
        )
        self._running = False
        self._read_task: asyncio.Task | None = None
        self._tmux_session_name: str | None = None

    async def start(
        self,
        cols: int = 80,
        rows: int = 24,
        command_override: str | None = None,
    ) -> None:
        """Start a shell session via ``podman exec`` over a PTY."""
        self._running = True
        env = _build_environment(
            command_override,
            self.user_home,
            user_id=self.user_id,
            user_handle=self.user_handle,
        )
        shell_cmd, self._tmux_session_name = _build_shell_command(
            session_name=self.session_name,
            user_home=self.user_home,
            socket_path=self.socket_path,
            join_session=self.join_session,
            read_only=self.read_only,
            tmux_enabled=terminal_tmux_enabled(),
        )
        work_dir = "/home"
        argv = _build_exec_argv(self.container_id, env, shell_cmd, work_dir)

        logger.info("Terminal exec argv: %s", argv)
        shell = _make_shell_process()
        try:
            await shell.start(argv, rows, cols)
        except Exception:
            self._running = False
            raise

        self._shell = shell
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info(
            "Terminal session started for container %s", self.container_id
        )

    async def _read_loop(self) -> None:
        """Read PTY output and queue it as text.

        Uses an *incremental* UTF-8 decoder so a multi-byte glyph (e.g. the
        box-drawing ``─`` = ``e2 94 80``) split across two ``os.read`` chunks is
        buffered and reassembled instead of being mangled into ``U+FFFD``
        replacement chars. Per-chunk ``bytes.decode`` corrupted such glyphs,
        shifting columns and desyncing the terminal cell model (ghosting).
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while self._running and self._shell is not None:
                data = await self._shell.read()
                if not data:
                    logger.info("Terminal read loop: EOF from PTY")
                    break
                text = decoder.decode(data)
                if text:
                    try:
                        self._output_queue.put_nowait(text)
                    except asyncio.QueueFull:  # pragma: no cover
                        pass  # drop output; don't block the PTY read
            # Flush any trailing partial sequence (a stream that ends
            # mid-character yields a single replacement char rather than
            # dropping bytes).
            tail = decoder.decode(b"", final=True)
            if tail:
                await self._output_queue.put(tail)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception:
            logger.exception("Error in terminal read loop")
        finally:
            self._output_queue.send_sentinel()

    @property
    def is_alive(self) -> bool:
        if self._shell is None:
            return False
        if self._read_task is not None and self._read_task.done():
            return False
        return self._running

    async def write(self, data: str) -> None:
        """Write user input to the terminal."""
        if self._shell is not None:
            try:
                await asyncio.wait_for(
                    self._shell.write(data.encode("utf-8")),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:  # pragma: no cover
                logger.warning(
                    "PTY write timed out after 30s, stopping session"
                )
                await self.stop()
            except OSError:
                logger.debug("Write to terminal failed", exc_info=True)

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal."""
        if self._shell is not None:
            try:
                self._shell.resize(rows, cols)
            except OSError:
                logger.debug("Terminal resize failed", exc_info=True)

    async def output(self) -> AsyncGenerator[str, None]:
        """Yield terminal output as it arrives."""
        while self._running:
            try:
                data = await asyncio.wait_for(
                    self._output_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if self._read_task is not None and self._read_task.done():
                    break
                continue  # pragma: no cover
            if data is None:
                break
            yield data

    async def stop(self) -> None:
        """Stop the terminal session and clean up."""
        self._running = False

        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error awaiting terminal read task")
            self._read_task = None

        if self._shell is not None:
            try:
                self._shell.close()
            except OSError:
                logger.debug("Error closing terminal shell", exc_info=True)
            self._shell = None

        # Kill the tmux session inside the container so the client
        # doesn't stay attached after the host-side process is gone.
        # For shared-socket sessions (-S), target the specific socket.
        # For joiner sessions (join_session set, no socket), kill on
        # the default server to prevent orphaned session-group members.
        # Owner sessions (no join_session, no socket) are left alive
        # so reconnecting users can reattach.
        if self._tmux_session_name and (self.socket_path or self.join_session):
            try:
                socket_args = (
                    ["-S", self.socket_path] if self.socket_path else []
                )
                argv = [
                    "exec",
                    "-u",
                    "klangk",
                    self.container_id,
                    "tmux",
                    *socket_args,
                    "kill-session",
                    "-t",
                    self._tmux_session_name,
                ]
                proc = await asyncio.create_subprocess_exec(
                    podman.PODMAN_BIN,
                    *argv,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=podman.subprocess_env(),
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # pragma: no cover
                logger.debug(
                    "Failed to kill tmux session %s",
                    self._tmux_session_name,
                    exc_info=True,
                )

        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
