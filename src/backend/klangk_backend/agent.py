"""Pi RPC client for the workspace chat agent.

Manages a long-lived ``pi --mode rpc`` subprocess per workspace
container, sending prompts and collecting responses via JSONL over
stdin/stdout of a ``podman exec`` session.
"""

import asyncio
import json
import logging
import re
import time

from . import model, podman, util, workspaces

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


class AgentError(Exception):
    """Base class for agent errors."""


class AgentProcessDied(AgentError):
    """Raised when the Pi RPC subprocess exits unexpectedly."""


class AgentSetupError(AgentError):
    """Raised when the agent's home directory cannot be set up."""


# Registry of active agent sessions keyed by workspace ID.
_agents: dict[str, "AgentSession"] = {}
_agents_lock = asyncio.Lock()

# Callback to get a workspace session for broadcasting.
# Set by wshandler at import time to break the circular dependency.
get_workspace_session = None


def is_disabled() -> bool:
    """True if the chat agent has been disabled by an admin.

    When disabled, the ``pi --mode rpc`` subprocess is never spawned —
    see ``ensure_started``, which consults this before creating the
    process.  Resolved at call time so tests can toggle it via
    ``monkeypatch.setenv``.
    """
    return util.resolve_env_bool("KLANGK_AGENT_DISABLED")


