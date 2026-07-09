"""Klangk CLI — typer app."""

from __future__ import annotations


import asyncio
import io
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import typer
import websockets
from rich.console import Console
from rich.live import Live
from rich.prompt import Prompt
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TransferSpeedColumn,
)
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .auth import (
    fetch_config,
    local_login,
    login,
    logout as do_logout,
    refresh_token,
)
from .client import (
    AuthError,
    KlangkClient,
    WorkspaceNotFoundError,
    decode_token_claims,
    drain_stdin,
    get_terminal_size,
    send_ignore_closed,
    exec_on_ws,
    wait_container_ready,
    ws_exec,
    ws_shell,
    reset_terminal,
    _server_mode_is_none,
)
from .config import CLIConfig, CLIState, seed_config
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
def app_callback(
    server: str | None = typer.Option(
        None, "--server", help="Server alias or URL"
    ),
) -> None:
    global _server_override
    if server is not None:
        _server_override = _cfg().resolve_server(server)


def server_url() -> str:
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
    return KlangkClient(server_url(), _state().get_token(server_url()))


def ws_max_size() -> int:
    return _cfg().get_ws_max_size(server_url())


_err = Console(stderr=True)


def require_auth() -> None:
    """Ensure the active server has a usable token.

    In ``none`` (no-auth) mode the server freely issues a token for the
    seeded default user, so any command auto-logs in on first run rather
    than demanding a prior ``klangkc login`` (#1374). The server's mode is
    probed live (not cached) so a mode switch takes effect immediately:
    flipping none->password after a command auto-logged in still leaves
    that token valid until it expires, but a *fresh* command with no
    stored token will see the new mode and not auto-login.
    """
    state = _state()
    url = server_url()
    if state.get_token(url):
        return
    if _maybe_none_login(state, url):
        return
    _err.print(
        "[red]Not logged in[/red] — run [bold]klangkc login[/bold] first."
    )
    raise typer.Exit(code=1)


def _maybe_none_login(state: CLIState, url: str) -> bool:
    """If the server is in ``none`` mode, fetch a free token and store it.

    Returns True on success (token stored, ``require_auth`` proceeds).
    Returns False if the server is not in ``none`` mode or unreachable,
    leaving the caller to emit the normal "Not logged in" error. The
    mode is probed live via /config on every call (no cache) — cheap for
    a single command entry point, and the only way to stay correct across
    a mode switch.
    """
    config = fetch_config(url)
    if not isinstance(config, dict):
        return False
    if config.get("auth_modes") != "none":
        return False
    try:
        email, token = local_login(url)
    except SystemExit:
        return False
    state.set_credentials(url, email, token)
    state.save()
    seed_config(url, email)
    return True


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
    """Show connection info (server, user, admin status)."""
    # status works even with no active server (unlike other commands).
    url = _server_override or _state().active_server
    state = _state()
    token = state.get_token(url) if url else None
    email = state.get_email(url) if url else None
    user_id = decode_token_claims(token).get("sub") if token else None
    # Admin status comes from /my-permissions (the canonical source the
    # frontend uses for isAdmin). Best-effort: if the probe fails (offline,
    # token expired, old server without /admin in the static set) status
    # still reports everything else rather than erroring out.
    is_admin: bool | None = None
    if token:
        try:
            client = _client()
            resp = client.get("/api/v1/my-permissions")
            client.check_auth(resp)
            if resp.status_code == 200:
                perms = resp.json().get("permissions", {})
                is_admin = "*" in perms.get("/admin", [])
        except Exception:
            is_admin = None
    if plain:
        print(f"server={url or '(none)'}")
        if token:
            print(f"user={email or 'unknown'}")
            print(f"user_id={user_id or 'unknown'}")
            print("status=logged_in")
            if is_admin is not None:
                print(f"admin={'yes' if is_admin else 'no'}")
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
        table.add_row("User ID", user_id or "unknown")
        table.add_row("Status", "[green]logged in[/green]")
        if is_admin:
            table.add_row("Admin", "[green]yes[/green]")
        elif is_admin is False:
            table.add_row("Admin", "no")
    else:
        table.add_row("Status", "[yellow]not logged in[/yellow]")
    console.print(table)


