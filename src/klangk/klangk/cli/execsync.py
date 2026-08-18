"""Commands: exec (run a command in a workspace), sync (rsync wrapper), images.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import httpx
import typer
from rich.console import Console

from .client import (
    ws_exec,
    WorkspaceNotFoundError,
)
from . import context


@context.app.command(
    "exec",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
def exec_cmd(
    ctx: typer.Context,
    workspace: str = typer.Argument(..., help="Workspace name"),
    raw: bool = typer.Option(
        False,
        "--raw",
        help=(
            "Pass the command as raw argv (no login shell). Defaults off, "
            "so commands run as a bash login shell and source ~/.profile "
            "just like a terminal (#1041). Intended for programmatic "
            "transports such as rsync; not for interactive use."
        ),
    ),
) -> None:
    """Run a command in a workspace container.

    By default the command runs as a bash login shell (``bash -lc``) so
    it sources ``~/.profile`` and sees the same environment an
    interactive terminal does -- PATH additions, tool homes
    (OPENCLAW_HOME, nvm/asdf), etc. (#1041). Pass ``--raw`` to run raw
    argv with no shell (used by ``klangk sync``'s rsync transport).

    Also usable as an rsync transport:
    rsync -avz -e "klangk exec --raw" src/ ws:/dest/
    """
    context.require_auth()

    command = ctx.args
    # With allow_extra_args + allow_interspersed_args=False, Click does
    # NOT consume the ``--`` end-of-options separator -- it lands in
    # ctx.args verbatim (verified), so ``klangk exec ws -- echo hi``
    # would try to run ``--`` as a command. Strip a single leading
    # ``--`` so the conventional separator works. A ``--`` elsewhere is
    # left alone (it is then a real command argument).
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        context._err.print("[red]No command specified[/red]")
        raise typer.Exit(code=1)

    client = context._client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        context._err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None

    surl = context.server_url()
    token = context._state().get_token(surl)

    exit_code = asyncio.run(
        ws_exec(
            surl,
            token,
            ws.id,
            command,
            max_size=context.ws_max_size(),
            login=not raw,
        )
    )
    raise typer.Exit(code=exit_code)


@context.app.command(
    "sync",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
def sync(
    ctx: typer.Context,
    src: str = typer.Argument(
        ..., help="Source (local path or workspace:path)"
    ),
    dest: str = typer.Argument(
        ..., help="Destination (local path or workspace:path)"
    ),
) -> None:
    """Sync files to/from a workspace container via rsync.

    Any extra flags after src and dest are passed directly to rsync.

    Examples:

        klangk sync ~/project my-workspace:/work/project

        klangk sync my-workspace:/work/output ~/output

        klangk sync ~/src ws:/work/src --delete --exclude .git
    """
    context.require_auth()

    klangk_bin = shutil.which("klangk")
    if not klangk_bin:  # pragma: no cover
        context._err.print("[red]Cannot find klangk in PATH[/red]")
        raise typer.Exit(code=1)

    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        context._err.print("[red]Cannot find rsync in PATH[/red]")
        raise typer.Exit(code=1)

    cmd = [
        rsync_bin,
        "-avz",
        "--blocking-io",
        "-e",
        # ``--raw`` so the rsync transport runs raw argv (no login
        # shell): rsync's binary protocol must not be corrupted by a
        # ~/.profile that prints to stdout, and rsync shell-quotes its
        # argv so a non-login round-trips cleanly. See #1041.
        f"{klangk_bin} exec --raw",
        *ctx.args,
        src,
        dest,
    ]
    context._err.print(f"[dim]{' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    raise typer.Exit(code=result.returncode)


@context.app.command()
def images() -> None:
    """List available container images for workspaces."""
    context.require_auth()
    try:
        data = context._client().list_images()
    except httpx.HTTPStatusError as exc:  # pragma: no cover
        detail = exc.response.json().get("detail", exc.response.text)
        context._err.print(f"[red]Failed to list images:[/red] {detail}")
        raise typer.Exit(code=1) from None
    console = Console()
    for img in data["allowed"]:
        prefix = "*" if img == data["default"] else " "
        console.print(f"  {prefix} {img}")


vol_app = typer.Typer(
    name="volumes",
    help="Manage container volumes for workspaces.",
    rich_markup_mode="rich",
)
context.app.add_typer(vol_app, name="volumes")


# --- Admin commands (site-wide admin privilege required) ---
# Grouped under `admin` to separate site-wide management (users,
# invitations, access control) from workspace-scoped commands. Every
# command here hits an endpoint gated by the admin ACL permission
# (acl.has_permission("admin")), so non-admins get a clear 403.
admin_app = typer.Typer(
    name="admin",
    help="Site-wide administration (requires admin privileges).",
    rich_markup_mode="rich",
)
context.app.add_typer(admin_app, name="admin")

# Nested noun subgroups, matching the existing `volumes`/`terminal`
# precedent so `admin --help` stays scannable as `admin <noun> <verb>`.
admin_users_app = typer.Typer(
    name="users", help="Manage user accounts.", rich_markup_mode="rich"
)
admin_app.add_typer(admin_users_app, name="users")

admin_invitations_app = typer.Typer(
    name="invitations",
    help="Manage user invitations.",
    rich_markup_mode="rich",
)
admin_app.add_typer(admin_invitations_app, name="invitations")
