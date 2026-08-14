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
import shlex
import shutil
import subprocess
import tempfile

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


def configure_outer_session(socket: str, session: str) -> list[list[str]]:
    """Make the outer session nearly invisible + pass-through for the inner.

    - ``prefix C-a``: steal a prefix distinct from the inner container tmux's
      ``C-b`` so the outer's popup controls are reachable.
    - ``status off``: no second status bar — only the inner container tmux's
      familiar status bar (with its ``+``) shows.
    - ``mouse off``: the outer does not intercept mouse events, so raw mouse
      sequences pass through to the inner container tmux (its ``+`` etc.).
    """
    return [
        _tmux(socket, "set-option", "-t", session, "prefix", OUTER_PREFIX),
        _tmux(socket, "set-option", "-t", session, "status", "off"),
        _tmux(socket, "set-option", "-t", session, "mouse", "off"),
    ]


def popup_viewer_shell_string(socket: str, hidden: str) -> str:
    """Shell command the popup runs: a client attaching to the hidden session.

    ``env -u TMUX`` unsets TMUX so the nested attach is permitted (the popup
    itself runs inside the outer tmux, where TMUX is set).
    """
    return f"env -u TMUX tmux -S {socket} attach -t {hidden}"


def display_popup_command(socket: str, hidden: str, *, w: int, h: int) -> str:
    """tmux command string that shows the consent popup (hook + binding value).

    Docked bottom-right (``-x``/``-y`` from the session size minus the popup
    size); ``-E`` closes the popup when its command (the viewer attach) exits
    — i.e. when the user hides it with ``q`` (which detaches the viewer).
    """
    viewer = popup_viewer_shell_string(socket, hidden)
    return (
        f"display-popup -E {shlex.quote(viewer)}"
        f" -w {w} -h {h}"
        f" -x #{{e|-:#{{session_width}},{w}}}"
        f" -y #{{e|-:#{{session_height}},{h}}}"
    )


def popup_hook_cmds(
    socket: str, outer: str, hidden: str, *, w: int, h: int
) -> list[list[str]]:
    """Auto-show the popup on attach + bind the reopen key (outer prefix)."""
    cmd = display_popup_command(socket, hidden, w=w, h=h)
    return [
        _tmux(socket, "set-hook", "-t", outer, "client-attached", cmd),
        # bind-key in the prefix table -> <outer-prefix> + REOPEN_KEY.
        _tmux(socket, "bind-key", REOPEN_KEY, cmd),
    ]


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


def _default_run(argv: list[str]) -> int:
    """Run a tmux control command, logging failures (never raising)."""
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError as exc:
        logger.warning(
            "consent-popup tmux command failed: %s (%s)", argv[0], exc
        )
        return 1


def _default_attach(argv: list[str]) -> int:
    """Attach the user's terminal to the outer session (blocks until detach)."""
    return subprocess.call(argv)


def run_consent_shell(
    *,
    workspace_id: str,
    inner_argv: list[str],
    decider_argv: list[str],
    popup_size: tuple[int, int] = DEFAULT_POPUP_SIZE,
    term_size: tuple[int, int] | None = None,
    run=_default_run,
    attach=_default_attach,
) -> int:
    """Bring up the consent-popup russian-doll and attach the user to it.

    *inner_argv* is the normal ``klangk shell`` invocation (re-runs the plain
    attach inside the outer session's window, with the recursion guard so it
    does not re-wrap). *decider_argv* is the ``klangk consent-decide``
    invocation in its persistent popup role. Returns the attach's exit code.

    On any setup failure the outer session may not exist; cleanup is
    best-effort and never raises.
    """
    socket = socket_path(workspace_id)
    outer = outer_session_name(workspace_id)
    hidden = hidden_session_name(workspace_id)
    cols, rows = term_size or _term_size()
    pw, ph = popup_size

    # 1. outer session running the inner (normal) shell.
    run(new_detached_session(socket, outer, inner_argv, x=cols, y=rows))
    # 2. make the outer nearly invisible + inner-friendly.
    for cmd in configure_outer_session(socket, outer):
        run(cmd)
    # 3. hidden decider session (sized to the popup so it doesn't reflow).
    run(new_detached_session(socket, hidden, decider_argv, x=pw, y=ph))
    # 4. auto-show the popup on attach + the C-a p reopen binding.
    for cmd in popup_hook_cmds(socket, outer, hidden, w=pw, h=ph):
        run(cmd)
    # 5. attach the user's terminal to the outer session (blocks).
    rc = attach(attach_cmd(socket, outer))
    # 6. cleanup: reap the hidden decider session (and a lingering outer).
    run(kill_session_cmd(socket, hidden))
    run(kill_session_cmd(socket, outer))
    return rc


def _term_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 80, 24


# Re-exported for the shell command to build the inner/decider invocations.
__all__ = [
    "DEFAULT_POPUP_SIZE",
    "EGRESS_INTERACTIVE",
    "OUTER_PREFIX",
    "REOPEN_KEY",
    "TMUX_MIN_VERSION",
    "attach_cmd",
    "configure_outer_session",
    "display_popup_command",
    "has_session_cmd",
    "hidden_session_name",
    "host_tmux_version",
    "kill_session_cmd",
    "new_detached_session",
    "outer_session_name",
    "parse_tmux_version",
    "popup_hook_cmds",
    "popup_viewer_shell_string",
    "run_consent_shell",
    "should_use_popup",
    "socket_path",
    "tmux_usable",
]