def workspace_status(ws) -> tuple[str, str]:
    """Return ``(label, rich_markup)`` describing a workspace's runtime state.

    The label is terminal-safe plain text; the markup is the colorized form
    for the rich table. Collapses two independent backend fields --
    ``running`` (container process up?) and ``health`` (the health-check
    probe result, if any) -- into one readable word:

    - not running                       -> stopped   (dim)
    - running, no health-check configured-> running   (green)
    - running + health=healthy          -> healthy   (green)
    - running + health=unhealthy        -> unhealthy (red)
    - running, health-check set, no
      probe completed yet              -> starting  (yellow)

    A workspace with no ``health_check`` never gets probed, so ``health``
    stays None forever -- ``starting`` would be a lie. ``running`` is the
    honest label: the container is up, and we make no health claim.
    """
    if not ws.running:
        return "stopped", "[dim]stopped[/dim]"
    if not ws.health_check:
        return "running", "[green]running[/green]"
    if ws.health == "healthy":
        return "healthy", "[green]healthy[/green]"
    if ws.health == "unhealthy":
        return "unhealthy", "[red]unhealthy[/red]"
    return "starting", "[yellow]starting[/yellow]"


def short_id(ws_id: str) -> str:
    """Shorten a workspace id to ``abc…xyz`` (first 3 + ellipsis + last 3).

    Long ids crowd the table; this keeps the column narrow while still
    distinguishing workspaces at a glance. Short ids are returned as-is.
    """
    if len(ws_id) <= 7:
        return ws_id
    return f"{ws_id[:3]}…{ws_id[-3:]}"


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
    require_auth()
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
            status, _ = workspace_status(ws)
            typer.echo(
                f"  {ws.name}  ({short_id(ws.id)})  "
                f"{status}  {ws.created_at[:10]}"
            )
        if shared_workspaces:
            typer.echo("Shared with me:")
            for ws in shared_workspaces:
                status, _ = workspace_status(ws)
                owner = f"  by {ws.owner_email}" if ws.owner_email else ""
                typer.echo(
                    f"  {ws.name}  ({short_id(ws.id)})  "
                    f"{status}  {ws.created_at[:10]}{owner}"
                )
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Created")
    if shared:
        table.add_column("Owner")
    for ws in workspaces:
        _, markup = workspace_status(ws)
        row = [ws.name, short_id(ws.id), markup, ws.created_at[:10]]
        if shared:
            row.append("")
        table.add_row(*row)
    for ws in shared_workspaces:
        _, markup = workspace_status(ws)
        row = [
            ws.name,
            short_id(ws.id),
            markup,
            ws.created_at[:10],
            ws.owner_email or "",
        ]
        table.add_row(*row)
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
    health_check: str | None = typer.Option(
        None,
        "--health-check",
        help=(
            "Shell command polled inside the container to gauge service "
            "health (exit 0 = healthy). See the Health Check docs."
        ),
    ),
    command: str | None = typer.Option(
        None,
        "--command",
        "-c",
        help="Service shell command (see `klangkc edit --command`).",
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
    require_auth()
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
            service_command=command,
            auto_start=auto_start,
            mounts=mount or None,
            env=env_dict,
            health_check=health_check,
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
    require_auth()
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
    require_auth()
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
    require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None
    resp = client.get(f"/api/v1/workspaces/{ws.id}/roles")
    client.check_auth(resp)
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
    require_auth()
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
    require_auth()
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
    require_auth()
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
        None, "--command", "-c", help="Service shell command (use '' to clear)"
    ),
    auto_start: bool | None = typer.Option(
        None,
        "--auto-start/--no-auto-start",
        help="Start container automatically on server boot",
    ),
    health_check: str | None = typer.Option(
        None,
        "--health-check",
        help=(
            "Shell command polled inside the container to gauge service "
            "health (exit 0 = healthy). Use '' to clear."
        ),
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
    require_auth()
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
        or health_check is not None
        or isinstance(mount, list)
        or isinstance(env, list)
    )
    if not has_flags:
        # Interactive mode
        new_name = _prompt("Name", ws.name)
        new_image = _prompt("Container Image", ws.image)
        new_command = _prompt("Service shell command", ws.service_command)
        new_health_check = _prompt("Health check command", ws.health_check)

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
            body["service_command"] = new_command or None
        if new_health_check is not _SENTINEL:
            body["health_check"] = new_health_check or None
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
            body["service_command"] = command or None
        if auto_start is not None:
            body["auto_start"] = auto_start
        if health_check is not None:
            body["health_check"] = health_check or None
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


