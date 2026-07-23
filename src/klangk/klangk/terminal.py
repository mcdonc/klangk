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
import re
import logging
import os
import pty
import uuid
import signal
import struct
import termios
import tty
from collections.abc import AsyncGenerator

from .podman import Podman, subprocess_env
from .exceptions import TerminalError
from .model.workspaces import SETUP_STATE_COMPLETE
from .util import BoundedOutputQueue

logger = logging.getLogger(__name__)

_READ_CHUNK = 65536
CONTAINER_USER = "klangk"

_SAFE_WINDOW_NAME = re.compile(r"^[A-Za-z0-9 _.\-]+$")
_MAX_WINDOW_NAME_LEN = 64


# ---------------------------------------------------------------------------
# Pure helpers (no podman dependency — stay module-level)
# ---------------------------------------------------------------------------


def validate_window_name(name: str) -> None:
    """Raise ``ValueError`` if *name* contains shell-unsafe characters."""
    if not name or len(name) > _MAX_WINDOW_NAME_LEN:
        raise ValueError(
            f"Window name must be 1-{_MAX_WINDOW_NAME_LEN} characters"
        )
    if not _SAFE_WINDOW_NAME.match(name):
        raise ValueError(
            "Window name may only contain letters, digits,"
            " spaces, hyphens, underscores, and dots"
        )


# Backend env vars stripped from the in-container shell.
_SENSITIVE_ENV_PREFIXES = (
    "KLANGKD_LLM_API_KEY",
    "ANTHROPIC_",
    "OPENAI_",
    "GOOGLE_",
    "GROQ_",
    "MISTRAL_",
)


def build_environment(
    user_home: str | None = None,
    user_id: str | None = None,
    user_handle: str | None = None,
    ssh_agent_socket: str | None = None,
) -> list[str]:
    env = ["TERM=xterm-256color", f"USER={CONTAINER_USER}"]
    if user_home is not None:
        env.append(f"HOME={user_home}")
    if user_id is not None:
        env.append(f"KLANGKWS_USER_ID={user_id}")
    if user_handle is not None:
        env.append(f"KLANGKWS_USER_HANDLE={user_handle}")
    if ssh_agent_socket is not None:
        env.append(f"SSH_AUTH_SOCK={ssh_agent_socket}")
    return env


# Name of the dedicated tmux window that runs a workspace's
# service_command, leaving the user's interactive window 0 free.
SERVICE_CMD_WINDOW = "service-cmd"

# The standalone tmux session that runs the workspace's service command,
# owned by the agent identity (not the owner). Constant name -- keyed in
# the session map by AGENT_USER_ID, never the renameable handle -- and
# decoupled from both the owner's interactive session and the
# ``pi --mode rpc`` subprocess lifecycle (it survives the agent process
# dying because it is just a tmux session) (#1133 D6).
SERVICE_SESSION = "service"


def should_fire_service_command(
    service_command: str | None, setup_state: str
) -> bool:
    """The setup-phase half of the firing predicate (#1033).

    The service command may fire iff it is configured AND setup is
    complete. ``pending`` and ``failed`` both block. ``setup_state`` is
    always one of the three lifecycle values -- the DB column is
    ``NOT NULL DEFAULT 'complete'`` and every SELECT includes it -- so
    there is no ``None`` case to handle here.

    The other half -- "the service-cmd window doesn't already exist" --
    is checked by the caller via :meth:`Terminal.service_cmd_window_exists`,
    since it is per-container and ephemeral.
    """
    if not service_command:
        return False
    return setup_state == SETUP_STATE_COMPLETE


