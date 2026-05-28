"""Bark CLI — typer app."""

from __future__ import annotations


import asyncio
from pathlib import Path

import httpx
import typer
import websockets
from rich.console import Console
from rich.table import Table

from .auth import login, logout as do_logout
from .client import (
    AuthError,
    BarkClient,
    WorkspaceNotFoundError,
    _ws_exec,
    _ws_shell,
)
from .config import CLIConfig

app = typer.Typer(
    name="bark",
    help="Bark — containerized development shell.",
    rich_markup_mode="rich",
)

_cfg_cache: CLIConfig | None = None


def _cfg() -> CLIConfig:
    global _cfg_cache  # pragma: no cover
    if _cfg_cache is None:  # pragma: no cover
        _cfg_cache = CLIConfig.load()  # pragma: no cover
    return _cfg_cache


def _client() -> BarkClient:  # pragma: no cover
    return BarkClient(_cfg())


_err = Console(stderr=True)


def _require_auth() -> None:
    cfg = _cfg()
    if not cfg.auth.token:
        _err.print(
            "[red]Not logged in[/red] — run [bold]bark login[/bold] first."
        )
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd(
    email: str | None = typer.Argument(None, help="Email address"),
    server: str | None = typer.Option(
        None,
        "--server",
        help="Bark server URL (e.g. http://localhost:8995)",
    ),
    password_file: str | None = typer.Option(
        None,
        "--password-file",
        help="Read password from file (use - for stdin)",
    ),
) -> None:
    """Authenticate with the Bark server."""
    if server is None:  # pragma: no cover
        server = _cfg().server.url
    password = None
    if password_file is not None:
        if password_file == "-":
            import sys

            password = sys.stdin.readline().rstrip("\n")
        else:
            password = Path(password_file).read_text().strip()
    login(server, email=email, password=password)


@app.command()
def logout() -> None:
    """Clear stored credentials."""
    do_logout()


