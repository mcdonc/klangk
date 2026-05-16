"""Terminal session: docker exec subprocess with PTY for interactive shell access."""

import asyncio
import fcntl
import logging
import os
import struct
import termios
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


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
        master_fd, slave_fd = os.openpty()

        # Set initial terminal size
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        self._master_fd = master_fd
        self._running = True

        # Spawn docker exec with the slave PTY as stdio
        self._proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-it", "-u", "bark", "-w", "/workspace",
            "-e", "TERM=xterm-256color",
            self.container_id, "/bin/bash",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)  # Parent doesn't need the slave end

        # Register async reader on the master fd
        loop = asyncio.get_event_loop()
        loop.add_reader(master_fd, self._on_readable)

        # Send init commands to set up prompt and aliases
        init = (
            r"PS1='\[\033[01;34m\]\w\[\033[00m\]\$ '"
            "\nalias ls='ls --color=auto'"
            "\nalias grep='grep --color=auto'"
            "\nclear\n"
        )
        os.write(master_fd, init.encode())

        logger.info("Terminal session started for container %s", self.container_id)

    def _on_readable(self) -> None:
        """Called when data is available on the PTY master fd."""
        try:
            data = os.read(self._master_fd, 65536)
            if data:
                self._output_queue.put_nowait(
                    data.decode("utf-8", errors="replace")
                )
            else:
                self._output_queue.put_nowait(None)
        except OSError:
            self._output_queue.put_nowait(None)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def write(self, data: str) -> None:
        """Write user input to the terminal."""
        if self._master_fd is not None:
            os.write(self._master_fd, data.encode("utf-8"))

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal PTY."""
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)

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
            except Exception:
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
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        logger.info("Terminal session stopped for container %s", self.container_id)