def build_shell_command(
    session_name: str | None = None,
    user_home: str | None = None,
    socket_path: str | None = None,
    join_session: str | None = None,
    read_only: bool = False,
    tmux_enabled: bool = True,
    ssh_agent_socket: str | None = None,
) -> tuple[list[str], str | None]:
    """Build the shell command for a terminal session.

    *session_name*: tmux session name (typically the user_id).
    *user_home*: sets ``HOME`` env var inside the session.
    *socket_path*: use ``-S`` for shared terminal sockets.
    *join_session*: join an existing session group (for shared terminals).
    *read_only*: attach with ``-r`` for spy mode.
    *tmux_enabled*: when ``False`` and this is a plain (non-shared)
    session, launch a bare login shell instead of tmux.  Shared/joined
    sessions (``socket_path``/``join_session``) always use tmux.

    Returns ``(command, unique_session_name)``.  *unique_session_name* is
    set only for shared terminal joins so ``stop()`` can kill the tmux
    session inside the container (preventing stale clients that deadlock
    the tmux server).
    """
    unset_args: list[str] = []
    for key in os.environ:
        if key.startswith(_SENSITIVE_ENV_PREFIXES):
            unset_args.extend(["-u", key])

    # Plain-shell mode: drop straight into a login shell (sources
    # /etc/profile -> /etc/bash.bashrc, same init path tmux's login shell
    # uses).  Only applies to the default session — sharing needs tmux.
    if not tmux_enabled and socket_path is None and join_session is None:
        cmd = ["env", *unset_args, "bash", "-l"]
        return cmd, None

    tmux_env: list[str] = []
    session_args: list[str] = []
    socket_args: list[str] = []
    unique: str | None = None
    if socket_path is not None:
        socket_args = ["-S", socket_path]
    if user_home is not None:
        tmux_env = ["-e", f"HOME={user_home}"]
    if ssh_agent_socket is not None:
        tmux_env += ["-e", f"SSH_AUTH_SOCK={ssh_agent_socket}"]
    if session_name is not None:
        if join_session is not None:
            # Join an existing session group.  Use a unique session name
            # so rapid re-joins don't collide with a stale session.
            unique = f"{session_name}-{uuid.uuid4().hex[:8]}"
            session_args = ["-t", join_session, "-s", unique]
        else:
            # Each connection gets a grouped session so that
            # select-window only affects this client.  The base
            # session is created detached if it doesn't exist yet
            # (via ensure_base_session), then we always create a
            # grouped session targeting it.
            unique = f"{session_name}-{uuid.uuid4().hex[:8]}"
            session_args = ["-t", session_name, "-s", unique]
    cmd = [
        "env",
        *unset_args,
        "tmux",
        *socket_args,
        "new-session",
        *session_args,
        *tmux_env,
    ]
    # Note: no refresh-client here for joins — the caller selects the
    # target window first, then triggers a refresh via resize.
    # Read-only is enforced in handle_terminal_input (wshandler.py),
    # which drops input when session.read_only is True.  tmux's
    # switch-client -r is not used because it caused display issues.
    return cmd, unique


def _build_exec_argv(
    container_id: str,
    env: list[str],
    shell_cmd: list[str],
    work_dir: str = "/home/work",
) -> list[str]:
    argv = ["exec", "-t", "-i", "-u", CONTAINER_USER, "-w", work_dir]
    for entry in env:
        argv += ["-e", entry]
    argv.append(container_id)
    argv += shell_cmd
    return argv


# ---------------------------------------------------------------------------
# Terminal — tmux-session management (cohesion over a shared Podman dep, #1480)
# ---------------------------------------------------------------------------


