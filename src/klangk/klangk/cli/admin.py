"""Admin commands: admin users/invitations, volumes, consent-decide.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations


import typer
from rich.console import Console
from rich.table import Table

from .client import KlangkClient
from rich.prompt import Prompt

from . import account
from . import context


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

vol_app = typer.Typer(
    name="volumes",
    help="Manage container volumes for workspaces.",
    rich_markup_mode="rich",
)
context.app.add_typer(vol_app, name="volumes")


def admin_error(resp) -> None:
    """Print a backend error detail and exit 1 for an admin API response."""
    detail = (
        resp.json().get("detail", resp.text)
        if resp.headers.get("content-type", "").startswith("application/json")
        else resp.text
    )
    context.err.print(f"[red]{detail}[/red]")
    raise typer.Exit(code=1)


@admin_users_app.command("ls")
def admin_users_ls(
    page: int = typer.Option(1, "--page", help="Page number"),
    page_size: int = typer.Option(
        50, "--page-size", help="Users per page (max 200)"
    ),
) -> None:
    """List all user accounts (admin only)."""
    context.require_auth()
    client = context.client()
    resp = client.get(
        "/api/v1/users",
        params={"page": page, "page_size": page_size},
    )
    client.check_auth(resp)
    if resp.status_code != 200:
        admin_error(resp)
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
    email: str = typer.Argument(
        ..., help="Email or handle of the user to update"
    ),
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
    context.require_auth()
    client = context.client()
    # Resolve email-or-handle -> user id. /users/search is prefix-match
    # (LIKE) on email *and* handle (#616), so exact-match the result on
    # either field; both are unique so there's at most one.
    search = client.get("/api/v1/users/search", params={"q": email})
    client.check_auth(search)
    if search.status_code != 200:
        admin_error(search)
    matches = [
        u
        for u in search.json()
        if u.get("email") == email or u.get("handle") == email
    ]
    if not matches:
        context.err.print(
            f"[red]No user found with email or handle {email}[/red]"
        )
        raise typer.Exit(code=1)
    user_id = matches[0]["id"]

    if password is None:
        password = Prompt.ask("[bold]New password[/bold]", password=True)
        confirm = Prompt.ask("[bold]Confirm password[/bold]", password=True)
        if password != confirm:
            context.err.print("[red]Passwords do not match[/red]")
            raise typer.Exit(code=1)

    resp = client.patch(
        f"/api/v1/users/{user_id}",
        json={"password": password},
    )
    client.check_auth(resp)
    if resp.status_code != 200:
        admin_error(resp)
    Console().print(f"Password set for [bold]{email}[/bold]")


@admin_invitations_app.command("send")
def admin_invitations_send(
    email: str = typer.Argument(
        ...,
        help="Email address to invite (must be email; invitations are delivered by email)",
    ),
) -> None:
    """Send an invitation email (admin only)."""
    # Same format check the server applies (#2668) — fail fast locally
    # instead of surfacing the API error after the round-trip.
    err = account.validate_email(email)
    if err:
        context.err.print(f"[red]{err}[/red]")
        raise typer.Exit(code=1)
    context.require_auth()
    client = context.client()
    resp = client.post("/api/v1/invitations", json={"email": email})
    client.check_auth(resp)
    if resp.status_code != 200:
        admin_error(resp)
    Console().print(f"Invitation sent to [bold]{email}[/bold]")


@admin_invitations_app.command("ls")
def admin_invitations_ls() -> None:
    """List all invitations (admin only)."""
    context.require_auth()
    client = context.client()
    resp = client.get("/api/v1/invitations?page_size=200")
    client.check_auth(resp)
    if resp.status_code != 200:
        admin_error(resp)
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
    context.require_auth()
    client = context.client()
    resp = client.get("/api/v1/volumes")
    client.check_auth(resp)
    resp.raise_for_status()
    # Paged envelope (#2993): {volumes, page, page_size, total}.
    volumes = resp.json()["volumes"]
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
    context.require_auth()
    client = context.client()
    resp = client.post("/api/v1/volumes", json={"name": name})
    client.check_auth(resp)
    if resp.status_code == 409:
        context.err.print(f"[red]Volume already exists:[/red] {name}")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Created volume {name}")


@vol_app.command("rm")
def volumes_rm(
    name: str = typer.Argument(..., help="Volume name"),
) -> None:
    """Delete a named container volume."""
    context.require_auth()
    client = context.client()
    resp = client.delete(f"/api/v1/volumes/{name}")
    client.check_auth(resp)
    if resp.status_code == 403:
        context.err.print(f"[red]Permission denied:[/red] {name}")
        raise typer.Exit(code=1)
    if resp.status_code == 404:
        context.err.print(f"[red]Volume not found:[/red] {name}")
        raise typer.Exit(code=1)
    if resp.status_code == 409:
        context.err.print(f"[red]Volume is in use:[/red] {name}")
        raise typer.Exit(code=1)
    resp.raise_for_status()
    typer.echo(f"Deleted volume {name}")


@context.app.command("consent-decide")
def consent_decide(
    workspace: str = typer.Argument(
        help="Workspace name or id whose held egress requests to decide"
    ),
    hold_timeout: float = typer.Option(
        120.0,
        "--hold-timeout",
        help=(
            "Seconds a held request counts down before auto-deny "
            "(match the server's KLANGKD_EGRESS_CONSENT_TIMEOUT)."
        ),
    ),
    popup_socket: str = typer.Option(
        None,
        "--popup-socket",
        hidden=True,
        help=(
            "Internal: tmux socket of the hidden decider session. Set by "
            "the shell-layer wrapper to enable the persistent popup role "
            "(#2383); `q` hides the viewer instead of quitting."
        ),
    ),
    popup_session: str = typer.Option(
        None,
        "--popup-session",
        hidden=True,
        help=(
            "Internal: name of the hidden decider tmux session (#2383). "
            "Set together with --popup-socket."
        ),
    ),
) -> None:
    """Decide a workspace's held egress requests live (#2310).

    Connects to the server's consent-decider stream, shows the workspace's
    held egress requests (a blocked destination the sidecar is holding for
    a verdict), and lets you accept (allow once) or deny each one while it
    is held. Accepting lets that exact connection proceed; denying (or the
    countdown hitting zero) fails it. Requires the egress-consent
    permission on the workspace (owner, coder, or collaborator — #2883;
    spectators are watch-only).
    """
    context.require_auth()
    client = context.client()
    ws = resolve_workspace_for_consent(client, workspace)
    token = context.session_token()
    # Lazy import so the textual dep only loads on this command path.
    # allow-deferred-import (textual, this path only)
    from .tui.consent import ConsentDeciderApp

    ConsentDeciderApp(
        context.server_url(),
        token,
        ws.id,
        ws.name,
        hold_timeout=hold_timeout,
        max_size=context.ws_max_size(),
        popup_socket=popup_socket,
        popup_session=popup_session,
    ).run()


def resolve_workspace_for_consent(client: KlangkClient, arg: str):
    """Resolve a workspace name OR id to a Workspace (for consent-decide)."""
    all_ws = client.list_workspaces(all_pages=True) + (
        client.list_shared_workspaces(all_pages=True)
    )
    match = next((w for w in all_ws if w.id == arg or w.name == arg), None)
    if match is None:
        context.err.print(f"[red]No such workspace:[/red] {arg}")
        raise typer.Exit(code=1)
    return match