@app.command()
def status(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """Show connection info (server, user)."""
    cfg = _cfg()
    if plain:
        print(f"server={cfg.server.url}")
        if cfg.auth.token:
            print(f"user={cfg.auth.email or 'unknown'}")
            print("status=logged_in")
        else:
            print("status=not_logged_in")
        return
    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Server", cfg.server.url)
    if cfg.auth.token:
        table.add_row("User", cfg.auth.email or "unknown")
        table.add_row("Status", "[green]logged in[/green]")
    else:
        table.add_row("Status", "[yellow]not logged in[/yellow]")
    console.print(table)


@app.command("list")
def list_workspaces(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """List all workspaces."""
    _require_auth()
    client = _client()
    workspaces = client.list_workspaces()
    if not workspaces:
        typer.echo("No workspaces found.")
        return
    if plain:
        for ws in workspaces:
            typer.echo(f"  {ws.name}  ({ws.id[:12]})  {ws.created_at[:10]}")
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Created")
    for ws in workspaces:
        table.add_row(ws.name, ws.id[:12], ws.created_at[:10])
    console.print(table)


@app.command()
def create(
    name: str = typer.Argument(..., help="Workspace name"),
    image: str | None = typer.Option(
        None, "--image", help="Docker image to use (see `bark images`)"
    ),
) -> None:
    """Create a new workspace."""
    _require_auth()
    try:
        ws = _client().create_workspace(name, image=image)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text)
        _err.print(f"[red]Failed to create workspace:[/red] {detail}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Created workspace [bold]{name}[/bold] ({ws.id[:12]})")


@app.command()
def delete(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Delete a workspace."""
    _require_auth()
    try:
        _client().delete_workspace(name)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Deleted workspace {name}")


@app.command("set-command")
def set_command(
    workspace: str = typer.Argument(..., help="Workspace name"),
    command: str | None = typer.Argument(
        None, help="Default command (omit to clear)"
    ),
) -> None:
    """Set or clear the default terminal command for a workspace."""
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None
    resp = client.put(
        f"/workspaces/{ws.id}",
        json={"default_command": command},
    )
    if resp.status_code == 404:
        _err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    if command:
        typer.echo(f"Default command set to: {command}")
    else:
        typer.echo("Default command cleared")


@app.command()
def shell(
    workspace: str | None = typer.Argument(
        None, help="Workspace name (or select interactively)"
    ),
    command: str | None = typer.Option(
        None,
        "--command",
        "-c",
        help="Override the workspace default command (use 'bash' for a plain shell)",
    ),
) -> None:
    """Connect to a workspace and drop into a bash shell."""
    cfg = _cfg()
    if not cfg.auth.token:  # pragma: no cover
        _err.print(
            "[red]Not logged in[/red] — run [bold]bark login[/bold] first."
        )  # pragma: no cover
        raise typer.Exit(code=1)  # pragma: no cover

    client = _client()

    # Resolve workspace
    if workspace:
        try:
            ws = client.resolve_workspace(workspace)
        except WorkspaceNotFoundError:  # pragma: no cover
            _err.print(f"[red]No workspace named[/red] '{workspace}'")
            raise typer.Exit(code=1) from None
    else:
        workspaces = client.list_workspaces()
        if not workspaces:
            typer.echo("No workspaces found — create one with bark create.")
            raise typer.Exit(code=1)
        if len(workspaces) == 1:
            ws = workspaces[0]
        else:
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
            ws = workspaces[idx]

    # Build WebSocket URL
    server_url = cfg.server.url.rstrip("/")
    if server_url.startswith("http://"):
        ws_url = server_url.replace("http://", "ws://") + "/ws"
    elif server_url.startswith("https://"):  # pragma: no cover
        ws_url = server_url.replace("https://", "wss://") + "/ws"
    else:  # pragma: no cover
        ws_url = f"ws://{server_url}/ws"

    token = cfg.auth.token
    _err.print(f"Connecting to [bold]{ws.name}[/bold]...")
    asyncio.run(_ws_shell(ws_url, token, ws.id, command_override=command))


@app.command(
    "exec",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
    },
)
def exec_cmd(
    ctx: typer.Context,
    workspace: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Run a command in a workspace container.

    Also usable as an rsync transport: rsync -avz -e "bark exec" src/ ws:/dest/
    """
    cfg = _cfg()
    _require_auth()

    command = ctx.args
    if not command:
        _err.print("[red]No command specified[/red]")
        raise typer.Exit(code=1)

    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None

    server_url = cfg.server.url.rstrip("/")
    if server_url.startswith("http://"):
        ws_url = server_url.replace("http://", "ws://") + "/ws"
    elif server_url.startswith("https://"):  # pragma: no cover
        ws_url = server_url.replace("https://", "wss://") + "/ws"
    else:  # pragma: no cover
        ws_url = f"ws://{server_url}/ws"

    exit_code = asyncio.run(_ws_exec(ws_url, cfg.auth.token, ws.id, command))
    raise typer.Exit(code=exit_code)


@app.command(
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

    Examples:

        bark sync ~/project my-workspace:/work/project

        bark sync my-workspace:/work/output ~/output

    Extra flags are passed to rsync (e.g. --delete, --exclude).
    """
    import shutil
    import subprocess

    _require_auth()

    bark_bin = shutil.which("bark")
    if not bark_bin:  # pragma: no cover
        _err.print("[red]Cannot find bark in PATH[/red]")
        raise typer.Exit(code=1)

    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        _err.print("[red]Cannot find rsync in PATH[/red]")
        raise typer.Exit(code=1)

    cmd = [
        rsync_bin,
        "-avz",
        "-e",
        f"{bark_bin} exec",
        *ctx.args,
        src,
        dest,
    ]
    _err.print(f"[dim]{' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    raise typer.Exit(code=result.returncode)


@app.command()
def images() -> None:
    """List available Docker images for workspaces."""
    _require_auth()
    try:
        data = _client().list_images()
    except httpx.HTTPStatusError as exc:  # pragma: no cover
        detail = exc.response.json().get("detail", exc.response.text)
        _err.print(f"[red]Failed to list images:[/red] {detail}")
        raise typer.Exit(code=1) from None
    console = Console()
    for img in data["allowed"]:
        prefix = "*" if img == data["default"] else " "
        console.print(f"  {prefix} {img}")


def main() -> None:  # pragma: no cover
    try:
        app()
    except AuthError as exc:
        _err.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    except httpx.ConnectError:
        _err.print("[red]Cannot connect to server[/red] — is it running?")
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        _err.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    except websockets.ConnectionClosed:
        _err.print("\n[red]Server disconnected[/red]")
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover
    main()
