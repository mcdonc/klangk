"""Klangk CLI — typer app."""

from __future__ import annotations


import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import typer
import websockets
from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TransferSpeedColumn,
)
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .auth import login, logout as do_logout
from .client import (
    AuthError,
    KlangkClient,
    WorkspaceNotFoundError,
    _drain_stdin,
    _get_terminal_size,
    _send_ignore_closed,
    _exec_on_ws,
    _wait_workspace_ready,
    _ws_exec,
    _ws_shell,
    reset_terminal,
)
from .config import CLIConfig, CLIState
from .mount import validate_mount_spec
from .sandbox import (
    build_all_mounts,
    build_copy_pairs,
    expand_container_path,
    load_sandbox_config,
    resolve_setup_command,
)

app = typer.Typer(
    name="klangkc",
    help="Klangk Client",
    rich_markup_mode="rich",
)

_cfg_cache: CLIConfig | None = None
_state_cache: CLIState | None = None


def _cfg() -> CLIConfig:
    global _cfg_cache  # pragma: no cover
    if _cfg_cache is None:  # pragma: no cover
        _cfg_cache = CLIConfig.load()  # pragma: no cover
    return _cfg_cache


def _state() -> CLIState:
    global _state_cache  # pragma: no cover
    if _state_cache is None:  # pragma: no cover
        _state_cache = CLIState.load()  # pragma: no cover
    return _state_cache


_server_override: str | None = None


@app.callback()
def _app_callback(
    server: str | None = typer.Option(
        None, "--server", help="Server alias or URL"
    ),
) -> None:
    global _server_override
    if server is not None:
        _server_override = _cfg().resolve_server(server)


def _server_url() -> str:
    if _server_override is not None:
        return _server_override
    active = _state().active_server
    if active is not None:
        return active
    _err.print(
        "[red]No server configured[/red] — run"
        " [bold]klangkc login <server>[/bold] first,"
        " or pass [bold]--server[/bold]."
    )
    raise typer.Exit(code=1)


def _client() -> KlangkClient:  # pragma: no cover
    return KlangkClient(_server_url(), _state().get_token(_server_url()))


def _ws_max_size() -> int:
    return _cfg().get_ws_max_size(_server_url())


_err = Console(stderr=True)


def _require_auth() -> None:
    if not _state().get_token(_server_url()):
        _err.print(
            "[red]Not logged in[/red] — run [bold]klangkc login[/bold] first."
        )
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd(
    server: str = typer.Argument(..., help="Server alias or URL"),
    user: str | None = typer.Argument(None, help="User (email or handle)"),
    password_file: str | None = typer.Option(
        None,
        "--password-file",
        help="Read password from file (use - for stdin)",
    ),
) -> None:
    """Authenticate with a Klangk server."""
    cfg = _cfg()
    resolved_url = cfg.resolve_server(server)
    # Default user from config if not provided on command line
    email = user or cfg.get_user(resolved_url)
    password = None
    if password_file is not None:
        if password_file == "-":
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = Path(password_file).read_text().strip()
    login(resolved_url, email=email, password=password)


@app.command()
def logout(
    server: str | None = typer.Argument(None, help="Server alias or URL"),
) -> None:
    """Clear stored credentials."""
    if server is not None:
        resolved_url = _cfg().resolve_server(server)
    else:
        active = _state().active_server
        if active is None:
            _err.print(
                "[red]No active server[/red] — pass a server argument"
                " or log in first."
            )
            raise typer.Exit(code=1)
        resolved_url = active
    do_logout(resolved_url)