def build_ws_url(server_url: str) -> str:
    """Convert an HTTP(S) server URL to a WebSocket URL."""
    if server_url.startswith("http://"):
        return server_url.replace("http://", "ws://") + "/ws"
    elif server_url.startswith("https://"):
        return server_url.replace("https://", "wss://") + "/ws"
    return f"ws://{server_url}/ws"


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
    token = _state().get_token(server_url())
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
    base_url = server_url().rstrip("/")
    ws_url = build_ws_url(base_url)

    _err.print(f"Connecting to [bold]{ws.name}[/bold]...")
    _err.print("[dim]Escape: Enter, then ~.[/dim]")
    forward_agent = resolve_forward_agent(
        forward_agent,
        config_default=_cfg().get_forward_agent(server_url()) or False,
    )
    try:
        asyncio.run(
            ws_shell(
                ws_url,
                token,
                ws.id,
                window=terminal,
                forward_agent=forward_agent,
                max_size=ws_max_size(),
            )
        )
    except websockets.InvalidStatus as e:
        reset_terminal()
        drain_stdin()
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
        drain_stdin()
        _err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None


def _dispatch_monitor_event(msg: dict, command: list[str]) -> None:
    """Act on one server event.

    With no *command*, the event is streamed as line-delimited JSON to
    stdout. With a command, its stdin gets the event JSON and env vars
    ``KLANGK_EVENT``, ``KLANGK_EVENT_TYPE``, ``KLANGK_WORKSPACE_ID`` and
    (for health events) ``KLANGK_HEALTHY`` / ``KLANGK_HEALTH_MESSAGE``
    are set.

    Pure (no WebSocket) so it can be unit-tested in isolation.
    """
    payload = json.dumps(msg)
    if not command:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return
    env = dict(os.environ)
    env["KLANGK_EVENT"] = payload
    env["KLANGK_EVENT_TYPE"] = str(msg.get("type", ""))
    wid = msg.get("workspace_id")
    if wid is not None:
        env["KLANGK_WORKSPACE_ID"] = str(wid)
    if msg.get("type") == "service_health":
        env["KLANGK_HEALTHY"] = "true" if msg.get("healthy") else "false"
        # ``running`` distinguishes "unhealthy check" from "container
        # stopped" -- both have healthy=false, but a death frame carries
        # running=false (#1175 item 2).  Defaults to true for older
        # servers that don't send the field.
        env["KLANGK_RUNNING"] = "true" if msg.get("running", True) else "false"
        health_msg = msg.get("health_message")
        if health_msg:
            env["KLANGK_HEALTH_MESSAGE"] = str(health_msg)
        checked_at = msg.get("health_checked_at")
        if checked_at:
            env["KLANGK_HEALTH_CHECKED_AT"] = str(checked_at)
        seq = msg.get("seq")
        if seq is not None:
            env["KLANGK_HEALTH_SEQ"] = str(seq)
    # FileNotFoundError (missing binary) propagates to the caller.
    subprocess.run(command, input=payload.encode(), env=env, check=False)


