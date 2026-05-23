"""Terminal session: docker exec subprocess with PTY for interactive shell access."""

import asyncio
import fcntl
import logging
import os
import struct
import termios
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


def openpty() -> tuple[int, int]:  # pragma: no cover
    return os.openpty()


def set_winsize(fd: int, rows: int, cols: int) -> None:  # pragma: no cover
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def fd_read(fd: int, size: int) -> bytes:  # pragma: no cover
    return os.read(fd, size)


def fd_write(fd: int, data: bytes) -> int:  # pragma: no cover
    return os.write(fd, data)


def fd_close(fd: int) -> None:  # pragma: no cover
    os.close(fd)


class TerminalSession:
    """Manages a docker exec shell session with PTY support."""

    def __init__(self, container_id: str):
        self.container_id = container_id
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._output_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._running = False

    async def start(self, cols: int = 80, rows: int = 24) -> None:
        """Start a shell session via docker exec with a PTY."""
        master_fd, slave_fd = openpty()

        set_winsize(master_fd, rows, cols)

        self._master_fd = master_fd
        self._running = True

        # Build docker exec command that fully unsets sensitive env vars
        # from the terminal session. Uses `env -u` inside the container
        # instead of `docker exec -e KEY=` (which only blanks them).
        env_unset = []
        for key in os.environ:
            if key.startswith(
                (
                    "LLM_",
                    "ANTHROPIC_",
                    "OPENAI_",
                    "GOOGLE_",
                    "GROQ_",
                    "MISTRAL_",
                )
            ):
                env_unset.extend(["-u", key])
        # Strip vars set on the container (not on the host, so not in
        # os.environ — must be listed explicitly).
        env_unset.extend(["-u", "BARK_RESUME_SESSION"])
        for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_SERVICE_NAME",
        ):
            env_unset.extend(["-u", key])
        exec_cmd = [
            "docker",
            "exec",
            "-it",
            "-u",
            "bark",
            "-w",
            "/work",
            "-e",
            "TERM=xterm-256color",
            self.container_id,
            "env",
            *env_unset,
            "/bin/bash",
        ]

        self._proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        fd_close(slave_fd)  # Parent doesn't need the slave end

        # Register async reader on the master fd
        loop = asyncio.get_event_loop()
        loop.add_reader(master_fd, self.on_readable)

        # Prompt and aliases come from /etc/bash.bashrc in the container image

        logger.info(
            "Terminal session started for container %s", self.container_id
        )

    def remove_reader(self) -> None:
        """Deregister the PTY master fd from the event loop."""
        if self._master_fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self._master_fd)
            except (ValueError, OSError):
                pass

    def on_readable(self) -> None:
        """Called when data is available on the PTY master fd."""
        try:
            data = fd_read(self._master_fd, 65536)
            if data:
                self._output_queue.put_nowait(
                    data.decode("utf-8", errors="replace")
                )
            else:
                self.remove_reader()
                self._output_queue.put_nowait(None)
        except OSError:
            self.remove_reader()
            self._output_queue.put_nowait(None)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def write(self, data: str) -> None:
        """Write user input to the terminal."""
        if self._master_fd is not None:
            fd_write(self._master_fd, data.encode("utf-8"))

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal PTY."""
        if self._master_fd is not None:
            set_winsize(self._master_fd, rows, cols)

    async def output(self) -> AsyncGenerator[str, None]:
        """Yield terminal output as it arrives."""
        while self._running:
            data = await self._output_queue.get()
            if data is None:
                break
            yield data

    async def stop(self) -> None:
        """Stop the terminal session and clean up."""
        self._running = False

        # Remove the fd reader
        if self._master_fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self._master_fd)
            except (ValueError, OSError):
                pass

        # Terminate the docker exec process
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                try:
                    self._proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            self._proc = None

        # Close the master fd
        if self._master_fd is not None:
            try:
                fd_close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