class Terminal:
    """Groups the ~25 tmux-session management functions that share a
    :class:`~klangk.podman.Podman` dependency.

    Constructed once in :func:`build_app` and stored on
    ``app.state.terminal`` (#1480). Reaches its dependencies — podman
    (resolved binary path + CLI wrappers), the container registry
    (per-container service-firing lock, #1188), and settings — through a
    single ``app_state`` reference rather than three separate ctor args.
    """

    def __init__(self, app):
        self._app = app

    def reconfigure(self, app) -> None:
        self._app = app

    @property
    def podman(self) -> Podman:
        return self._app.state.podman

    @property
    def registry(self):
        return self._app.state.container_registry

    def tmux_enabled(self) -> bool:
        """Whether new terminal sessions are wrapped in tmux.

        Defaults to enabled (the historical behaviour).  Set
        ``KLANGKD_DISABLE_TMUX`` to a truthy value (``1``/``true``/``yes``) to
        drop users straight into a plain login shell instead.  Note this only
        affects the default per-user terminal; shared/joined terminals are
        built on tmux session groups and always use tmux regardless.
        """
        val = self._app.state.settings.disable_tmux.lower()
        return val not in ("1", "true", "yes")

    # --- tmux session / window queries ---

    async def has_tmux_session(
        self, container_id: str, session_name: str
    ) -> bool:
        """Return True if a tmux session named *session_name* exists."""
        try:
            rc, _, _ = await self.podman.exec_container(
                container_id,
                ["tmux", "has-session", "-t", session_name],
                user=CONTAINER_USER,
                timeout=5,
            )
        except Exception:
            return False
        return rc == 0

    async def service_cmd_window_exists(
        self, container_id: str, session_name: str
    ) -> bool:
        """Return True if the ``service-cmd`` window exists in *session_name*.

        This is the ephemeral "has the service command already fired in
        THIS container" check (#1033). Unlike ``setup_state`` it is
        per-container: it resets on container recreation, so the boot path
        re-fires the service command for an already-``complete`` workspace.
        tmux allows duplicate window names, so we must inspect the list
        rather than rely on ``new-window`` failing.
        """
        try:
            rc, stdout, _ = await self.podman.exec_container(
                container_id,
                [
                    "tmux",
                    "list-windows",
                    "-t",
                    session_name,
                    "-F",
                    "#{window_name}",
                ],
                user=CONTAINER_USER,
                timeout=5,
            )
        except Exception:
            return False
        if rc != 0:
            return False
        return SERVICE_CMD_WINDOW in {
            line.strip() for line in stdout.splitlines() if line.strip()
        }

    async def _ensure_tmux_session(
        self,
        container_id: str,
        session_name: str,
        user_home: str | None = None,
        ssh_agent_socket: str | None = None,
    ) -> bool:
        """Ensure a detached base tmux session exists for *session_name*.

        Idempotent: returns ``True`` if the session was freshly created,
        ``False`` if it already existed or could not be created. HOME and
        SSH_AUTH_SOCK are passed as tmux ``-e`` flags (part of the command,
        not podman's), so the session's window-0 shell sources the right
        profile.
        """
        if await self.has_tmux_session(container_id, session_name):
            return False
        env_args: list[str] = []
        if user_home is not None:
            env_args += ["-e", f"HOME={user_home}"]
        if ssh_agent_socket is not None:
            env_args += ["-e", f"SSH_AUTH_SOCK={ssh_agent_socket}"]
        try:
            await self.podman.exec_container(
                container_id,
                ["tmux", "new-session", "-d", "-s", session_name, *env_args],
                user=CONTAINER_USER,
                timeout=10,
            )
        except Exception:
            logger.warning(
                "Failed to create base tmux session %s", session_name
            )
            return False
        return True

    async def ensure_base_session(
        self,
        container_id: str,
        session_name: str,
        user_home: str | None = None,
        ssh_agent_socket: str | None = None,
    ) -> bool:
        """Ensure the firing user's base tmux session + window 0 exist.

        Idempotent: returns ``True`` if the session was freshly created.
        The service command no longer lives in any user's session -- it
        runs in the standalone ``service`` session owned by the agent
        identity (see :meth:`ensure_service_session`), so this is purely
        the interactive-shell session every ``terminal_start`` needs
        regardless of setup state (#1133).
        """
        return await self._ensure_tmux_session(
            container_id, session_name, user_home, ssh_agent_socket
        )

    async def ensure_service_session(
        self,
        container_id: str,
        agent_home: str,
        service_command: str,
        setup_state: str = SETUP_STATE_COMPLETE,
    ) -> None:
        """Ensure the standalone ``service`` session; maybe fire service-cmd.

        The workspace's service command runs in a tmux session with a
        CONSTANT name ``service`` (not the owner's id), with the
        ``service-cmd`` window inside it -> ``service:service-cmd``. The
        session is owned by the agent identity and decoupled from both the
        owner's interactive session and the ``pi --mode rpc`` subprocess
        lifecycle -- it survives the agent subprocess dying/restarting
        because it is just a tmux session (#1133 D6).

        *agent_home* (e.g. ``/home/clanker``) is the session's HOME. The
        service command fires iff the predicate holds (configured AND setup
        complete) AND the ``service-cmd`` window doesn't already exist
        (exactly-once-per-container). Idempotent: safe to call from every
        ``terminal_start`` (#1033) and the boot path alike -- the
        window-exists check makes it a no-op after the first fire.

        The window-exists -> new-window -> send-keys sequence is serialized
        per container via the registry's service-session lock (#1188): without
        it the boot path and the per-connection path could both pass the
        existence check before either created the window, producing two
        duplicate-named ``service-cmd`` windows (tmux allows duplicate
        names), leaving later ``send-keys -t service:service-cmd`` ambiguous.
        """
        # Hold the per-container lock across the entire read-modify-write so
        # a concurrent caller (boot vs first terminal_start, owner vs
        # collaborator) cannot interleave: the second waits, then observes
        # the window exists and no-ops. This also bounds the partial-failure
        # window from #1186.
        async with self.registry.get_service_session_lock(container_id):
            await self._ensure_tmux_session(
                container_id, SERVICE_SESSION, agent_home
            )
            if not (
                should_fire_service_command(service_command, setup_state)
            ) or await self.service_cmd_window_exists(
                container_id, SERVICE_SESSION
            ):
                return
            try:
                await self.podman.exec_container(
                    container_id,
                    [
                        "tmux",
                        "new-window",
                        "-d",
                        "-t",
                        SERVICE_SESSION,
                        "-n",
                        SERVICE_CMD_WINDOW,
                    ],
                    user=CONTAINER_USER,
                    timeout=5,
                )
            except Exception:
                logger.warning(
                    "Failed to create %s window in %s",
                    SERVICE_CMD_WINDOW,
                    SERVICE_SESSION,
                )
                return
            # The new window's shell needs a moment to source .profile / .bashrc
            # before it can resolve PATH-dependent commands (nvm, openclaw, ...).
            # Same race as #1030.
            await asyncio.sleep(1)
            try:
                await self.podman.exec_container(
                    container_id,
                    [
                        "tmux",
                        "send-keys",
                        "-t",
                        f"{SERVICE_SESSION}:{SERVICE_CMD_WINDOW}",
                        service_command,
                        "Enter",
                    ],
                    user=CONTAINER_USER,
                    timeout=5,
                )
                # The service command just fired -- reset the health-check
                # startup-grace anchor so the monitor gives the service time
                # to boot before a failing poll can flag it unhealthy (e.g.
                # a gateway that isn't accepting connections yet).  Set only
                # on a successful send-keys; the except path below never
                # launched the command, so it must not start the grace
                # window.
                self.registry.mark_service_started(container_id)
            except Exception:
                logger.warning(
                    "Failed to send service command to %s", SERVICE_SESSION
                )
                # The window was created above but we never typed the command
                # into it. Kill it so the next fire re-runs the whole sequence
                # instead of no-op'ing forever on the half-created window (#1186).
                try:
                    await self.podman.exec_container(
                        container_id,
                        [
                            "tmux",
                            "kill-window",
                            "-t",
                            f"{SERVICE_SESSION}:{SERVICE_CMD_WINDOW}",
                        ],
                        user=CONTAINER_USER,
                        timeout=5,
                    )
                except Exception:
                    logger.warning(
                        "Failed to clean up %s window in %s",
                        SERVICE_CMD_WINDOW,
                        SERVICE_SESSION,
                    )

    # --- container-side helpers ---

    async def attach_browser(self, container_id: str, browser_id: str) -> None:
        """Run ``klangk-attach-browser <browser_id>`` inside the container.

        This stores the browser ID in the tmux global environment so that
        ``klangk-browser-id`` can read it dynamically.  Called after each
        ``terminal_start`` (including re-attach after browser refresh).
        """
        rc, _stdout, stderr = await self.podman.exec_container(
            container_id,
            ["klangk-attach-browser", browser_id],
            user=CONTAINER_USER,
            timeout=10,
        )
        if rc != 0:
            logger.warning(
                "klangk-attach-browser failed (rc=%d): %s",
                rc,
                stderr.strip(),
            )

    async def set_workspace_token(self, container_id: str, token: str) -> None:
        """Write a workspace token to ``/run/klangk/workspace-token`` inside
        the container via ``klangk-set-workspace-token``.
        """
        rc, _stdout, stderr = await self.podman.exec_container(
            container_id,
            ["klangk-set-workspace-token", token],
            user=CONTAINER_USER,
            timeout=10,
        )
        if rc != 0:
            logger.warning(
                "klangk-set-workspace-token failed (rc=%d): %s",
                rc,
                stderr.strip(),
            )

    # --- tmux command primitives ---

    async def tmux_command(
        self, container_id: str, session_name: str, args: list[str]
    ) -> str:
        """Run a tmux command in the container and return stdout.

        Retries up to 3 times on socket-not-found errors, which can occur
        when the tmux server is still starting in a fresh container.
        """
        for attempt in range(3):
            rc, stdout, stderr = await self.podman.exec_container(
                container_id,
                ["tmux", *args],
                user=CONTAINER_USER,
                timeout=10,
            )
            if rc == 0:
                return stdout
            if "No such file or directory" in stderr and attempt < 2:
                await asyncio.sleep(0.5)
                continue
            raise TerminalError(f"tmux command failed: {stderr.strip()}")
        return ""  # pragma: no cover

    async def list_windows(
        self, container_id: str, session_name: str
    ) -> list[dict]:
        """List tmux windows for a session. Returns [{index, name}, ...]."""
        output = await self.tmux_command(
            container_id,
            session_name,
            [
                "list-windows",
                "-t",
                session_name,
                "-F",
                "#{window_id}|||#{window_index}|||#{window_name}|||#{window_active}",
            ],
        )
        windows = []
        for line in output.strip().splitlines():
            parts = line.split("|||")
            if len(parts) >= 4:
                windows.append(
                    {
                        "id": parts[0],  # e.g. "@0" — unique, never reused
                        "index": int(parts[1]),
                        "name": parts[2],
                        "active": parts[3] == "1",
                    }
                )
        return windows

    async def new_window(
        self,
        container_id: str,
        session_name: str,
        name: str | None = None,
    ) -> list[dict]:
        """Create a new tmux window and return the updated window list.

        If *name* is not provided, auto-generates a unique name.
        Raises ``ValueError`` if *name* duplicates an existing window name.

        Uses a single podman exec with a shell script to minimize
        round-trips (list + create + list in one call).
        """
        if name is not None:
            validate_window_name(name)
            # Explicit name — check + create + list in one exec. The window
            # name and session name are passed as positional argv ($1/$2),
            # never interpolated into the script, so shell metacharacters in
            # either are harmless (name is validated above regardless).
            script = (
                'name="$1"; sn="$2";'
                ' existing=$(tmux list-windows -t "$sn"'
                " -F '#{window_name}' 2>/dev/null);"
                ' echo "$existing" | grep -qx "$name"'
                " && echo 'DUPLICATE' && exit 1;"
                ' tmux new-window -t "$sn" -n "$name";'
                ' tmux list-windows -t "$sn"'
                " -F '#{window_id}|||#{window_index}|||#{window_name}|||#{window_active}'"
            )
            argv = ["bash", "-c", script, "bash", name, session_name]
        else:
            # Auto-name — find next number, create, list. session_name is $1.
            script = (
                'sn="$1";'
                ' names=$(tmux list-windows -t "$sn"'
                " -F '#{window_name}' 2>/dev/null);"
                ' n=1; while echo "$names" | grep -qx "$n"; do n=$((n+1)); done;'
                ' tmux new-window -t "$sn" -n "$n";'
                ' tmux list-windows -t "$sn"'
                " -F '#{window_id}|||#{window_index}|||#{window_name}|||#{window_active}'"
            )
            argv = ["bash", "-c", script, "bash", session_name]
        rc, output, stderr = await self.podman.exec_container(
            container_id,
            argv,
            user=CONTAINER_USER,
            timeout=10,
        )
        if rc != 0:
            if "DUPLICATE" in output:
                raise ValueError(f"Window name '{name}' already exists")
            raise TerminalError(f"new_window failed: {stderr.strip()}")
        windows = []
        for line in output.strip().splitlines():
            parts = line.split("|||")
            if len(parts) >= 4:
                windows.append(
                    {
                        "id": parts[0],
                        "index": int(parts[1]),
                        "name": parts[2],
                        "active": parts[3] == "1",
                    }
                )
        return windows

    async def rename_window(
        self,
        container_id: str,
        session_name: str,
        index: int,
        name: str,
    ) -> None:
        """Rename a tmux window.

        Raises ``ValueError`` if *name* contains unsafe characters or
        duplicates another window's name.
        """
        validate_window_name(name)
        existing = await self.list_windows(container_id, session_name)
        if any(w["name"] == name and w["index"] != index for w in existing):
            raise ValueError(f"Window name '{name}' already exists")
        await self.tmux_command(
            container_id,
            session_name,
            ["rename-window", "-t", f"{session_name}:{index}", name],
        )

    async def select_window(
        self,
        container_id: str,
        session_name: str,
        target: int | str,
    ) -> None:
        """Switch the active tmux window.

        *target* can be a window index (int), window name (str), or
        window id (``@N`` string — preferred, globally unique).
        """
        # Window IDs (@N) can be used directly as targets without
        # a session prefix.
        if isinstance(target, str) and target.startswith("@"):
            t = target
        else:
            t = f"{session_name}:{target}"
        await self.tmux_command(
            container_id,
            session_name,
            ["select-window", "-t", t],
        )

    async def close_window(
        self,
        container_id: str,
        session_name: str,
        target: int | str,
    ) -> list[dict]:
        """Close a tmux window and return the updated window list.

        *target* can be a window index (int), window name (str), or
        window id (``@N`` string — preferred, globally unique).
        """
        if isinstance(target, str) and target.startswith("@"):
            t = target
        else:
            t = f"{session_name}:{target}"
        await self.tmux_command(
            container_id,
            session_name,
            ["kill-window", "-t", t],
        )
        return await self.list_windows(container_id, session_name)

    async def kill_joiner_sessions(
        self, container_id: str, owner_handle: str
    ) -> None:
        """Kill all session-group sessions except the owner's own session.

        Used when unsharing to disconnect spectators/collaborators.
        """
        try:
            output = await self.tmux_command(
                container_id,
                owner_handle,
                [
                    "list-sessions",
                    "-F",
                    "#{session_name}",
                ],
            )
            for session_name in output.strip().splitlines():
                if session_name != owner_handle:
                    try:
                        await self.tmux_command(
                            container_id,
                            owner_handle,
                            ["kill-session", "-t", session_name],
                        )
                    except TerminalError:
                        pass  # Session may have already exited
        except TerminalError:
            pass  # No sessions


