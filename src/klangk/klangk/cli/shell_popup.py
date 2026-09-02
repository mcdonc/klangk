"""Client-side consent-popup shell wrapper — the "tmux russian-doll" (#2383).

When a user shells into an ``interactive``-egress workspace, ``klangk shell``
wraps the normal shell in a *local* tmux (on the user's machine) that also
hosts the consent decider, and floats the decider over the shell in a
``display-popup``. The shell itself is unchanged — it is the normal
``klangk shell`` (the container's tmux, full machinery) running in the outer
tmux's window.

Layout (one local tmux server, per-workspace socket):

- **outer session** ``klangk-shell-<ws>`` — window 0 runs the inner
  ``klangk shell`` (today's container-tmux shell). Configured to be nearly
  invisible: ``prefix C-a`` (distinct from the inner ``C-b``), ``status off``,
  ``mouse off``. Because the outer tmux is first in the keypath, it consumes
  only ``C-a``; everything else (``C-b``, raw mouse clicks) passes through to
  the inner container tmux — so the inner status-bar ``+`` and window keys
  keep working exactly as today.
- **hidden session** ``klangk-consent-<ws>`` — runs ``klangk consent-decide``
  in its persistent popup role (PR 1). Always registered while you shell.
- **popup viewer** — a ``display-popup`` on the outer tmux that attaches to
  the hidden session, auto-shown on client-attach and reopened with ``C-a p``.
  ``q`` (in the decider) detaches the viewer (hides it; decider stays
  registered); ``Q`` confirms a real quit.

No server change: the inner shell is the normal ``klangk shell`` and the
decider / popup are purely client-side. Stays within ``klangk.cli`` (only
stdlib + third-party + sibling cli modules).

The tmux command sequence is built by pure, unit-tested builders; the
orchestrator runs them through an injectable runner so the sequence is
testable without a live tmux. Interactive tmux behaviour (popup rendering,
hook targeting, sizing) is validated manually.
"""

from __future__ import annotations

import logging
import os
import re
import signal
from collections.abc import Callable
from contextlib import contextmanager
import shlex
import shutil
import subprocess
import tempfile
import uuid

logger = logging.getLogger(__name__)

# display-popup landed in tmux 3.2; below that the wrapper falls back to a
# plain attach.
TMUX_MIN_VERSION = (3, 2)

# Only interactive-egress workspaces hold egress requests for the decider to
# act on; for allow/static egress there is nothing to decide, so no wrapper.
EGRESS_INTERACTIVE = "interactive"

# The outer (local) tmux prefix is deliberately NOT C-b: the inner container
# tmux uses C-b, and the outer must steal a different prefix so its popup
# controls are reachable. C-a is the conventional nested-tmux outer prefix.
OUTER_PREFIX = "C-a"
# Reopen the (hidden) consent popup: <outer-prefix> + this key.
REOPEN_KEY = "p"

# Default consent-popup viewer size (cols x rows). Sized to read the decider
# comfortably without covering the whole shell.
DEFAULT_POPUP_SIZE = (70, 14)


# ---------------------------------------------------------------------------
# tmux detection + naming
# ---------------------------------------------------------------------------


def parse_tmux_version(out: str) -> tuple[int, int] | None:
    """Parse ``tmux -V`` output (e.g. ``"tmux 3.6a"``) -> ``(3, 6)``.

    Returns None when no ``MAJOR.MINOR`` pair is present.
    """
    m = re.search(r"(\d+)\.(\d+)", out)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def host_tmux_version() -> tuple[int, int] | None:
    """The host tmux version, or None if tmux is absent / unparseable."""
    if not shutil.which("tmux"):
        return None
    try:
        proc = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return parse_tmux_version(proc.stdout)


def tmux_usable(version: tuple[int, int] | None) -> bool:
    """True when the host tmux is present and new enough for display-popup."""
    return version is not None and version >= TMUX_MIN_VERSION


def _sanitize(name: str) -> str:
    """Make a tmux-safe session-name fragment (no ``.`` or ``:``)."""
    return re.sub(r"[^A-Za-z0189_-]", "-", name)[:24] or "ws"