async def monitor_connection(
    ws_url: str,
    token: str,
    max_size: int,
    command: list[str],
    types: list[str],
    workspaces: list[str],
) -> None:
    """One connection: dispatch events until the socket closes.

    Network/auth errors propagate to :func:`monitor_run`, which owns
    reconnect + refresh. Filtering by event type and workspace id is
    applied here so the dispatcher only sees relevant events.
    """
    type_filter = {t for t in types}
    ws_filter = {w for w in workspaces}
    async with websockets.connect(
        f"{ws_url}?token={token}", max_size=max_size
    ) as conn:
        async for raw in conn:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = msg.get("type")
            if etype is None:
                continue  # control/ack messages aren't events
            if type_filter and etype not in type_filter:
                continue
            wid = msg.get("workspace_id")
            if ws_filter and (wid is None or wid not in ws_filter):
                continue
            _dispatch_monitor_event(msg, command)


def monitor_backoff(attempt: int, max_delay: float) -> float:
    """Capped exponential backoff with jitter (mirrors the web UI)."""
    base = min(1 << attempt, max_delay)
    jitter = random.random() * base
    return (base + jitter) / 2


async def refresh_token_threaded(server_url: str, token: str) -> str | None:
    """Refresh the JWT off-loop; returns the new token or None.

    In ``none`` (no-auth) mode a refresh failure falls back to a free
    re-login via ``/auth/local`` — re-login costs nothing, so it's
    strictly better than reconnecting with a dead token (#1374).
    """
    new = await asyncio.to_thread(refresh_token, server_url, token)
    if new:
        return new
    if await asyncio.to_thread(_server_mode_is_none, server_url):
        try:
            _email, new = await asyncio.to_thread(local_login, server_url)
        except SystemExit:
            return None
        return new
    return None


async def monitor_run(
    server_url: str,
    ws_url: str,
    token: str,
    max_size: int,
    command: list[str],
    types: list[str],
    workspaces: list[str],
    *,
    max_reconnects: int | None,
    max_delay: float,
) -> None:
    """Run the monitor with automatic reconnect + JWT refresh.

    Reconnects indefinitely when *max_reconnects* is ``None`` (the
    default), or up to that many times, with capped exponential
    backoff. On an auth-related close (HTTP/WS 4001 or 4002) it tries
    to refresh the JWT via the server's refresh endpoint before
    reconnecting; if refresh fails it keeps retrying with the current
    token so the monitor self-heals once the server/token recovers.
    """
    current_token = token
    _err.print("[green]Monitoring events. Press Ctrl+C to stop.[/green]")
    attempt = 0
    while True:
        auth_close = False
        try:
            await monitor_connection(
                ws_url, current_token, max_size, command, types, workspaces
            )
            reason = "connection closed"
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            auth_close = code in (4001, 4002)
            reason = f"closed (code {code})"
        except websockets.InvalidStatus as exc:
            code = exc.response.status_code
            auth_close = code in (4001, 4002)
            reason = f"rejected (HTTP {code})"
        except (OSError, asyncio.TimeoutError) as exc:
            reason = f"network error: {exc}"

        # On an auth-related close, try to refresh the JWT. A successful
        # refresh lets the next attempt authenticate cleanly; a failed
        # one still reconnects (the server/token may recover).
        if auth_close:
            new = await refresh_token_threaded(server_url, current_token)
            if new:
                current_token = new
                _err.print("[green]Token refreshed.[/green]")
            else:
                _err.print(
                    "[yellow]Token refresh failed; retrying with the"
                    " current token.[/yellow]"
                )

        if max_reconnects is not None and attempt >= max_reconnects:
            _err.print(
                f"[red]{reason}; max reconnects ({max_reconnects})"
                " reached, giving up.[/red]"
            )
            raise typer.Exit(code=1)
        attempt += 1
        delay = monitor_backoff(attempt, max_delay)
        _err.print(
            f"[yellow]{reason}; reconnecting in {delay:.1f}s"
            f" (attempt {attempt})...[/yellow]"
        )
        await asyncio.sleep(delay)


