"""Terminal session: interactive shell via ``podman exec`` over a PTY.

A single local PTY (slave set to raw mode) bridges the ``podman exec``
subprocess's stdio to the container-side PTY allocated by ``-t``.  Raw
mode keeps the local line discipline from consuming escape sequences
(arrow keys, etc.).  Resize sets the master window size and signals
podman with ``SIGWINCH`` so it resizes the container PTY.
"""

import asyncio
import codecs
import contextlib
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

from .container.spec import SHARED_HOME
from .podman import Podman, classify, subprocess_env
from .exceptions import ContainerGoneError, TerminalError
from .model.workspaces import SETUP_STATE_COMPLETE
from .util import BoundedOutputQueue

logger = logging.getLogger(__name__)

_WRITE_TIMEOUT = 30.0  # seconds before a stuck PTY write stops the session
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

# Per-exec budget for the tmux control-plane calls in the service-command
# fire sequence (has-session / list-windows / new-window / send-keys /
# kill-window). These are cheap when the host is healthy (0.1-0.2s) but
# pure control-plane -- nothing user-visible blocks on them being fast --
# so the old 5s budget only ever fired as a false kill under runner CPU
# saturation (a killed exec returns rc=-1 from ``Podman.run``, the
# container side may or may not have completed). 15s keeps a bound on a
# wedged runtime while leaving headroom for concurrent E2E jobs on a
# shared host (#2740).
SERVICE_EXEC_TIMEOUT = 15.0


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


def _sensitive_env_unsets() -> list[str]:
    """``env -u`` args stripping sensitive host env from the container."""
    unset_args: list[str] = []
    for key in os.environ:
        if key.startswith(_SENSITIVE_ENV_PREFIXES):
            unset_args.extend(["-u", key])
    return unset_args


def _tmux_env_args(
    user_home: str | None,
    ssh_agent_socket: str | None,
    user_id: str | None,
    user_handle: str | None,
) -> list[str]:
    """``-e`` args for the tmux session's environment."""
    tmux_env: list[str] = []
    if user_home is not None:
        tmux_env = ["-e", f"HOME={user_home}"]
    if ssh_agent_socket is not None:
        tmux_env += ["-e", f"SSH_AUTH_SOCK={ssh_agent_socket}"]
    if user_id is not None:
        tmux_env += ["-e", f"KLANGKWS_USER_ID={user_id}"]
    if user_handle is not None:
        tmux_env += ["-e", f"KLANGKWS_USER_HANDLE={user_handle}"]
    return tmux_env


def _session_args(
    session_name: str | None, join_session: str | None
) -> tuple[list[str], str | None]:
    """``new-session`` targeting args plus the unique grouped-session name
    (set only for grouped/joined sessions)."""
    if session_name is None:
        return [], None
    if join_session is not None:
        # Join an existing session group.  Use a unique session name
        # so rapid re-joins don't collide with a stale session.
        unique = f"{session_name}-{uuid.uuid4().hex[:8]}"
        return ["-t", join_session, "-s", unique], unique
    # Each connection gets a grouped session so that
    # select-window only affects this client.  The base
    # session is created detached if it doesn't exist yet
    # (via ensure_base_session), then we always create a
    # grouped session targeting it.
    unique = f"{session_name}-{uuid.uuid4().hex[:8]}"
    return ["-t", session_name, "-s", unique], unique


