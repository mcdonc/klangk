"""Terminal commands: terminal ls, window share/unshare, workspace share/unshare.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import json

import typer
from rich.table import Table

from .client import (
    is_container_ready_event,
    recv_until,
    get_terminal_size,
    send_ignore_closed,
    workspace_ws,
    WorkspaceNotFoundError,
)
from . import context
from .sandboxcmd import resolve_workspace_and_url


async def recv_until_event(conn, timeout: float, on_message=None):
    """Wait for the post-``ui_ready`` container_ready event frame.

    Thin wrapper over :func:`client.recv_until` adding the optional
    side-channel callback the ``terminal ls`` path needs (capturing
    shared_terminals frames that arrive before the ready event).
    """
    if on_message is None:
        return await recv_until(conn, is_container_ready_event, timeout)

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:  # pragma: no cover
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
        msg = json.loads(raw)
        on_message(msg)
        if is_container_ready_event(msg):
            return msg


def frame_is(frame_type: str):
    """Predicate for recv_until that surfaces server errors immediately.

    A bare ``lambda m: m.get("type") == t`` waits out the whole timeout
    when the server answers with an ``error`` frame instead — a 10s hang
    and a cryptic traceback instead of the server's reason (the #1966
    lesson, applied to the terminal command paths after the #2633 CI
    flake: ``terminal share`` blind-timed-out over an ignored
    "Window not found"). Raises ConnectionError carrying the server's
    message so the caller exits fast and loudly.
    """

    def predicate(msg) -> bool:
        if msg.get("type") == "error":
            raise ConnectionError(msg.get("message", "terminal error"))
        return msg.get("type") == frame_type

    return predicate


# Seconds to wait for the shared_terminals confirmation frame after a
# share_window/unshare_window command. A server that silently drops the
# command (no error frame) surfaces as a timeout here rather than a
# hang (#2876); module-level so tests can shrink it.
CONFIRM_TIMEOUT = 10


terminal_app = typer.Typer(
    name="terminal",
    help="Manage workspace terminals.",
    rich_markup_mode="rich",
)
context.app.add_typer(terminal_app, name="terminal")


@terminal_app.command("ls")
def terminals(
    workspace: str = typer.Argument(help="Workspace name"),
) -> None:
    """List all terminals (own + shared) in a workspace."""
    ws, sspec, token = resolve_workspace_and_url(workspace)
    max_size = context.ws_max_size()

    # We need to start a terminal to get the window list, then also
    # get shared terminals. Use _ws_command to get each.
    async def _list() -> None:
        async with workspace_ws(
            sspec, token, ws.id, max_size=max_size
        ) as conn:
            await conn.send(json.dumps({"cmd": "ui_ready"}))

            # Wait for container_ready, collecting shared_terminals along
            # the way (sent during ui_ready).
            shared: list[dict] = []

            def _capture_shared(m):
                if m.get("type") == "shared_terminals":
                    shared[:] = m.get("terminals", [])

            await recv_until_event(conn, 60, on_message=_capture_shared)

            # Start terminal to get own windows.
            # terminal_windows arrives after terminal_started — skip
            # terminal_output and other messages until we get it.
            cols, rows = get_terminal_size()
            await conn.send(
                json.dumps(
                    {"cmd": "terminal_start", "cols": cols, "rows": rows}
                )
            )
            msg = await recv_until(conn, frame_is("terminal_windows"), 30)
            own_windows: list[dict] = msg.get("windows", [])

            # Print results
            table = Table(title=f"Terminals in {ws.name}")
            table.add_column("ID")
            table.add_column("Name")
            table.add_column("Type")
            table.add_column("Owner")
            for w in own_windows:
                table.add_row(w.get("id", ""), w["name"], "own", "")
            for t in shared:
                table.add_row(
                    t.get("window_id", ""),
                    t["window_name"],
                    "shared",
                    t.get("handle", ""),
                )
            context.err.print(table)

            await send_ignore_closed(
                conn, json.dumps({"cmd": "terminal_stop"})
            )

    context.run_ws_command(_list)


_VALID_ROLES = ["owner", "coder", "collaborator", "spectator"]
_ROLE_TO_GROUP = {
    "owner": "owners",
    "coder": "coders",
    "collaborator": "collaborators",
    "spectator": "spectators",
}


@context.app.command("share")
def share_workspace(
    workspace: str = typer.Argument(help="Workspace name"),
    email: str = typer.Argument(help="Email or handle of user to add"),
    role: str = typer.Option(
        "coder", help="Role: owner, coder, collaborator, or spectator"
    ),
) -> None:
    """Share a workspace with a user."""
    context.require_auth()
    if role not in _VALID_ROLES:
        context.err.print(
            f"[red]Invalid role '{role}'[/red]."
            f" Choose from: {', '.join(_VALID_ROLES)}"
        )
        raise typer.Exit(code=1)
    group_suffix = _ROLE_TO_GROUP[role]
    try:
        result = context.client().add_workspace_member(
            workspace, email, role=group_suffix
        )
    except WorkspaceNotFoundError:
        context.err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Shared workspace {workspace} with {result['email']} as {role}"
    )


@context.app.command("unshare")
def unshare_workspace(
    workspace: str = typer.Argument(help="Workspace name"),
    email: str = typer.Argument(help="Email or handle of user to remove"),
) -> None:
    """Remove a user's access to a workspace."""
    context.require_auth()
    try:
        context.client().remove_workspace_member(workspace, email)
    except WorkspaceNotFoundError as e:
        context.err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    typer.echo(f"Removed {email} from workspace {workspace}")


