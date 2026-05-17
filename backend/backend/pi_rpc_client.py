"""Communicates with a Pi coding agent container via docker exec stdin/stdout."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class PiRpcClient:
    """Communicates with a Pi container via `docker attach` subprocess."""

    def __init__(self, container_id: str):
        self.container_id = container_id
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._running = False

    async def connect(self) -> None:
        """Attach to the container via `docker attach` subprocess."""
        self._proc = await asyncio.create_subprocess_exec(
            "docker",
            "attach",
            "--no-stdin=false",
            self.container_id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("Attached to container %s via docker attach", self.container_id)

    async def _read_loop(self) -> None:
        """Read newline-delimited JSON events from stdout.

        Uses chunked reads instead of readline() to avoid buffer limits —
        Pi can emit very large JSON lines (e.g., message_update events that
        include the full accumulated message content).
        """
        buf = b""
        try:
            while self._running and self._proc and self._proc.stdout:
                if self._proc.stdout.at_eof():
                    break
                if self._proc.returncode is not None:
                    break
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                        await self._event_queue.put(event)
                    except json.JSONDecodeError:
                        logger.warning("Non-JSON line from Pi: %s", text[:200])
        except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
            logger.error("Pi RPC read error: %s", e)
        finally:
            await self._event_queue.put(None)

    @property
    def is_alive(self) -> bool:
        """Check if the docker attach process is still running."""
        return self._proc is not None and self._proc.returncode is None

    async def send_command(self, command: dict) -> None:
        """Send a JSON command to Pi's stdin."""
        if not self.is_alive:
            raise RuntimeError("Pi process is dead")
        line = json.dumps(command) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def prompt(self, text: str, images: list[dict] | None = None) -> None:
        cmd = {"type": "prompt", "message": text}
        if images:
            cmd["images"] = images
        await self.send_command(cmd)

    async def steer(self, text: str) -> None:
        await self.send_command({"type": "steer", "message": text})

    async def follow_up(self, text: str) -> None:
        await self.send_command({"type": "follow_up", "message": text})

    async def abort(self) -> None:
        await self.send_command({"type": "abort"})

    async def events(self) -> AsyncGenerator[dict, None]:
        """Yield Pi RPC events as they arrive."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def disconnect(self) -> None:
        self._running = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
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
