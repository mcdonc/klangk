"""The `klangk shell` command and its agent-forwarding / consent-popup helpers.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import os
import sys

import typer
import websockets

from .client import (
    drain_stdin,
    reset_terminal,
    ws_shell,
)
from . import context
from .shell_popup import (
    EGRESS_INTERACTIVE,
    OUTER_PREFIX,
    REOPEN_KEY,
    host_tmux_version,
    popup_session_names,
    run_consent_shell,
    should_use_popup,
    socket_path,
)


def resolve_forward_agent(
    forward_agent: bool | None,
    config_default: bool = False,
) -> bool:
    """Resolve forward_agent: CLI flag wins, then config file default.

    *forward_agent* is the CLI flag (True/False/None).  None means the
    user did not pass ``--forward-agent`` or ``--no-forward-agent``.
    """
    if forward_agent is not None:
        result = forward_agent
    else:
        result = config_default
    if result:
        if not os.environ.get("SSH_AUTH_SOCK"):
            context.err.print(
                "[yellow]Warning: forward-agent is enabled but SSH_AUTH_SOCK"
                " is not set. Agent forwarding will be skipped.[/yellow]"
            )
    return result


# ---------------------------------------------------------------------------
# Consent-popup shell wrapper (the "tmux russian-doll", #2383)
# ---------------------------------------------------------------------------


def klangk_argv(*args: str) -> list[str]:
    """Argv re-invoking this klangk CLI (same interpreter + module)."""
    return [sys.executable, "-m", "klangk.cli.main", *args]


def popup_inner_shell_argv(
    server: str, ws_name: str, target: str | None, forward_agent: bool
) -> list[str]:
    """The normal ``klangk shell`` that runs inside the outer tmux window.

    Adds ``--no-consent-popup`` as a recursion guard so the inner does the
    plain attach instead of re-wrapping.
    """
    argv = klangk_argv("--server", server, "shell", ws_name)
    if target:
        argv.append(target)
    argv.append("--no-consent-popup")
    argv.append("--forward-agent" if forward_agent else "--no-forward-agent")
    return argv


def popup_decider_argv(
    server: str, ws_arg: str, socket: str, hidden: str
) -> list[str]:
    """The ``klangk consent-decide`` invocation for the hidden decider session."""
    return klangk_argv(
        "--server",
        server,
        "consent-decide",
        ws_arg,
        "--popup-socket",
        socket,
        "--popup-session",
        hidden,
    )


def consent_popup_enabled(ws, no_consent_popup: bool) -> bool:
    """True when ``klangk shell`` should wrap in the consent-popup russian-doll."""
    # Cheap checks first so non-interactive / non-tty contexts (incl. the test
    # suite, which calls shell() directly) never spawn `tmux -V`.
    if (
        no_consent_popup
        or ws.egress_mode != EGRESS_INTERACTIVE
        or not sys.stdin.isatty()
    ):
        return False
    return should_use_popup(
        ws.egress_mode,
        isatty=True,
        tmux_version=host_tmux_version(),
    )


def run_consent_popup(ws, terminal: str | None, forward_agent: bool) -> int:
    """Bring up the consent-popup russian-doll + attach the user to it (#2383)."""
    server = context.server_url()
    socket = socket_path(ws.id)
    # One per-invocation (outer, hidden) pair shared by the wrapper and the
    # decider argv — deterministic per-workspace names made a concurrent
    # second shell attach to the FIRST shell's session (#2692).
    names = popup_session_names(ws.id)
    inner = popup_inner_shell_argv(server, ws.name, terminal, forward_agent)
    decider = popup_decider_argv(server, ws.name, socket, names[1])
    context.err.print(
        f"Connecting to [bold]{ws.name}[/bold] with consent popup…"
    )
    context.err.print(
        f"[dim]A consent popup appears when an egress request is held; "
        f"{OUTER_PREFIX} {REOPEN_KEY} reopens  ·  q/Q hides  ·  "
        f"Enter, then ~. exits[/dim]"
    )
    rc = run_consent_shell(
        workspace_id=ws.id,
        inner_argv=inner,
        decider_argv=decider,
        session_names=names,
    )
    context.err.print(f"Disconnected from [bold]{ws.name}[/bold].")
    return rc


def resolve_shell_workspace(client, workspace: str | None):
    """Resolve the shell target: the named workspace, or an interactive
    pick (auto-select when exactly one exists)."""
    if workspace:
        return context.resolve_or_exit(client, workspace)
    workspaces = client.list_workspaces(all_pages=True)
    if not workspaces:
        typer.echo("No workspaces found — create one with klangk create.")
        raise typer.Exit(code=1)
    if len(workspaces) == 1:
        return workspaces[0]
    typer.echo("Select a workspace:")
    for i, w in enumerate(workspaces, 1):
        typer.echo(f"  {i}. {w.name}")
    choice = input("> ").strip()
    if not choice:  # pragma: no cover
        raise typer.Exit()
    try:
        idx = int(choice) - 1
    except ValueError:  # pragma: no cover
        raise typer.Exit(code=1)  # pragma: no cover
    return workspaces[idx]


@context.app.command()
def shell(
    workspace: str | None = typer.Argument(
        None, help="Workspace name (or select interactively)"
    ),
    terminal: str | None = typer.Argument(
        None,
        help=(
            "Terminal to select: @N (exact window id), a name, or "
            "handle:@N / handle:name for a shared terminal. Names may "
            "duplicate; use @N to disambiguate (see `klangk terminal ls`)."
        ),
    ),
    forward_agent: bool | None = typer.Option(
        None,
        "--forward-agent/--no-forward-agent",
        "-A",
        help="Forward local SSH agent into the container",
    ),
    no_consent_popup: bool = typer.Option(
        False,
        "--no-consent-popup",
        hidden=True,
        help=(
            "Internal: skip the consent-popup shell wrapper. Set by the "
            "wrapper itself when it re-runs the shell inside the outer tmux "
            "(#2383)."
        ),
    ),
) -> None:
    """Connect to a workspace shell."""
    # When called directly (not via typer CLI), forward_agent may be a
    # typer.models.OptionInfo instead of bool/None.  Normalize to None.
    if not isinstance(forward_agent, bool):
        forward_agent = None
    if not isinstance(no_consent_popup, bool):
        no_consent_popup = False
    token = context.session_token()
    if not token:  # pragma: no cover
        context.err.print(
            "[red]Not logged in[/red] — run [bold]klangk login[/bold] first."
        )  # pragma: no cover
        raise typer.Exit(code=1)  # pragma: no cover

    client = context.client()

    # Resolve workspace
    ws = resolve_shell_workspace(client, workspace)

    context.err.print(f"Connecting to [bold]{ws.name}[/bold]...")
    context.err.print(
        "[dim]Exit this shell: press Enter, then ~. (like ssh).[/dim]"
    )
    forward_agent = resolve_forward_agent(
        forward_agent,
        config_default=context.cfg().get_forward_agent(context.server_url())
        or False,
    )
    if consent_popup_enabled(ws, no_consent_popup):
        # Wrap the normal shell in the consent-popup russian-doll (#2383).
        # Falls back to the plain attach below when tmux is prevented, opted
        # out (--no-consent-popup / the inner re-invocation), or the workspace
        # is not interactive-egress.
        raise typer.Exit(code=run_consent_popup(ws, terminal, forward_agent))
    try:
        asyncio.run(
            ws_shell(
                context.server_url(),
                token,
                ws.id,
                window=terminal,
                forward_agent=forward_agent,
                max_size=context.ws_max_size(),
            )
        )
        context.err.print(f"Disconnected from [bold]{ws.name}[/bold].")
    except websockets.InvalidStatus as e:
        reset_terminal()
        drain_stdin()
        if e.response.status_code in (4001, 4002):
            context.err.print(
                "[red]Session expired. Run `klangk login`"
                " to re-authenticate.[/red]"
            )
        else:
            context.err.print(f"[red]Connection rejected: {e}[/red]")
        raise typer.Exit(code=1) from None
    except ConnectionError as e:
        reset_terminal()
        drain_stdin()
        context.err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