# ---------------------------------------------------------------------------
# Shell process (PTY layer — needs podman.bin, no Terminal ref)
# ---------------------------------------------------------------------------


class ShellProcess:
    """Owns the PTY + ``podman exec`` subprocess for one shell.

    The master fd is set to non-blocking mode and registered with the
    asyncio event loop via ``add_reader``.  This avoids the default
    thread-pool executor whose limited threads (typically 6) are
    easily exhausted by blocking PTY I/O, causing cascading stalls
    across all terminal sessions.
    """

    def __init__(self, podman=None) -> None:
        self._podman = podman
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._read_event: asyncio.Event | None = None

    async def start(  # pragma: no cover
        self, argv: list[str], rows: int, cols: int
    ) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            tty.setraw(slave_fd)
            _set_winsize(master_fd, rows, cols)
            self._proc = await asyncio.create_subprocess_exec(
                self._podman.bin,
                *argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                env=subprocess_env(),
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        # Set non-blocking and register with the event loop so reads
        # and writes never consume a thread-pool slot.
        os.set_blocking(master_fd, False)
        self._read_event = asyncio.Event()
        asyncio.get_running_loop().add_reader(master_fd, self._read_event.set)

    async def read(self) -> bytes:  # pragma: no cover
        try:
            while True:
                try:
                    return os.read(self._master_fd, _READ_CHUNK)
                except BlockingIOError:
                    self._read_event.clear()
                    await self._read_event.wait()
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:  # pragma: no cover
        try:
            os.write(self._master_fd, data)
        except BlockingIOError:
            # Buffer full — run in executor as fallback so we don't
            # spin.  This is rare; normally the buffer accepts input.
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
                asyncio.get_running_loop().remove_reader(self._master_fd)
            except (ValueError, RuntimeError):
                pass  # loop already closed or fd not registered
            if self._read_event is not None:
                self._read_event.set()  # unblock any pending read
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


def _set_winsize(fd: int, rows: int, cols: int) -> None:  # pragma: no cover
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def make_shell_process(podman=None) -> ShellProcess:
    return ShellProcess(podman)


# ---------------------------------------------------------------------------
# Interactive terminal session (PTY shell — orchestrates Terminal + ShellProcess)
# ---------------------------------------------------------------------------


class TerminalSession:
    """Manages an interactive shell session over a PTY."""

    def __init__(
        self,
        container_id: str,
        session_name: str | None = None,
        user_home: str | None = None,
        socket_path: str | None = None,
        join_session: str | None = None,
        read_only: bool = False,
        user_id: str | None = None,
        user_handle: str | None = None,
        ssh_agent_socket: str | None = None,
        terminal: Terminal | None = None,
    ):
        self.container_id = container_id
        self._terminal = terminal
        self.session_name = session_name
        self.user_home = user_home
        self.socket_path = socket_path
        self.join_session = join_session
        self.read_only = read_only
        self.user_id = user_id
        self.user_handle = user_handle
        self.ssh_agent_socket = ssh_agent_socket
        self._shell: ShellProcess | None = None
        self._output_queue: BoundedOutputQueue[str] = BoundedOutputQueue(
            maxsize=64
        )
        self._running = False
        self._read_task: asyncio.Task | None = None
        self.tmux_session_name: str | None = None

    @property
    def _podman(self):
        """Reach podman via the Terminal instance (#1480)."""
        return self._terminal.podman

    async def start(
        self,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        """Start a shell session via ``podman exec`` over a PTY."""
        self._running = True
        # Ensure the base tmux session exists before building a grouped
        # session command that targets it.  Only needed for own sessions
        # (not joins/shared, which target a different session).
        if (
            self.session_name
            and not self.join_session
            and not self.socket_path
            and self._terminal.tmux_enabled()
        ):
            await self._terminal.ensure_base_session(
                self.container_id,
                self.session_name,
                user_home=self.user_home,
                ssh_agent_socket=self.ssh_agent_socket,
            )
        env = build_environment(
            self.user_home,
            user_id=self.user_id,
            user_handle=self.user_handle,
            ssh_agent_socket=self.ssh_agent_socket,
        )
        shell_cmd, self.tmux_session_name = build_shell_command(
            session_name=self.session_name,
            user_home=self.user_home,
            socket_path=self.socket_path,
            join_session=self.join_session,
            read_only=self.read_only,
            tmux_enabled=self._terminal.tmux_enabled(),
            ssh_agent_socket=self.ssh_agent_socket,
        )
        work_dir = "/home"
        argv = _build_exec_argv(self.container_id, env, shell_cmd, work_dir)

        logger.info("Terminal exec argv: %s", argv)
        shell = make_shell_process(self._podman)
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

        # If SSH agent forwarding is active, inject SSH_AUTH_SOCK into
        # the tmux session environment.  This is needed because
        # `tmux new-session -A` reattaches to an existing session and
        # ignores the `-e` flags — the env var must be set explicitly.
        if self.ssh_agent_socket and self.session_name:
            try:
                await self._podman.exec_container(
                    self.container_id,
                    [
                        "tmux",
                        "set-environment",
                        "-t",
                        self.session_name,
                        "SSH_AUTH_SOCK",
                        self.ssh_agent_socket,
                    ],
                )
            except OSError as e:  # pragma: no cover
                logger.warning("Failed to set SSH_AUTH_SOCK in tmux: %s", e)

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
                    logger.info("Terminal read loop: EOF from PTY")
                    break
                text = decoder.decode(data)
                if text:
                    try:
                        self._output_queue.put_nowait(text)
                    except asyncio.QueueFull:  # pragma: no cover
                        pass  # drop output; don't block the PTY read
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
                await asyncio.wait_for(
                    self._shell.write(data.encode("utf-8")),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:  # pragma: no cover
                logger.warning(
                    "PTY write timed out after 30s, stopping session"
                )
                await self.stop()
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

        # Kill the tmux session inside the container so the client
        # doesn't stay attached after the host-side process is gone.
        # All grouped sessions (own, join, shared) are killed — the
        # base session persists independently.  tmux_session_name is
        # a unique grouped name for all connection types.
        if self.tmux_session_name:
            try:
                socket_args = (
                    ["-S", self.socket_path] if self.socket_path else []
                )
                await self._podman.exec_container(
                    self.container_id,
                    [
                        "tmux",
                        *socket_args,
                        "kill-session",
                        "-t",
                        self.tmux_session_name,
                    ],
                    user=CONTAINER_USER,
                    timeout=5,
                )
            except Exception:
                logger.debug(
                    "Failed to kill tmux session %s",
                    self.tmux_session_name,
                    exc_info=True,
                )

        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