def socket_path(workspace_id: str) -> str:
    """Stable per-workspace local tmux socket path.

    Per-user dir under the temp dir so concurrent users don't collide and a
    startup sweep can find leftovers.
    """
    uid = os.getuid() if hasattr(os, "getuid") else 0
    base = os.path.join(tempfile.gettempdir(), f"klangk-shell-{uid}")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        # Fall back to a process-specific path if the shared dir is unwritable;
        # the wrapper still works, just not sweepable across runs.
        base = os.path.join(
            tempfile.gettempdir(), f"klangk-shell-{uid}-{os.getpid()}"
        )
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{_sanitize(workspace_id)}.sock")


def outer_session_name(workspace_id: str) -> str:
    return f"klangk-shell-{_sanitize(workspace_id)}"


def hidden_session_name(workspace_id: str) -> str:
    return f"klangk-consent-{_sanitize(workspace_id)}"


def popup_session_names(workspace_id: str) -> tuple[str, str]:
    """Per-invocation (outer, hidden) session names for the russian-doll.

    The names embed the wrapper pid + a random suffix so each
    ``klangk shell`` invocation gets its OWN outer + hidden pair on the
    shared per-workspace socket. Deterministic per-workspace names made a
    second concurrent shell into the same workspace fail its
    ``new-session`` (duplicate session) and silently attach to the FIRST
    shell's session — showing that shell's window regardless of the
    terminal the user selected (#2692). The caller must use one call's
    result for BOTH the decider argv and ``run_consent_shell`` so the
    names stay paired. The pid lets :func:`sweep_dead_sessions` reap
    sessions whose wrapper died without cleanup (SIGKILL; SIGHUP and
    exceptions are handled in ``run_consent_shell``) — per-invocation
    names are never reused, so orphans would otherwise accumulate
    (#2693 review).
    """
    suffix = f"p{os.getpid()}-{uuid.uuid4().hex[:6]}"
    return (
        f"{outer_session_name(workspace_id)}-{suffix}",
        f"{hidden_session_name(workspace_id)}-{suffix}",
    )


