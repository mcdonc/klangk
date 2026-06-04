"""Klangk CLI — typer app."""

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
    KlangkClient,
    WorkspaceNotFoundError,
    _ws_exec,
    _ws_shell,
)
from .config import CLIConfig
from ..container import validate_mount_spec

app = typer.Typer(
    name="klangk",
    help="Klangk — containerized development shell.",
    rich_markup_mode="rich",
)

_cfg_cache: CLIConfig | None = None


def _cfg() -> CLIConfig:
    global _cfg_cache  # pragma: no cover
    if _cfg_cache is None:  # pragma: no cover
        _cfg_cache = CLIConfig.load()  # pragma: no cover
    return _cfg_cache


def _client() -> KlangkClient:  # pragma: no cover
    return KlangkClient(_cfg())


_err = Console(stderr=True)


def _require_auth() -> None:
    cfg = _cfg()
    if not cfg.auth.token:
        _err.print(
            "[red]Not logged in[/red] — run [bold]klangk login[/bold] first."
        )
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd(
    email: str | None = typer.Argument(None, help="Email address"),
    server: str | None = typer.Option(
        None,
        "--server",
        help="Klangk server URL (e.g. http://localhost:8995)",
    ),
    password_file: str | None = typer.Option(
        None,
        "--password-file",
        help="Read password from file (use - for stdin)",
    ),
) -> None:
    """Authenticate with the Klangk server."""
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
        None, "--image", help="Docker image to use (see `klangk images`)"
    ),
    mount: list[str] | None = typer.Option(
        None,
        "--mount",
        help="Mount, repeatable (e.g. /home/me/src:/work/src, nix-vol:/nix)",
    ),
    env: list[str] | None = typer.Option(
        None,
        "--env",
        help="Environment variable, repeatable (e.g. KEY=VALUE)",
    ),
) -> None:
    """Create a new workspace."""
    _require_auth()
    if isinstance(mount, list):
        for m in mount:
            err = validate_mount_spec(m)
            if err:
                _err.print(f"[red]{err}[/red]")
                raise typer.Exit(code=1)
    env_dict = _parse_env_list(env) if isinstance(env, list) else None
    try:
        ws = _client().create_workspace(
            name, image=image, mounts=mount or None, env=env_dict
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text)
        _err.print(f"[red]Failed to create workspace:[/red] {detail}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Created workspace [bold]{name}[/bold] ({ws.id[:12]})")


@app.command("dup")
def dup(
    source: str = typer.Argument(..., help="Source workspace name"),
    new_name: str = typer.Argument(..., help="New workspace name"),
) -> None:
    """Duplicate a workspace."""
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(source)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{source}'")
        raise typer.Exit(code=1) from None
    resp = client.post(
        f"/workspaces/{ws.id}/duplicate", json={"name": new_name}
    )
    if resp.status_code == 409:
        _err.print(
            f"[red]A workspace named[/red] '{new_name}' [red]already exists[/red]"
        )
        raise typer.Exit(code=1)
    if resp.status_code == 404:
        _err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    data = resp.json()
    _out = Console()
    _out.print(
        f"Duplicated [bold]{source}[/bold] → [bold]{new_name}[/bold] ({data['id'][:12]})"
    )


@app.command("rm")
def rm(
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


@app.command("export")
def export_workspace(
    name: str = typer.Argument(..., help="Workspace name"),
    output: Path = typer.Option(
        None, "-o", "--output", help="Output file (default: <name>.tar.gz)"
    ),
) -> None:
    """Export a workspace to a .tar.gz archive (admin only)."""
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(name)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    out_path = output or Path(f"{name}.tar.gz")
    try:
        from rich.progress import (
            Progress,
            BarColumn,
            DownloadColumn,
            TransferSpeedColumn,
        )

        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
        ) as progress:
            task_id = progress.add_task("Exporting...", total=0)

            def _update(downloaded, total):
                if total is not None:
                    progress.update(task_id, total=total, completed=downloaded)
                else:
                    progress.update(
                        task_id, total=downloaded, completed=downloaded
                    )

            client.export_workspace(ws.id, out_path, on_progress=_update)
    except httpx.HTTPStatusError as e:
        _err.print(f"[red]Export failed:[/red] {e.response.text}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Exported [bold]{name}[/bold] → {out_path}")