def build_shell_command(
    session_name: str | None = None,
    user_home: str | None = None,
    socket_path: str | None = None,
    join_session: str | None = None,
    read_only: bool = False,
    tmux_enabled: bool = True,
    ssh_agent_socket: str | None = None,
    user_id: str | None = None,
    user_handle: str | None = None,
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
    unset_args = _sensitive_env_unsets()

    # Plain-shell mode: drop straight into a login shell (sources
    # /etc/profile -> /etc/bash.bashrc, same init path tmux's login shell
    # uses).  Only applies to the default session — sharing needs tmux.
    if not tmux_enabled and socket_path is None and join_session is None:
        cmd = ["env", *unset_args, "bash", "-l"]
        return cmd, None

    socket_args = ["-S", socket_path] if socket_path is not None else []
    tmux_env = _tmux_env_args(
        user_home, ssh_agent_socket, user_id, user_handle
    )
    session_args, unique = _session_args(session_name, join_session)
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
    work_dir: str = SHARED_HOME,
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


# tmux window-list wire format (#2564): one window per line, fields joined
# by "|||" (a separator that cannot appear in a window name). Shared by
# list_windows and new_window's combined create+list script so the format
# string and the parser cannot drift apart.
_WINDOW_FMT = (
    "#{window_id}|||#{window_index}|||#{window_name}|||#{window_active}"
)


def parse_windows(output: str) -> list[dict]:
    """Parse tmux ``list-windows -F _WINDOW_FMT`` output into dicts."""
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


def _window_target(
    session_name: str, target: int | str, *, allow_id: bool = False
) -> str:
    """A tmux window target string for *target* in *session_name*.

    By default the target is always session-qualified so that in a
    session group the command affects only the caller's grouped session,
    not whichever session tmux considers "most recent" (#1883) — even a
    window id (``@N``), which stays qualified for the same reason
    (rename/select callers rely on this). With ``allow_id=True``
    (close_window) a window id is used unqualified: ids are globally
    unique, so kill-window needs no session scoping.
    """
    if allow_id and isinstance(target, str) and target.startswith("@"):
        return target
    return f"{session_name}:{target}"


def _session_env_args(
    user_home: str | None,
    ssh_agent_socket: str | None,
    user_id: str | None,
    user_handle: str | None,
) -> list[str]:
    """tmux ``-e`` flags carrying HOME, SSH_AUTH_SOCK, and the user
    identity vars into the session's window-0 shell (#2259)."""
    env = {
        "HOME": user_home,
        "SSH_AUTH_SOCK": ssh_agent_socket,
        "KLANGKWS_USER_ID": user_id,
        "KLANGKWS_USER_HANDLE": user_handle,
    }
    args: list[str] = []
    for key, value in env.items():
        if value is not None:
            args += ["-e", f"{key}={value}"]
    return args


def _container_gone(stderr: str) -> bool:
    """True when an exec failed because the container is gone.

    Podman's 404 (the container recycled between terminal start and this
    call, #2178) and its *stopped*-container wording (an idle-reap racing
    this exec — "state improper" / "can only create exec sessions on
    running containers", #2514) are the same recoverable recycle race,
    not a tmux/server failure."""
    if classify(stderr) == 404:
        return True
    low = stderr.lower()
    return "state improper" in low or "running containers" in low


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
                timeout=SERVICE_EXEC_TIMEOUT,
            )
        except Exception:
            return False
        return rc == 0

    async def service_cmd_window_exists(
        self, container_id: str, session_name: str
    ) -> bool | None:
        """Return whether the ``service-cmd`` window exists in *session_name*.

        This is the ephemeral "has the service command already fired in
        THIS container" check (#1033). Unlike ``setup_state`` it is
        per-container: it resets on container recreation, so the boot path
        re-fires the service command for an already-``complete`` workspace.
        tmux allows duplicate window names, so we must inspect the list
        rather than rely on ``new-window`` failing.

        Tri-state (#2740): ``True``/``False`` when tmux answered (rc=0),
        ``None`` when the check itself failed (exec killed under load,
        launch failure). A killed exec is NOT evidence the window is
        absent -- assuming "absent" fired a duplicate ``service-cmd``
        window and re-sent the command into a service that may already be
        running. Callers treat ``None`` as "unknown, skip this round" and
        retry on the next terminal_start/reconnect.
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
                timeout=SERVICE_EXEC_TIMEOUT,
            )
        except Exception:
            return None
        if rc != 0:
            return None
        return SERVICE_CMD_WINDOW in {
            line.strip() for line in stdout.splitlines() if line.strip()
        }

    async def _ensure_tmux_session(
        self,
        container_id: str,
        session_name: str,
        user_home: str | None = None,
        ssh_agent_socket: str | None = None,
        user_id: str | None = None,
        user_handle: str | None = None,
    ) -> bool:
        """Ensure a detached base tmux session exists for *session_name*.

        Idempotent: returns ``True`` if the session was freshly created,
        ``False`` if it already existed or could not be created. HOME,
        SSH_AUTH_SOCK, and user identity vars are passed as tmux ``-e``
        flags (part of the command, not podman's), so the session's
        window-0 shell inherits them (#2259).
        """
        if await self.has_tmux_session(container_id, session_name):
            return False
        env_args = _session_env_args(
            user_home, ssh_agent_socket, user_id, user_handle
        )
        try:
            await self.podman.exec_container(
                container_id,
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session_name,
                    "-n",
                    "term",
                    *env_args,
                ],
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
        user_id: str | None = None,
        user_handle: str | None = None,
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
            container_id,
            session_name,
            user_home,
            ssh_agent_socket,
            user_id=user_id,
            user_handle=user_handle,
        )

    async def set_workspace_name(
        self, container_id: str, workspace_name: str
    ) -> None:
        """Set ``@workspace_name`` globally so the tmux status bar shows it.

        Uses ``set -g`` (global) because each connection creates a grouped
        session that does not inherit session-level options from the base
        session.  Called on every ``terminal_start`` — idempotent (#1880).
        """
        try:
            await self.podman.exec_container(
                container_id,
                ["tmux", "set", "-g", "@workspace_name", workspace_name],
                user=CONTAINER_USER,
                timeout=5,
            )
        except Exception:
            logger.warning(
                "Failed to set @workspace_name for container %s",
                container_id,
            )

    async def kill_service_cmd_window(self, container_id: str) -> bool:
        """Kill the ``service:service-cmd`` window; True iff it died.

        The #1186 cleanup, factored out so every failure mode in the fire
        sequence (failed new-window, failed send-keys, cancellation) shares
        it. Best-effort: returns False on a failed/killed exec or launch
        error, leaving the caller to mark the fire pending (#2740).
        """
        try:
            rc, _, _ = await self.podman.exec_container(
                container_id,
                [
                    "tmux",
                    "kill-window",
                    "-t",
                    f"{SERVICE_SESSION}:{SERVICE_CMD_WINDOW}",
                ],
                user=CONTAINER_USER,
                timeout=SERVICE_EXEC_TIMEOUT,
            )
        except Exception:
            return False
        return rc == 0

    async def _send_service_command(
        self, container_id: str, service_command: str
    ) -> bool:
        """send-keys the service command into ``service:service-cmd``.

        Shared by the fresh-fire path (window just created) and the
        pending-retry path (window survived a failed fire). Returns True
        only on a clean rc=0 exec -- a killed exec (rc=-1 under the
        #2740 load budget) means the send state is UNKNOWN, not OK.
        """
        try:
            rc, _, _ = await self.podman.exec_container(
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
                timeout=SERVICE_EXEC_TIMEOUT,
            )
        except Exception:
            return False
        return rc == 0

    async def ensure_service_session(
        self,
        container_id: str,
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

        The session's HOME is **always** ``/home/klangk`` -- the shared
        home, under both layouts (#2717) -- pinned explicitly via the
        tmux ``-e HOME`` flag: the image's uid-1000 passwd home is
        ``/home`` (the mount point), so the podman-exec default is not
        the shared home. No ``per_handle_home`` branch exists on this
        path. The pin applies only at session creation; a ``service``
        session that already exists (its container outliving a daemon
        upgrade) keeps whatever HOME it was created with for the
        remainder of that container's life.

        The service command fires iff the predicate holds (configured AND
        setup complete) AND the ``service-cmd`` window doesn't already
        exist (exactly-once-per-container). Idempotent: safe to call from
        every ``terminal_start`` (#1033) and the boot path alike -- the
        window-exists check makes it a no-op after the first fire.

        The window-exists -> new-window -> send-keys sequence is serialized
        per container via the registry's service-session lock (#1188): without
        it the boot path and the per-connection path could both pass the
        existence check before either created the window, producing two
        duplicate-named ``service-cmd`` windows (tmux allows duplicate
        names), leaving later ``send-keys -t service:service-cmd`` ambiguous.

        Recovery (#2740): ``Podman.exec_container`` runs ``check=False``, so
        a timed-out exec returns ``rc=-1`` instead of raising -- the old
        ``except Exception`` guards never fired for timeouts, and a killed
        ``send-keys`` was mistaken for success (window exists, command never
        typed -> every later fire suppressed forever). Every step now checks
        rc, and any failure lands in a recoverable state:

        - failed new-window or send-keys: kill the half-created window so
          the next ``terminal_start`` re-runs the whole sequence (#1186's
          cleanup extended to the earlier steps).
        - cleanup kill-window also fails: mark the fire pending on the
          registry; the next call sees the window + pending flag and
          retries ONLY the send into the surviving window.
        - unknown window-exists (killed list-windows): skip this round;
          the next ``terminal_start`` retries the check.
        - cancellation (client WS drop) mid-sequence: mark pending, run
          the cleanup, re-raise. A re-run of an already-typed command is
          possible on the retry path; that is the accepted cost versus
          permanently suppressing the service.
        """
        # Hold the per-container lock across the entire read-modify-write so
        # a concurrent caller (boot vs first terminal_start, owner vs
        # collaborator) cannot interleave: the second waits, then observes
        # the window exists and no-ops. This also bounds the partial-failure
        # window from #1186.
        async with self.registry.get_service_session_lock(container_id):
            await self._ensure_tmux_session(
                container_id, SERVICE_SESSION, user_home=SHARED_HOME
            )
            if not should_fire_service_command(service_command, setup_state):
                return
            exists = await self.service_cmd_window_exists(
                container_id, SERVICE_SESSION
            )
            if exists is None:
                # The check itself failed (killed exec under load): the
                # window state is UNKNOWN. Firing here could duplicate the
                # window and re-send into a running service; skipping keeps
                # the exactly-once invariant and the next terminal_start
                # retries the check (#2740).
                logger.debug(
                    "service-cmd window state unknown for %s; "
                    "deferring fire to next terminal_start",
                    container_id,
                )
                return
            if exists:
                await self._retry_pending_service_send(
                    container_id, service_command
                )
                return
            await self.fire_service_command(container_id, service_command)

    async def _retry_pending_service_send(
        self, container_id: str, service_command: str
    ) -> None:
        """Retry only the send for a half-completed fire (#2740).

        The window exists and the fire flag is pending: a previous fire's
        cleanup failed, so the command was (probably) never typed. Retry
        only the send -- recreating the window would lose the settled
        shell."""
        if not self.registry.service_fire_pending(container_id):
            return
        if await self._send_service_command(container_id, service_command):
            self.registry.clear_service_fire_pending(container_id)
            self.registry.mark_service_started(container_id)
        else:
            logger.warning(
                "Service command retry failed for %s; "
                "will retry on next terminal_start",
                container_id,
            )

    async def _create_service_cmd_window(self, container_id: str) -> bool:
        """The ``tmux new-window`` step of a service fire. ``False`` on
        any failure — an exec killed client-side is indistinguishable
        from a real failure."""
        try:
            rc, _, _ = await self.podman.exec_container(
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
                timeout=SERVICE_EXEC_TIMEOUT,
            )
        except Exception:
            rc = -1
        if rc != 0:
            logger.warning(
                "Failed to create %s window in %s",
                SERVICE_CMD_WINDOW,
                SERVICE_SESSION,
            )
            return False
        return True

    async def _abort_service_fire(
        self, container_id: str, *, note: str
    ) -> None:
        """Mark the fire pending, then clean up the half-fired window; a
        failed cleanup leaves the pending flag so the next fire retries
        the whole sequence (#2740, #1186)."""
        self.registry.mark_service_fire_pending(container_id)
        if await self.kill_service_cmd_window(container_id):
            self.registry.clear_service_fire_pending(container_id)
            return
        logger.warning(
            "Failed to clean up %s window in %s%s",
            SERVICE_CMD_WINDOW,
            SERVICE_SESSION,
            note,
        )

    async def fire_service_command(
        self, container_id: str, service_command: str
    ) -> None:
        """Fresh fire: create the window, send the command; clean up (and
        mark pending) on failure (see ensure_service_session's docstring
        for the recovery states)."""
        # Fresh fire. Any stale pending flag (e.g. the window died some
        # other way) is moot once this sequence runs.
        self.registry.clear_service_fire_pending(container_id)
        try:
            if not await self._create_service_cmd_window(container_id):
                # The exec may have been killed client-side while the
                # container side created the window -- clean it up so
                # the next fire re-runs the whole sequence instead of
                # suppressing on a command-less window (#2740).
                await self._abort_service_fire(container_id, note="")
                return
            # The new window's shell needs a moment to source
            # .profile / .bashrc before it can resolve PATH-dependent
            # commands (nvm, openclaw, ...). Same race as #1030.
            await asyncio.sleep(1)
            if await self._send_service_command(container_id, service_command):
                # The service command just fired -- reset the
                # health-check startup-grace anchor so the monitor
                # gives the service time to boot before a failing poll
                # can flag it unhealthy (e.g. a gateway that isn't
                # accepting connections yet). Only on a clean send:
                # the failure paths never launched the command, so they
                # must not start the grace window.
                self.registry.mark_service_started(container_id)
                return
            logger.warning(
                "Failed to send service command to %s", SERVICE_SESSION
            )
            # The window was created above but the command never
            # landed in it. Kill it so the next fire re-runs the whole
            # sequence instead of no-op'ing forever on the
            # half-created window (#1186). If the cleanup itself
            # fails, leave the fire pending so the next call retries
            # the send into the surviving window (#2740).
            await self._abort_service_fire(
                container_id, note="; fire marked pending"
            )
        except asyncio.CancelledError:
            # The caller (e.g. the _start_terminal task on a client WS
            # drop) went away mid-sequence. The synchronous pending
            # flag lands BEFORE the cleanup await, so even a second
            # cancellation during cleanup leaves a recoverable state:
            # the next terminal_start sees window + pending and retries
            # the send (#2740).
            self.registry.mark_service_fire_pending(container_id)
            with contextlib.suppress(Exception):
                await self.kill_service_cmd_window(container_id)
            raise

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
        """Write a workspace token to ``/tmp/klangk/workspace-token`` inside
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

    # Cold-start failures tmux_command retries: the socket path missing
    # (server still starting in a fresh container) and the target session
    # missing. The latter covers the terminal-start race (#2623): the
    # controller stores its TerminalSession before ``start()`` completes,
    # and ``start()`` only spawns the attach exec — the grouped tmux
    # session (``<user_id>-<uuid>``) is created asynchronously by that
    # exec's ``tmux new-session`` client. A window command addressed to
    # the unique session name (e.g. the client's select-window fired
    # right after terminal_start) can run before the session exists;
    # under load (a 3s podman exec on a busy CI runner) the gap is
    # seconds. tmux's wording is "can't find session: <name>" on modern
    # versions; "no such session" appears on older ones.
    _COLD_START_STDERR = (
        "no such file or directory",
        "can't find session",
        "no such session",
    )

    def _cold_start_retry(
        self, stderr: str, attempt: int, attempts: int
    ) -> bool:
        """A cold-start failure with retry budget left (#2623): the tmux
        socket missing while the server boots in a fresh container, or
        the target session missing while a just-spawned attach is still
        creating it."""
        low = stderr.lower()
        return (
            any(s in low for s in self._COLD_START_STDERR)
            and attempt < attempts - 1
        )

    async def tmux_command(
        self, container_id: str, session_name: str, args: list[str]
    ) -> str:
        """Run a tmux command in the container and return stdout.

        Retries cold-start failures (see ``_COLD_START_STDERR``): the
        tmux socket missing while the server boots in a fresh container,
        or the target session missing while a just-spawned attach is
        still creating it (#2623). Up to 6 attempts, 0.5s apart — the
        observed CI gap was ~1-2s, so 2.5s of retry budget covers it;
        the pre-retry behavior (immediate TerminalError) is unchanged
        once the budget is spent.
        """
        attempts = 6
        for attempt in range(attempts):
            rc, stdout, stderr = await self.podman.exec_container(
                container_id,
                ["tmux", *args],
                user=CONTAINER_USER,
                timeout=10,
            )
            if rc == 0:
                return stdout
            if self._cold_start_retry(stderr, attempt, attempts):
                await asyncio.sleep(0.5)
                continue
            if _container_gone(stderr):
                raise ContainerGoneError(
                    f"container {container_id!r} is gone: {stderr.strip()}"
                )
            raise TerminalError(f"tmux command failed: {stderr.strip()}")
        # Unreachable: every iteration returns (rc==0), retries with
        # attempt < attempts-1, or raises — the arc past the loop never runs.
        return ""  # pragma: no cover — loop always exits inside

    async def list_windows(
        self, container_id: str, session_name: str
    ) -> list[dict]:
        """List tmux windows for a session. Returns [{index, name}, ...]."""
        output = await self.tmux_command(
            container_id,
            session_name,
            ["list-windows", "-t", session_name, "-F", _WINDOW_FMT],
        )
        return parse_windows(output)

    async def new_window(
        self,
        container_id: str,
        session_name: str,
        name: str | None = None,
    ) -> list[dict]:
        """Create a new tmux window and return the updated window list.

        The window is named *name* when given, else ``bash`` (matching
        window 0) instead of a consecutive number (#2179). Window names are
        display-only: tmux permits duplicate names, so duplicates are
        allowed — a user may rename a tab to match another — and nothing in
        klangk keys window identity on the name (#2192). The stable
        identity is the tmux window id (``@N``).

        Uses a single podman exec with a shell script to minimize
        round-trips (create + list in one call). The session name and
        window label are passed as positional argv ($1/$2), never
        interpolated into the script, so shell metacharacters in either
        are harmless (the label is validated above regardless).
        """
        label = name if name is not None else "term"
        if name is not None:
            validate_window_name(name)
        script = (
            'sn="$1"; lbl="$2";'
            ' tmux new-window -t "$sn" -n "$lbl";'
            ' tmux list-windows -t "$sn"'
            f" -F '{_WINDOW_FMT}'"
        )
        argv = ["bash", "-c", script, "bash", session_name, label]
        rc, output, stderr = await self.podman.exec_container(
            container_id,
            argv,
            user=CONTAINER_USER,
            timeout=10,
        )
        if rc != 0:
            raise TerminalError(f"new_window failed: {stderr.strip()}")
        return parse_windows(output)

    async def rename_window(
        self,
        container_id: str,
        session_name: str,
        index: int,
        name: str,
    ) -> None:
        """Rename a tmux window.

        Raises ``ValueError`` if *name* contains unsafe characters.
        Window names are display-only, so duplicate names are permitted —
        a tab may be renamed to match another — and window identity is
        never keyed on the name (#2192).
        """
        validate_window_name(name)
        await self.tmux_command(
            container_id,
            session_name,
            [
                "rename-window",
                "-t",
                _window_target(session_name, index),
                name,
            ],
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

        Always qualifies the target with ``session_name`` so that in a
        session group the command affects only the caller's grouped
        session, not whichever session tmux considers "most recent"
        (#1883).
        """
        await self.tmux_command(
            container_id,
            session_name,
            [
                "select-window",
                "-t",
                _window_target(session_name, target),
            ],
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
        await self.tmux_command(
            container_id,
            session_name,
            [
                "kill-window",
                "-t",
                _window_target(session_name, target, allow_id=True),
            ],
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
        self.podman = podman
        self._master_fd: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._read_event: asyncio.Event | None = None

    async def start(self, argv: list[str], rows: int, cols: int) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            tty.setraw(slave_fd)
            _set_winsize(master_fd, rows, cols)
            self._proc = await asyncio.create_subprocess_exec(
                self.podman.bin,
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

    async def read(self) -> bytes:
        try:
            while True:
                try:
                    return os.read(self._master_fd, _READ_CHUNK)
                except BlockingIOError:
                    self._read_event.clear()
                    await self._read_event.wait()
        except OSError:
            return b""

    async def write(self, data: bytes) -> None:
        try:
            os.write(self._master_fd, data)
        except BlockingIOError:
            # Buffer full — run in executor as fallback so we don't
            # spin.  This is rare; normally the buffer accepts input.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.write, self._master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        _set_winsize(self._master_fd, rows, cols)
        if self._proc is not None:
            os.kill(self._proc.pid, signal.SIGWINCH)

    def _close_master_fd(self) -> None:
        """Tear down the PTY master fd: drop the loop reader, unblock any
        pending read, close the fd."""
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

    def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            self._close_master_fd()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
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
        workspace_name: str | None = None,
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
        self.workspace_name = workspace_name
        self._shell: ShellProcess | None = None
        self._output_queue: BoundedOutputQueue[str] = BoundedOutputQueue(
            maxsize=64
        )
        self._running = False
        self._read_task: asyncio.Task | None = None
        self.tmux_session_name: str | None = None

    @property
    def podman(self):
        """Reach podman via the Terminal instance (#1480)."""
        return self._terminal.podman

    async def _ensure_base_tmux_session(self) -> None:
        """Ensure the base tmux session exists before building a grouped
        session command that targets it.  Only needed for own sessions
        (not joins/shared, which target a different session)."""
        if (
            not self.session_name
            or self.join_session
            or self.socket_path
            or not self._terminal.tmux_enabled()
        ):
            return
        await self._terminal.ensure_base_session(
            self.container_id,
            self.session_name,
            user_home=self.user_home,
            ssh_agent_socket=self.ssh_agent_socket,
            user_id=self.user_id,
            user_handle=self.user_handle,
        )

    async def _refresh_workspace_status_name(self) -> None:
        """Set @workspace_name globally so every grouped session's status
        bar picks it up.  Runs on every terminal_start (idempotent)
        because the base session may have been created by older code
        that didn't set it (#1880)."""
        if self.workspace_name and self._terminal.tmux_enabled():
            await self._terminal.set_workspace_name(
                self.container_id, self.workspace_name
            )

    async def start(
        self,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        """Start a shell session via ``podman exec`` over a PTY."""
        self._running = True
        await self._ensure_base_tmux_session()
        await self._refresh_workspace_status_name()
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
            user_id=self.user_id,
            user_handle=self.user_handle,
        )
        work_dir = "/home"
        argv = _build_exec_argv(self.container_id, env, shell_cmd, work_dir)

        logger.info("Terminal exec argv: %s", argv)
        shell = make_shell_process(self.podman)
        try:
            await shell.start(argv, rows, cols)
        except Exception:
            self._running = False
            raise

        self._shell = shell
        self._read_task = asyncio.create_task(self.read_loop())
        logger.info(
            "Terminal session started for container %s", self.container_id
        )

        # If SSH agent forwarding is active, inject SSH_AUTH_SOCK into
        # the tmux session environment.  This is needed because
        # `tmux new-session -A` reattaches to an existing session and
        # ignores the `-e` flags — the env var must be set explicitly.
        if self.ssh_agent_socket and self.session_name:
            try:
                await self.podman.exec_container(
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
            except OSError as e:
                logger.warning("Failed to set SSH_AUTH_SOCK in tmux: %s", e)

    async def _log_exec_exit_code(self) -> None:
        """Best-effort: log the podman-exec exit code behind the EOF."""
        _p = getattr(self._shell, "_proc", None)
        if _p is None:
            return
        try:
            _rc = await asyncio.wait_for(_p.wait(), timeout=2)
            logger.info(
                "Terminal exec exited rc=%s "
                "(nonzero = tmux/podman error; 0 = clean detach)",
                _rc,
            )
        except Exception:
            pass

    async def _pump_chunk(self, decoder, data: bytes) -> None:
        """Decode one PTY chunk and queue it; output is dropped on
        back-pressure (never block the PTY read)."""
        text = decoder.decode(data)
        if not text:
            return
        try:
            self._output_queue.put_nowait(text)
        except asyncio.QueueFull:
            pass  # drop output; don't block the PTY read

    async def _flush_decoder_tail(self, decoder) -> None:
        """Flush any trailing partial sequence (a stream that ends
        mid-character yields a single replacement char rather than
        dropping bytes)."""
        tail = decoder.decode(b"", final=True)
        if tail:
            await self._output_queue.put(tail)

    async def _read_chunks(self, decoder) -> None:
        """The read loop proper: PTY chunks until EOF/stop."""
        while self._running and self._shell is not None:
            data = await self._shell.read()
            if not data:
                logger.info("Terminal read loop: EOF from PTY")
                await self._log_exec_exit_code()
                break
            await self._pump_chunk(decoder, data)
        await self._flush_decoder_tail(decoder)

    async def read_loop(self) -> None:
        """Read PTY output and queue it as text.

        Uses an *incremental* UTF-8 decoder so a multi-byte glyph (e.g. the
        box-drawing ``─`` = ``e2 94 80``) split across two ``os.read`` chunks is
        buffered and reassembled instead of being mangled into ``U+FFFD``
        replacement chars. Per-chunk ``bytes.decode`` corrupted such glyphs,
        shifting columns and desyncing the terminal cell model (ghosting).
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            await self._read_chunks(decoder)
        except asyncio.CancelledError:
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
                    timeout=_WRITE_TIMEOUT,
                )
            except asyncio.TimeoutError:
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

    def _read_task_done(self) -> bool:
        """True when the PTY read task has finished (nothing more can
        arrive)."""
        return self._read_task is not None and self._read_task.done()

    async def output(self) -> AsyncGenerator[str, None]:
        """Yield terminal output as it arrives."""
        while self._running:
            try:
                data = await asyncio.wait_for(
                    self._output_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                if self._read_task_done():
                    break
                continue
            if data is None:
                break
            yield data

    async def _cancel_read_task(self) -> None:
        """Cancel (and await) the PTY read task."""
        if self._read_task is None:
            return
        self._read_task.cancel()
        try:
            await self._read_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error awaiting terminal read task")
        self._read_task = None

    async def _close_shell(self) -> None:
        """Close the host-side PTY shell."""
        if self._shell is None:
            return
        try:
            self._shell.close()
        except OSError:
            logger.debug("Error closing terminal shell", exc_info=True)
        self._shell = None

    async def _kill_grouped_tmux_session(self) -> None:
        """Kill the tmux session inside the container so the client
        doesn't stay attached after the host-side process is gone.
        All grouped sessions (own, join, shared) are killed — the
        base session persists independently. tmux_session_name is a
        unique grouped name for all connection types."""
        if not self.tmux_session_name:
            return
        try:
            socket_args = ["-S", self.socket_path] if self.socket_path else []
            await self.podman.exec_container(
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

    async def stop(self) -> None:
        """Stop the terminal session and clean up."""
        self._running = False
        await self._cancel_read_task()
        await self._close_shell()
        await self._kill_grouped_tmux_session()
        logger.info(
            "Terminal session stopped for container %s", self.container_id
        )