def list_session_names(socket: str) -> list[str] | None:
    """Session names on the socket, or None when no server is running."""
    try:
        proc = subprocess.run(
            _tmux(socket, "list-sessions", "-F", "#{session_name}"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


def session_pid_is_dead(name: str, alive=os.path.exists) -> int | None:
    """The pid embedded in *name* when its process is dead, else None.

    No pid in the name (old sessions, foreign sessions) → None (left
    alone). This wrapper's own pid and live pids are left alone too.
    """
    m = re.search(r"-p(\d+)-[0-9a-f]+$", name)
    if not m:
        return None
    pid = int(m.group(1))
    if pid == os.getpid() or alive(f"/proc/{pid}"):
        return None
    return pid


def sweep_dead_sessions(
    workspace_id: str, run=None, alive=os.path.exists
) -> int:
    """Kill wrapper sessions whose owning process is dead. Returns count.

    Session names embed the wrapper pid (``...-p<pid>-<rand>``). A session
    whose ``/proc/<pid>`` is absent was left by a SIGKILLed wrapper (every
    softer death path runs cleanup in ``run_consent_shell``) and is safe
    to reap — it can never be reattached, and the hidden decider inside
    it would keep its consent WebSocket reconnecting forever. Called at
    wrapper startup, BEFORE this invocation's own sessions exist, so a
    live wrapper's sessions are never at risk. No pid in the name (old
    sessions, foreign sessions) → left alone. Best-effort: never raises.
    """
    socket = socket_path(workspace_id)
    runner = run or default_run
    names = list_session_names(socket)
    if names is None:
        return 0
    reaped = 0
    for name in names:
        if session_pid_is_dead(name, alive) is not None:
            runner(kill_session_cmd(socket, name), quiet=True)
            reaped += 1
    return reaped


# ---------------------------------------------------------------------------
# pure tmux command builders
# ---------------------------------------------------------------------------


def _tmux(socket: str, *args: str) -> list[str]:
    return ["tmux", "-S", socket, *args]


def new_detached_session(
    socket: str, session: str, argv: list[str], *, x: int, y: int
) -> list[str]:
    """``tmux new-session -d`` running *argv* at the given size."""
    return _tmux(
        socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-x",
        str(x),
        "-y",
        str(y),
        *argv,
    )


def configure_hidden_session(socket: str, session: str) -> list[list[str]]:
    """Hide the hidden session's status bar so the popup shows only the decider.

    Without this the consent popup renders the decider PLUS a tmux status bar
    across its bottom (#2383).
    """
    return [_tmux(socket, "set-option", "-t", session, "status", "off")]


def configure_outer_session(socket: str, session: str) -> list[list[str]]:
    """Make the outer session nearly invisible + pass-through for the inner.

    - ``prefix C-a``: steal a prefix distinct from the inner container tmux's
      ``C-b`` so the outer's popup controls are reachable.
    - ``status off``: no second status bar — only the inner container tmux's
      familiar status bar (with its ``+``) shows.
    - ``mouse off``: the outer does not intercept mouse events, so raw mouse
      sequences pass through to the inner container tmux (its ``+`` etc.).
    - ``set-clipboard on`` + the ``clipboard`` terminal feature: the inner
      shell (the container tmux's attach client) writes OSC 52 (clipboard
      set) into its pane when a selection is made in the container tmux
      (#2694); the outer re-emits it to the real terminal. This MUST be
      ``on``, not ``external`` — tmux only forwards pane-originated OSC 52
      to its own clients in ``on`` mode (verified against tmux 3.5a/3.6a;
      ``external`` drops pane sequences silently). The clipboard feature is
      set for every TERM because stock terminfo lacks the Ms capability;
      terminals that can't do OSC 52 just ignore the sequence. Both are set
      explicitly (they are defaults-adjacent) to survive a user's
      ``~/.tmux.conf`` on this socket's server.
    """
    return [
        _tmux(socket, "set-option", "-t", session, "prefix", OUTER_PREFIX),
        _tmux(socket, "set-option", "-t", session, "status", "off"),
        _tmux(socket, "set-option", "-t", session, "mouse", "off"),
        _tmux(socket, "set-option", "-g", "set-clipboard", "on"),
        _tmux(
            socket, "set-option", "-ga", "terminal-features", ",*:clipboard"
        ),
    ]


def popup_viewer_shell_string(socket: str, hidden: str) -> str:
    """Shell command the popup runs: a client attaching to the hidden session.

    ``env -u TMUX`` unsets TMUX so the nested attach is permitted (the popup
    itself runs inside the outer tmux, where TMUX is set).
    """
    return f"env -u TMUX tmux -S {socket} attach -t {hidden}"


def display_popup_command(socket: str, hidden: str, *, w: int, h: int) -> str:
    """tmux command string that shows the consent popup (hook + binding value).

    ``-E`` closes the popup when its command (the viewer attach) exits — i.e.
    when the user hides it with ``q`` (which detaches the viewer). The popup
    uses tmux's default centered position; an earlier bottom-right ``-x/-y``
    docking was dropped — the ``#{e|-:...}`` arithmetic value made tmux
    reject the command ("-x expects an argument").

    The shell-command MUST be the final positional: tmux's ``display-popup``
    treats it as absorbing every trailing token, so all options (``-w -h``)
    have to come before it or they get swallowed into the command and the
    popup renders blank.
    """
    viewer = popup_viewer_shell_string(socket, hidden)
    return f"display-popup -E -w {w} -h {h} {shlex.quote(viewer)}"


def popup_binding_cmds(
    socket: str, hidden: str, *, w: int, h: int
) -> list[list[str]]:
    """The ``<outer-prefix> p`` reopen binding.

    No auto-show hook: the popup is shown only when the decider receives a
    held request (it runs :func:`show_popup_argv` itself), so the user is not
    bothered at shell startup when there is nothing to decide.
    """
    cmd = display_popup_command(socket, hidden, w=w, h=h)
    # bind-key in the prefix table -> <outer-prefix> + REOPEN_KEY.
    return [_tmux(socket, "bind-key", REOPEN_KEY, cmd)]


def outer_clients(socket: str, hidden: str) -> list[str]:
    """tmux clients attached to a session other than the hidden one.

    These are the user's shell client(s) to show the popup on. The hidden
    session's own viewer client (when the popup is open) is excluded.
    """
    try:
        proc = subprocess.run(
            _tmux(
                socket,
                "list-clients",
                "-F",
                "#{client_name}\t#{client_session}",
            ),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    clients: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] != hidden:
            clients.append(parts[0])
    return clients


def hidden_has_client(socket: str, hidden: str) -> bool:
    """True when the hidden session has a viewer client attached (popup open)."""
    try:
        proc = subprocess.run(
            _tmux(socket, "list-clients", "-t", hidden),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(proc.stdout.strip())


def show_popup_argv(
    socket: str, hidden: str, client: str, *, w: int, h: int
) -> list[str]:
    """Argv to show the popup on a specific client (the decider's show path).

    Unlike :func:`display_popup_command` (a string for the key binding, which
    targets the invoking client), this is run server-side by the decider when a
    held request arrives, so it names the target client explicitly with ``-c``.
    """
    viewer = popup_viewer_shell_string(socket, hidden)
    return _tmux(
        socket,
        "display-popup",
        "-c",
        client,
        "-E",
        "-w",
        str(w),
        "-h",
        str(h),
        viewer,
    )


def attach_cmd(socket: str, session: str) -> list[str]:
    return _tmux(socket, "attach", "-t", session)


def kill_session_cmd(socket: str, session: str) -> list[str]:
    return _tmux(socket, "kill-session", "-t", session)


def has_session_cmd(socket: str, session: str) -> list[str]:
    return _tmux(socket, "has-session", "-t", session)


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def should_use_popup(
    egress_mode: str | None,
    *,
    isatty: bool,
    tmux_version: tuple[int, int] | None,
    enabled: bool = True,
) -> bool:
    """True when ``klangk shell`` should wrap in the consent-popup russian-doll.

    All four must hold, else today's plain attach:
    - ``enabled``: not opted out / not the inner re-invocation (recursion guard).
    - interactive egress (nothing to decide otherwise).
    - a real tty (the wrapper attaches interactively).
    - host tmux new enough for display-popup (>= 3.2).
    """
    if not enabled:
        return False
    if egress_mode != EGRESS_INTERACTIVE:
        return False
    if not isatty:
        return False
    return tmux_usable(tmux_version)


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def default_run(argv: list[str], quiet: bool = False) -> int:
    """Run a tmux control command, logging failures (never raising).

    Output is captured, never shown: post-exit cleanup commands
    (kill-session on a server that already terminated when its last
    session died) would spray tmux's ``no server running on <sock>``
    stderr into the terminal, reading like a crash instead of a clean
    disconnect (#2685 follow-up). Cleanup callers pass ``quiet=True`` —
    their failures are expected and logged at debug (the CLI configures
    no logging, so debug is effectively silent). Setup-step failures stay
    at warning: they are real failures the user must be able to see (a
    failed outer ``new-session`` is exactly the #2692 failure class).
    """
    try:
        proc = subprocess.run(
            argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        logger.warning(
            "consent-popup tmux command failed: %s (%s)", argv[0], exc
        )
        return 1
    if proc.returncode != 0:
        log = logger.debug if quiet else logger.warning
        log(
            "consent-popup tmux command failed (rc=%d): %s %s",
            proc.returncode,
            argv,
            proc.stderr.decode(errors="replace").strip(),
        )
    return proc.returncode


def default_attach(argv: list[str]) -> int:
    """Attach the user's terminal to the outer session (blocks until detach)."""
    return subprocess.call(argv)


def run_all(run, cmds) -> None:
    """Run each tmux command built by a configure/binding helper."""
    for cmd in cmds:
        run(cmd)


def run_consent_shell(
    *,
    workspace_id: str,
    inner_argv: list[str],
    decider_argv: list[str],
    popup_size: tuple[int, int] = DEFAULT_POPUP_SIZE,
    term_size: tuple[int, int] | None = None,
    run=default_run,
    attach=default_attach,
    session_names: tuple[str, str] | None = None,
) -> int:
    """Bring up the consent-popup russian-doll and attach the user to it.

    *inner_argv* is the normal ``klangk shell`` invocation (re-runs the plain
    attach inside the outer session's window, with the recursion guard so it
    does not re-wrap). *decider_argv* is the ``klangk consent-decide``
    invocation in its persistent popup role. Returns the attach's exit code.

    *session_names* is the per-invocation ``(outer, hidden)`` pair from
    :func:`popup_session_names`; when omitted it is generated here. Callers
    that build the decider argv from the hidden session name MUST pass the
    same pair so the decider and the wrapper agree (#2692).

    On any setup failure the outer session may not exist; cleanup is
    best-effort and never raises.
    """
    socket = socket_path(workspace_id)
    # Reap sessions left by wrappers that died without cleanup (SIGKILL);
    # runs before this invocation's own sessions exist (#2693 review).
    sweep_dead_sessions(workspace_id)
    outer, hidden = session_names or popup_session_names(workspace_id)
    cols, rows = term_size or _term_size()
    pw, ph = popup_size

    def cleanup() -> None:
        # Best-effort reap of this invocation's sessions. quiet=True: the
        # tmux server may already be gone (it exits with its last session),
        # and post-exit failure noise read like a crash (#2685 follow-up).
        run(kill_session_cmd(socket, hidden), quiet=True)
        run(kill_session_cmd(socket, outer), quiet=True)

    # 1. outer session running the inner (normal) shell.
    run(new_detached_session(socket, outer, inner_argv, x=cols, y=rows))
    # 2. make the outer nearly invisible + inner-friendly.
    run_all(run, configure_outer_session(socket, outer))
    # 3. hidden decider session (sized to the popup so it doesn't reflow).
    run(new_detached_session(socket, hidden, decider_argv, x=pw, y=ph))
    # 3b. hide the hidden session's status bar so the popup shows only the
    #     decider (no tmux status bar across the popup's bottom).
    run_all(run, configure_hidden_session(socket, hidden))
    # 4. the C-a p reopen binding (no auto-show — the decider shows the popup
    #    itself when a held request arrives, so the shell isn't bothered at
    #    startup when there's nothing to decide).
    run_all(run, popup_binding_cmds(socket, hidden, w=pw, h=ph))
    # 5. attach the user's terminal to the outer session (blocks). Cleanup
    #    runs even when the attach dies abnormally — the terminal window
    #    being closed delivers SIGHUP mid-attach, and an unhandled
    #    KeyboardInterrupt/OSError must not strand the detached sessions
    #    (per-invocation names are never reused, so leaks accumulate, #2693
    #    review).
    with cleanup_on_signal(cleanup):
        try:
            rc = attach(attach_cmd(socket, outer))
        except BaseException:
            cleanup()
            raise
    # 6. cleanup (normal path): reap the hidden decider session (and a
    #    lingering outer).
    cleanup()
    return rc


def _term_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
        # A pty can report 0x0 (no TIOCSWINSZ ever applied); tmux rejects
        # ``new-session -x 0 -y 0`` outright, so fall back to the default.
        if size.columns >= 1 and size.lines >= 1:
            return size.columns, size.lines
    except OSError:
        pass
    return 80, 24


@contextmanager
def cleanup_on_signal(cleanup: Callable[[], None]):
    """Run *cleanup* if SIGHUP arrives inside the block.

    The wrapper's attach blocks in ``tmux attach``; closing the terminal
    window delivers SIGHUP to the process group. tmux dies with it, but the
    *detached* outer/hidden sessions survive — and with per-invocation names
    they are never reused, so each window-close would strand a tmux server,
    two sessions, and the decider's consent WebSocket forever (#2693
    review). The handler reaps them, then re-raises the default SIGHUP
    behavior (process death) so the exit status stays truthful.
    """
    prev = signal.getsignal(signal.SIGHUP)

    def on_hup(signum, frame):
        try:
            cleanup()
        finally:
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGHUP)

    try:
        signal.signal(signal.SIGHUP, on_hup)
        yield
    finally:
        signal.signal(signal.SIGHUP, prev)


# Re-exported for the shell command to build the inner/decider invocations.
__all__ = [
    "DEFAULT_POPUP_SIZE",
    "EGRESS_INTERACTIVE",
    "OUTER_PREFIX",
    "REOPEN_KEY",
    "TMUX_MIN_VERSION",
    "attach_cmd",
    "configure_hidden_session",
    "configure_outer_session",
    "display_popup_command",
    "has_session_cmd",
    "hidden_has_client",
    "hidden_session_name",
    "host_tmux_version",
    "kill_session_cmd",
    "new_detached_session",
    "outer_clients",
    "outer_session_name",
    "parse_tmux_version",
    "popup_binding_cmds",
    "popup_session_names",
    "popup_viewer_shell_string",
    "run_consent_shell",
    "should_use_popup",
    "sweep_dead_sessions",
    "show_popup_argv",
    "socket_path",
    "tmux_usable",
]
