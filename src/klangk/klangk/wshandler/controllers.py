"""Collaborator controllers: SSH agent, exec, terminal, shared terminal."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import TYPE_CHECKING

from fastapi import WebSocketDisconnect

from .. import model
from ..container.spec import SHARED_HOME
from ..exceptions import ContainerGoneError, TerminalError
from ..podman import ExecSession
from ..terminal import (
    TerminalSession,
    SERVICE_CMD_WINDOW,
    SERVICE_SESSION,
)
from .safe_websocket import SlowClientError, WS_ERRORS
from .session import get_shared_terminals
from .support import MAX_INPUT_SIZE, send_error, send_event

if TYPE_CHECKING:
    from .connection import Connection
    from .session import WorkspaceSession

logger = logging.getLogger(__name__)

# SSH-agent relay readiness (#2535): ``ssh_agent_started`` must mean the
# container-side socat is *listening*, not merely that the local
# ``podman exec`` client was spawned. ``create_subprocess_exec`` returns
# as soon as the client process exists — the container-side socat appears
# some conmon/crun latency later — so a client that fires a follow-up exec
# immediately (the e2e suite's ``pgrep -c socat``, or any scripted
# ``ssh-add``) can land before the relay is visible, and under load that
# window widens. socat's stderr is DEVNULL (no startup message to poll
# for), so the readiness signal is the bound socket file: ``unlink-early``
# removes any stale file and ``bind()`` creates this one, so its existence
# means a live listener.
SSH_AGENT_READY_TIMEOUT = 10.0
SSH_AGENT_READY_POLL = 0.1

# Read-only ("spectate") terminal-input whitelist (issue #1716).
#
# A read-only joiner may only send the terminal-protocol RESPONSES that
# tmux needs to complete client initialization: it queries the joining
# terminal for its capabilities/colors and expects replies.  Anything
# else — user typing AND arbitrary escape sequences such as OSC 52
# clipboard read/write, title sets, size reports, or DCS passthrough —
# is dropped.  The previous gate ("starts with ESC") let any ESC byte
# through, so a spectator could inject OSC 52 and exfiltrate the
# owner's clipboard.
#
# The whitelist mirrors what the terminal emulator on the read-only
# path (flterm via ghostty_terminal.dart) actually replies with during
# tmux's attach handshake: device attributes (DA1/DA2/DA3),
# cursor-position report,
# color reports (OSC 10/11/12 default fg/bg/cursor + OSC 4 palette),
# XTVERSION, and XTGETTCAP.  Each of these reply types is matched with
# a tight, bounded pattern; OSC 52 (clipboard) and every other OSC/
# CSI/DCS sequence fall through to the drop.
#
# Atomicity assumption: each terminal_input WebSocket message contains
# one or more WHOLE responses, never a fragment of one.  This holds
# because the frontend forwards each onOutput/onData callback as a
# single terminal_input (ws_client.sendTerminalInput) and VT parsers
# emit a generated response as a complete byte string in one callback.
# The match below is a fullmatch — a mid-response split across two
# messages would be dropped.  If a future terminal library ever
# fragments responses, the fix is a per-connection reassembly buffer
# here, not loosening the gate.
#
# ST (string terminator) is ESC '\\' or BEL ('\x07').
_ST = r"(?:\x1b\\|\x07)"
# DA1/DA2/DA3 responses: CSI [?>=] <params> c
_READ_ONLY_DA = r"\x1b\[[?>=][\d;]*c"
# DSR cursor-position report: CSI <row> ; <col> R
_READ_ONLY_DSR_CURSOR = r"\x1b\[\d+;\d+R"
# OSC color reports (default fg/bg/cursor via 10/11/12, palette via 4;<n>).
# The value may be an X11 `rgb:R/G/B` (1-4 hex/channel), an xterm-style
# `#rrggbb`/`#rrrrggggbbbb`, or an intensity `rgbi:r/g/b`.
_READ_ONLY_COLOR_VALUE = (
    r"rgb:[0-9A-Fa-f]{1,4}/[0-9A-Fa-f]{1,4}/[0-9A-Fa-f]{1,4}"
    r"|#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{6})?"
    r"|rgbi:\d+(?:\.\d+)?/\d+(?:\.\d+)?/\d+(?:\.\d+)?"
)
_READ_ONLY_OSC_COLOR = (
    r"\x1b\](?:4;\d+|10|11|12);(?:" + _READ_ONLY_COLOR_VALUE + r")" + _ST
)
# XTVERSION response: DCS > | <name SP version> ST (printable payload
# only — no nested escape possible since ESC is excluded).
_READ_ONLY_XTVERSION = r"\x1bP>\|[\x20-\x7e]*" + _ST
# XTGETTCAP response: DCS (0|1) + r <hex>=<hex>(;<hex>=<hex>)* ST.
# 1+r is success, 0+r is failure; payload is hex + '=' + ';'.
_READ_ONLY_XTGETTCAP = r"\x1bP[01]\+r[0-9A-Fa-f=;]*" + _ST
_READ_ONLY_INPUT = re.compile(
    r"(?:"
    + r"|".join(
        (
            _READ_ONLY_DA,
            _READ_ONLY_DSR_CURSOR,
            _READ_ONLY_OSC_COLOR,
            _READ_ONLY_XTVERSION,
            _READ_ONLY_XTGETTCAP,
        )
    )
    + r")+"
)


def is_allowed_read_only_input(data: str) -> bool:
    """True only for the terminal-protocol responses a read-only client
    may legitimately send during tmux initialization.

    Strict whitelist (issue #1716): user input and arbitrary escape
    sequences — notably OSC 52 clipboard read/write — are rejected.
    The whole payload must be one or more whitelisted responses; any
    extra byte (typed text, a different OSC/CSI/DCS) fails the match
    and the message is dropped.
    """
    return _READ_ONLY_INPUT.fullmatch(data) is not None


def ssh_agent_socket_path(user_id: str) -> str:
    """Deterministic in-container path for a user's forwarded SSH-agent socket.

    ``/tmp/klangk-ssh-agent-<user_id>.sock`` is the path the socat relay
    (:meth:`SshAgentForwarder.start`) binds when a client opts into agent
    forwarding. It is stable across reconnections and independent of *when*
    the relay starts — which is what lets every interactive terminal wire
    ``SSH_AUTH_SOCK`` here at creation time and go live the moment a relay
    binds this path, instead of only working when the relay happened to be
    up first (#2001).
    """
    return f"/tmp/klangk-ssh-agent-{user_id}.sock"


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
        container_id = self._conn.container_id
        if not container_id:
            send_error(
                self._conn.sock, "No container for SSH agent forwarding"
            )
            return
        # Clean up any existing agent relay.
        await self.stop()
        user_id = self._conn.user["id"]
        sock_path = ssh_agent_socket_path(user_id)
        # Reap any competing relay a prior connection left behind.
        #
        # Each ``klangk shell -A`` is a fresh Connection with its own
        # SshAgentForwarder; the previous connection's stop() ran against
        # *its* proc (unknown to us), and if it didn't fully tear down — a
        # crashed/disconnected client, or a reconnect before the old relay
        # finished dying — its socat is still listening on this path. Two
        # socats with ``unlink-early`` on the same socket unlinks each
        # other's accept-time socket file, so ``ssh-add`` sees the path
        # flicker in and out ("No such file or directory") (#2001).
        #
        # ``pkill -f`` matches the full argv; scoped to this user's exact
        # socket string it only reaps *this* user's stale socat relays — never
        # another user's relay or an unrelated process. ``pkill`` exits 1 when
        # nothing matches (first connect on a clean container);
        # ``exec_container`` runs ``check=False`` (``Podman.run`` only raises on
        # non-zero when ``check`` is set), so that no-match exit is swallowed
        # and the bind proceeds.
        #
        # Isolation caveat: the socket is bound ``mode=600``, which isolates
        # it across OS users — but in a shared workspace all members run as
        # the same in-container uid (``klangk``), so a collaborator can reach
        # another member's forwarded-agent socket and use its identities. That
        # exposure predates #2001 (the relay always bound this path); a
        # per-user private socket directory would close it.
        await self._conn.app.state.podman.exec_container(
            container_id,
            ["pkill", "-f", f"UNIX-LISTEN:{sock_path}"],
        )
        # Remove stale socket if it exists from a previous session.
        await self._conn.app.state.podman.exec_container(
            container_id, ["rm", "-f", sock_path]
        )
        # Start socat: listen on the Unix socket, relay to stdin/stdout.
        proc = await asyncio.create_subprocess_exec(
            self._conn.app.state.podman.bin,
            "exec",
            "-i",
            container_id,
            "socat",
            f"UNIX-LISTEN:{sock_path},mode=600,unlink-early,fork",
            "STDIO",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.proc = proc
        self.socket = sock_path
        self.task = asyncio.create_task(self.forward_output())
        # Gate the "started" event on the relay actually listening
        # (#2535). A timeout is not fatal — the socat exec may still be
        # working its way through the runtime (headroom under load), so
        # the event is emitted anyway and the failure mode stays the old
        # one (a too-early client sees an inert socket) instead of a new
        # one (clients waiting on an event that never comes).
        if not await self._wait_until_listening(container_id, sock_path):
            logger.warning(
                "SSH agent socket %s not bound after %.0fs; "
                "continuing anyway (relay may still be starting)",
                sock_path,
                SSH_AGENT_READY_TIMEOUT,
            )
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

    async def _wait_until_listening(
        self, container_id: str, sock_path: str
    ) -> bool:
        """True once socat has bound *sock_path*; False on timeout or error.

        Polls ``test -S`` inside the container until the socket file exists
        (see the constants above for why the file is the readiness
        signal). A podman launch failure returns False immediately — the
        relay is dead either way, and ``stop()`` (on disconnect or the
        next start) handles teardown.
        """
        deadline = time.monotonic() + SSH_AGENT_READY_TIMEOUT
        while True:
            try:
                (
                    rc,
                    _out,
                    _err,
                ) = await self._conn.app.state.podman.exec_container(
                    container_id, ["test", "-S", sock_path], timeout=5.0
                )
            except Exception:
                return False
            if rc == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(SSH_AGENT_READY_POLL)

    async def forward_output(self) -> None:
        """Read from socat stdout and send to the CLI as ssh_agent_response."""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    break
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
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        raw = msg.get("data", "")
        if raw:
            decoded = base64.b64decode(raw)
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
        # Reap the in-container socat directly. ``proc.kill()`` above sends
        # SIGKILL to the local ``podman exec`` handle, which does NOT reliably
        # terminate the container-side socat it spawned — so the listener can
        # survive as an orphan. With ``unlink-early`` that orphan then races
        # the next connection's bind (and ``rm -f`` below leaves a live
        # listener with no socket file). Killing by the deterministic path
        # (same ``pkill -f`` ``start()`` uses) guarantees the container-side
        # process is gone before we remove the socket (#2001).
        if self.socket and container_id:
            # Best-effort teardown: a failure here (container already gone,
            # podman subprocess can't launch) must not break disconnect.
            # ``exec_container`` runs ``check=False`` so non-zero exits don't
            # raise; this catches the remaining launch/IO failures.
            try:
                await self._conn.app.state.podman.exec_container(
                    container_id,
                    ["pkill", "-f", f"UNIX-LISTEN:{self.socket}"],
                )
            except Exception as e:  # best-effort reap
                logger.debug("Failed to reap SSH agent socat: %s", e)
            try:
                await self._conn.app.state.podman.exec_container(
                    container_id,
                    ["rm", "-f", self.socket],
                )
            except Exception as e:
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
        # #2706/#2712: the one-shot exec channel is a programmatic
        # bulk-read/write path — and it is also what ``klangk sync``
        # rides on (its rsync transport is ``klangk exec --raw``), so
        # this gate is the enforcement point for both. It uses the
        # dedicated ``exec-and-sync`` permission, separate from
        # ``code-in-isolation`` (isolated terminals), so an admin can
        # keep terminals available while stopping one-shot command
        # execution and bulk sync — revoking ``exec-and-sync`` blocks
        # both sync directions along with ``klangk exec``.
        if not await self._conn.has_perm("exec-and-sync"):
            send_error(
                self._conn.sock,
                "exec requires the exec-and-sync permission",
            )
            return
        await self.stop()
        command = msg.get("command", [])
        if not command:
            send_error(self._conn.sock, "exec_start requires a command list")
            return
        env: list[str] = []
        work_dir = SHARED_HOME
        user_home = self._conn._user_home
        if user_home is not None:
            env.append(f"HOME={user_home}")
            work_dir = user_home
        # Wire SSH_AUTH_SOCK to the deterministic per-user path on every
        # exec, not only when a relay is already active (#2001): exec
        # sessions are short-lived one-shot commands and have no persistent
        # base session, but the same creation-path uniformity applies — the
        # var is inert until a relay binds this path. The forwarder's
        # ``ssh_agent.socket`` (when the relay is up) is the same path, so
        # this strictly generalizes the old relay-gated wiring.
        env.append(
            f"SSH_AUTH_SOCK={ssh_agent_socket_path(self._conn.user['id'])}"
        )
        # `login` (default raw) selects whether the command runs as a
        # bash login shell (sources ~/.profile, like a terminal) or as
        # raw argv (no shell, for programmatic transports like rsync).
        # klangk exec sends login=true; klangk exec --raw and the
        # rsync transport send login=false. See #1041.
        login = bool(msg.get("login", False))
        session = ExecSession(
            container_id,
            self._conn.app.state.podman,
            env=env,
            work_dir=work_dir,
        )
        await session.start(command, login=login)
        self.session = session
        self.task = asyncio.create_task(self.forward_output(session))
        self._conn.app.state.container_registry.record_activity(container_id)

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
        self._conn.app.state.container_registry.record_activity(
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
                    self._conn.app.state.container_registry.record_activity(
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

        #2710: when the deploy disabled the browser delegate
        (KLANGKD_BROWSER_DELEGATE_ENABLED=false, read live so a SIGHUP
        reload applies), nothing is registered and any pre-disable
        registration for this socket is revoked — the bridge endpoints
        403 regardless, but this keeps the routing table honest.
        """
        if not self._conn.app.state.settings.browser_delegate_enabled:
            self._conn.app.state.container_registry.revoke_browser(
                self._conn.sock
            )
            self._conn.browser_id = None
            return
        if browser_id and browser_id != "klangkshell":
            self._conn.app.state.container_registry.revoke_browser(
                self._conn.sock
            )
            self._conn.app.state.container_registry.register_browser(
                browser_id, self._conn.workspace_id, self._conn.sock
            )
        self._conn.browser_id = browser_id

    async def _sync_windows(self) -> None:
        """List current tmux windows, sync in-memory state, and send
        the window list and shared terminals to the client."""
        conn = self._conn
        sname = conn.tmux_session_name()
        ws_session = conn.app.state.sockets.get_session(conn.workspace_id)

        windows = await conn.app.state.terminal.list_windows(
            conn.container_id, sname
        )
        conn.sync_terminal_windows(windows)
        conn.sock.send_json({"type": "terminal_windows", "windows": windows})
        # Discover the agent's ``service:service-cmd`` window so it shows
        # up as shared (e.g. a visitor connecting after auto-start fired
        # it) -- the service session is owned by the agent, not any user
        # who has connected (#1133).
        if ws_session:
            await self.sync_service_windows(ws_session)

    def _send_shared_terminals(self) -> None:
        """Send the current shared terminal list to the client."""
        ws_session = self._conn.app.state.sockets.get_session(
            self._conn.workspace_id
        )
        if ws_session:
            terminals = get_shared_terminals(
                ws_session, self._conn.app.state.sockets
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
            ws = await self._conn.app.state.model.workspaces.get_workspace(
                self._conn.workspace_id
            )
        except Exception:
            return "complete"
        if ws is None:
            return "complete"
        return ws.get("setup_state") or "complete"

    async def fire_service_command(self) -> None:
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
        # The service session's HOME is pinned to the constant shared
        # home (/home/klangk, under both layouts) inside
        # ensure_service_session (#2717) -- nothing to resolve here.
        await self._conn.app.state.terminal.ensure_service_session(
            self._conn.container_id,
            service_command,
            setup_state=setup_state,
        )

    async def sync_service_windows(self, ws_session) -> bool:
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
            windows = await self._conn.app.state.terminal.list_windows(
                self._conn.container_id,
                SERVICE_SESSION,
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
            ws_session.agent_handle = (
                await self._conn.app.state.model.users.agent_handle()
            )
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
        if not await self._conn.has_perm("code-in-isolation"):
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
        logger.info(
            "terminal_start: cols=%s rows=%s (0 may make tmux/podman attach fail)",
            cols,
            rows,
        )
        self.cols = cols
        self.rows = rows

        # The service command no longer lives in any user's session --
        # it runs in the standalone ``service`` session owned by the agent
        # identity (#1133). ``TerminalSession`` is purely the firing user's
        # interactive shell now; the service command is fired separately
        # in ``_start_terminal`` (after the shell session is up) so a
        # post-setup ``terminal_start`` still triggers it (#1033).
        ws = self._conn.workspace
        # Wire SSH_AUTH_SOCK to the deterministic per-user agent-socket path
        # on EVERY terminal, whether or not a relay is active yet (#2001).
        # The var is inert until a relay binds this path (which only happens
        # when the client opts into forwarding), so pre-pointing it here means
        # a base session created before the relay starts — the TUI path opens
        # the tmux session, then spawns `klangk shell -A` which starts the
        # relay — already has it set, and goes live the moment the relay binds.
        # A reconnect to an existing session inherits it from that first
        # creation. No per-creation-path special-casing.
        session = TerminalSession(
            self._conn.container_id,
            session_name=self._conn.user["id"],
            user_home=self._conn._user_home,
            user_id=self._conn.user["id"],
            user_handle=self._conn.user.get("handle"),
            ssh_agent_socket=ssh_agent_socket_path(self._conn.user["id"]),
            terminal=self._conn.app.state.terminal,
            workspace_name=ws.get("name") if ws else None,
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
                await ctrl.fire_service_command()
                # Attach the browser ID (if any) into the container's tmux
                # env. Reads conn.browser_id — the post-_register_browser
                # value — so a deploy that disabled the delegate (#2710)
                # never advertises an ID to the container.
                if conn.browser_id:
                    await conn.app.state.terminal.attach_browser(
                        conn.container_id, conn.browser_id
                    )
                if not await conn.activate_session(session):
                    return
                conn.sock.send_json({"type": "terminal_started"})
                try:
                    await ctrl._sync_windows()
                except ContainerGoneError as e:
                    # The container was recycled between session start and
                    # the window sync — an expected race, not a tmux
                    # failure. Don't traceback it: log a clean warning,
                    # stop the now-dead session, and tell the client so it
                    # can re-trigger workspace open against the fresh
                    # container (#2178).
                    logger.warning(
                        "_start_terminal: container gone before window "
                        "sync (user=%s container=%s): %s",
                        conn.user.get("email"),
                        conn.container_id,
                        e,
                    )
                    await session.stop()
                    conn.app.state.container_registry.revoke_browser(conn.sock)
                    conn.browser_id = None
                    try:
                        send_error(
                            conn.sock,
                            "Container was recycled; reopening terminal",
                        )
                    except WS_ERRORS:
                        pass
                    return
                except (TerminalError, OSError):
                    logger.exception("_start_terminal: window list failed")
                ctrl._send_shared_terminals()
            except asyncio.CancelledError:
                await session.stop()
                conn.app.state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
                raise
            except (SlowClientError, WebSocketDisconnect):
                await session.stop()
                conn.app.state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
            except Exception as e:
                await session.stop()
                conn.app.state.container_registry.revoke_browser(conn.sock)
                conn.browser_id = None
                logger.exception("Terminal start failed: %s", e)
                try:
                    send_error(conn.sock, "Terminal start failed")
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
        if not self._conn.app.state.settings.browser_delegate_enabled:
            # #2710: the deploy disabled the browser-delegate bridge —
            # re-attach would (re)register the tab for bridge routing and
            # re-advertise the ID into the container env; instead drop any
            # pre-disable registration so the routing table goes quiet.
            # Checked before the browser_id/container guards so a disabled
            # deploy also clears a stale registration here.
            self._conn.app.state.container_registry.revoke_browser(
                self._conn.sock
            )
            self._conn.browser_id = None
            return
        if not browser_id or not self._conn.container_id:
            return
        self._conn.app.state.container_registry.revoke_browser(self._conn.sock)
        self._conn.app.state.container_registry.register_browser(
            browser_id, self._conn.workspace_id, self._conn.sock
        )
        self._conn.browser_id = browser_id
        logger.info(
            "browser_reattach: browser_id=%s user=%s workspace=%s",
            browser_id,
            self._conn.user.get("email"),
            self._conn.workspace_id,
        )
        await self._conn.app.state.terminal.attach_browser(
            self._conn.container_id, browser_id
        )

    async def input(self, msg: dict) -> None:
        t0 = time.monotonic()
        session = self.session
        if session is None or not session.is_alive:
            logger.warning("terminal_input: no session or not alive")
            return
        data = msg.get("data", "")
        if len(data) > MAX_INPUT_SIZE:
            logger.warning(
                "terminal_input too large (%d bytes), dropping", len(data)
            )
            return
        if session.read_only and not is_allowed_read_only_input(data):
            # Read-only spectators may only send the terminal-protocol
            # responses tmux needs to complete initialization (DA,
            # color, cursor-position, XTVERSION, XTGETTCAP reports).
            # User typing and arbitrary escape sequences — notably OSC
            # 52 clipboard read/write — are dropped (#1716).
            return
        self._conn.app.state.container_registry.record_activity(
            self._conn.container_id
        )
        await session.write(data)
        elapsed = time.monotonic() - t0
        if elapsed > 0.1:
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

        ws_session = self._conn.app.state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        user_id = self._conn.user["id"]
        # Shared merge and delta detection live in one place
        # (WorkspaceSession.apply_window_list, #2633 CI race + #2651):
        # entries are matched by tmux window id
        # (unique and never reused within a server's lifetime — stable
        # across renames and index reuse, #2192), shared flags carry
        # over, service-cmd stays shared. The window-watcher sync uses
        # the same method, so whichever path applies a shared-window
        # rename first is also the one that broadcasts it — a watcher
        # re-sync landing between the rename and this handler's
        # list_windows used to erase the delta and the coadmin's
        # shared_terminals frame was never sent (#2651).
        if ws_session.apply_window_list(user_id, windows):
            ws_session.broadcast_shared_terminals()

    def _merge_service_windows(self, ws_session, windows: list[dict]) -> None:
        """Merge the agent's ``service`` session windows into the map.

        Unlike ``sync_terminal_windows`` (which serves the firing user and
        broadcasts/saves), this is a quiet merge used by
        ``sync_service_windows`` to make the ``service:service-cmd`` window
        discoverable as shared. Windows are attributed to the agent
        (``AGENT_USER_ID``) and ``service-cmd`` is forced shared (#1133).
        """
        old = ws_session.terminal_windows.get(model.AGENT_USER_ID, [])
        old_by_id = {w["id"]: w for w in old if "id" in w}
        new_entries = []
        for w in windows:
            prev = old_by_id.get(w["id"])
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

        ws_session = self._conn.app.state.sockets.get_session(
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
            conn = self._conn.app.state.sockets.connections.get(sock)
            if conn and conn.user.get("id") == user_id:
                sock.send_json(msg)
        # Keep the control-mode watcher's diff baseline current so it doesn't
        # re-broadcast the state we just pushed (a duplicate debounced frame
        # raced the e2e terminal-tabs tests, #2174).
        ws_session._last_windows[user_id] = windows

    def _notify_terminals_changed(
        self, windows: list[dict] | None = None
    ) -> None:
        """Nudge all of this user's status connections to refresh terminals.

        Carries ``windows`` so push-fed consumers (e.g. the TUI detail screen)
        can update without re-enumerating -- complementing
        ``notify_user_terminal_windows`` (which pushes the full list to
        workspace-WS subscribers for the Flutter UI) (#1894).
        """
        ws_id = self._conn.workspace_id
        if not ws_id:
            return
        self._conn.app.state.sockets.notify_user_terminals_changed(
            self._conn.user["id"], ws_id, windows
        )

    async def new_window(self, msg: dict) -> None:
        t0 = time.monotonic()
        if not self._conn.container_id or not self._conn._user_home:
            return

        session_name = self.tmux_session_name()
        name = msg.get("name")
        try:
            windows = await self._conn.app.state.terminal.new_window(
                self._conn.container_id,
                session_name,
                name=name,
            )
            logger.info(
                "handle_terminal_new_window: %.3fs",
                time.monotonic() - t0,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
            self._notify_terminals_changed(windows)
        except Exception as e:
            logger.exception("Failed to create window: %s", e)
            send_error(self._conn.sock, "Failed to create window")

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
            await self._conn.app.state.terminal.select_window(
                self._conn.container_id,
                session_name,
                target,
            )
            logger.info(
                "handle_terminal_select_window: target=%s %.3fs",
                target,
                time.monotonic() - t0,
            )
        except Exception as e:
            logger.exception("Failed to select window: %s", e)
            send_error(self._conn.sock, "Failed to select window")

    async def close_window(self, msg: dict) -> None:
        if not self._conn.container_id or not self._conn._user_home:
            return

        session_name = self.tmux_session_name()
        # Prefer @N window_id (stable); fall back to index for compat (#1965).
        target: int | str = msg.get("window_id") or msg.get("index", 0)
        try:
            terminal = self._conn.app.state.terminal
            windows = await terminal.list_windows(
                self._conn.container_id, session_name
            )
            if len(windows) <= 1:
                send_error(
                    self._conn.sock,
                    "Cannot close the last terminal window.",
                )
                return
            windows = await terminal.close_window(
                self._conn.container_id,
                session_name,
                target,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
            self._notify_terminals_changed(windows)
        except Exception as e:
            logger.exception("Failed to close window: %s", e)
            send_error(self._conn.sock, "Failed to close window")

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
            await self._conn.app.state.terminal.rename_window(
                self._conn.container_id,
                session_name,
                index,
                name,
            )
            windows = await self._conn.app.state.terminal.list_windows(
                self._conn.container_id,
                session_name,
            )
            self.sync_terminal_windows(windows)
            self.notify_user_terminal_windows(windows)
            self._notify_terminals_changed(windows)
        except Exception as e:
            logger.exception("Failed to rename window: %s", e)
            send_error(self._conn.sock, "Failed to rename window")

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
            windows = await self._conn.app.state.terminal.list_windows(
                self._conn.container_id,
                session_name,
            )
            self._conn.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            logger.exception("Failed to list windows: %s", e)
            send_error(self._conn.sock, "Failed to list windows")

    async def claim_and_stop(self) -> None:
        session = self.session
        self.session = None
        if session is not None:
            await session.stop()

    async def activate_session(self, session: TerminalSession) -> bool:
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
        # a DB op (e.g. ``sync_service_windows``) is in flight, the
        # orphaned transaction's connection leaks past event-loop
        # teardown (#1250).
        self.output_task = asyncio.create_task(self.forward_output(session))
        # Resize to force tmux to redraw at the client's terminal size.
        # Without this, reattaching shows a blank screen because tmux
        # skips the redraw when the PTY size matches the default.
        #
        # Resize to the controller's CURRENT dims (``self.cols``/
        # ``self.rows``), not the dims captured when terminal_start was
        # handled (#2671). The client can shrink between the two -- e.g.
        # the tab strip appearing fires a terminal_resize before the
        # attach exec's PTY exists, and ``TerminalSession.resize`` drops
        # it while ``_shell`` is None. Forcing the stale start-time size
        # then makes tmux repaint TALLER than the client grid (e.g. 29
        # rows into 27), and the extra line-feeds scroll the prompt off
        # the top of the (alternate-screen) viewport -- the "bash prompt
        # invisible until Enter" first-load bug. Resizing to the latest
        # client size makes the forced redraw match the real grid.
        await session.resize(self.cols, self.rows)
        self._conn.app.state.container_registry.record_activity(
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
            ws_session = self._conn.app.state.sockets.get_session(
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
                    self._conn.app.state.container_registry.record_activity(
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
        if not await self._conn.has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return
        window_id = msg.get("window_id", "")
        if not window_id:
            send_error(self._conn.sock, "Window ID required")
            return
        user_id = self._conn.user["id"]
        ws_session = self._conn.app.state.sockets.get_session(
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
        """Remove sharing from a window and kick joiners.

        Only ever operates on the caller's own windows, so — unlike
        ``share_window`` — it needs no ``share-terminals`` permission:
        unsharing *reduces* exposure, and gating it would strand a
        member whose permission was revoked after sharing (#2875).
        """

        if not self._conn.container_id or not self._conn._user_home:
            return

        window_id = msg.get("window_id", "")
        if not window_id:
            send_error(self._conn.sock, "Window ID required")
            return
        user_id = self._conn.user["id"]
        session_name = self._conn.tmux_session_name()
        ws_session = self._conn.app.state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        match = self.find_window(ws_session, user_id, window_id)
        if match is None:
            return
        if not match.get("shared"):
            # Already unshared — a no-op (idempotent, and cheap if a
            # zero-permission client spams the command: no joiner
            # kills, no broadcasts).
            return
        match["shared"] = False
        # Kick spectators/collaborators
        try:
            await self._conn.app.state.terminal.kill_joiner_sessions(
                self._conn.container_id,
                session_name,
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
        terminal,
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
                )
            except TerminalError:
                await terminal.select_window(
                    container_id, owner_user_id, window_id
                )
        else:
            await terminal.select_window(
                container_id, owner_user_id, window_id
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
        if not await self._conn.has_perm("spectate-on-shared-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return

        owner_user_id = msg.get("user_id", "").strip()
        window_id = msg.get("window_id", "").strip()
        if not owner_user_id or not window_id:
            send_error(self._conn.sock, "user_id and window_id required")
            return

        ws_session = self._conn.app.state.sockets.get_session(
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
            await self._conn.has_perm("code-in-shared-terminals")
            or await self._conn.has_perm("share-terminals")
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
            # Same deterministic path as the owner's interactive terminal
            # (#2001): the joiner's shell gets its own per-user agent
            # socket pre-wired, live the moment it forwards an agent.
            ssh_agent_socket=ssh_agent_socket_path(self._conn.user["id"]),
            terminal=self._conn.app.state.terminal,
        )
        self._conn.terminal_session = session
        conn = self._conn

        cols = self._conn.terminal_cols
        rows = self._conn.terminal_rows

        async def _start_shared() -> None:
            try:
                await session.start(cols, rows)
                await self._select_shared_window(
                    conn.container_id,
                    session,
                    join_target,
                    window_id,
                    conn.app.state.terminal,
                )
                if not await conn.activate_session(session):
                    return
                conn.sock.send_json(
                    {
                        "type": "terminal_started",
                        "shared_user_id": owner_user_id,
                        "shared_window": window_name,
                        "readOnly": read_only,
                    }
                )
                ws_sess = conn.app.state.sockets.get_session(conn.workspace_id)
                if ws_sess:
                    conn.broadcast_shared_terminals(ws_sess)
            except asyncio.CancelledError:
                await session.stop()
                raise
            except Exception as e:
                await session.stop()
                logger.exception("Shared terminal join failed: %s", e)
                send_error(
                    conn.sock,
                    "Failed to join shared terminal",
                )

        self._conn.terminal_task = asyncio.create_task(_start_shared())

    async def list_shared_terminals(self) -> None:

        if not self._conn.workspace_id:
            return
        if not await self._conn.has_perm("spectate-on-shared-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return
        ws_session = self._conn.app.state.sockets.get_session(
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
        await self._conn.terminal.sync_service_windows(ws_session)
        terminals = get_shared_terminals(
            ws_session, self._conn.app.state.sockets
        )
        self._conn.sock.send_json(
            {"type": "shared_terminals", "terminals": terminals}
        )

    def broadcast_shared_terminals(self, ws_session) -> None:
        """Broadcast the current shared terminal list to all subscribers."""
        ws_session.broadcast_shared_terminals()

    # Keep old handler name for backwards compat with existing E2E tests
    async def create_shared_terminal(self, msg: dict) -> None:
        """Create a new shared terminal (legacy API — creates a new window
        and marks it shared)."""

        name = await self._share_terminals_name(msg)
        if name is None:
            return
        session_name = self._conn.tmux_session_name()
        try:
            windows = await self._conn.app.state.terminal.new_window(
                self._conn.container_id,
                session_name,
                name=name,
            )
        except Exception as e:
            logger.exception("Failed to create shared terminal: %s", e)
            send_error(self._conn.sock, "Failed to create shared terminal")
            return
        # The newly created window is the active one. Identify it by its
        # window id (@N), not its name — names are display-only and may
        # duplicate, so a name match could hit the wrong window (#2192).
        new_id = next(
            (w["id"] for w in windows if w.get("active") and "id" in w),
            None,
        )
        # Sync with tmux to get proper window_id, then mark the new
        # window as shared.
        self._conn.sync_terminal_windows(windows)
        self._mark_window_shared(new_id)

    async def _resolve_shared_terminal(self, msg: dict) -> tuple | None:
        """Validate the delete-shared-terminal request and resolve it to
        (ws_session, owner_user_id, window_id, window_name); None after
        sending the refusal. Only the terminal's owner — or the workspace
        owner — may delete it: the owner_user_id comes from the client and
        must not be trusted blindly, otherwise any collaborator with the
        share-terminals permission could close other users' windows."""
        if not self._conn.container_id:
            return None
        if not await self._conn.has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return None

        owner_user_id = msg.get("user_id", "").strip()
        window_id = msg.get("window_id", "").strip()
        if not owner_user_id or not window_id:
            send_error(self._conn.sock, "user_id and window_id required")
            return None
        if not await self._may_delete_shared_terminal(owner_user_id):
            return None
        ws_session = self._conn.app.state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return None
        match = self.find_window(
            ws_session,
            owner_user_id,
            window_id,
            error_msg="Terminal not found",
        )
        if match is None:
            return None
        return ws_session, owner_user_id, window_id, match["name"]

    async def _close_shared_window(
        self, owner_user_id: str, window_id: str
    ) -> bool:
        """Close the window (and its joiner sessions); False (after sending
        the error frame) on failure."""
        try:
            await self._conn.app.state.terminal.kill_joiner_sessions(
                self._conn.container_id,
                owner_user_id,
            )
            await self._conn.app.state.terminal.close_window(
                self._conn.container_id,
                owner_user_id,
                window_id,
            )
            return True
        except Exception as e:
            logger.exception("Failed to delete shared terminal: %s", e)
            send_error(self._conn.sock, "Failed to delete shared terminal")
            return False

    async def _may_delete_shared_terminal(self, owner_user_id: str) -> bool:
        """The terminal's owner — or the workspace owner — may delete it;
        sends "Permission denied" when neither."""
        if owner_user_id == self._conn.user["id"]:
            return True
        workspace = (
            await self._conn.app.state.model.workspaces.get_workspace_by_id(
                self._conn.workspace_id
            )
        )
        if (
            workspace is not None
            and workspace["user_id"] == self._conn.user["id"]
        ):
            return True
        send_error(self._conn.sock, "Permission denied")
        return False

    async def _share_terminals_name(self, msg: dict) -> str | None:
        """Guard the create-shared-terminal preconditions; the stripped
        name, or None after sending the refusal."""
        if not self._conn.container_id or not self._conn._user_home:
            return None
        if not await self._conn.has_perm("share-terminals"):
            send_error(self._conn.sock, "Permission denied")
            return None
        name = msg.get("name", "").strip()
        if not name:
            send_error(self._conn.sock, "Name required")
            return None
        return name

    def _mark_window_shared(self, new_id) -> None:
        """Mark the freshly-created window shared (if found) and broadcast
        the updated shared-terminal list."""
        ws_session = self._conn.app.state.sockets.get_session(
            self._conn.workspace_id
        )
        if not ws_session:
            return
        user_id = self._conn.user["id"]
        if new_id is not None:
            for w in ws_session.terminal_windows.get(user_id, []):
                if w.get("id") == new_id:
                    w["shared"] = True
                    break
        self.broadcast_shared_terminals(ws_session)

    async def delete_shared_terminal(self, msg: dict) -> None:
        """Delete a shared terminal (legacy API — unshares and closes
        the window)."""

        resolved = await self._resolve_shared_terminal(msg)
        if resolved is None:
            return
        ws_session, owner_user_id, window_id, window_name = resolved
        if not await self._close_shared_window(owner_user_id, window_id):
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
    async def handle_list_error(self, e: Exception) -> None:
        logger.exception("Failed to list shared terminals: %s", e)
        send_error(self._conn.sock, "Failed to list shared terminals")
