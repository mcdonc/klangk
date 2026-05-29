"""Terminal session: Docker API exec with single PTY for interactive shell."""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator

import aiodocker

logger = logging.getLogger(__name__)


class TerminalSession:
    """Manages a Docker exec shell session via the Docker API.

    Uses aiodocker's exec API to create a single PTY connection,
    avoiding the double-PTY issue that occurs with `docker exec -it`
    as a subprocess (which consumed ESC bytes from arrow key sequences).
    """

    def __init__(self, container_id: str):
        self.container_id = container_id
        self._stream: aiodocker.stream.Stream | None = None
        self._exec: aiodocker.execs.Exec | None = None
        self._output_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._running = False
        self._read_task: asyncio.Task | None = None

    async def start(
        self,
        cols: int = 80,
        rows: int = 24,
        command_override: str | None = None,
    ) -> None:
        """Start a shell session via Docker API exec."""
        self._running = True

        # Build environment for the exec
        env = ["TERM=xterm-256color"]
        if command_override is not None:
            env.append(f"KLANGK_CMD_OVERRIDE={command_override}")

        # Build the command: env -u KEY ... /bin/bash
        # Strip sensitive env vars from the terminal session.
        unset_args = []
        for key in os.environ:
            if key.startswith(
                (
                    "KLANGK_LLM_API_KEY",
                    "ANTHROPIC_",
                    "OPENAI_",
                    "GOOGLE_",
                    "GROQ_",
                    "MISTRAL_",
                )
            ):
                unset_args.extend(["-u", key])
        for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_HEADERS",
            "OTEL_SERVICE_NAME",
        ):
            unset_args.extend(["-u", key])
        cmd = ["env", *unset_args, "/bin/bash"]

        # Create and start exec via Docker API (single PTY)
        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(self.container_id)
            self._exec = await container.exec(
                cmd,
                tty=True,
                stdin=True,
                stdout=True,
                stderr=True,
                user="klangk",
                workdir="/home/klangk/work",
                environment=env,
            )
            self._stream = self._exec.start()

            # Do the first read to establish the WebSocket connection,
            # then resize. The stream connects lazily on first I/O.
            first_msg = await self._stream.read_out()
            if first_msg is not None:
                self._output_queue.put_nowait(
                    first_msg.data.decode("utf-8", errors="replace")
                )

            await self._exec.resize(h=rows, w=cols)
        except Exception:
            await docker.close()
            raise

        self._docker = docker
        self._read_task = asyncio.create_task(self._read_loop())

        logger.info(
            "Terminal session started for container %s", self.container_id
        )

    async def _read_loop(self) -> None:
        """Read output from the Docker exec stream."""
        try:
            while self._running and self._stream is not None:
                msg = await self._stream.read_out()
                if msg is None:
                    break
                text = msg.data.decode("utf-8", errors="replace")
                if text:
                    self._output_queue.put_nowait(text)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._output_queue.put_nowait(None)

    @property
    def is_alive(self) -> bool:
        if self._exec is None:
            return False
        if self._read_task is not None and self._read_task.done():
            return False
        return self._running

    async def write(self, data: str) -> None:
        """Write user input to the terminal."""
        if self._stream is not None:
            try:
                await self._stream.write_in(data.encode("utf-8"))
            except Exception:
                pass

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal."""
        if self._exec is not None:
            try:
                await self._exec.resize(h=rows, w=cols)
            except Exception:
                pass

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

        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None

        if self._stream is not None:
            try:
                await self._stream.close()
            except Exception:
                pass
            self._stream = None

        if hasattr(self, "_docker") and self._docker is not None:
            try:
                await self._docker.close()
            except Exception:
                pass
            self._docker = None

        self._exec = None

        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
