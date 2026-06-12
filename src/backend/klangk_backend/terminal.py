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
import signal
import struct
import termios
import tty
from collections.abc import AsyncGenerator

from . import podman
from .util import BoundedOutputQueue

logger = logging.getLogger(__name__)

_READ_CHUNK = 65536

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
    bridge_token: str | None,
    user_home: str | None = None,
) -> list[str]:
    env = ["TERM=xterm-256color"]
    if user_home is not None:
        env.append(f"HOME={user_home}")
    if command_override is not None:
        env.append(f"KLANGK_CMD_OVERRIDE={command_override}")
    if bridge_token is not None:
        env.append(f"KLANGK_BRIDGE_TOKEN={bridge_token}")
    return env


def _build_shell_command(user_home: str | None = None) -> list[str]:
    unset_args: list[str] = []
    for key in os.environ:
        if key.startswith(_SENSITIVE_ENV_PREFIXES):
            unset_args.extend(["-u", key])
    tmux_env: list[str] = []
    session_args: list[str] = []
    if user_home is not None:
        tmux_env = ["-e", f"HOME={user_home}"]
        # Name the session after the handle so reconnects reattach.
        # -A attaches if the session exists, creates if not.
        handle = user_home.rsplit("/", 1)[-1]
        session_args = ["-A", "-s", handle]
    return [
        "env",
        *unset_args,
        "tmux",
        "new-session",
        *session_args,
        *tmux_env,
    ]


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


def _session_name(user_home: str | None) -> str | None:
    """Extract the tmux session name from a user_home path."""
    if user_home is None:
        return None
    return user_home.rsplit("/", 1)[-1]


async def tmux_command(
    container_id: str, session_name: str, args: list[str]
) -> str:
    """Run a tmux command in the container and return stdout."""
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
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"tmux command failed: {err}")
    return stdout.decode("utf-8", errors="replace")


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
            "#{window_index}|||#{window_name}|||#{window_active}",
        ],
    )
    windows = []
    for line in output.strip().splitlines():
        parts = line.split("|||")
        if len(parts) >= 3:
            windows.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "active": parts[2] == "1",
                }
            )
    return windows


async def new_window(
    container_id: str, session_name: str, name: str | None = None
) -> list[dict]:
    """Create a new tmux window and return the updated window list.

    Raises ``ValueError`` if *name* duplicates an existing window name.
    """
    if name:
        existing = await list_windows(container_id, session_name)
        if any(w["name"] == name for w in existing):
            raise ValueError(f"Window name '{name}' already exists")
    args = ["new-window", "-t", session_name]
    if name:
        args += ["-n", name]
    await tmux_command(container_id, session_name, args)
    return await list_windows(container_id, session_name)


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
    container_id: str, session_name: str, index: int
) -> None:
    """Switch the active tmux window."""
    await tmux_command(
        container_id,
        session_name,
        ["select-window", "-t", f"{session_name}:{index}"],
    )


async def close_window(
    container_id: str, session_name: str, index: int
) -> list[dict]:
    """Close a tmux window and return the updated window list."""
    await tmux_command(
        container_id,
        session_name,
        ["kill-window", "-t", f"{session_name}:{index}"],
    )
    return await list_windows(container_id, session_name)


class ShellProcess:
    """Owns the PTY + ``podman exec`` subprocess for one shell."""

    def __init__(self) -> None:
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None

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

    async def read(self) -> bytes:  # pragma: no cover
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, os.read, self._master_fd, _READ_CHUNK
            )
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:  # pragma: no cover
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

    def __init__(self, container_id: str, user_home: str | None = None):
        self.container_id = container_id
        self.user_home = user_home
        self._shell: ShellProcess | None = None
        self._output_queue: BoundedOutputQueue[str] = BoundedOutputQueue(
            maxsize=64
        )
        self._running = False
        self._read_task: asyncio.Task | None = None

    async def start(
        self,
        cols: int = 80,
        rows: int = 24,
        command_override: str | None = None,
        bridge_token: str | None = None,
    ) -> None:
        """Start a shell session via ``podman exec`` over a PTY."""
        self._running = True
        env = _build_environment(
            command_override, bridge_token, self.user_home
        )
        shell_cmd = _build_shell_command(self.user_home)
        work_dir = "/home"
        argv = _build_exec_argv(self.container_id, env, shell_cmd, work_dir)

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
                    break
                text = decoder.decode(data)
                if text:
                    await self._output_queue.put(text)
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
                await self._shell.write(data.encode("utf-8"))
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

        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