@app.command()
def monitor(
    command: list[str] = typer.Argument(
        None,
        help=(
            "Optional command to run for each event. Pass it after '--' "
            "so its own flags aren't parsed by klangkc."
        ),
    ),
    event_type: list[str] = typer.Option(
        [],
        "--type",
        "-t",
        help=(
            "Only react to these event types (repeatable). Common: "
            "service_health, container_status, workspaces_changed."
        ),
    ),
    workspace: list[str] = typer.Option(
        [],
        "--workspace",
        "-w",
        help="Only react to events for these workspace ids (repeatable).",
    ),
    no_reconnect: bool = typer.Option(
        False,
        "--no-reconnect",
        help="Exit after the first disconnect instead of reconnecting.",
    ),
    max_reconnects: int | None = typer.Option(
        None,
        "--max-reconnects",
        help=(
            "Stop after this many failed reconnects. Default: retry"
            " forever. Implied as 0 by --no-reconnect."
        ),
    ),
    max_delay: float = typer.Option(
        60.0,
        "--max-delay",
        help="Cap (seconds) on the reconnect backoff.",
    ),
) -> None:
    """Stream server events, optionally running a command for each.

    Connects to the server and listens for the same events the web UI
    receives (health-check transitions, container starts/stops, workspace
    changes). With no command, events are printed as line-delimited JSON
    (pipe to jq to inspect). With a command after '--', the command's
    stdin gets the event JSON and env vars KLANGK_EVENT_TYPE,
    KLANGK_WORKSPACE_ID, and (for health events) KLANGK_HEALTHY,
    KLANGK_RUNNING, KLANGK_HEALTH_MESSAGE, KLANGK_HEALTH_CHECKED_AT and
    KLANGK_HEALTH_SEQ are set.

    ``service_health`` frames now carry ``running`` (#1175 item 2): a
    container death emits a frame with ``healthy=false`` *and*
    ``running=false`` (KLANGK_RUNNING=false), so a command can tell
    "check failed" from "container stopped" without also subscribing
    to ``container_status``. ``health_checked_at`` / ``seq`` give
    freshness and gap detection.

    A separate ``service_health_heartbeat`` event type is available for
    liveness: send ``{"cmd": "subscribe_health_heartbeat", "enabled":
    true}`` to opt in, and the server ticks a heartbeat each health-loop
    interval. It's its own type, so ``--type service_health`` filters it
    out; drop the filter to observe it.

    The monitor reconnects automatically (by default forever, with
    capped exponential backoff) and refreshes its JWT on auth failures,
    so it survives server restarts and token expiry. Use
    ``--max-reconnects`` or ``--no-reconnect`` to bound it.

    \b
    Examples:
      klangkc monitor                                # stream all events
      klangkc monitor --type service_health | jq .   # pretty health events
      klangkc monitor --type service_health -- sh -c \
        '[ "$KLANGK_HEALTHY" = false ] && notify-send "Service unhealthy"'
      klangkc monitor --type service_health -- sh -c \
        '[ "$KLANGK_RUNNING" = false ] && echo "container stopped"'
      klangkc monitor --workspace <id> --type service_health
    """
    require_auth()
    base_url = server_url().rstrip("/")
    ws_url = build_ws_url(base_url)
    token = _state().get_token(base_url)
    if not token:  # pragma: no cover  # require_auth already guards this
        _err.print("[red]Not logged in. Run `klangkc login` first.[/red]")
        raise typer.Exit(code=1)
    effective_max = 0 if no_reconnect else max_reconnects
    try:
        asyncio.run(
            monitor_run(
                base_url,
                ws_url,
                token,
                max_size=ws_max_size(),
                command=list(command) if command else [],
                types=event_type,
                workspaces=workspace,
                max_reconnects=effective_max,
                max_delay=max_delay,
            )
        )
    except websockets.InvalidStatus as e:
        # A rejection during the very first connect (before the loop's
        # reconnect path is established).
        _err.print(f"[red]Connection rejected: {e}[/red]")
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        _err.print("[dim]Stopped.[/dim]")


