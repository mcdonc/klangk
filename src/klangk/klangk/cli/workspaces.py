"""Workspace commands: ls, create, dup, rm, members, restart, stop, start, export, import.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TransferSpeedColumn,
)
from rich.table import Table

from .client import KlangkClient  # noqa: F401 (type annotations)
from .client import WorkspaceNotFoundError
from . import context
from .mount import validate_allowed_domain_spec, validate_mount_spec


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


@context.app.command("ls")
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
    context.require_auth()
    client = context._client()
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


@context.app.command()
def create(
    name: str = typer.Argument(..., help="Workspace name"),
    image: str | None = typer.Option(
        None, "--image", help="Container image to use (see `klangk images`)"
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help="Start container automatically on server boot",
    ),
    per_handle_home: bool | None = typer.Option(
        None,
        "--per-handle-home/--shared-home",
        help=(
            "Home layout: per-handle gives each member a private "
            "/home/<handle>; shared puts everyone in /home/klangk. "
            "Omitted = server default"
        ),
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
        help="Service shell command (see `klangk edit --command`).",
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
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        help="Allowed egress domain, repeatable (e.g. github.com:443, pypi.org)",
    ),
    reject: list[str] | None = typer.Option(
        None,
        "--reject",
        help=(
            "Rejected egress domain (NXDOMAIN'd), repeatable "
            "(e.g. evil.example.com). CIDR ranges are not supported."
        ),
    ),
    idle_timeout: int | None = typer.Option(
        None,
        "--idle-timeout",
        help="Idle timeout in seconds (0 = never idle out)",
    ),
    cpu_limit: float | None = typer.Option(
        None, "--cpu-limit", help="CPU limit (e.g. 2.0)"
    ),
    memory_limit: str | None = typer.Option(
        None, "--memory-limit", help="Memory limit (e.g. 4g, 512m)"
    ),
    pids_limit: int | None = typer.Option(
        None, "--pids-limit", help="PIDs limit (e.g. 512)"
    ),
    allow_sudo: bool | None = typer.Option(
        None,
        "--sudo/--no-sudo",
        help=(
            "Workspace sudo posture (server-permitting): --no-sudo locks "
            "this workspace down (no passwordless sudo) even when the "
            "server allows it; --sudo follows the server default. Applies "
            "when the container is next created"
        ),
    ),
) -> None:
    """Create a new workspace."""
    context.require_auth()
    if isinstance(mount, list):
        for m in mount:
            err = validate_mount_spec(m)
            if err:
                context._err.print(f"[red]{err}[/red]")
                raise typer.Exit(code=1)
    if isinstance(allow, list):
        for spec in allow:
            err = validate_allowed_domain_spec(spec)
            if err:
                context._err.print(f"[red]{err}[/red]")
                raise typer.Exit(code=1)
    if isinstance(reject, list):
        for spec in reject:
            # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
            err = validate_allowed_domain_spec(spec, allow_cidr=False)
            if err:
                context._err.print(f"[red]{err}[/red]")
                raise typer.Exit(code=1)
    env_dict = _parse_env_list(env) if isinstance(env, list) else None
    settings = _build_settings(
        idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
    )
    try:
        ws = context._client().create_workspace(
            name,
            image=image,
            service_command=command,
            auto_start=auto_start,
            mounts=mount or None,
            env=env_dict,
            health_check=health_check,
            allowed_domains=allow or None,
            rejected_domains=reject or None,
            settings=settings,
            per_handle_home=per_handle_home,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text)
        context._err.print(f"[red]Failed to create workspace:[/red] {detail}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Created workspace [bold]{name}[/bold] ({ws.id[:12]})")


@context.app.command("dup")
def dup(
    source: str = typer.Argument(..., help="Source workspace name"),
    new_name: str = typer.Argument(..., help="New workspace name"),
) -> None:
    """Duplicate a workspace."""
    context.require_auth()
    client = context._client()
    ws = context.resolve_or_exit(client, source)
    resp = client.post(
        f"/api/v1/workspaces/{ws.id}/duplicate", json={"name": new_name}
    )
    if resp.status_code == 409:
        context._err.print(
            f"[red]A workspace named[/red] '{new_name}' [red]already exists[/red]"
        )
        raise typer.Exit(code=1)
    if resp.status_code == 404:
        context._err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    data = resp.json()
    _out = Console()
    _out.print(
        f"Duplicated [bold]{source}[/bold] → [bold]{new_name}[/bold] ({data['id'][:12]})"
    )


@context.app.command("rm")
def rm(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Delete a workspace."""
    context.require_auth()
    try:
        context._client().delete_workspace(name)
    except WorkspaceNotFoundError:
        context._err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Deleted workspace {name}")


