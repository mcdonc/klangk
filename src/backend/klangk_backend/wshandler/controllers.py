"""Collaborator controllers: SSH agent, exec, terminal, shared terminal."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING

from fastapi import WebSocketDisconnect

from .. import model, terminal
from ..exceptions import TerminalError
from ..util import resolve_env_value
from ..podman import ExecSession
from ..terminal import (
    TerminalSession,
    attach_browser,
    SERVICE_CMD_WINDOW,
    SERVICE_SESSION,
)
from .safe_websocket import SlowClientError, WS_ERRORS
from .constants import MAX_INPUT_SIZE
from .helpers import send_error, send_event, get_shared_terminals

if TYPE_CHECKING:
    from .connection import Connection  # noqa: allow-deferred-import
    from .session import WorkspaceSession  # noqa: allow-deferred-import

logger = logging.getLogger(__name__)


class SshAgentForwarder:
    """SSH agent forwarding relay via socat inside the container.

    Owns the socat subprocess, its stdout-relay task, and the socket
    path.  ``Connection`` delegates the ``ssh_agent_*`` WebSocket
    commands here, and reads :attr:`socket` (the in-container
    ``SSH_AUTH_SOCK`` path) when starting terminals/exec sessions.

    Extracted from ``Connection`` (issue #961) so the relay can be
    unit-tested in isolation without standing up a full connection.
    """

    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.socket: str | None = None

    async def start(self) -> None:
        """Start SSH agent forwarding via socat inside the container."""
        _debug_agent = resolve_env_value("KLANGKC_DEBUG_SSH_AGENT", "")
        container_id = self._conn.container_id
        if not container_id:
            send_error(
                self._conn.sock, "No container for SSH agent forwarding"
            )
            return
        # Clean up any existing agent relay.
        await self.stop()
        user_id = self._conn.user["id"]
        sock_path = f"/tmp/klangk-ssh-agent-{user_id}.sock"
        # Remove stale socket if it exists from a previous session.
        await self._conn.app_state.podman.exec_container(
            container_id, ["rm", "-f", sock_path]
        )
        if _debug_agent:
            logger.info("[ssh-agent] starting socat at %s", sock_path)
        # Start socat: listen on the Unix socket, relay to stdin/stdout.
        proc = await asyncio.create_subprocess_exec(
            self._conn.app_state.podman.bin,
            "exec",
            "-i",
            container_id,
            "socat",
            f"UNIX-LISTEN:{sock_path},mode=600,unlink-early,fork",
            "STDIO",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
            if _debug_agent
            else asyncio.subprocess.DEVNULL,
        )
        self.proc = proc
        self.socket = sock_path
        self.task = asyncio.create_task(self.forward_output())
        if _debug_agent and proc.stderr is not None:
            asyncio.create_task(self.log_stderr())
        self._conn.sock.send_json(
            {
                "type": "ssh_agent_started",
                "socket": sock_path,
            }
        )
        logger.info(
            "SSH agent forwarding started for user %s at %s",
            user_id,
            sock_path,
        )

    async def log_stderr(self) -> None:
        """Log socat stderr when KLANGKC_DEBUG_SSH_AGENT is set."""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.info(
                    "[ssh-agent] socat stderr: %s", line.decode().rstrip()
                )
        except (asyncio.CancelledError, OSError):
            pass

    async def forward_output(self) -> None:
        """Read from socat stdout and send to the CLI as ssh_agent_response."""
        _debug_agent = resolve_env_value("KLANGKC_DEBUG_SSH_AGENT", "")
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    if _debug_agent:
                        logger.info("[ssh-agent] socat stdout EOF")
                    break
                if _debug_agent:
                    logger.info(
                        "[ssh-agent] socat stdout: %d bytes", len(data)
                    )
                self._conn.sock.send_json(
                    {
                        "type": "ssh_agent_response",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
        except asyncio.CancelledError:
            logger.debug("SSH agent output relay cancelled")
        except OSError as e:
            logger.warning("SSH agent output relay error: %s", e)

    async def data(self, msg: dict) -> None:
        """Write data from the CLI's local agent into socat stdin."""
        _debug_agent = resolve_env_value("KLANGKC_DEBUG_SSH_AGENT", "")
        proc = self.proc
        if proc is None or proc.stdin is None:
            if _debug_agent:
                logger.info(
                    "[ssh-agent] data received but no proc (proc=%s)",
                    proc,
                )
            return
        raw = msg.get("data", "")
        if raw:
            decoded = base64.b64decode(raw)
            if _debug_agent:
                logger.info(
                    "[ssh-agent] writing %d bytes to socat stdin",
                    len(decoded),
                )
            proc.stdin.write(decoded)
            await proc.stdin.drain()

    async def stop_command(self) -> None:
        """Stop SSH agent forwarding and notify the client."""
        await self.stop()
        self._conn.sock.send_json({"type": "ssh_agent_stopped"})

    async def stop(self) -> None:
        """Clean up the SSH agent relay process."""
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.proc is not None:
            try:
                self.proc.kill()
                await self.proc.wait()
            except ProcessLookupError:
                logger.debug("SSH agent process already exited")
            self.proc = None
        container_id = self._conn.container_id
        if self.socket and container_id:
            try:
                await self._conn.app_state.podman.exec_container(
                    container_id,
                    ["rm", "-f", self.socket],
                )
            except OSError as e:
                logger.warning(
                    "Failed to remove SSH agent socket %s: %s",
                    self.socket,
                    e,
                )
        self.socket = None


class ExecController:
    """Exec session lifecycle: start, input, output forwarding, stop.

    Owns the current ``ExecSession`` and its output-forwarding task.
    ``Connection`` delegates the ``exec_*`` WebSocket commands here,
    and reads :attr:`session` when wiring up new exec runs.

    Extracted from ``Connection`` (issue #961) so the exec subsystem
    can be unit-tested in isolation without standing up a full
    connection.  Follows the same collaborator pattern as
    :class:`SshAgentForwarder`.
    """

    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.session: ExecSession | None = None
        self.task: asyncio.Task | None = None

    async def start(self, msg: dict) -> None:
        container_id = self._conn.container_id
        if not container_id:
            return
        if not await self._conn._has_perm("code-in-isolation"):
            send_error(
                self._conn.sock,
                "exec requires code-in-isolation permission",
            )
            return
        await self.stop()
        command = msg.get("command", [])
        if not command:
            send_error(self._conn.sock, "exec_start requires a command list")
            return
        env: list[str] = []
        work_dir = "/home/work"
        user_home = self._conn._user_home
        if user_home is not None:
            env.append(f"HOME={user_home}")
            work_dir = user_home
        ssh_agent_socket = self._conn._ssh_agent_socket
        if ssh_agent_socket is not None:
            env.append(f"SSH_AUTH_SOCK={ssh_agent_socket}")
        # `login` (default raw) selects whether the command runs as a
        # bash login shell (sources ~/.profile, like a terminal) or as
        # raw argv (no shell, for programmatic transports like rsync).
        # klangkc exec sends login=true; klangkc exec --raw and the
        # rsync transport send login=false. See #1041.
        login = bool(msg.get("login", False))
        session = ExecSession(
            container_id,
            self._conn.app_state.podman,
            env=env,
            work_dir=work_dir,
        )
        await session.start(command, login=login)
        self.session = session
        self.task = asyncio.create_task(self.forward_output(session))
        self._conn.app_state.container_registry.record_activity(container_id)

    async def input(self, msg: dict) -> None:
        session = self.session
        if session is None or not session.is_alive:
            return
        raw = base64.b64decode(msg.get("data", ""))
        if len(raw) > MAX_INPUT_SIZE:
            logger.warning(
                "exec_input too large (%d bytes), dropping", len(raw)
            )
            return
        self._conn.app_state.container_registry.record_activity(
            self._conn.container_id
        )
        await session.write(raw)

    async def close_stdin(self) -> None:
        session = self.session
        if session is None:
            return
        await session.close_stdin()

    async def stop_command(self) -> None:
        await self.stop()

    async def claim_and_stop(self) -> None:
        """Drop and stop the current session (idempotent)."""
        session = self.session
        self.session = None
        if session is not None:
            await session.stop()

    async def stop(self) -> None:
        """Cancel the output-forwarding task and stop the session."""
        task = self.task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self.claim_and_stop()

    async def forward_output(self, session: ExecSession) -> None:
        """Forward exec stdout to the client via WebSocket as base64."""
        try:
            async for data in session.output():
                self._conn.sock.send_json(
                    {
                        "type": "exec_output",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
                if self._conn.container_id:
                    self._conn.app_state.container_registry.record_activity(
                        self._conn.container_id
                    )
            # Process exited — send exit code
            self._conn.sock.send_json(
                {
                    "type": "exec_exit",
                    "code": session.returncode
                    if session.returncode is not None
                    else 1,
                }
            )
        except asyncio.CancelledError:
            raise
        except WS_ERRORS as e:
            logger.error("Exec output forwarding error: %s", e)
        finally:
            await self.claim_and_stop()


class TerminalController:
    """Terminal session lifecycle: start, input, window management, stop.

    Owns the current ``TerminalSession`` (``session``), its
    output-forwarding task (``task``), and the client's last-known
    terminal dimensions (``cols``/``rows``).  ``Connection``
    delegates the ``terminal_*`` WebSocket commands here.

    Extracted from ``Connection`` (issue #961) so the terminal
    subsystem can be unit-tested in isolation.  Follows the same
    collaborator pattern as :class:`SshAgentForwarder` and
    :class:`ExecController`.  Shared-terminal state
    (``viewing_shared``, ``handle_join_shared_terminal``) remains
    on ``Connection`` for now; this controller touches it only
    through ``self._conn`` so a later SharedTerminalController
    stage can own it without further changes here.
    """

    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.session: TerminalSession | None = None
        self.task: asyncio.Task | None = None
        self.output_task: asyncio.Task | None = None
        self.cols: int = 80
        self.rows: int = 24

    def _register_browser(self, browser_id: str | None) -> None:
        """Register a browser ID for bridge routing.

        The browser sends its sessionStorage UUID with terminal_start;
        on refresh the same ID re-registers with the new WebSocket.
        The CLI sends "klangkshell" as a sentinel — store it in tmux
        env but don't register it for bridge routing.
        """
        if browser_id and browser_id != "klangkshell":
            self._conn.app_state.container_registry.revoke_browser(
                self._conn.sock
            )
            self._conn.app_state.container_registry.register_browser(
                browser_id, self._conn.workspace_id, self._conn.sock
            )
        self._conn.browser_id = browser_id

    async def _sync_windows(self) -> None:
        """List current tmux windows, sync in-memory state, and send
        the window list and shared terminals to the client."""
        conn = self._conn
        sname = conn.tmux_session_name()
        ws_session = conn.app_state.sockets.get_session(conn.workspace_id)

        windows = await terminal.list_windows(
            conn.container_id, sname, conn.app_state.podman
        )
        conn.sync_terminal_windows(windows)
        conn.sock.send_json({"type": "terminal_windows", "windows": windows})
        # Discover the agent's ``service:service-cmd`` window so it shows
        # up as shared (e.g. a visitor connecting after auto-start fired
        # it) -- the service session is owned by the agent, not any user
        # who has connected (#1133).
        if ws_session:
            await self._sync_service_windows(ws_session)

    def _send_shared_terminals(self) -> None:
        """Send the current shared terminal list to the client."""
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if ws_session:
            terminals = get_shared_terminals(
                ws_session, self._conn.app_state.sockets
            )
            self._conn.sock.send_json(
                {"type": "shared_terminals", "terminals": terminals}
            )

    async def _setup_state_for_workspace(self) -> str:
        """Fetch the workspace's setup_state fresh from the DB (#1033).

        Returns the literal lifecycle value, defaulting to 'complete'
        if the workspace can't be loaded or the lookup fails. A failed
        lookup must NOT crash terminal_start -- defaulting to
        'complete' preserves the historical fire-by-default behaviour
        rather than silently disabling service commands.
        """
        try:
            ws = await model.get_workspace(self._conn.workspace_id)
        except Exception:
            return "complete"
        if ws is None:
            return "complete"
        return ws.get("setup_state") or "complete"

    async def _fire_service_command(self) -> None:
        """Fire the service command in the agent's ``service`` session.

        The post-setup path (#1033): a non-auto-start workspace's service
        command first fires here when a ``terminal_start`` lands after
        setup completes. It runs in the standalone ``service`` session
        owned by the agent identity, not any user's session (#1133).
        Idempotent via the window-exists check, so every terminal_start
        calling it is safe.
        """
        service_command = self._conn._service_command
        if not service_command or not self._conn.container_id:
            return
        # Read setup_state FRESH from the DB -- not a cached connection
        # field. The setup-owner connection caches 'pending' at connect
        # time, but by terminal_start (after setup.sh returns) the DB
        # holds 'complete' (#1033).
        setup_state = await self._setup_state_for_workspace()
        # The agent home is eagerly provisioned at container create
        # (#1157) and persists in the bind-mount volume, so resolving the
        # path here is cheap and correct -- no provisioning needed.
        agent_home = f"/home/{await model.agent_handle()}"
        await terminal.ensure_service_session(
            self._conn.container_id,
            agent_home,
            service_command,
            setup_state=setup_state,
            app_state=self._conn.app_state,
        )

    async def _sync_service_windows(self, ws_session) -> bool:
        """Discover the agent's ``service`` session windows (#1133).

        The service command runs in a standalone ``service`` tmux
        session owned by the agent identity (``AGENT_USER_ID``), not in
        any user's session. ``ws_session.terminal_windows`` is only
        populated when a user connects + syncs, so without this the
        ``service:service-cmd`` window would never appear in the shared
        list. This lists the ``service`` session from tmux and merges the
        result, attributing it to the agent (whose handle is always
        resolvable via ``model.agent_handle()`` -- the agent is never
        "offline" the way the owner could be under the old model).

        Returns ``True`` if the service windows were (re)synced from tmux.
        """
        if not self._conn.container_id:
            return False
        try:
            windows = await terminal.list_windows(
                self._conn.container_id,
                terminal.SERVICE_SESSION,
                self._conn.app_state.podman,
            )
        except (TerminalError, OSError):
            return False  # service session doesn't exist yet
        if not windows:
            return False
        # Best-effort attribution: if the agent handle can't be resolved
        # (e.g. a transient DB issue), skip discovery this round -- it
        # retries on the next connect / list_shared_terminals. Never let
        # discovery break the terminal-start flow.
        try:
            ws_session.agent_handle = await model.agent_handle()
        except Exception:
            return False
        self._merge_service_windows(ws_session, windows)
        return True

    @staticmethod
    def _window_shared(name: str, prev_shared: bool) -> bool:
        """A window is shared if flagged so before, OR it is service-cmd.

        The service-cmd window is shared by definition (#1114): it is
        the workspace's singleton service terminal, owned by the agent
        and joinable by every subscriber.
        """
        return name == SERVICE_CMD_WINDOW or prev_shared

    async def start(self, msg: dict) -> None:

        logger.info(
            "handle_terminal_start: user=%s workspace=%s "
            "container=%s user_home=%s",
            self._conn.user.get("email"),
            self._conn.workspace_id,
            self._conn.container_id,
            self._conn._user_home,
        )
        if not self._conn.container_id:
            logger.info("handle_terminal_start: no container_id, skipping")
            return
        # Debounce: if the last terminal start was very recent, skip.
        # This prevents rapid retry loops when the PTY exits immediately.
        now = time.monotonic()
        if hasattr(self._conn, "_last_terminal_start"):
            if now - self._conn._last_terminal_start < 2.0:
                logger.warning(
                    "Ignoring rapid terminal_start (%.1fs since last)",
                    now - self._conn._last_terminal_start,
                )
                return
        self._conn._last_terminal_start = now
        if self._conn._user_home is None:
            send_error(self._conn.sock, "Handle not set")
            return
        if not await self._conn._has_perm("code-in-isolation"):
            logger.info(
                "Skipping isolated terminal for user=%s "
                "(no code-in-isolation)",
                self._conn.user.get("email"),
            )
            self._conn.sock.send_json({"type": "terminal_started"})
            return
        # Stop existing terminal if any
        await self.stop()
        cols = msg.get("cols", self.cols)
        rows = msg.get("rows", self.rows)
        self.cols = cols
        self.rows = rows

        # The service command no longer lives in any user's session --
        # it runs in the standalone ``service`` session owned by the agent
        # identity (#1133). ``TerminalSession`` is purely the firing user's
        # interactive shell now; the service command is fired separately
        # in ``_start_terminal`` (after the shell session is up) so a
        # post-setup ``terminal_start`` still triggers it (#1033).
        session = TerminalSession(
            self._conn.container_id,
            session_name=self._conn.user["id"],
            user_home=self._conn._user_home,
            user_id=self._conn.user["id"],
            user_handle=self._conn.user.get("handle"),
            ssh_agent_socket=self._conn._ssh_agent_socket,
            podman=self._conn.app_state.podman,
        )

        browser_id = msg.get("browser_id")
        self._register_browser(browser_id)

        # Store session immediately so stop_terminal can clean it up
        # if another terminal_start arrives before this one finishes.
        self.session = session
        conn = self._conn
        ctrl = self

        async def _start_terminal() -> None:
            try:
                logger.info(
                    "_start_terminal: starting for user=%s container=%s",
                    conn.user.get("email"),
                    conn.container_id,
                )
                await asyncio.wait_for(
                    session.start(cols, rows),
                    timeout=30,
                )
                # Fire the service command in the agent's ``service``
                # session. This handles the post-setup case (#1033): a
                # non-auto-start workspace's service command first fires
                # here once setup is complete, not in any user's session.
                # The window-exists check makes it a no-op after the first
                # fire. Done before window sync so discovery picks it up.
                await ctrl._fire_service_command()
                if browser_id:
                    await attach_browser(
                        conn.container_id, browser_id, conn.app_state.podman
                    )
                if not await conn.activate_session(session, cols, rows):
                    return
                conn.sock.send_json({"type": "terminal_started"})
                try:
                    await ctrl._sync_windows()
                except (TerminalError, OSError):
                    logger.exception("_start_terminal: window list failed")
                ctrl._send_shared_terminals()
            except asyncio.CancelledError:
                await session.stop()
                conn.app_state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
                raise
            except (SlowClientError, WebSocketDisconnect):
                await session.stop()
                conn.app_state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
            except Exception as e:
                await session.stop()
                conn.app_state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
                logger.exception("Terminal start failed: %s", e)
                try:
                    send_error(conn.sock, f"Terminal start failed: {e}")
                except WS_ERRORS:
                    pass

        self.task = asyncio.create_task(_start_terminal())

    async def browser_reattach(self, msg: dict) -> None:
        """Re-register the browser ID and update the container's tmux env.

        Sent by the frontend when the terminal gains focus (e.g. tab
        switch) so the container always routes bridge requests to the
        active browser tab.
        """
        browser_id = msg.get("browser_id")
        if not browser_id or not self._conn.container_id:
            return
        self._conn.app_state.container_registry.revoke_browser(self._conn.sock)
        self._conn.app_state.container_registry.register_browser(
            browser_id, self._conn.workspace_id, self._conn.sock
        )
        self._conn.browser_id = browser_id
        logger.info(
            "browser_reattach: browser_id=%s user=%s workspace=%s",
            browser_id,
            self._conn.user.get("email"),
            self._conn.workspace_id,
        )
        await attach_browser(
            self._conn.container_id, browser_id, self._conn.app_state.podman
        )

    async def input(self, msg: dict) -> None:
        t0 = time.monotonic()
        session = self.session
        if session is None or not session.is_alive:
            logger.warning("terminal_input: no session or not alive")
            return
        data = msg.get("data", "")
        if session.read_only:
            # Allow terminal protocol responses (DA queries, color
            # reports) through so tmux can complete initialization.
            # Block user-typed input.
            if not data.startswith("\x1b"):
                return
        if len(data) > MAX_INPUT_SIZE:
            logger.warning(
                "terminal_input too large (%d bytes), dropping", len(data)
            )
            return
        self._conn.app_state.container_registry.record_activity(
            self._conn.container_id
        )
        await session.write(data)
        elapsed = time.monotonic() - t0
        if elapsed > 0.1:  # pragma: no cover
            logger.warning("terminal_input SLOW: %.3fs", elapsed)

    async def resize(self, msg: dict) -> None:
        self.cols = msg.get("cols", 80)
        self.rows = msg.get("rows", 24)
        session = self.session
        if session is None:
            return
        await session.resize(self.cols, self.rows)

    async def stop_command(self) -> None:
        await self.stop()

    def tmux_session_name(self) -> str:
        """Get the tmux session name (user_id).

        Callers must check ``_user_home`` before calling this method.
        """
        return self._conn.user["id"]

    def sync_terminal_windows(self, windows: list[dict]) -> None:
        """Update in-memory terminal_windows from tmux list_windows result."""

        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        user_id = self._conn.user["id"]
        old = ws_session.terminal_windows.get(user_id, [])
        # Match old entries to new tmux entries by window_id (@N) —
        # a tmux-assigned unique identifier that is never reused within
        # a server's lifetime.  This is stable across renames and
        # index reuse.
        old_by_id = {w["id"]: w for w in old if "id" in w}
        # Name-based fallback for matching after container restart where
        # window_ids change but names are restored.
        old_by_name = {w["name"]: w for w in old if "name" in w}
        old_shared = {w["id"] for w in old if w.get("shared") and "id" in w}
        new_entries = []
        for w in windows:
            prev = old_by_id.get(w["id"]) or old_by_name.get(w["name"])
            prev_shared = prev.get("shared", False) if prev else False
            new_entries.append(
                {
                    "id": w["id"],
                    "name": w["name"],
                    "index": w["index"],
                    "shared": self._window_shared(w["name"], prev_shared),
                }
            )
        ws_session.terminal_windows[user_id] = new_entries
        new_shared = {w["id"] for w in new_entries if w.get("shared")}
        # Broadcast if shared set changed (e.g. shared window was closed)
        # or if any shared window was renamed.
        old_shared_names = {
            (w["id"], w["name"]) for w in old if w.get("shared") and "id" in w
        }
        new_shared_names = {
            (w["id"], w["name"]) for w in new_entries if w.get("shared")
        }
        if old_shared != new_shared or old_shared_names != new_shared_names:
            self._conn.broadcast_shared_terminals(ws_session)

    def _merge_service_windows(self, ws_session, windows: list[dict]) -> None:
        """Merge the agent's ``service`` session windows into the map.

        Unlike ``sync_terminal_windows`` (which serves the firing user and
        broadcasts/saves), this is a quiet merge used by
        ``_sync_service_windows`` to make the ``service:service-cmd`` window
        discoverable as shared. Windows are attributed to the agent
        (``AGENT_USER_ID``) and ``service-cmd`` is forced shared (#1133).
        """
        old = ws_session.terminal_windows.get(model.AGENT_USER_ID, [])
        old_by_id = {w["id"]: w for w in old if "id" in w}
        old_by_name = {w["name"]: w for w in old if "name" in w}
        new_entries = []
        for w in windows:
            prev = old_by_id.get(w["id"]) or old_by_name.get(w["name"])
            prev_shared = prev.get("shared", False) if prev else False
            new_entries.append(
                {
                    "id": w["id"],
                    "name": w["name"],
                    "index": w["index"],
                    "shared": self._window_shared(w["name"], prev_shared),
                }
            )
        ws_session.terminal_windows[model.AGENT_USER_ID] = new_entries

    def notify_user_terminal_windows(self, windows: list[dict]) -> None:
        """Send terminal_windows to all connections for this user."""

        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            self._conn.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
            return
        user_id = self._conn.user["id"]
        msg = {"type": "terminal_windows", "windows": windows}
        for sock in list(ws_session.subscribers):
            conn = self._conn.app_state.sockets.connections.get(sock)
            if conn and conn.user.get("id") == user_id:
                sock.send_json(msg)

    async def new_window(self, msg: dict) -> None:
        t0 = time.monotonic()
        if not self._conn.container_id or not self._conn._user_home:
            return

        session_name = self.tmux_session_name()
        name = msg.get("name")
        try:
            windows = await terminal.new_window(
                self._conn.container_id,
                session_name,
                name=name,
                podman=self._conn.app_state.podman,
            )
            logger.info(
                "handle_terminal_new_window: %.3fs",
                time.monotonic() - t0,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
        except Exception as e:
            send_error(self._conn.sock, f"Failed to create window: {e}")

    async def select_window(self, msg: dict) -> None:
        t0 = time.monotonic()
        if not self._conn.container_id or not self._conn._user_home:
            return

        # Use this connection's grouped session so select-window only
        # affects this client, not other connections to the same workspace.
        session = self.session
        session_name = (
            session.tmux_session_name
            if session and session.tmux_session_name
            else self.tmux_session_name()
        )
        # Prefer @N window_id (stable); fall back to index for compat.
        target: int | str = msg.get("window_id") or msg.get("index", 0)
        try:
            await terminal.select_window(
                self._conn.container_id,
                session_name,
                target,
                self._conn.app_state.podman,
            )
            logger.info(
                "handle_terminal_select_window: target=%s %.3fs",
                target,
                time.monotonic() - t0,
            )
        except Exception as e:
            send_error(self._conn.sock, f"Failed to select window: {e}")

    async def close_window(self, msg: dict) -> None:
        if not self._conn.container_id or not self._conn._user_home:
            return

        session_name = self.tmux_session_name()
        index = msg.get("index", 0)
        try:
            windows = await terminal.close_window(
                self._conn.container_id,
                session_name,
                index,
                self._conn.app_state.podman,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
        except Exception as e:
            send_error(self._conn.sock, f"Failed to close window: {e}")

    async def rename_window(self, msg: dict) -> None:
        if not self._conn.container_id or not self._conn._user_home:
            return

        session_name = self.tmux_session_name()
        index = msg.get("index", 0)
        name = msg.get("name", "")
        if not name:
            send_error(self._conn.sock, "Name required")
            return
        try:
            await terminal.rename_window(
                self._conn.container_id,
                session_name,
                index,
                name,
                self._conn.app_state.podman,
            )
            windows = await terminal.list_windows(
                self._conn.container_id,
                session_name,
                self._conn.app_state.podman,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
        except Exception as e:
            send_error(self._conn.sock, f"Failed to rename window: {e}")

    async def list_windows(self) -> None:
        if not self._conn.container_id or not self._conn._user_home:
            return

        # Use this connection's grouped session so the active flag
        # reflects this client's view, not the base session's.
        session = self.session
        session_name = (
            session.tmux_session_name
            if session and session.tmux_session_name
            else self.tmux_session_name()
        )
        try:
            windows = await terminal.list_windows(
                self._conn.container_id,
                session_name,
                self._conn.app_state.podman,
            )
            self._conn.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            send_error(self._conn.sock, f"Failed to list windows: {e}")

    async def claim_and_stop(self) -> None:
        session = self.session
        self.session = None
        if session is not None:
            await session.stop()

    async def activate_session(
        self, session: TerminalSession, cols: int, rows: int
    ) -> bool:
        """Wire up a started session for output forwarding.

        Checks the session is still current, creates the output task,
        resizes to force a tmux redraw, and records activity.
        Returns False if the session was superseded.
        """
        if self.session is not session:
            await session.stop()
            return False
        # Track the output-forwarding task separately from the start task
        # (``self.task``). ``activate_session`` runs *from inside* the
        # ``_start_terminal`` task; overwriting ``self.task`` here would
        # orphan that task -- and if it is then cancelled/torn down while
        # a DB op (e.g. ``_sync_service_windows``) is in flight, the
        # orphaned transaction's connection leaks past event-loop
        # teardown (#1250).
        self.output_task = asyncio.create_task(self.forward_output(session))
        # Resize to force tmux to redraw at the client's terminal size.
        # Without this, reattaching shows a blank screen because tmux
        # skips the redraw when the PTY size matches the default.
        await session.resize(cols, rows)
        self._conn.app_state.container_registry.record_activity(
            self._conn.container_id
        )
        return True

    async def stop(self) -> None:

        was_viewing = self._conn.viewing_shared
        self._conn.viewing_shared = None
        # Cancel both the start task and the output-forwarding task.
        # They are tracked separately (see ``activate_session``) so that
        # tearing the terminal down cancels the start task even after
        # output forwarding took over ``self.output_task`` -- otherwise
        # the start task could be orphaned mid-transaction (#1250).
        for attr in ("task", "output_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, attr, None)
        await self.claim_and_stop()
        # Broadcast viewer change so other users see updated viewer list
        if was_viewing and self._conn.workspace_id:
            ws_session = self._conn.app_state.sockets.get_session(
                self._conn.workspace_id
            )
            if ws_session:
                self._conn.broadcast_shared_terminals(ws_session)
        # Reset debounce so the next explicit start isn't blocked.
        self._conn._last_terminal_start = 0

    async def forward_output(self, session: TerminalSession) -> None:
        """Forward terminal output to the frontend via WebSocket."""
        logger.info(
            "forward_terminal_output: starting for user=%s container=%s",
            self._conn.user.get("email"),
            self._conn.container_id,
        )
        try:
            async for data in session.output():
                self._conn.sock.send_json(
                    {"type": "terminal_output", "data": data}
                )
                if self._conn.container_id:
                    self._conn.app_state.container_registry.record_activity(
                        self._conn.container_id
                    )
            # Stream ended — the tmux session exited (not necessarily the
            # container). Don't send container_stopped; the idle timeout
            # or shutdown button handles actual container death.
            logger.info(
                "forward_terminal_output: stream ended for user=%s",
                self._conn.user.get("email"),
            )
        except asyncio.CancelledError:
            raise  # Normal cleanup, don't send event
        except WS_ERRORS as e:
            logger.error("Terminal output forwarding error: %s", e)
            try:
                send_event(self._conn.sock, "container_stopped")
            except WS_ERRORS:
                pass
        finally:
            await self.claim_and_stop()


class SharedTerminalController:
    """Shared-terminal state and commands: share/unshare/join/list.

    Owns the connection's ``viewing_shared`` marker (which shared
    terminal this connection is currently viewing) and the
    share/unshare/join/list/create/delete command handlers.
    ``Connection`` delegates the ``share_window``/``unshare_window``/
    ``join_shared_terminal``/``list_shared_terminals``/
    ``create_shared_terminal``/``delete_shared_terminal`` WebSocket
    commands here.

    Extracted from ``Connection`` (issue #961) as the final
    collaborator, following :class:`SshAgentForwarder`,
    :class:`ExecController`, and :class:`TerminalController`.
    ``join_shared_terminal`` still wires the joiner's terminal session
    through ``self._conn.terminal`` (and ``stop_terminal``) because
    terminal ownership lives on ``TerminalController``.
    """

    def __init__(self, conn: "Connection") -> None:
        self._conn = conn
        self.viewing_shared: dict | None = None  # {user_id, window_id}

    def find_window(
        self,
        ws_session: WorkspaceSession,
        user_id: str,
        window_id: str,
        *,
        shared: bool = False,
        error_msg: str = "Window not found",
    ) -> dict | None:
        """Look up a terminal window by id, sending an error if absent.

        Returns the matching window dict, or None after sending
        *error_msg* to the socket.  When *shared* is True, only
        windows already marked shared are considered (used when
        joining another user's terminal).
        """
        windows = ws_session.terminal_windows.get(user_id, [])
        match = next(
            (
                w
                for w in windows
                if w.get("id") == window_id and (not shared or w.get("shared"))
            ),
            None,
        )
        if match is None:
            send_error(self._conn.sock, error_msg)
            return None
        return match

    async def share_window(self, msg: dict) -> None:
        """Mark one of the user's own windows as shared."""

        if not self._conn.container_id or not self._conn._user_home:
            return
        if not await self._conn._has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return
        window_id = msg.get("window_id", "")
        if not window_id:
            send_error(self._conn.sock, "Window ID required")
            return
        user_id = self._conn.user["id"]
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        match = self.find_window(ws_session, user_id, window_id)
        if match is None:
            return
        match["shared"] = True
        self.broadcast_shared_terminals(ws_session)

    async def unshare_window(self, msg: dict) -> None:
        """Remove sharing from a window and kick joiners."""

        if not self._conn.container_id or not self._conn._user_home:
            return
        if not await self._conn._has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return

        window_id = msg.get("window_id", "")
        if not window_id:
            send_error(self._conn.sock, "Window ID required")
            return
        user_id = self._conn.user["id"]
        session_name = self._conn.tmux_session_name()
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        match = self.find_window(ws_session, user_id, window_id)
        if match is None:
            return
        match["shared"] = False
        # Kick spectators/collaborators
        try:
            await terminal.kill_joiner_sessions(
                self._conn.container_id,
                session_name,
                self._conn.app_state.podman,
            )
        except Exception:
            logger.debug("Failed to kill joiner sessions", exc_info=True)
        ws_session.broadcast(
            {
                "type": "shared_terminal_deleted",
                "user_id": user_id,
                "window_name": match["name"],
                "window_id": window_id,
            }
        )
        self.broadcast_shared_terminals(ws_session)

    @staticmethod
    async def _select_shared_window(
        container_id: str,
        session: TerminalSession,
        owner_user_id: str,
        window_id: str,
        podman,
    ) -> None:
        """Select the target window in the joiner's tmux session.

        Targets the joiner's grouped session so the active window
        changes for the joiner, not the group owner.  Falls back
        to bare @N if the session isn't ready yet.
        """
        joiner_session = session.tmux_session_name
        if joiner_session:
            try:
                await terminal.tmux_command(
                    container_id,
                    joiner_session,
                    [
                        "select-window",
                        "-t",
                        f"{joiner_session}:{window_id}",
                    ],
                    podman,
                )
            except TerminalError:
                await terminal.select_window(
                    container_id, owner_user_id, window_id, podman
                )
        else:
            await terminal.select_window(
                container_id, owner_user_id, window_id, podman
            )

    async def join_shared_terminal(self, msg: dict) -> None:
        """Join another user's shared window via session group."""

        logger.info(
            "handle_join_shared_terminal: user=%s msg=%s",
            self._conn.user.get("email"),
            msg,
        )
        if not self._conn.container_id or not self._conn._user_home:
            return
        if not await self._conn._has_perm("spectate-on-shared-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return

        owner_user_id = msg.get("user_id", "").strip()
        window_id = msg.get("window_id", "").strip()
        if not owner_user_id or not window_id:
            send_error(self._conn.sock, "user_id and window_id required")
            return

        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        match = self.find_window(
            ws_session,
            owner_user_id,
            window_id,
            shared=True,
            error_msg="Shared terminal not found",
        )
        if match is None:
            return
        window_name = match["name"]

        read_only = not (
            await self._conn._has_perm("code-in-shared-terminals")
            or await self._conn._has_perm("share-terminals")
        )

        await self._conn.stop_terminal()
        self.viewing_shared = {
            "user_id": owner_user_id,
            "window_id": window_id,
        }
        # The service window (service-cmd) lives in the standalone
        # ``service`` tmux session (#1158), not a session named after the
        # agent's user_id. Route agent-owned joins to that session so the
        # grouped attach actually finds a target. Other windows keep joining
        # the owner's user-named session as before (#1159).
        join_target = (
            SERVICE_SESSION
            if owner_user_id == model.AGENT_USER_ID
            else owner_user_id
        )
        session = TerminalSession(
            self._conn.container_id,
            session_name=self._conn.user["id"],
            user_home=self._conn._user_home,
            join_session=join_target,
            read_only=read_only,
            user_id=self._conn.user["id"],
            user_handle=self._conn.user.get("handle"),
            podman=self._conn.app_state.podman,
        )
        self._conn.terminal_session = session
        conn = self._conn

        cols = self._conn._terminal_cols
        rows = self._conn._terminal_rows

        async def _start_shared() -> None:
            try:
                await session.start(cols, rows)
                await self._select_shared_window(
                    conn.container_id,
                    session,
                    join_target,
                    window_id,
                    conn.app_state.podman,
                )
                if not await conn.activate_session(session, cols, rows):
                    return
                conn.sock.send_json(
                    {
                        "type": "terminal_started",
                        "shared_user_id": owner_user_id,
                        "shared_window": window_name,
                        "readOnly": read_only,
                    }
                )
                ws_sess = conn.app_state.sockets.get_session(conn.workspace_id)
                if ws_sess:
                    conn.broadcast_shared_terminals(ws_sess)
            except asyncio.CancelledError:  # pragma: no cover
                await session.stop()
                raise
            except Exception as e:
                await session.stop()
                logger.exception("Shared terminal join failed: %s", e)
                send_error(
                    conn.sock,
                    f"Failed to join shared terminal: {e}",
                )

        self._conn.terminal_task = asyncio.create_task(_start_shared())

    async def list_shared_terminals(self) -> None:

        if not self._conn.workspace_id:
            return
        if not await self._conn._has_perm("spectate-on-shared-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            self._conn.sock.send_json(
                {"type": "shared_terminals", "terminals": []}
            )
            return
        # Discover the agent's ``service:service-cmd`` window in case it
        # was fired (e.g. auto-start) before anyone connected to discover
        # it (#1133).
        await self._conn.terminal._sync_service_windows(ws_session)
        terminals = get_shared_terminals(
            ws_session, self._conn.app_state.sockets
        )
        self._conn.sock.send_json(
            {"type": "shared_terminals", "terminals": terminals}
        )

    def broadcast_shared_terminals(self, ws_session) -> None:
        """Broadcast the current shared terminal list to all subscribers."""
        terminals = get_shared_terminals(
            ws_session, self._conn.app_state.sockets
        )
        ws_session.broadcast(
            {"type": "shared_terminals", "terminals": terminals}
        )

    # Keep old handler name for backwards compat with existing E2E tests
    async def create_shared_terminal(self, msg: dict) -> None:
        """Create a new shared terminal (legacy API — creates a new window
        and marks it shared)."""

        if not self._conn.container_id or not self._conn._user_home:
            return
        if not await self._conn._has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return
        name = msg.get("name", "").strip()
        if not name:
            send_error(self._conn.sock, "Name required")
            return
        session_name = self._conn.tmux_session_name()
        try:
            windows = await terminal.new_window(
                self._conn.container_id,
                session_name,
                name=name,
                podman=self._conn.app_state.podman,
            )
        except Exception as e:
            send_error(
                self._conn.sock, f"Failed to create shared terminal: {e}"
            )
            return
        # Sync with tmux to get proper window_id, then mark the new
        # window as shared.
        self._conn.sync_terminal_windows(windows)
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        user_id = self._conn.user["id"]
        for w in ws_session.terminal_windows.get(user_id, []):
            if w["name"] == name:
                w["shared"] = True
                break
        self.broadcast_shared_terminals(ws_session)

    async def delete_shared_terminal(self, msg: dict) -> None:
        """Delete a shared terminal (legacy API — unshares and closes
        the window)."""

        if not self._conn.container_id:
            return
        if not await self._conn._has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return

        owner_user_id = msg.get("user_id", "").strip()
        window_id = msg.get("window_id", "").strip()
        if not owner_user_id or not window_id:
            send_error(self._conn.sock, "user_id and window_id required")
            return
        # Only the terminal's owner — or the workspace owner — may
        # delete it. The owner_user_id comes from the client and must
        # not be trusted blindly, otherwise any collaborator with the
        # share-terminals permission could close other users' windows.
        if owner_user_id != self._conn.user["id"]:
            workspace = await model.get_workspace_by_id(
                self._conn.workspace_id
            )
            if (
                workspace is None
                or workspace["user_id"] != self._conn.user["id"]
            ):
                send_error(self._conn.sock, "Permission denied")
                return
        ws_session = self._conn.app_state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        match = self.find_window(
            ws_session,
            owner_user_id,
            window_id,
            error_msg="Terminal not found",
        )
        if match is None:
            return
        window_name = match["name"]
        try:
            await terminal.kill_joiner_sessions(
                self._conn.container_id,
                owner_user_id,
                self._conn.app_state.podman,
            )
            await terminal.close_window(
                self._conn.container_id,
                owner_user_id,
                window_id,
                self._conn.app_state.podman,
            )
        except Exception as e:
            send_error(
                self._conn.sock, f"Failed to delete shared terminal: {e}"
            )
            return
        owner_windows = ws_session.terminal_windows.get(owner_user_id, [])
        owner_windows[:] = [
            w for w in owner_windows if w.get("id") != window_id
        ]
        ws_session.broadcast(
            {
                "type": "shared_terminal_deleted",
                "user_id": owner_user_id,
                "window_name": window_name,
                "window_id": window_id,
            }
        )
        self.broadcast_shared_terminals(ws_session)

    # Legacy error handler kept for coverage
    async def handle_list_error(
        self, e: Exception
    ) -> None:  # pragma: no cover
        send_error(self._conn.sock, f"Failed to list shared terminals: {e}")