@app.command("import")
def import_workspace(
    archive: Path = typer.Argument(..., help="Path to .tar.gz archive"),
    name: str = typer.Option(
        None, "--name", help="Override workspace name from archive"
    ),
) -> None:
    """Import a workspace from a .tar.gz archive."""
    _require_auth()
    if not archive.exists():
        _err.print(f"[red]File not found:[/red] {archive}")
        raise typer.Exit(code=1)
    client = _client()
    try:
        ws = client.import_workspace(archive, name=name)
    except httpx.HTTPStatusError as e:
        _err.print(f"[red]Import failed:[/red] {e.response.text}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Imported [bold]{ws.name}[/bold] ({ws.id[:12]})")


_SENTINEL = object()


def _parse_env_list(env_list: list[str]) -> dict[str, str]:
    """Parse ['KEY=VALUE', ...] into a dict."""
    result = {}
    for item in env_list:
        if "=" not in item:
            _err.print(
                f"[red]Invalid env var (expected KEY=VALUE):[/red] {item}"
            )
            raise typer.Exit(code=1)
        key, _, value = item.partition("=")
        result[key] = value
    return result


def _prompt(label: str, current: str | None) -> str | _SENTINEL.__class__:
    """Prompt for a value, showing the current default.

    Returns the new value, or _SENTINEL if the user pressed Enter to keep.
    Empty input (just whitespace) clears the value and returns "".
    """
    display = current or "(none)"
    raw = input(f"{label} [{display}]: ")
    if raw == "":
        return _SENTINEL  # keep current
    return raw.strip()


@app.command()
def edit(
    workspace: str = typer.Argument(..., help="Workspace name"),
    name: str | None = typer.Option(None, "--name", help="New name"),
    image: str | None = typer.Option(None, "--image", help="Container image"),
    command: str | None = typer.Option(
        None, "--command", "-c", help="Default shell command (use '' to clear)"
    ),
    mount: list[str] | None = typer.Option(
        None,
        "--mount",
        help="Mount, repeatable (e.g. /home/me/src:/work/src, nix-vol:/nix)",
    ),
    env: list[str] | None = typer.Option(
        None,
        "--env",
        help="Environment variable, repeatable (e.g. KEY=VALUE)",
    ),
) -> None:
    """Edit workspace settings.

    Without flags, interactively prompts for each field.
    Press Enter to keep the current value.
    """
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None

    has_flags = (
        name is not None
        or image is not None
        or command is not None
        or isinstance(mount, list)
        or isinstance(env, list)
    )
    if not has_flags:
        # Interactive mode
        new_name = _prompt("Name", ws.name)
        new_image = _prompt("Container Image", ws.image)
        new_command = _prompt("Default shell command", ws.default_command)

        # Interactive mount editing loop
        current_mounts = list(ws.mounts or [])
        mounts_changed = False
        while True:
            if current_mounts:
                typer.echo("\nCurrent mounts:")
                for i, m in enumerate(current_mounts, 1):
                    typer.echo(f"  {i}. {m}")
            else:
                typer.echo("\nNo mounts configured.")

            add = input(
                "\nAdd mount (e.g. /host/path:/container/path, or Enter to skip): "
            ).strip()
            if add:
                err = validate_mount_spec(add)
                if err:
                    typer.echo(err)
                    continue
                current_mounts.append(add)
                mounts_changed = True
                continue

            if current_mounts:
                rm = input("Remove mount number (or Enter to skip): ").strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        if 0 <= idx < len(current_mounts):
                            removed = current_mounts.pop(idx)
                            typer.echo(f"Removed: {removed}")
                            mounts_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        # Interactive env var editing loop
        current_env = dict(ws.env or {})
        env_changed = False
        while True:
            if current_env:
                typer.echo("\nCurrent environment variables:")
                env_items = list(current_env.items())
                for i, (k, v) in enumerate(env_items, 1):
                    typer.echo(f"  {i}. {k}={v}")
            else:
                typer.echo("\nNo environment variables configured.")

            add = input(
                "\nAdd env var (e.g. KEY=VALUE, or Enter to skip): "
            ).strip()
            if add:
                if "=" not in add:
                    typer.echo("Invalid format, expected KEY=VALUE.")
                    continue
                key, _, value = add.partition("=")
                current_env[key] = value
                env_changed = True
                continue

            if current_env:
                rm = input(
                    "Remove env var number (or Enter to skip): "
                ).strip()
                if rm:
                    try:
                        idx = int(rm) - 1
                        env_items = list(current_env.items())
                        if 0 <= idx < len(env_items):
                            removed_key = env_items[idx][0]
                            del current_env[removed_key]
                            typer.echo(f"Removed: {removed_key}")
                            env_changed = True
                            continue
                        else:
                            typer.echo("Invalid number.")
                            continue
                    except ValueError:
                        typer.echo("Invalid number.")
                        continue

            break  # both add and remove were skipped

        body: dict = {}
        if new_name is not _SENTINEL:
            body["name"] = new_name or ws.name  # don't allow empty name
        if new_image is not _SENTINEL:
            body["image"] = new_image or None
        if new_command is not _SENTINEL:
            body["default_command"] = new_command or None
        if mounts_changed:
            body["mounts"] = current_mounts or None
        if env_changed:
            body["env"] = current_env or None
    else:
        # Flags mode — only send provided fields
        body = {}
        if name is not None:
            body["name"] = name
        if image is not None:
            body["image"] = image or None
        if command is not None:
            body["default_command"] = command or None
        if isinstance(mount, list):
            for m in mount:
                err = validate_mount_spec(m)
                if err:
                    _err.print(f"[red]{err}[/red]")
                    raise typer.Exit(code=1)
            body["mounts"] = mount or None
        if isinstance(env, list):
            body["env"] = _parse_env_list(env) or None

    if not body:
        typer.echo("No changes.")
        return

    resp = client.put(f"/workspaces/{ws.id}", json=body)
    if resp.status_code == 404:
        _err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Updated workspace {ws.name}")