@context.app.command("members")
def members(
    workspace: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """List members of a workspace by role."""
    context.require_auth()
    client = context._client()
    ws = context.resolve_or_exit(client, workspace)
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


@context.app.command("restart")
def restart(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Restart the container for a workspace."""
    context.require_auth()
    try:
        context._client().restart_workspace(name)
    except WorkspaceNotFoundError:
        context._err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Restarted workspace {name}")


@context.app.command("stop")
def stop(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Stop the container for a workspace."""
    context.require_auth()
    try:
        context._client().stop_workspace(name)
    except WorkspaceNotFoundError:
        context._err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Stopped workspace {name}")


@context.app.command("start")
def start(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Start the container for a workspace."""
    context.require_auth()
    try:
        context._client().start_workspace(name)
    except WorkspaceNotFoundError:
        context._err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"Started workspace {name}")


@context.app.command("export")
def export_workspace(
    name: str = typer.Argument(..., help="Workspace name"),
    output: Path = typer.Option(
        None, "-o", "--output", help="Output file (default: <name>.tar.gz)"
    ),
) -> None:
    """Export a workspace to a .tar.gz archive (admin only)."""
    context.require_auth()
    client = context._client()
    ws = context.resolve_or_exit(client, name)
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
        context._err.print(f"[red]Export failed:[/red] {e.response.text}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Exported [bold]{name}[/bold] → {out_path}")


@context.app.command("import")
def import_workspace(
    archive: Path = typer.Argument(..., help="Path to .tar.gz archive"),
    name: str = typer.Option(
        None, "--name", help="Override workspace name from archive"
    ),
) -> None:
    """Import a workspace from a .tar.gz archive."""
    context.require_auth()
    if not archive.exists():
        context._err.print(f"[red]File not found:[/red] {archive}")
        raise typer.Exit(code=1)
    client = context._client()
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
        context._err.print(f"[red]Import failed:[/red] {e.response.text}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Imported [bold]{ws.name}[/bold] ({ws.id[:12]})")


_SENTINEL = object()


def _parse_env_list(env_list: list[str]) -> dict[str, str]:
    """Parse ['KEY=VALUE', ...] into a dict."""
    result = {}
    for item in env_list:
        if "=" not in item:
            context._err.print(
                f"[red]Invalid env var (expected KEY=VALUE):[/red] {item}"
            )
            raise typer.Exit(code=1)
        key, _, value = item.partition("=")
        result[key] = value
    return result


def _build_settings(
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None = None,
) -> dict | None:
    """Build a workspace settings dict from CLI flags, or None if all unset."""
    settings: dict = {}
    if idle_timeout is not None:
        settings["idle_timeout"] = idle_timeout
    if cpu_limit is not None:
        settings["cpu_limit"] = cpu_limit
    if memory_limit is not None:
        settings["memory_limit"] = memory_limit
    if pids_limit is not None:
        settings["pids_limit"] = pids_limit
    # #2017: None (flag omitted) leaves the bag untouched — the workspace
    # follows the deploy posture. False locks the workspace down; True is
    # the explicit "follow the server" (the server setting is a ceiling,
    # so True can never raise sudo above the deploy default).
    if allow_sudo is not None:
        settings["allow_sudo"] = allow_sudo
    return settings or None


class _SENTINEL:
    pass


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
