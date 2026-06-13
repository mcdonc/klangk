"""Pi RPC client for the workspace chat agent.

Manages a long-lived ``pi --mode rpc`` subprocess per workspace
container, sending prompts and collecting responses via JSONL over
stdin/stdout of a ``podman exec`` session.
"""

import asyncio
import json
import logging
import re

from . import podman

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

_CHAT_SYSTEM_PROMPT = """\
You are {name}, a helpful AI assistant in a collaborative workspace chat.
You participate alongside human users who are working together on code
and projects.

Guidelines:
- Be concise — chat messages should be short and to the point.
- You can read and write files, run commands, and help with code.
- When asked a question, answer it directly.
- When asked to do something, do it and report back briefly.
- You see recent chat messages as context. Respond to what was asked.
- Don't repeat greetings or say "hi" unless specifically greeted.
- If you don't know something, say so rather than guessing.
- Your working directory is /home. Project files are in /home/work.
- Always create and store files you generate in /home/work.
"""

logger = logging.getLogger(__name__)


class AgentProcessDied(Exception):
    """Raised when the Pi RPC subprocess exits unexpectedly."""


# Registry of active agent sessions keyed by container ID.
_agents: dict[str, "AgentSession"] = {}


class AgentSession:
    """Wraps a ``pi --mode rpc`` subprocess inside a container."""

    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        from . import model

        system_prompt = _CHAT_SYSTEM_PROMPT.format(name=model.AGENT_EMAIL)
        argv = [
            "exec",
            "-i",
            "-u",
            "klangk",
            "-w",
            "/home",
            self.container_id,
            "pi",
            "--mode",
            "rpc",
            "--no-session",
            "--append-system-prompt",
            system_prompt,
        ]
        logger.info("Starting Pi RPC for container %s", self.container_id)
        self._proc = await asyncio.create_subprocess_exec(
            podman.PODMAN_BIN,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=podman.subprocess_env(),
        )
        return self._proc

    async def send_prompt(self, message: str, timeout: float = 120) -> str:
        """Send a prompt to Pi and return the accumulated text response."""
        async with self._lock:
            proc = await self._ensure_started()
            assert proc.stdin is not None
            assert proc.stdout is not None

            cmd = json.dumps({"type": "prompt", "message": message})
            proc.stdin.write((cmd + "\n").encode())
            await proc.stdin.drain()

            # Collect text deltas until agent_end
            text_parts: list[str] = []
            try:
                response = await asyncio.wait_for(
                    self._read_until_agent_end(proc.stdout, text_parts),
                    timeout=timeout,
                )
                # If the process exited, raise so the caller can report it
                if proc.returncode is not None:
                    self._proc = None
                    raise AgentProcessDied(
                        f"Agent process exited with code {proc.returncode}"
                    )
                return response
            except asyncio.CancelledError:  # pragma: no cover
                self._send_abort(proc)
                raise
            except asyncio.TimeoutError:
                self._send_abort(proc)
                logger.warning(
                    "Pi RPC timed out after %.0fs for container %s",
                    timeout,
                    self.container_id,
                )
                return "Sorry, I timed out processing your request."

    def _send_abort(
        self, proc: asyncio.subprocess.Process
    ) -> None:  # pragma: no cover
        """Send an abort command to Pi."""
        if proc.stdin and not proc.stdin.is_closing():
            try:
                cmd = json.dumps({"type": "abort"})
                proc.stdin.write((cmd + "\n").encode())
            except Exception:  # pragma: no cover
                pass

    async def _read_until_agent_end(
        self,
        stdout: asyncio.StreamReader,
        text_parts: list[str],
    ) -> str:
        """Read JSONL events from Pi until agent_end."""
        while True:
            line = await stdout.readline()
            if not line:
                # Process exited
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")

            if event_type == "message_update":
                delta = event.get("assistantMessageEvent", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("delta", ""))

            elif event_type == "agent_end":
                break

        text = "".join(text_parts)
        # Strip <think>...</think> tags that some models emit
        text = _THINK_RE.sub("", text).strip()
        return text or "I had nothing to say."

    async def stop(self) -> None:
        """Kill the Pi subprocess."""
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:  # pragma: no cover
                pass
        self._proc = None


async def get_session(container_id: str) -> AgentSession:
    """Get or create an AgentSession for the given container."""
    if container_id not in _agents:
        _agents[container_id] = AgentSession(container_id)
    return _agents[container_id]


def is_running(container_id: str) -> bool:
    """Return True if an agent subprocess is alive for this container."""
    session = _agents.get(container_id)
    if session is None:
        return False
    return session._proc is not None and session._proc.returncode is None


def any_running() -> bool:
    """Return True if any agent subprocess is alive."""
    return any(
        s._proc is not None and s._proc.returncode is None
        for s in _agents.values()
    )


async def stop_session(container_id: str) -> None:
    """Stop and remove the agent session for a container."""
    session = _agents.pop(container_id, None)
    if session:
        await session.stop()
