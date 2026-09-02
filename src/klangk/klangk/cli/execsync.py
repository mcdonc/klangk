"""Commands: exec (run a command in a workspace), sync (rsync wrapper), images.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import httpx
import typer
from rich.console import Console

from .client import (
    WorkspaceNotFoundError,
    ws_exec,
)
from . import context


def remote_host(spec: str) -> str | None:
    """The remote host when *spec* is a ``host:path`` spec, else None.

    Mirrors rsync's own remote-path rule: a colon before any slash makes
    the text before it the remote host (reached via the ``-e``
    transport). A colon after a slash (``./a:b``) is a plain local
    filename. Either side being remote means the sync rides the exec
    channel of that workspace (#2706).
    """
    m = re.match(r"^([^/:]+):", spec)
    return m.group(1) if m else None


def sync_denied(client, host: str) -> bool:
    """True when *host* is a workspace the user may not exec against.

    #2706/#2712: ``klangk sync`` rides the one-shot exec channel (its
    transport is ``klangk exec --raw``), which is gated server-side on
    the ``exec-and-sync`` permission — so a denied member cannot sync in
    either direction. This preflight exists only so the CLI can fail fast
    with a clear message instead of rsync's transport-error noise.
    Unknown hosts, API failures, and missing data return False — rsync
    or the server reports those.
    """
    try:
        ws = client.resolve_workspace(host)
    except (WorkspaceNotFoundError, httpx.HTTPError):
        return False
    resource = f"/workspaces/{ws.id}"
    try:
        resp = client.get(
            "/api/v1/my-permissions", params={"resource": resource}
        )
        perms = resp.json().get("permissions", {}).get(resource)
    except (httpx.HTTPError, ValueError):
        # ValueError: a non-JSON error body (e.g. an HTML 500 page from
        # a proxy in front of klangkd) must not crash the preflight.
        return False
    if perms is None:
        return False
    return "exec-and-sync" not in perms


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
        context.err.print("[red]No command specified[/red]")
        raise typer.Exit(code=1)

    client = context.client()
    ws = context.resolve_or_exit(client, workspace)

    surl = context.server_url()
    token = context.session_token()

    exit_code = context.run_ws_command(
        lambda: ws_exec(
            surl,
            token,
            ws.id,
            command,
            max_size=context.ws_max_size(),
            login=not raw,
        )
    )
    raise typer.Exit(code=exit_code)


def require_binary(name: str) -> str:
    """Resolve *name* on PATH or exit with the standard error."""
    path = shutil.which(name)
    if not path:
        context.err.print(f"[red]Cannot find {name} in PATH[/red]")
        raise typer.Exit(code=1)
    return path


def ensure_sync_allowed(client, host: str | None, direction: str) -> None:
    """Exit with a clear error if syncing via *host* is not permitted.

    #2706/#2712: ``klangk sync`` rides the one-shot exec channel, gated
    on the ``exec-and-sync`` permission. Fail fast with a clear
    permission error; the server still enforces when rsync gets that
    far. No-op when *host* is None (local side) or allowed.
    """
    if host and sync_denied(client, host):
        context.err.print(
            f"[red]Permission denied:[/red] syncing {direction} workspace"
            f" '{host}' requires the exec-and-sync permission"
        )
        raise typer.Exit(code=1)


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

    klangk_bin = require_binary("klangk")
    rsync_bin = require_binary("rsync")

    client = context.client()
    ensure_sync_allowed(client, remote_host(src), "out of")
    ensure_sync_allowed(client, remote_host(dest), "into")

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
    context.err.print(f"[dim]{' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    raise typer.Exit(code=result.returncode)


@context.app.command()
def images() -> None:
    """List available container images for workspaces."""
    context.require_auth()
    try:
        data = context.client().list_images()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text)
        context.err.print(f"[red]Failed to list images:[/red] {detail}")
        raise typer.Exit(code=1) from None
    console = Console()
    for img in data["allowed"]:
        prefix = "*" if img == data["default"] else " "
        console.print(f"  {prefix} {img}")
