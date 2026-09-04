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
from .client import AuthError, WorkspaceNotFoundError
from . import context
from .mount import validate_allowed_domain_spec, validate_mount_spec
from .options import (
    AllowOption,
    ClassificationBannerOption,
    CpuLimitOption,
    EnvOption,
    IdleTimeoutOption,
    MemoryLimitOption,
    MountOption,
    PidsLimitOption,
    RejectOption,
    SudoOption,
)


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


def fetch_shared_workspaces(
    client, shared: bool, limit, all_pages, sort, order, q
):
    """Shared-with-me workspaces when requested, else empty."""
    if not shared:
        return []
    return client.list_shared_workspaces(
        limit=limit, all_pages=all_pages, sort=sort, order=order, q=q
    )


def workspace_row(ws, shared: bool) -> list[str]:
    """One rich-table row for an owned workspace."""
    _, markup = workspace_status(ws)
    row = [ws.name, short_id(ws.id), markup, ws.created_at[:10]]
    if shared:
        row.append("")
    return row


def shared_workspace_row(ws) -> list[str]:
    """One rich-table row for a shared-with-me workspace (with owner)."""
    _, markup = workspace_status(ws)
    return [
        ws.name,
        short_id(ws.id),
        markup,
        ws.created_at[:10],
        ws.owner_email or "",
    ]


def print_workspace_table(workspaces, shared_workspaces, shared: bool) -> None:
    """The rich-table listing (owned workspaces, then shared-with-me)."""
    console = Console()
    table = Table(box=None, pad_edge=False)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Created")
    if shared:
        table.add_column("Owner")
    for ws in workspaces:
        table.add_row(*workspace_row(ws, shared))
    for ws in shared_workspaces:
        table.add_row(*shared_workspace_row(ws))
    console.print(table)


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
    client = context.client()
    workspaces = client.list_workspaces(
        limit=limit,
        all_pages=all_workspaces,
        sort=sort,
        order=order,
        q=filter,
    )
    shared_workspaces = fetch_shared_workspaces(
        client, shared, limit, all_workspaces, sort, order, filter
    )
    if not workspaces and not shared_workspaces:
        typer.echo("No workspaces found.")
        return
    if plain:
        _print_plain_listing(workspaces, shared_workspaces)
        return
    print_workspace_table(workspaces, shared_workspaces, shared)