def resolve_own_window(
    own_windows: list[dict], terminal: str
) -> tuple[dict | None, str | None]:
    """Resolve a terminal reference to one own window.

    *terminal* is an ``@N`` window id (exact) or a name. Names are not
    unique (#2192): a name matching several windows is an error rather
    than a silent first match. Returns ``(match, error)`` — exactly one
    is set.
    """
    if terminal.startswith("@"):
        match = next((w for w in own_windows if w.get("id") == terminal), None)
        if match is None:
            return None, f"Window '{terminal}' no longer exists"
        return match, None
    return _resolve_window_by_name(own_windows, terminal)


def _resolve_window_by_name(
    own_windows: list[dict], terminal: str
) -> tuple[dict | None, str | None]:
    """A name reference: names are not unique (#2192), so several matches
    are an error rather than a silent first match."""
    name_matches = [w for w in own_windows if w.get("name") == terminal]
    if len(name_matches) > 1:
        ids = ", ".join(w["id"] for w in name_matches if w.get("id"))
        return None, (
            f"Multiple terminals named '{terminal}'; specify one by id: {ids}"
        )
    if not name_matches:
        return None, f"Terminal '{terminal}' not found"
    return name_matches[0], None


@terminal_app.command("share")
def share_terminal(
    workspace: str = typer.Argument(help="Workspace name"),
    terminal: str = typer.Argument(
        help="Terminal to share: @N (exact id) or name (see `klangk terminal ls`)"
    ),
) -> None:
    """Share a terminal with other workspace members."""
    _set_terminal_shared(
        workspace,
        terminal,
        cmd="share_window",
        done_msg=f"Terminal '{terminal}' is now shared",
    )


@terminal_app.command("unshare")
def unshare_terminal(
    workspace: str = typer.Argument(help="Workspace name"),
    terminal: str = typer.Argument(
        help="Terminal to unshare: @N (exact id) or name (see `klangk terminal ls`)"
    ),
) -> None:
    """Stop sharing a terminal."""
    _set_terminal_shared(
        workspace,
        terminal,
        cmd="unshare_window",
        done_msg=f"Terminal '{terminal}' is no longer shared",
    )


def _set_terminal_shared(
    workspace: str, terminal: str, *, cmd: str, done_msg: str
) -> None:
    """Shared body of ``klangk terminal share`` / ``unshare``.

    Connects, starts a scratch terminal to enumerate the own-window
    list, resolves *terminal* to one window, sends *cmd* (``share_window``
    or ``unshare_window``) for it, waits for the refreshed
    shared-terminals list, then stops the scratch terminal.
    """
    ws, sspec, token = resolve_workspace_and_url(workspace)
    max_size = context.ws_max_size()

    async def run() -> None:
        async with workspace_ws(
            sspec, token, ws.id, max_size=max_size
        ) as conn:
            await conn.send(json.dumps({"cmd": "ui_ready"}))

            # Wait for container_ready
            await recv_until_event(conn, 60)

            # Start terminal to get window list
            cols, rows = get_terminal_size()
            await conn.send(
                json.dumps(
                    {"cmd": "terminal_start", "cols": cols, "rows": rows}
                )
            )
            msg = await recv_until(conn, frame_is("terminal_windows"), 30)
            match, err = resolve_own_window(msg.get("windows", []), terminal)
            if err is not None:
                context.err.print(f"[red]{err}[/red]")
                raise typer.Exit(code=1)

            await conn.send(json.dumps({"cmd": cmd, "window_id": match["id"]}))
            # Wait for shared_terminals confirmation
            await recv_until(
                conn, frame_is("shared_terminals"), CONFIRM_TIMEOUT
            )
            context.err.print(f"[green]{done_msg}[/green]")

            await send_ignore_closed(
                conn, json.dumps({"cmd": "terminal_stop"})
            )

    context.run_ws_command(run)