async def ensure_agent_home(workspace_id: str, container_id: str) -> str:
    """Eagerly provision the agent's home directory with Pi config.

    Creates ``/home/.users/{AGENT_USER_ID}`` on the host bind-mount and
    populates it via ``klangk-setup-pi`` (the same path real users
    take).  Returns the container path, e.g. ``/home/clanker``.

    Idempotent: ``ensure_home_symlink`` is a no-op when the symlink
    already exists (skeleton files only on first creation), and
    ``klangk-setup-pi --force`` re-writes Pi config to pick up env-var
    changes.  Called eagerly at container bring-up (so
    ``$KLANGK_AGENT_HOME`` points at a populated directory for every
    process) and again from chat-start, which caches the result per
    ``AgentSession``.
    """
    ws = await model.get_workspace_by_id(workspace_id)
    if not ws:
        raise AgentSetupError(
            f"Workspace {workspace_id} not found in database"
        )
    workspace_home = workspaces.home_path(workspace_id)

    agent_handle = await model.agent_handle()
    container_home, created = await workspaces.ensure_home_symlink(
        workspace_home, agent_handle, model.AGENT_USER_ID
    )
    if created:
        await workspaces.populate_home_skel(container_id, model.AGENT_USER_ID)

    # Run klangk-setup-pi to populate ~/.pi/agent/ with models.json,
    # settings.json, etc.  Unlike real users, the agent has no
    # personal preferences — --force deletes settings.json first so
    # it picks up the current KLANGK_LLM_MODEL env var.
    proc = await asyncio.create_subprocess_exec(
        podman._podman_bin(),
        "exec",
        "-u",
        "klangk",
        "-e",
        f"HOME={container_home}",
        container_id,
        "/opt/klangk/bin/klangk-setup-pi",
        "--force",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=podman.subprocess_env(),
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    # Check the return code but do NOT fail the container bring-up over a
    # provisioning hiccup: the workspace stays usable, and the lazy
    # chat-start path (AgentSession._ensure_home) will retry on first
    # mention.  Surface the failure loudly, though, so it's not silently
    # swallowed (a previous silent-unconditional "ready" log hid a
    # missing ~/.pi/agent entirely). #1162
    if proc.returncode != 0:
        logger.warning(
            "klangk-setup-pi exited %s for container %s; agent home at "
            "%s may be incomplete (chat will retry on first mention):\n"
            "stdout: %s\nstderr: %s",
            proc.returncode,
            container_id,
            container_home,
            stdout.decode(errors="replace") if stdout else "",
            stderr.decode(errors="replace") if stderr else "",
        )
    else:
        logger.info(
            "Agent home ready at %s for container %s",
            container_home,
            container_id,
        )
    return container_home


class AgentSession:
    """Wraps a ``pi --mode rpc`` subprocess inside a container."""

    def __init__(self, workspace_id: str, app_state=None) -> None:
        self.workspace_id = workspace_id
        self.app_state = app_state
        self._proc: asyncio.subprocess.Process | None = None
        # Serializes prompt round-trips so two concurrent prompts can't
        # interleave their stdin writes / stdout reads on one subprocess.
        self._lock = asyncio.Lock()
        # Serializes the check-then-spawn in ``ensure_started`` so the
        # auto-restart path (``_monitor_process``) and the lazy-start
        # path (``send_prompt``) can't both spawn a subprocess for the
        # same dead process (#1189).  Separate from ``_lock`` because
        # ``send_prompt`` already holds ``_lock`` when it calls
        # ``ensure_started``, and ``asyncio.Lock`` is not reentrant.
        self._spawn_lock = asyncio.Lock()
        self._home_ready = False
        self._monitor_task: asyncio.Task | None = None
        self._restart_attempts = 0
        self._gave_up = False
        self._last_container_id: str | None = None

    def _resolve_container_id(self) -> str:
        """Look up the current container ID for this workspace."""
        state = self.app_state.container_registry.get_state(self.workspace_id)
        if state is None:
            raise AgentError(
                f"No container running for workspace {self.workspace_id}"
            )
        cid = state.container_id
        if cid != self._last_container_id:
            # Container changed — reset state for the new container.
            self._home_ready = False
            self._restart_attempts = 0
            self._gave_up = False
            self._last_container_id = cid
        return cid

    async def _ensure_home(self, container_id: str) -> str:
        """Ensure the agent has a home directory with Pi config (cached).

        Thin per-instance cache over :func:`ensure_agent_home`.  The
        actual provisioning — and the eager bring-up call site — live
        there.  Returns the container path, e.g. ``/home/clanker``.
        """
        if self._home_ready:
            handle = await model.agent_handle()
            return f"/home/{handle}"
        container_home = await ensure_agent_home(
            self.workspace_id, container_id
        )
        self._home_ready = True
        return container_home

    async def ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        if is_disabled():
            # Admin kill switch: refuse to spawn the subprocess.  This is
            # the single place the env var is enforced — if it's set, the
            # agent never runs for any workspace.
            logger.info(
                "Agent disabled by admin; not spawning for workspace %s",
                self.workspace_id,
            )
            raise AgentError("chat agent is disabled by admin")
        if self._gave_up:
            raise AgentError(
                "Agent gave up restarting for workspace %s" % self.workspace_id
            )

        # Serialize the check-then-spawn.  ``ensure_started`` has two
        # concurrent callers on this singleton session: ``_monitor_process``
        # (auto-restart, no other lock) and ``send_prompt`` (lazy start,
        # under ``self._lock``).  Between the fast-path check above and the
        # spawn below sits ``_ensure_home``, which does real podman work,
        # so the race window is seconds wide — without this lock both
        # callers observe ``self._proc is None`` and each spawns a
        # ``pi --mode rpc`` subprocess, orphaning the loser (#1189).  The
        # re-check inside the lock makes the spawn idempotent: the caller
        # that lost the race finds the process the winner already started
        # and returns it instead of spawning again.
        async with self._spawn_lock:
            if self._proc is not None and self._proc.returncode is None:
                return self._proc
            container_id = self._resolve_container_id()
            container_home = await self._ensure_home(container_id)
            agent_handle = await model.agent_handle()
            system_prompt = _CHAT_SYSTEM_PROMPT.format(name=agent_handle)
            argv = [
                "exec",
                "-i",
                "-u",
                "klangk",
                "-e",
                f"HOME={container_home}",
                "-w",
                container_home,
                container_id,
                "pi",
                "--mode",
                "rpc",
                "--append-system-prompt",
                system_prompt,
            ]
            logger.info(
                "Starting Pi RPC for workspace %s (container %s)",
                self.workspace_id,
                container_id,
            )
            self._proc = await asyncio.create_subprocess_exec(
                podman._podman_bin(),
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=podman.subprocess_env(),
            )
            self._monitor_task = asyncio.create_task(
                self._monitor_process(self._proc)
            )
            return self._proc

    async def _monitor_process(self, proc: asyncio.subprocess.Process) -> None:
        """Wait for the agent subprocess to exit, broadcast disconnect, and restart."""
        await proc.wait()
        # Only act if this is still our current process (not replaced)
        if self._proc is not proc:
            return
        self._proc = None

        # Drain stderr for diagnostics.
        stderr_text = ""
        if proc.stderr is not None:
            try:
                stderr_bytes = await asyncio.wait_for(
                    proc.stderr.read(8192), timeout=2
                )
                stderr_text = stderr_bytes.decode(errors="replace").strip()
            except (asyncio.TimeoutError, OSError):
                pass
        logger.warning(
            "Agent process died (rc=%s) for workspace %s%s",
            proc.returncode,
            self.workspace_id,
            f": {stderr_text}" if stderr_text else "",
        )

        await broadcast_agent_disconnect(self.workspace_id)
        # Auto-restart after a brief delay to avoid tight loops
        self._restart_attempts += 1
        if self._restart_attempts > 2:
            self._gave_up = True
            logger.error(
                "Agent exceeded 3 restart attempts for workspace %s, giving up",
                self.workspace_id,
            )
            return
        await asyncio.sleep(2)
        if self._proc is not None:
            return  # something else already restarted it
        # If the container is gone (workspace deleted), don't restart.
        if (
            self.app_state.container_registry.get_state(self.workspace_id)
            is None
        ):
            logger.info(
                "Container gone for workspace %s, not restarting agent",
                self.workspace_id,
            )
            return
        try:
            await self.ensure_started()
            await broadcast_agent_reconnect(self.workspace_id)
        except Exception:
            logger.exception(
                "Failed to auto-restart agent for workspace %s",
                self.workspace_id,
            )

    async def send_prompt(self, message: str, timeout: float = 120) -> str:
        """Send a prompt to Pi and return the accumulated text response."""
        async with self._lock:
            proc = await self.ensure_started()
            assert proc.stdin is not None
            assert proc.stdout is not None

            cmd = json.dumps({"type": "prompt", "message": message})
            proc.stdin.write((cmd + "\n").encode())
            await proc.stdin.drain()

            # Wait for the command ack before reading events.  Pi
            # sends {"type":"response","command":"prompt","success":true}
            # first; any lines before it are leftover from a previous
            # turn and must be discarded.
            try:
                await asyncio.wait_for(
                    self._wait_for_ack(proc.stdout), timeout=30
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Pi RPC ack timed out for workspace %s",
                    self.workspace_id,
                )

            # Skip past any leftover events to the current turn's
            # agent_start, then collect deltas until agent_end.
            await asyncio.wait_for(
                self._skip_to_agent_start(proc.stdout), timeout=30
            )
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
                # A prompt completed end-to-end: the process is healthy,
                # so recent restart failures no longer count against the
                # limit. Resetting here keeps the restart counter a
                # measure of *recent* failures rather than lifetime ones,
                # so a few transient deaths spread over a long-running
                # workspace can't permanently disable the agent.
                self._restart_attempts = 0
                self._gave_up = False
                return response
            except asyncio.CancelledError:  # pragma: no cover
                self._send_abort(proc)
                raise
            except asyncio.TimeoutError:
                self._send_abort(proc)
                logger.warning(
                    "Pi RPC timed out after %.0fs for workspace %s",
                    timeout,
                    self.workspace_id,
                )
                # The abort is fire-and-forget: Pi may still emit
                # trailing turn-1 events (message_end / agent_end) and
                # the abort ack into the stdout pipe.  Reusing this
                # process would force the next turn to resync past all
                # of that, which is fragile -- a late turn-1 agent_end
                # landing inside turn 2's read window would truncate
                # turn 2 (#894).  Tear the process down so the next
                # prompt starts from a clean stream.
                await self._reset_process(proc)
                return "Sorry, I timed out processing your request."

    async def _reset_process(self, proc: asyncio.subprocess.Process) -> None:
        """Tear down the current process so the next prompt starts fresh.

        Cancels the death monitor (preventing a spurious disconnect
        broadcast), clears ``self._proc``, and kills the subprocess.
        Used on timeout, where leftover turn-1 events would otherwise
        linger in the stdout pipe and risk corrupting the next turn
        (#894).  The next ``send_prompt`` spawns a fresh subprocess via
        ``ensure_started``.
        """
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        self._proc = None
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _send_abort(self, proc: asyncio.subprocess.Process) -> None:
        """Send an abort command to Pi."""
        if proc.stdin and not proc.stdin.is_closing():
            try:
                cmd = json.dumps({"type": "abort"})
                proc.stdin.write((cmd + "\n").encode())
            except (OSError, RuntimeError):
                pass  # process stdin already closed

    async def _wait_for_ack(self, stdout: asyncio.StreamReader) -> None:
        """Read lines until the Pi RPC command acknowledgement.

        Discards any leftover events from a previous turn that may
        still be in the pipe.
        """
        while True:
            line = await stdout.readline()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                event.get("type") == "response"
                and event.get("command") == "prompt"
            ):
                return

    async def _skip_to_agent_start(self, stdout: asyncio.StreamReader) -> None:
        """Skip events until agent_start for the current turn.

        After the ack, Pi emits agent_start before sending any deltas.
        If there are leftover events from a prior turn (e.g. after a
        timeout), they appear before agent_start and must be discarded.
        """
        while True:
            line = await stdout.readline()
            if not line:
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "agent_start":
                return
            if etype == "auto_retry_start":
                logger.info(
                    "Pi auto-retry %d/%d for workspace %s: %s",
                    event.get("attempt", "?"),
                    event.get("maxAttempts", "?"),
                    self.workspace_id,
                    str(event.get("errorMessage", ""))[:100],
                )

    async def _read_until_agent_end(
        self,
        stdout: asyncio.StreamReader,
        text_parts: list[str],
    ) -> str:
        """Read JSONL events from Pi until the final agent_end.

        Pi may emit multiple agent_start/agent_end cycles when it
        auto-retries on errors (e.g. 429 rate limits).  We keep
        reading until an agent_end with ``willRetry: false`` (or no
        ``willRetry`` key, which is the normal success case).
        """
        thinking_parts: list[str] = []
        last_error: str = ""
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
                delta_type = delta.get("type", "")
                if delta_type == "text_delta":
                    text_parts.append(delta.get("delta", ""))
                elif delta_type == "thinking_delta":
                    thinking_parts.append(delta.get("delta", ""))
                elif delta_type not in ("thinking_start", "thinking_end"):
                    logger.debug("Unhandled delta type: %s", delta_type)

            elif event_type in ("message_start", "message_end"):
                # Capture error messages from the LLM provider.
                msg = event.get("message", {})
                if msg.get("stopReason") == "error":
                    last_error = msg.get("errorMessage", "")

            elif event_type == "agent_end":
                if not event.get("willRetry", False):
                    break
                # Pi is retrying — reset parts for the next attempt
                # and wait for the next agent_start.
                text_parts.clear()
                thinking_parts.clear()
                last_error = ""
                await self._skip_to_agent_start(stdout)

            # auto_retry_start is consumed by _skip_to_agent_start

        text = "".join(text_parts)
        # Strip <think>...</think> tags that some models emit
        text = _THINK_RE.sub("", text).strip()
        if not text:
            # Some models put their response in thinking only (no
            # text_delta). Fall back to the thinking content, stripped
            # of reasoning artifacts.
            text = "".join(thinking_parts).strip()
            text = _THINK_RE.sub("", text).strip()
        if not text and last_error:
            # Surface the LLM error to the user.
            text = f"Error from LLM: {last_error}"
        return text or "I had nothing to say."

    async def stop(self) -> None:
        """Kill the Pi subprocess."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:  # pragma: no cover
                pass
        self._proc = None


async def get_session(workspace_id: str, app_state=None) -> AgentSession:
    """Get or create an AgentSession for the given workspace.

    The session resolves the current container ID from the container
    registry on each startup, so it automatically picks up container
    restarts without needing to be told the new ID.

    Serialized with ``stop_session`` via ``_agents_lock`` so that a new
    session is never installed for a container that ``stop_session`` has
    already torn down (#1298).
    """
    async with _agents_lock:
        session = _agents.get(workspace_id)
        if session is None:
            session = AgentSession(workspace_id, app_state=app_state)
            _agents[workspace_id] = session
        return session


def is_running(workspace_id: str) -> bool:
    """Return True if an agent subprocess is alive for this workspace."""
    session = _agents.get(workspace_id)
    if session is None:
        return False
    return session._proc is not None and session._proc.returncode is None


def any_running() -> bool:
    """Return True if any agent subprocess is alive."""
    return any(
        s._proc is not None and s._proc.returncode is None
        for s in _agents.values()
    )


async def stop_session(workspace_id: str) -> None:
    """Stop and remove the agent session for a workspace.

    Serialized with ``get_session`` via ``_agents_lock`` so that a
    concurrent ``get_session`` cannot install a new session for a
    container that is being torn down (#1298).
    """
    async with _agents_lock:
        session = _agents.pop(workspace_id, None)
        if session:
            await session.stop()


async def stop_all_sessions() -> None:
    """Stop and remove every active agent session.

    Used by the SIGHUP runtime-restart path: each agent session is a
    Pi RPC subprocess attached to a container that is about to be
    stopped, so they must be torn down before the containers go.
    """
    for ws_id in list(_agents.keys()):
        await stop_session(ws_id)


def ephemeral_system_message(
    workspace_id: str,
    agent_email: str,
    agent_handle: str,
    text: str,
) -> dict:
    """Build a transient agent presence system message for live broadcast.

    Mirrors the shape returned by [model.add_chat_message] so the frontend
    renders it the same as a persisted system message, but is never written
    to chat history — agent presence transitions (disconnect/reconnect) are
    driven by container lifecycle (idle-stop, restart, crash) and would
    otherwise pollute history with stale, unbalanced "has disconnected"
    entries.  ``id`` is a synthetic prefix so the frontend can dedupe;
    ``created_at`` is empty (the frontend tolerates that).
    """
    return {
        "id": f"ephemeral-agent-{workspace_id}-{int(time.monotonic() * 1000)}",
        "workspace_id": workspace_id,
        "user_id": model.AGENT_USER_ID,
        "user_email": agent_email,
        "user_handle": agent_handle,
        "message": text,
        "message_type": model.MSG_SYSTEM,
        "created_at": "",
        "mentions": [],
    }


async def broadcast_agent_disconnect(workspace_id: str) -> None:
    """Broadcast a disconnect system message when the agent process dies.

    Ephemeral only — sent to currently-connected subscribers, never written
    to chat history.  The agent subprocess lives inside the workspace
    container, so it dies on every container idle-stop / restart; persisting
    "has disconnected" made those lifecycle events linger in chat history
    and surface as a stale leading message on the next visit, with no
    symmetric persisted "has connected" to balance them.
    """
    if not workspace_id:
        return
    # Workspace may have been deleted — skip if gone.
    if await model.get_workspace_by_id(workspace_id) is None:
        return
    agent_handle = await model.agent_handle()
    agent_email = await model.agent_email()
    sys_msg = ephemeral_system_message(
        workspace_id,
        agent_email,
        agent_handle,
        f"{agent_handle} has disconnected",
    )
    session = (
        get_workspace_session(workspace_id) if get_workspace_session else None
    )
    if session:
        session.broadcast({"type": "agent_thinking", "thinking": False})
        session.broadcast({"type": "chat_message", **sys_msg})
        session.broadcast(
            {
                "type": "presence_leave",
                "user_id": model.AGENT_USER_ID,
                "user_email": agent_email,
                "user_handle": agent_handle,
            }
        )


async def broadcast_agent_reconnect(workspace_id: str) -> None:
    """Broadcast a reconnect system message after auto-restart.

    Ephemeral only — see [broadcast_agent_disconnect].
    """
    if not workspace_id:
        return
    # Workspace may have been deleted — skip if gone.
    if await model.get_workspace_by_id(workspace_id) is None:
        return
    agent_handle = await model.agent_handle()
    agent_email = await model.agent_email()
    sys_msg = ephemeral_system_message(
        workspace_id,
        agent_email,
        agent_handle,
        f"{agent_handle} has reconnected",
    )
    session = (
        get_workspace_session(workspace_id) if get_workspace_session else None
    )
    if session:
        session.broadcast({"type": "chat_message", **sys_msg})
        session.broadcast(
            {
                "type": "presence_join",
                "user_id": model.AGENT_USER_ID,
                "user_email": agent_email,
                "user_handle": agent_handle,
            }
        )