def _print_plain_listing(workspaces, shared_workspaces) -> None:
    """The --plain text listing (owned workspaces, then shared-with-me)."""
    for ws in workspaces:
        status, _ = workspace_status(ws)
        typer.echo(
            f"  {ws.name}  ({short_id(ws.id)})  {status}  {ws.created_at[:10]}"
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


def validated_or_exit(specs: list[str], validate) -> None:
    """Exit 1 on the first invalid spec in a repeatable-flag list."""
    for spec in specs:
        err = validate(spec)
        if err:
            context.err.print(f"[red]{err}[/red]")
            raise typer.Exit(code=1)


def validate_create_specs(mount, allow, reject) -> None:
    """Validate repeatable create options up front; exits 1 on the first
    invalid spec."""
    if isinstance(mount, list):
        validated_or_exit(mount, validate_mount_spec)
    if isinstance(allow, list):
        validated_or_exit(allow, validate_allowed_domain_spec)
    if isinstance(reject, list):
        # CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
        validated_or_exit(
            reject,
            lambda spec: validate_allowed_domain_spec(spec, allow_cidr=False),
        )


def ensure_autostart_allowed(client, requested) -> None:
    """Refuse up front when the deploy forbids auto-start (#3184).

    ``KLANGKD_ALLOW_AUTOSTART`` is a ceiling on per-workspace auto-start:
    the web UI and the TUI forms hide their Auto start control when it
    is off, so ``klangk create --auto-start`` / ``klangk edit
    --auto-start`` check it too — before any create/edit request is
    sent, so no workspace is created and no after-the-fact 400 surfaces.
    Only opting in is capped: ``requested`` falsy (flag omitted, or
    ``--no-auto-start``) returns without a config round trip. A
    config-fetch failure degrades to "allowed": the request itself
    reports the real error, and the server enforces the ceiling
    regardless (``_check_autostart`` in ``api/workspaces.py``).
    """
    if not requested:
        return
    try:
        allowed = client.config().get("allow_autostart") is True
    except (httpx.HTTPError, AuthError, ValueError):
        return
    if not allowed:
        context.err.print(
            "[red]Auto-start is not enabled on this server"
            " (set KLANGKD_ALLOW_AUTOSTART=1)[/red]"
        )
        raise typer.Exit(code=1)


def create_workspace_or_exit(
    client,
    name,
    *,
    image,
    command,
    auto_start,
    mount,
    env_dict,
    health_check,
    allow,
    reject,
    settings,
    per_handle_home,
    classification_banner,
):
    """Create the workspace, exiting 1 with the server's detail on failure."""
    try:
        return client.create_workspace(
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
            classification_banner=classification_banner,
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text)
        context.err.print(f"[red]Failed to create workspace:[/red] {detail}")
        raise typer.Exit(code=1) from None


@context.app.command()
def create(
    name: str = typer.Argument(..., help="Workspace name"),
    image: str | None = typer.Option(
        None, "--image", help="Container image to use (see `klangk images`)"
    ),
    auto_start: bool = typer.Option(
        False,
        "--auto-start",
        help=(
            "Start container automatically on server boot"
            " (requires KLANGKD_ALLOW_AUTOSTART=1 on the server)"
        ),
    ),
    per_handle_home: bool | None = typer.Option(
        None,
        "--per-handle-home/--shared-home",
        help=(
            "Home layout (server-permitting): per-handle gives each "
            "member a private /home/<handle>; shared puts everyone in "
            "/home/klangk. Requires KLANGKD_PER_HANDLE_HOME=true on "
            "the server — otherwise every workspace gets the shared "
            "home. Omitted = server default; applies when the container "
            "is next created"
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
    mount: MountOption = None,
    env: EnvOption = None,
    allow: AllowOption = None,
    reject: RejectOption = None,
    idle_timeout: IdleTimeoutOption = None,
    cpu_limit: CpuLimitOption = None,
    memory_limit: MemoryLimitOption = None,
    pids_limit: PidsLimitOption = None,
    allow_sudo: SudoOption = None,
    classification_banner: ClassificationBannerOption = None,
) -> None:
    """Create a new workspace."""
    context.require_auth()
    validate_create_specs(mount, allow, reject)
    ensure_autostart_allowed(context.client(), auto_start)
    env_dict = parse_env_list(env) if isinstance(env, list) else None
    settings = build_settings(
        idle_timeout, cpu_limit, memory_limit, pids_limit, allow_sudo
    )
    ws = create_workspace_or_exit(
        context.client(),
        name,
        image=image,
        command=command,
        auto_start=auto_start,
        mount=mount,
        env_dict=env_dict,
        health_check=health_check,
        allow=allow,
        reject=reject,
        settings=settings,
        per_handle_home=per_handle_home,
        classification_banner=classification_banner,
    )
    _out = Console()
    _out.print(f"Created workspace [bold]{name}[/bold] ({ws.id[:12]})")


@context.app.command("dup")
def dup(
    source: str = typer.Argument(..., help="Source workspace name"),
    new_name: str = typer.Argument(..., help="New workspace name"),
) -> None:
    """Duplicate a workspace."""
    context.require_auth()
    client = context.client()
    ws = context.resolve_or_exit(client, source)
    resp = client.post(
        f"/api/v1/workspaces/{ws.id}/duplicate", json={"name": new_name}
    )
    if resp.status_code == 409:
        context.err.print(
            f"[red]A workspace named[/red] '{new_name}' [red]already exists[/red]"
        )
        raise typer.Exit(code=1)
    if resp.status_code == 404:
        context.err.print("[red]Workspace not found[/red]")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    data = resp.json()
    _out = Console()
    _out.print(
        f"Duplicated [bold]{source}[/bold] → [bold]{new_name}[/bold] ({data['id'][:12]})"
    )


def run_workspace_action(name: str, method: str, verb: str) -> None:
    """Run a workspace lifecycle command (rm/restart/stop/start).

    Auth-gates, resolves the client lazily (after the auth check, matching
    the original per-command ordering), maps a missing workspace to the
    standard "No workspace named" error + exit 1, and echoes
    ``<verb> workspace <name>`` on success.
    """
    context.require_auth()
    try:
        getattr(context.client(), method)(name)
    except WorkspaceNotFoundError:
        context.err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None
    typer.echo(f"{verb} workspace {name}")


@context.app.command("rm")
def rm(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Delete a workspace."""
    run_workspace_action(name, "delete_workspace", "Deleted")


@context.app.command("members")
def members(
    workspace: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """List members of a workspace by role."""
    context.require_auth()
    client = context.client()
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
    run_workspace_action(name, "restart_workspace", "Restarted")


@context.app.command("stop")
def stop(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Stop the container for a workspace."""
    run_workspace_action(name, "stop_workspace", "Stopped")


@context.app.command("start")
def start(
    name: str = typer.Argument(..., help="Workspace name"),
) -> None:
    """Start the container for a workspace."""
    run_workspace_action(name, "start_workspace", "Started")


def unique_export_path(name: str, output: Path | None) -> Path:
    """The export output path; a default name avoids overwriting a file."""
    out_path = output or Path(f"{name}.tar.gz")
    if out_path.exists() and output is None:
        # Don't overwrite — find a unique name
        stem = name
        n = 1
        while out_path.exists():
            out_path = Path(f"{stem}-{n}.tar.gz")
            n += 1
    return out_path


def export_error(e: httpx.HTTPStatusError) -> None:
    """Print the export failure (403 gets the permission hint) and exit."""
    if e.response.status_code == 403:
        context.err.print(
            "[red]Export failed:[/red] permission denied — you need"
            " the export permission on this workspace"
        )
    else:
        context.err.print(f"[red]Export failed:[/red] {e.response.text}")
    raise typer.Exit(code=1) from None


@context.app.command("export")
def export_workspace(
    name: str = typer.Argument(..., help="Workspace name"),
    output: Path = typer.Option(
        None, "-o", "--output", help="Output file (default: <name>.tar.gz)"
    ),
) -> None:
    """Export a workspace to a .tar.gz archive."""
    context.require_auth()
    client = context.client()
    ws = context.resolve_or_exit(client, name)
    out_path = unique_export_path(name, output)
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

        def update(downloaded, total):
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
            client.export_workspace(ws.id, out_path, on_progress=update)
            # Ensure progress bar hits 100% regardless of estimate accuracy
            if started[0]:
                final = progress.tasks[task_id].completed
                progress.update(task_id, total=final, completed=final)
    except httpx.HTTPStatusError as e:
        export_error(e)
    except httpx.RequestError:
        # A mid-body abort (server-side tar failure breaks the stream
        # instead of shipping a truncated 200, #3101) lands here —
        # remove the partial archive rather than leave a file that
        # only fails confusingly at import time.
        out_path.unlink(missing_ok=True)
        context.err.print(
            "[red]Export failed:[/red] transfer interrupted —"
            " the archive was not completed"
        )
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
        context.err.print(f"[red]File not found:[/red] {archive}")
        raise typer.Exit(code=1)
    client = context.client()
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

        def update(uploaded, total):
            progress.update(task_id, completed=uploaded)

        with progress:
            ws = client.import_workspace(
                archive, name=name, on_progress=update
            )
    except httpx.HTTPStatusError as e:
        context.err.print(f"[red]Import failed:[/red] {e.response.text}")
        raise typer.Exit(code=1) from None
    _out = Console()
    _out.print(f"Imported [bold]{ws.name}[/bold] ({ws.id[:12]})")


def parse_env_list(env_list: list[str]) -> dict[str, str]:
    """Parse ['KEY=VALUE', ...] into a dict."""
    result = {}
    for item in env_list:
        if "=" not in item:
            context.err.print(
                f"[red]Invalid env var (expected KEY=VALUE):[/red] {item}"
            )
            raise typer.Exit(code=1)
        key, _, value = item.partition("=")
        result[key] = value
    return result


def build_settings(
    idle_timeout: int | None,
    cpu_limit: float | None,
    memory_limit: str | None,
    pids_limit: int | None,
    allow_sudo: bool | None = None,
) -> dict | None:
    """Build a workspace settings dict from CLI flags, or None if all unset."""
    values = {
        "idle_timeout": idle_timeout,
        "cpu_limit": cpu_limit,
        "memory_limit": memory_limit,
        "pids_limit": pids_limit,
        # #2017: None (flag omitted) leaves the bag untouched — the workspace
        # follows the deploy posture. False locks the workspace down; True is
        # the explicit "follow the server" (the server setting is a ceiling,
        # so True can never raise sudo above the deploy default).
        "allow_sudo": allow_sudo,
    }
    settings = {
        key: value for key, value in values.items() if value is not None
    }
    return settings or None


class SENTINEL:
    pass


def prompt(label: str, current: str | None) -> str | SENTINEL.__class__:
    """Prompt for a value, showing the current default.

    Returns the new value, or SENTINEL if the user pressed Enter to keep.
    Empty input (just whitespace) clears the value and returns "".
    """
    display = current or "(none)"
    raw = input(f"{label} [{display}]: ")
    if raw == "":
        return SENTINEL  # keep current
    return raw.strip()