def _resolve_workspace_and_url(
    workspace_name: str,
) -> tuple:
    """Resolve a workspace by name and return (ws, ws_url, token)."""
    require_auth()
    client = _client()
    try:
        ws = client.resolve_workspace(workspace_name)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace_name}'")
        raise typer.Exit(code=1) from None
    base_url = server_url().rstrip("/")
    ws_url = build_ws_url(base_url)
    return ws, ws_url, _state().get_token(server_url())


async def sandbox_setup(ws, config, sandbox_root, handle):
    """Copy files and run setup script on an open WebSocket.

    Called once after workspace creation, before the shell starts.
    The caller has already connected and called wait_container_ready.

    Returns the setup script's exit code, or ``None`` if no setup
    command was configured (in which case there is nothing to fail).
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
        exit_code = await exec_on_ws(
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
        exit_code = await exec_on_ws(
            ws,
            ["sh", "-c", shell_cmd],
            stdout=sys.stderr.buffer,
            timeout=timeout,
        )
        if exit_code == 124:
            _err.print(f"[yellow]Setup timed out after {timeout}s[/yellow]")
        elif exit_code != 0:
            _err.print(f"[yellow]Setup exited with code {exit_code}[/yellow]")
        return exit_code
    return None


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
    token = _state().get_token(server_url())
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
    ws_url = build_ws_url(server_url().rstrip("/"))
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
            service_command=config.service_command,
            auto_start=config.auto_start,
            mounts=all_mounts,
            setup_state="pending"
            if resolve_setup_command(config, handle)
            else None,
            health_check=config.health_check,
        )
        _err.print(f"Workspace [bold]{workspace}[/bold] created.")
        created = True

    need_setup = created or force

    if need_setup:
        _err.print(f"Connecting to [bold]{workspace}[/bold] for setup...")
        try:
            asyncio.run(
                sandbox_setup_only(
                    ws_url,
                    token,
                    ws.id,
                    config,
                    sandbox_root,
                    handle,
                    max_size=ws_max_size(),
                    client=client,
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


async def sandbox_setup_only(
    ws_url,
    token,
    workspace_id,
    config,
    sandbox_root,
    handle,
    max_size=None,
    client=None,
):
    """Connect to workspace, run setup, then disconnect (no shell).

    After setup.sh returns, marks the workspace's ``setup_state``
    (#1033): ``complete`` on success (or when no setup command is
    configured), ``failed`` otherwise. Only fires the service command
    via ``terminal_start`` on success -- a failed setup must not
    auto-run the service command (that is the failure-masquerade the
    issue objects to). The state is marked BEFORE ``terminal_start``
    is sent, so the server reads ``complete`` from the DB when it
    decides whether to create the service-cmd window.
    """
    kwargs = {}
    if max_size is not None:
        kwargs["max_size"] = max_size
    async with websockets.connect(f"{ws_url}?token={token}", **kwargs) as ws:
        await wait_container_ready(ws, workspace_id)
        # Re-enter 'pending' before running setup (#1033). On first
        # create the workspace is already 'pending', but on --force
        # re-setup it may be 'complete'/'failed'; either way this is
        # idempotent and ensures a visitor during (re-)setup is blocked
        # from firing the service command prematurely.
        if client is not None:
            try:
                await asyncio.to_thread(
                    client.set_setup_state, workspace_id, "pending"
                )
            except Exception as e:  # pragma: no cover
                _err.print(
                    f"[yellow]Warning: could not mark setup_state"
                    f" = pending: {e}[/yellow]"
                )
        exit_code = await sandbox_setup(ws, config, sandbox_root, handle)

        # Mark setup_state before anything else (#1033). 'complete'
        # when setup ran and returned 0, or when there was no setup
        # command at all (nothing to fail); 'failed' otherwise.
        setup_ok = exit_code is None or exit_code == 0
        new_state = "complete" if setup_ok else "failed"
        if client is not None:
            try:
                await asyncio.to_thread(
                    client.set_setup_state, workspace_id, new_state
                )
            except Exception as e:  # pragma: no cover
                _err.print(
                    f"[yellow]Warning: could not mark setup_state"
                    f" = {new_state}: {e}[/yellow]"
                )

        # After setup, start a terminal so the service command runs
        # in a dedicated "service-cmd" tmux window (visible as a tab
        # but not occupying the user's interactive window 0). Skipped
        # on setup failure -- the service command's prerequisites are
        # not met.
        if config.service_command and setup_ok:
            await ws.send(
                json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
            )
            # Wait (bounded) for the terminal to start so the default
            # command actually runs before we disconnect.  Other
            # messages are ignored.
            deadline = asyncio.get_event_loop().time() + 30
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:  # pragma: no cover
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    break
                if json.loads(raw).get("type") == "terminal_started":
                    break


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
    max_size = ws_max_size()

    # We need to start a terminal to get the window list, then also
    # get shared terminals. Use _ws_command to get each.
    async def _list() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await wait_container_ready(conn, ws.id)

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
            cols, rows = get_terminal_size()
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

            await send_ignore_closed(
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
    require_auth()
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
    require_auth()
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
    max_size = ws_max_size()

    async def _share() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await wait_container_ready(conn, ws.id)

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
            cols, rows = get_terminal_size()
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

            await send_ignore_closed(
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
    max_size = ws_max_size()

    async def _unshare() -> None:
        async with websockets.connect(
            f"{ws_url}?token={token}", max_size=max_size
        ) as conn:
            await wait_container_ready(conn, ws.id)

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
            cols, rows = get_terminal_size()
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

            await send_ignore_closed(
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
    argv with no shell (used by ``klangkc sync``'s rsync transport).

    Also usable as an rsync transport:
    rsync -avz -e "klangkc exec --raw" src/ ws:/dest/
    """
    require_auth()

    command = ctx.args
    # With allow_extra_args + allow_interspersed_args=False, Click does
    # NOT consume the ``--`` end-of-options separator -- it lands in
    # ctx.args verbatim (verified), so ``klangkc exec ws -- echo hi``
    # would try to run ``--`` as a command. Strip a single leading
    # ``--`` so the conventional separator works. A ``--`` elsewhere is
    # left alone (it is then a real command argument).
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        _err.print("[red]No command specified[/red]")
        raise typer.Exit(code=1)

    client = _client()
    try:
        ws = client.resolve_workspace(workspace)
    except WorkspaceNotFoundError:
        _err.print(f"[red]No workspace named[/red] '{workspace}'")
        raise typer.Exit(code=1) from None

    base_url = server_url().rstrip("/")
    ws_url = build_ws_url(base_url)
    token = _state().get_token(server_url())

    exit_code = asyncio.run(
        ws_exec(
            ws_url,
            token,
            ws.id,
            command,
            max_size=ws_max_size(),
            login=not raw,
        )
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
    require_auth()

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
        # ``--raw`` so the rsync transport runs raw argv (no login
        # shell): rsync's binary protocol must not be corrupted by a
        # ~/.profile that prints to stdout, and rsync shell-quotes its
        # argv so a non-login round-trips cleanly. See #1041.
        f"{klangkc_bin} exec --raw",
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
    require_auth()
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
app.add_typer(admin_app, name="admin")

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


def _admin_error(resp) -> None:
    """Print a backend error detail and exit 1 for an admin API response."""
    detail = (
        resp.json().get("detail", resp.text)
        if resp.headers.get("content-type", "").startswith("application/json")
        else resp.text
    )
    _err.print(f"[red]{detail}[/red]")
    raise typer.Exit(code=1)


@admin_users_app.command("ls")
def admin_users_ls(
    page: int = typer.Option(1, "--page", help="Page number"),
    page_size: int = typer.Option(
        50, "--page-size", help="Users per page (max 200)"
    ),
) -> None:
    """List all user accounts (admin only)."""
    require_auth()
    client = _client()
    resp = client.get(
        "/api/v1/admin/users",
        params={"page": page, "page_size": page_size},
    )
    client.check_auth(resp)
    if resp.status_code != 200:
        _admin_error(resp)
    body = resp.json()
    users = body.get("users", [])
    if not users:
        typer.echo("No users.")
        return
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("ID", style="dim")
    table.add_column("Email", style="bold")
    table.add_column("Handle")
    table.add_column("Verified")
    table.add_column("Provider")
    table.add_column("Created")
    for u in users:
        table.add_row(
            u["id"],
            u["email"],
            u.get("handle") or "",
            "yes" if u.get("verified") else "no",
            u.get("provider") or "password",
            (u.get("created_at") or "")[:10],
        )
    total = body.get("total", len(users))
    console.print(table)
    if total > len(users):
        console.print(
            f"\n[dim]Showing {len(users)} of {total} "
            f"(use --page to see more)[/dim]"
        )


@admin_users_app.command("set-password")
def admin_users_set_password(
    email: str = typer.Argument(..., help="Email of the user to update"),
    password: str | None = typer.Option(
        None,
        "--password",
        "-p",
        help="New password (prompted if omitted)",
    ),
) -> None:
    """Set a user's password (admin only).

    Resolves the email to a user id, then PATCHes the password. Used to
    give the seeded default (no-password) user a real credential before
    switching the server from `none` to `password` mode — the
    self-service `change-password` route refuses accounts with no
    password hash, so this is the non-lockout path for the hero.
    """
    require_auth()
    client = _client()
    # Resolve email -> user id. /users/search is prefix-match (LIKE), so
    # exact-match the result; emails are unique so there's at most one.
    search = client.get("/api/v1/users/search", params={"q": email})
    client.check_auth(search)
    if search.status_code != 200:
        _admin_error(search)
    matches = [u for u in search.json() if u.get("email") == email]
    if not matches:
        _err.print(f"[red]No user found with email {email}[/red]")
        raise typer.Exit(code=1)
    user_id = matches[0]["id"]

    if password is None:
        password = Prompt.ask("[bold]New password[/bold]", password=True)
        confirm = Prompt.ask("[bold]Confirm password[/bold]", password=True)
        if password != confirm:
            _err.print("[red]Passwords do not match[/red]")
            raise typer.Exit(code=1)

    resp = client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"password": password},
    )
    client.check_auth(resp)
    if resp.status_code != 200:
        _admin_error(resp)
    Console().print(f"Password set for [bold]{email}[/bold]")


@admin_invitations_app.command("send")
def admin_invitations_send(
    email: str = typer.Argument(..., help="Email address to invite"),
) -> None:
    """Send an invitation email (admin only)."""
    require_auth()
    client = _client()
    resp = client.post("/api/v1/admin/invitations", json={"email": email})
    client.check_auth(resp)
    if resp.status_code != 200:
        _admin_error(resp)
    Console().print(f"Invitation sent to [bold]{email}[/bold]")


@admin_invitations_app.command("ls")
def admin_invitations_ls() -> None:
    """List all invitations (admin only)."""
    require_auth()
    client = _client()
    resp = client.get("/api/v1/admin/invitations?page_size=200")
    client.check_auth(resp)
    if resp.status_code != 200:
        _admin_error(resp)
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
    require_auth()
    client = _client()
    resp = client.get("/api/v1/volumes")
    client.check_auth(resp)
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
    require_auth()
    client = _client()
    resp = client.post("/api/v1/volumes", json={"name": name})
    client.check_auth(resp)
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
    require_auth()
    client = _client()
    resp = client.delete(f"/api/v1/volumes/{name}")
    client.check_auth(resp)
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