@app.command()
def shell(
    workspace: str | None = typer.Argument(
        None, help="Workspace name (or select interactively)"
    ),
    command: str | None = typer.Option(
        None,
        "--command",
        "-c",
        help="Override the default shell command",
    ),
) -> None:
    """Connect to a workspace and execute the default shell command."""
    cfg = _cfg()
    if not cfg.auth.token:  # pragma: no cover
        _err.print(
            "[red]Not logged in[/red] — run [bold]klangk login[/bold] first."
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
            typer.echo("No workspaces found — create one with klangk create.")
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

    Also usable as an rsync transport: rsync -avz -e "klangk exec" src/ ws:/dest/
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

    Any extra flags after src and dest are passed directly to rsync.

    Examples:

        klangk sync ~/project my-workspace:/work/project

        klangk sync my-workspace:/work/output ~/output

        klangk sync ~/src ws:/work/src --delete --exclude .git
    """
    import shutil
    import subprocess

    _require_auth()

    klangk_bin = shutil.which("klangk")
    if not klangk_bin:  # pragma: no cover
        _err.print("[red]Cannot find klangk in PATH[/red]")
        raise typer.Exit(code=1)

    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        _err.print("[red]Cannot find rsync in PATH[/red]")
        raise typer.Exit(code=1)

    cmd = [
        rsync_bin,
        "-avz",
        "--blocking-io",
        "-e",
        f"{klangk_bin} exec",
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


vol_app = typer.Typer(
    name="volumes",
    help="Manage Docker volumes for workspaces.",
    rich_markup_mode="rich",
)
app.add_typer(vol_app, name="volumes")


@vol_app.command("ls")
def volumes_list(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """List klangk-managed Docker volumes."""
    _require_auth()
    client = _client()
    resp = client.get("/volumes")
    client._check_auth(resp)
    resp.raise_for_status()
    volumes = resp.json()
    if not volumes:
        typer.echo("No volumes.")
        return
    if plain:
        for v in volumes:
            typer.echo(f"  {v['name']}")
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Name", style="bold")
    table.add_column("Created")
    for v in volumes:
        table.add_row(v["name"], v.get("created", "")[:19])
    console.print(table)


@vol_app.command("create")
def volumes_create(
    name: str = typer.Argument(..., help="Volume name"),
) -> None:
    """Create a named Docker volume."""
    _require_auth()
    client = _client()
    resp = client.post("/volumes", json={"name": name})
    client._check_auth(resp)
    if resp.status_code == 409:
        _err.print(f"[red]Volume already exists:[/red] {name}")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Created volume {name}")


@vol_app.command("rm")
def volumes_rm(
    name: str = typer.Argument(..., help="Volume name"),
) -> None:
    """Delete a named Docker volume."""
    _require_auth()
    client = _client()
    resp = client.delete(f"/volumes/{name}")
    client._check_auth(resp)
    if resp.status_code == 404:
        _err.print(f"[red]Volume not found:[/red] {name}")
        raise typer.Exit(code=1)
    if resp.status_code == 409:
        _err.print(f"[red]Volume is in use:[/red] {name}")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Deleted volume {name}")


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
