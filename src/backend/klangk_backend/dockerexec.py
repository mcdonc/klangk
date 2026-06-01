"""Raw exec session: docker exec subprocess without PTY for piped commands."""

import asyncio
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class ExecSession:
    """Manages a docker exec session with raw stdin/stdout pipes (no PTY)."""

    def __init__(self, container_id: str):
        self.container_id = container_id
        self._proc: asyncio.subprocess.Process | None = None
        self._output_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=64
        )
        self._running = False

    async def start(self, command: list[str]) -> None:
        """Start a command via docker exec with piped stdin/stdout."""
        exec_cmd = [
            "docker",
            "exec",
            "-i",
            "-u",
            "klangk",
            "-w",
            "/home/klangk/work",
            self.container_id,
            *command,
        ]

        self._running = True
        self._proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._read_stdout())
        logger.info(
            "Exec session started for container %s: %s",
            self.container_id,
            command,
        )

    async def _read_stdout(self) -> None:
        """Read stdout in a background task and queue chunks."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        try:
            while True:
                data = await self._proc.stdout.read(65536)
                if not data:
                    break
                # Bounded queue: blocks when full, back-pressuring the
                # process via its kernel pipe buffer.
                await self._output_queue.put(data)
        except (OSError, asyncio.CancelledError):
            pass
        # Wait for the process to exit so returncode is set before
        # the caller reads it.
        if self._proc and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (
                asyncio.TimeoutError,
                ProcessLookupError,
                OSError,
            ):  # pragma: no cover
                pass
        # Blocks until consumer drains a slot; no deadlock since
        # consumer and producer are different tasks.
        await self._output_queue.put(None)

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def write(self, data: bytes) -> None:
        """Write data to the process stdin."""
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (
                BrokenPipeError,
                ConnectionResetError,
                OSError,
            ):  # pragma: no cover
                pass  # Process already exited

    async def close_stdin(self) -> None:
        """Signal EOF on stdin."""
        if self._proc is not None and self._proc.stdin is not None:
            self._proc.stdin.close()

    async def output(self) -> AsyncGenerator[bytes, None]:
        """Yield stdout data as it arrives."""
        while self._running:
            data = await self._output_queue.get()
            if data is None:
                break
            yield data

    async def stop(self) -> None:
        """Stop the exec session and clean up."""
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError, OSError):
                try:
                    self._proc.kill()  # pragma: no cover
                except (ProcessLookupError, OSError):  # pragma: no cover
                    pass
            self._proc = None
        logger.info("Exec session stopped for container %s", self.container_id)

    @property
    def returncode(self) -> int | None:
        """Return the process exit code, or None if still running."""
        if self._proc is None:
            return None
        return self._proc.returncode