@app.command()
def status(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """Show connection info (server, user)."""
    # status works even with no active server (unlike other commands).
    url = _server_override or _state().active_server
    state = _state()
    token = state.get_token(url) if url else None
    email = state.get_email(url) if url else None
    if plain:
        print(f"server={url or '(none)'}")
        if token:
            print(f"user={email or 'unknown'}")
            print("status=logged_in")
        else:
            print("status=not_logged_in")
        return
    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Server", url or "(none)")
    if token:
        table.add_row("User", email or "unknown")
        table.add_row("Status", "[green]logged in[/green]")
    else:
        table.add_row("Status", "[yellow]not logged in[/yellow]")
    console.print(table)


@app.command("ls")
def list_workspaces(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
    shared: bool = typer.Option(
        False, "--shared", help="Include workspaces shared with you"
    ),
    limit: int = typer.Option(
        10, "--limit", help="Max workspaces to list per section"
    ),
    all_workspaces: bool = typer.Option(
        False, "--all", help="List every workspace (follow pagination)"
    ),
    sort: str = typer.Option(
        "created",
        "--sort",
        help="Sort by 'created' or 'name'",
    ),
    order: str = typer.Option(
        "desc",
        "--order",
        help="Sort direction: 'asc' or 'desc'",
    ),
    filter: str = typer.Option(
        None,
        "--filter",
        help="Substring filter on workspace name",
    ),
) -> None:
    """List workspaces.

    Lists one page at a time (default 10). Pass --all to page through
    every workspace. Sort with --sort/--order and filter by name substring
    with --filter.
    """
    _require_auth()
    client = _client()
    workspaces = client.list_workspaces(
        limit=limit,
        all_pages=all_workspaces,
        sort=sort,
        order=order,
        q=filter,
    )
    shared_workspaces = (
        client.list_shared_workspaces(
            limit=limit,
            all_pages=all_workspaces,
            sort=sort,
            order=order,
            q=filter,
        )
        if shared
        else []
    )
    if not workspaces and not shared_workspaces:
        typer.echo("No workspaces found.")
        return
    if plain:
        for ws in workspaces:
            typer.echo(f"  {ws.name}  ({ws.id[:12]})  {ws.created_at[:10]}")
        if shared_workspaces:
            typer.echo("Shared with me:")
            for ws in shared_workspaces:
                owner = f"  by {ws.owner_email}" if ws.owner_email else ""
                typer.echo(
                    f"  {ws.name}  ({ws.id[:12]})  {ws.created_at[:10]}{owner}"
                )
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Created")
    if shared:
        table.add_column("Owner")
    for ws in workspaces:
        row = [ws.name, ws.id[:12], ws.created_at[:10]]
        if shared:
            row.append("")
        table.add_row(*row)
    for ws in shared_workspaces:
        table.add_row(
            ws.name, ws.id[:12], ws.created_at[:10], ws.owner_email or ""
        )
    console.print(table)


@app.command()
def create(
    name: str = typer.Argument(..., help="Workspace name"),
    image: str | None = typer.Option(
        None, "--image", help="Container image to use (see `klangkc images`)"
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Start container automatically on server boot",
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
            name,
            image=image,
            auto_start=auto_start,
            mounts=mount or None,
            env=env_dict,
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
        f"/api/v1/workspaces/{ws.id}/duplicate", json={"name": new_name}
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


@app.command("members")
def members(
    workspace: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """List members of a workspace by role."""
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None
    resp = client.get(f"/api/v1/workspaces/{ws.id}/roles")
    client._check_auth(resp)
    resp.raise_for_status()
    roles = resp.json()
    any_members = False
    for r in roles:
        if not r["members"]:
            continue
        any_members = True
        role_name = r["role"].rstrip("s")  # "coders" -> "coder"
        for m in r["members"]:
            email = m.get("email", "")
            typer.echo(f"  {email} ({role_name})")
    if not any_members:
        typer.echo("No shared members")


@app.command("restart")
def restart(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Restart the container for a workspace."""
    _require_auth()
    try:
        _client().restart_workspace(name)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Restarted workspace {name}")


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
    if out_path.exists() and output is None:
        # Don't overwrite — find a unique name
        stem = name
        n = 1
        while out_path.exists():
            out_path = Path(f"{stem}-{n}.tar.gz")
            n += 1
    try:

        class _EstDownloadColumn(DownloadColumn):
            def render(self, task):
                result = super().render(task)
                return Text.assemble(result, " (est)")

        class _SafeSpeedColumn(TransferSpeedColumn):
            def render(
                self, task
            ):  # pragma: no cover — only called during live terminal render
                if task.finished:
                    return Text("")
                return super().render(task)

        progress = Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            _EstDownloadColumn(),
            _SafeSpeedColumn(),
        )
        task_id = progress.add_task("Downloading...", total=0)
        started = [False]

        def _update(downloaded, total):
            if not started[0]:
                started[0] = True
                live.update(progress)
            if total is not None:
                progress.update(task_id, total=total, completed=downloaded)
            else:
                progress.update(
                    task_id, total=downloaded, completed=downloaded
                )

        spinner = Spinner("dots", text="Building archive on server...")
        with Live(spinner, refresh_per_second=10) as live:
            client.export_workspace(ws.id, out_path, on_progress=_update)
            # Ensure progress bar hits 100% regardless of estimate accuracy
            if started[0]:
                final = progress.tasks[task_id].completed
                progress.update(task_id, total=final, completed=final)
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
        progress = Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
        )
        task_id = progress.add_task(
            "Uploading...", total=archive.stat().st_size
        )

        def _update(uploaded, total):
            progress.update(task_id, completed=uploaded)

        with progress:
            ws = client.import_workspace(
                archive, name=name, on_progress=_update
            )
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
    auto_start: bool | None = typer.Option(
        None,
        "--auto-start/--no-auto-start",
        help="Start container automatically on server boot",
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
        or auto_start is not None
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
        if auto_start is not None:
            body["auto_start"] = auto_start
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

    resp = client.put(f"/api/v1/workspaces/{ws.id}", json=body)
    if resp.status_code == 404:
        _err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Updated workspace {ws.name}")


def _build_ws_url(server_url: str) -> str:
    """Convert an HTTP(S) server URL to a WebSocket URL."""
    if server_url.startswith("http://"):
        return server_url.replace("http://", "ws://") + "/ws"
    elif server_url.startswith("https://"):
        return server_url.replace("https://", "wss://") + "/ws"
    return f"ws://{server_url}/ws"


def _resolve_forward_agent(
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
            _err.print(
                "[yellow]Warning: --forward-agent set but SSH_AUTH_SOCK"
                " is not set. Agent forwarding will be skipped.[/yellow]"
            )
    return result


@app.command()
def shell(
    workspace: str | None = typer.Argument(
        None, help="Workspace name (or select interactively)"
    ),
    terminal: str | None = typer.Argument(
        None,
        help="Terminal name to select (or handle:name for shared)",
    ),
    forward_agent: bool | None = typer.Option(
        None,
        "--forward-agent/--no-forward-agent",
        "-A",
        help="Forward local SSH agent into the container",
    ),
) -> None:
    """Connect to a workspace shell."""
    # When called directly (not via typer CLI), forward_agent may be a
    # typer.models.OptionInfo instead of bool/None.  Normalize to None.
    if not isinstance(forward_agent, bool):
        forward_agent = None
    token = _state().get_token(_server_url())
    if not token:  # pragma: no cover
        _err.print(
            "[red]Not logged in[/red] — run [bold]klangkc login[/bold] first."
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
        workspaces = client.list_workspaces(all_pages=True)
        if not workspaces:
            typer.echo("No workspaces found — create one with klangkc create.")
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
    server_url = _server_url().rstrip("/")
    ws_url = _build_ws_url(server_url)

    _err.print(f"Connecting to [bold]{ws.name}[/bold]...")
    _err.print("[dim]Escape: Enter, then ~.[/dim]")
    forward_agent = _resolve_forward_agent(
        forward_agent,
        config_default=_cfg().get_forward_agent(_server_url()) or False,
    )
    try:
        asyncio.run(
            _ws_shell(
                ws_url,
                token,
                ws.id,
                window=terminal,
                forward_agent=forward_agent,
                max_size=_ws_max_size(),
            )
        )
    except websockets.InvalidStatus as e:
        reset_terminal()
        _drain_stdin()
        if e.response.status_code in (4001, 4002):
            _err.print(
                "[red]Session expired. Run `klangkc login`"
                " to re-authenticate.[/red]"
            )
        else:
            _err.print(f"[red]Connection rejected: {e}[/red]")
        raise typer.Exit(code=1) from None
    except ConnectionError as e:
        reset_terminal()
        _drain_stdin()
        _err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None


def _resolve_workspace_and_url(
    workspace_name: str,
) -> tuple:
    """Resolve a workspace by name and return (ws, ws_url, token)."""
    _require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace_name)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace_name}'")
        raise typer.Exit(code=1) from None
    server_url = _server_url().rstrip("/")
    ws_url = _build_ws_url(server_url)
    return ws, ws_url, _state().get_token(_server_url())


async def _sandbox_setup(ws, config, sandbox_root, handle):
    """Copy files and run setup script on an open WebSocket.

    Called once after workspace creation, before the shell starts.
    The caller has already connected and called _wait_workspace_ready.
    """
    # Copy files into container home.
    for host_path, container_dest in build_copy_pairs(
        config, sandbox_root, handle
    ):
        src = Path(host_path)
        if not src.exists():
            _err.print(
                f"[yellow]Warning: copy source {host_path} not"
                f" found, skipping[/yellow]"
            )
            continue
        _err.print(f"  [dim]copy:[/dim] {host_path} → {container_dest}")
        parent = str(Path(container_dest).parent)
        stdout_buf = io.BytesIO()
        exit_code = await _exec_on_ws(
            ws,
            ["sh", "-c", f"mkdir -p {parent} && cat > {container_dest}"],
            stdin=io.BytesIO(src.read_bytes()),
            stdout=stdout_buf,
        )
        if exit_code != 0:
            _err.print(
                f"[yellow]Warning: copy to {container_dest}"
                f" failed (exit {exit_code})[/yellow]"
            )

    # Run setup script — stream output to stderr in real time.
    setup_cmd = resolve_setup_command(config, handle)
    if setup_cmd:
        mount_at = expand_container_path(config.mount_at, handle)
        _err.print(f"[dim]setup:[/dim] {setup_cmd}")
        # Set GIT_SSH_COMMAND so SSH accepts new host keys automatically.
        # Setup runs non-interactively (no TTY), so SSH cannot prompt the
        # user for host-key confirmation; without this, git-over-SSH hangs
        # indefinitely waiting for input that will never arrive.
        shell_cmd = (
            "export GIT_SSH_COMMAND="
            "'ssh -o StrictHostKeyChecking=accept-new'"
            f" && cd {mount_at} && bash -c '{setup_cmd}'"
        )
        timeout = config.setup_timeout or None
        exit_code = await _exec_on_ws(
            ws,
            ["sh", "-c", shell_cmd],
            stdout=sys.stderr.buffer,
            timeout=timeout,
        )
        if exit_code == 124:
            _err.print(f"[yellow]Setup timed out after {timeout}s[/yellow]")
        elif exit_code != 0:
            _err.print(f"[yellow]Setup exited with code {exit_code}[/yellow]")


@app.command()
def sandbox(
    workspace: str = typer.Argument(help="Workspace name"),
    path: str = typer.Argument(
        ".",
        help="Path to sandbox root (directory containing .klangk/)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-apply config and re-run setup on an existing workspace",
    ),
) -> None:
    """Create a sandbox workspace from .klangk-sandbox.yaml.

    Creates the workspace with the configured image, mounts, and
    volumes, copies files, and runs the setup script.  Use
    ``klangkc shell`` afterwards to connect.
    """
    token = _state().get_token(_server_url())
    if not token:  # pragma: no cover
        _err.print(
            "[red]Not logged in[/red] — run [bold]klangkc login[/bold] first."
        )
        raise typer.Exit(code=1)

    sandbox_root = Path(path).resolve()
    try:
        config = load_sandbox_config(sandbox_root)
    except FileNotFoundError as e:
        _err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    except ValueError as e:
        _err.print(f"[red]Invalid sandbox config:[/red] {e}")
        raise typer.Exit(code=1) from None

    client = _client()
    handle = client.get_handle()
    ws_url = _build_ws_url(_server_url().rstrip("/"))
    created = False

    # Check if workspace already exists.
    try:
        ws = client.resolve_workspace(workspace)
        if not force:
            _err.print(
                f"[red]Workspace [bold]{workspace}[/bold] already"
                " exists.[/red] Pass [bold]--force[/bold] to re-apply"
                " config and re-run setup."
            )
            raise typer.Exit(code=1)
        _err.print(
            f"Workspace [bold]{workspace}[/bold] exists, re-applying config..."
        )
    except WorkspaceNotFoundError:
        all_mounts = build_all_mounts(config, sandbox_root, handle)
        _err.print(f"Creating workspace [bold]{workspace}[/bold]...")
        ws = client.create_workspace(
            workspace,
            image=config.image,
            default_command=config.default_command,
            auto_start=config.auto_start,
            mounts=all_mounts,
        )
        _err.print(f"Workspace [bold]{workspace}[/bold] created.")
        created = True

    need_setup = created or force

    if need_setup:
        _err.print(f"Connecting to [bold]{workspace}[/bold] for setup...")
        try:
            asyncio.run(
                _sandbox_setup_only(
                    ws_url,
                    token,
                    ws.id,
                    config,
                    sandbox_root,
                    handle,
                    max_size=_ws_max_size(),
                )
            )
        except websockets.InvalidStatus as e:  # pragma: no cover
            if e.response.status_code in (4001, 4002):
                _err.print(
                    "[red]Session expired.[/red] Run"
                    " [bold]klangkc login[/bold] to re-authenticate."
                )
                raise typer.Exit(code=1) from None
            raise
        except ConnectionError as e:
            _err.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1) from None

    _err.print(
        f"[green]Done.[/green] Run [bold]klangkc shell"
        f" {workspace}[/bold] to connect."
    )


async def _sandbox_setup_only(
    ws_url,
    token,
    workspace_id,
    config,
    sandbox_root,
    handle,
    max_size=None,
):
    """Connect to workspace, run setup, then disconnect (no shell)."""
    kwargs = {}
    if max_size is not None:
        kwargs["max_size"] = max_size
    async with websockets.connect(f"{ws_url}?token={token}", **kwargs) as ws:
        await _wait_workspace_ready(ws, workspace_id)
        await _sandbox_setup(ws, config, sandbox_root, handle)


terminal_app = typer.Typer(
    name="terminal",
    help="Manage workspace terminals.",
    rich_markup_mode="rich",
)
app.add_typer(terminal_app, name="terminal")


@terminal_app.command("ls")
def terminals(
    workspace: str = typer.Argument(help="Workspace name"),
) -> None:
    """List all terminals (own + shared) in a workspace."""
    ws, ws_url, token = _resolve_workspace_and_url(workspace)
    max_size = _ws_max_size()

    # We need to start a terminal to get the window list, then also
    # get shared terminals. Use _ws_command to get each.
    async def _list() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await _wait_workspace_ready(conn, ws.id)

            await conn.send(json.dumps({"cmd": "ui_ready"}))

            # Wait for container_ready, collecting shared_terminals along
            # the way (sent during ui_ready).
            shared: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 60
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "shared_terminals":
                    shared = msg.get("terminals", [])
                if (
                    msg.get("type") == "event"
                    and isinstance(msg.get("event"), dict)
                    and msg["event"].get("name") == "container_ready"
                ):
                    break

            # Start terminal to get own windows.
            # terminal_windows arrives after terminal_started — skip
            # terminal_output and other messages until we get it.
            cols, rows = _get_terminal_size()
            await conn.send(
                json.dumps(
                    {"cmd": "terminal_start", "cols": cols, "rows": rows}
                )
            )
            own_windows: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "terminal_windows":
                    own_windows = msg.get("windows", [])
                    break

            # Print results
            table = Table(title=f"Terminals in {ws.name}")
            table.add_column("Name")
            table.add_column("Type")
            table.add_column("Owner")
            for w in own_windows:
                table.add_row(w["name"], "own", "")
            for t in shared:
                table.add_row(
                    t["window_name"],
                    "shared",
                    t.get("handle", ""),
                )
            _err.print(table)

            await _send_ignore_closed(
                conn, json.dumps({"cmd": "terminal_stop"})
            )

    asyncio.run(_list())


_VALID_ROLES = ["owner", "coder", "collaborator", "spectator"]
_ROLE_TO_GROUP = {
    "owner": "owners",
    "coder": "coders",
    "collaborator": "collaborators",
    "spectator": "spectators",
}


@app.command("share")
def share_workspace(
    workspace: str = typer.Argument(help="Workspace name"),
    email: str = typer.Argument(help="Email of user to add"),
    role: str = typer.Option(
        "coder", help="Role: owner, coder, collaborator, or spectator"
    ),
) -> None:
    """Share a workspace with a user."""
    _require_auth()
    if role not in _VALID_ROLES:
        _err.print(
            f"[red]Invalid role '{role}'[/red]."
            f" Choose from: {', '.join(_VALID_ROLES)}"
        )
        raise typer.Exit(code=1)
    group_suffix = _ROLE_TO_GROUP[role]
    try:
        result = _client().add_workspace_member(
            workspace, email, role=group_suffix
        )
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Shared workspace {workspace} with {result['email']} as {role}"
    )


@app.command("unshare")
def unshare_workspace(
    workspace: str = typer.Argument(help="Workspace name"),
    email: str = typer.Argument(help="Email of user to remove"),
) -> None:
    """Remove a user's access to a workspace."""
    _require_auth()
    try:
        _client().remove_workspace_member(workspace, email)
    except WorkspaceNotFoundError as e:
        _err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    typer.echo(f"Removed {email} from workspace {workspace}")


@terminal_app.command("share")
def share_terminal(
    workspace: str = typer.Argument(help="Workspace name"),
    terminal: str = typer.Argument(help="Terminal name to share"),
) -> None:
    """Share a terminal with other workspace members."""
    ws, ws_url, token = _resolve_workspace_and_url(workspace)
    max_size = _ws_max_size()

    async def _share() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await _wait_workspace_ready(conn, ws.id)

            await conn.send(json.dumps({"cmd": "ui_ready"}))

            # Wait for container_ready
            deadline = asyncio.get_event_loop().time() + 60
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if (
                    msg.get("type") == "event"
                    and isinstance(msg.get("event"), dict)
                    and msg["event"].get("name") == "container_ready"
                ):
                    break

            # Start terminal to get window list
            cols, rows = _get_terminal_size()
            await conn.send(
                json.dumps(
                    {"cmd": "terminal_start", "cols": cols, "rows": rows}
                )
            )
            own_windows: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "terminal_windows":
                    own_windows = msg.get("windows", [])
                    break
            match = next(
                (w for w in own_windows if w["name"] == terminal), None
            )
            if match is None:
                _err.print(f"[red]Terminal '{terminal}' not found[/red]")
                raise typer.Exit(code=1)

            await conn.send(
                json.dumps({"cmd": "share_window", "window_id": match["id"]})
            )
            # Wait for shared_terminals confirmation
            deadline = asyncio.get_event_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "shared_terminals":
                    _err.print(
                        f"[green]Terminal '{terminal}' is now shared[/green]"
                    )
                    break

            await _send_ignore_closed(
                conn, json.dumps({"cmd": "terminal_stop"})
            )

    asyncio.run(_share())


@terminal_app.command("unshare")
def unshare_terminal(
    workspace: str = typer.Argument(help="Workspace name"),
    terminal: str = typer.Argument(help="Terminal name to unshare"),
) -> None:
    """Stop sharing a terminal."""
    ws, ws_url, token = _resolve_workspace_and_url(workspace)
    max_size = _ws_max_size()

    async def _unshare() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await _wait_workspace_ready(conn, ws.id)

            await conn.send(json.dumps({"cmd": "ui_ready"}))

            # Wait for container_ready
            deadline = asyncio.get_event_loop().time() + 60
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if (
                    msg.get("type") == "event"
                    and isinstance(msg.get("event"), dict)
                    and msg["event"].get("name") == "container_ready"
                ):
                    break

            # Start terminal to get window list
            cols, rows = _get_terminal_size()
            await conn.send(
                json.dumps(
                    {"cmd": "terminal_start", "cols": cols, "rows": rows}
                )
            )
            own_windows: list[dict] = []
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "terminal_windows":
                    own_windows = msg.get("windows", [])
                    break

            match = next(
                (w for w in own_windows if w["name"] == terminal), None
            )
            if match is None:
                _err.print(f"[red]Terminal '{terminal}' not found[/red]")
                raise typer.Exit(code=1)

            await conn.send(
                json.dumps({"cmd": "unshare_window", "window_id": match["id"]})
            )
            # Wait for shared_terminals confirmation
            deadline = asyncio.get_event_loop().time() + 10
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                raw = await asyncio.wait_for(conn.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "shared_terminals":
                    _err.print(
                        f"[green]Terminal '{terminal}' is no longer"
                        " shared[/green]"
                    )
                    break

            await _send_ignore_closed(
                conn, json.dumps({"cmd": "terminal_stop"})
            )

    asyncio.run(_unshare())


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

    Also usable as an rsync transport: rsync -avz -e "klangkc exec" src/ ws:/dest/
    """
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

    server_url = _server_url().rstrip("/")
    ws_url = _build_ws_url(server_url)
    token = _state().get_token(_server_url())

    exit_code = asyncio.run(
        _ws_exec(ws_url, token, ws.id, command, max_size=_ws_max_size())
    )
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

        klangkc sync ~/project my-workspace:/work/project

        klangkc sync my-workspace:/work/output ~/output

        klangkc sync ~/src ws:/work/src --delete --exclude .git
    """
    _require_auth()

    klangkc_bin = shutil.which("klangkc")
    if not klangkc_bin:  # pragma: no cover
        _err.print("[red]Cannot find klangkc in PATH[/red]")
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
        f"{klangkc_bin} exec",
        *ctx.args,
        src,
        dest,
    ]
    _err.print(f"[dim]{' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd)
    raise typer.Exit(code=result.returncode)


@app.command()
def images() -> None:
    """List available container images for workspaces."""
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
    help="Manage container volumes for workspaces.",
    rich_markup_mode="rich",
)
app.add_typer(vol_app, name="volumes")


@app.command()
def invite(
    email: str = typer.Argument(..., help="Email address to invite"),
) -> None:
    """Send an invitation email (admin only)."""
    _require_auth()
    client = _client()
    resp = client.post("/api/v1/admin/invitations", json={"email": email})
    client._check_auth(resp)
    if resp.status_code in (400, 403):
        detail = resp.json().get("detail", resp.text)
        _err.print(f"[red]{detail}[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    Console().print(f"Invitation sent to [bold]{email}[/bold]")


@app.command("invitations")
def list_invitations() -> None:
    """List all invitations (admin only)."""
    _require_auth()
    client = _client()
    resp = client.get("/api/v1/admin/invitations?page_size=200")
    client._check_auth(resp)
    resp.raise_for_status()
    data = resp.json().get("invitations", [])
    if not data:
        typer.echo("No invitations.")
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Email", style="bold")
    table.add_column("Status")
    table.add_column("Invited By")
    table.add_column("Created")
    for inv in data:
        table.add_row(
            inv["email"],
            inv["status"],
            inv.get("invited_by_email", ""),
            inv["created_at"][:10],
        )
    console.print(table)


@vol_app.command("ls")
def volumes_list(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """List klangk-managed container volumes."""
    _require_auth()
    client = _client()
    resp = client.get("/api/v1/volumes")
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
    """Create a named container volume."""
    _require_auth()
    client = _client()
    resp = client.post("/api/v1/volumes", json={"name": name})
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
    """Delete a named container volume."""
    _require_auth()
    client = _client()
    resp = client.delete(f"/api/v1/volumes/{name}")
    client._check_auth(resp)
    if resp.status_code == 403:
        _err.print(f"[red]Permission denied:[/red] {name}")
        raise typer.Exit(code=1)
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
