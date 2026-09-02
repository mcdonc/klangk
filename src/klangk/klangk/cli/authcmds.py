"""Session commands: login, logout, status.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import account
from .auth import login, logout as do_logout
from .client import decode_token_claims
from . import context


@context.app.command("login")
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
    cfg = context.cfg()
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


@context.app.command()
def logout(
    server: str | None = typer.Argument(None, help="Server alias or URL"),
) -> None:
    """Clear stored credentials."""
    if server is not None:
        resolved_url = context.cfg().resolve_server(server)
    else:
        active = context.state().active_server
        if active is None:
            context.err.print(
                "[red]No active server[/red] — pass a server argument"
                " or log in first."
            )
            raise typer.Exit(code=1)
        resolved_url = active
    do_logout(resolved_url)


def admin_status(token: str | None) -> bool | None:
    """Admin status from /my-permissions (the canonical source the
    frontend uses for isAdmin). Best-effort: if the probe fails (offline,
    token expired, old server without the is_admin flag) status still
    reports everything else rather than erroring out."""
    is_admin: bool | None = None
    if token:
        try:
            client = context.client()
            resp = client.get("/api/v1/my-permissions")
            client.check_auth(resp)
            if resp.status_code == 200:
                is_admin = resp.json().get("is_admin")
        except Exception:
            is_admin = None
    return is_admin


def identity_lines(email, user_id) -> list[str]:
    """The user= / user_id= plain-status lines."""
    return [f"user={email or 'unknown'}", f"user_id={user_id or 'unknown'}"]


def admin_field(is_admin) -> str | None:
    """The admin= plain-status line, or None when unknown."""
    if is_admin is None:
        return None
    return f"admin={'yes' if is_admin else 'no'}"


def print_status_plain(url, token, email, user_id, is_admin) -> None:
    print(f"server={url or '(none)'}")
    if token:
        for line in identity_lines(email, user_id):
            print(line)
        print("status=logged_in")
        admin = admin_field(is_admin)
        if admin:
            print(admin)
    else:
        print("status=not_logged_in")


def identity_rows(email, user_id) -> list[tuple[str, str]]:
    """The User / User ID table rows."""
    return [("User", email or "unknown"), ("User ID", user_id or "unknown")]


def admin_row(is_admin) -> tuple[str, str] | None:
    """The Admin table row, or None when unknown."""
    if is_admin:
        return "Admin", "[green]yes[/green]"
    if is_admin is False:
        return "Admin", "no"
    return None


def print_status_table(url, token, email, user_id, is_admin) -> None:
    console = Console()
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Server", url or "(none)")
    if token:
        for label, value in identity_rows(email, user_id):
            table.add_row(label, value)
        table.add_row("Status", "[green]logged in[/green]")
        admin = admin_row(is_admin)
        if admin:
            table.add_row(*admin)
    else:
        table.add_row("Status", "[yellow]not logged in[/yellow]")
    console.print(table)


def current_session_info() -> tuple[
    str | None, str | None, str | None, str | None
]:
    """(url, token, email, user_id) for the active session.

    status works even with no active server (unlike other commands), so
    url — and everything derived from it — may be None.
    """
    url = context.server_override or context.state().active_server
    state = context.state()
    token = state.get_token(url) if url else None
    email = state.get_email(url) if url else None
    user_id = decode_token_claims(token).get("sub") if token else None
    return url, token, email, user_id


@context.app.command()
def status(
    plain: bool = typer.Option(False, "--plain", help="Plain text output"),
) -> None:
    """Show connection info (server, user, admin status)."""
    url, token, email, user_id = current_session_info()
    is_admin = admin_status(token)
    if plain:
        print_status_plain(url, token, email, user_id, is_admin)
        return
    print_status_table(url, token, email, user_id, is_admin)


# ---------------------------------------------------------------------
# Account self-service (change password / handle / email)
# ---------------------------------------------------------------------

account_app = typer.Typer(
    help="Account self-service: change your password, handle, or email."
)
context.app.add_typer(account_app, name="account")


@account_app.command("show")
def account_show() -> None:
    """Show your current handle and email."""
    context.require_auth()
    me = context.client().get_me()
    handle = me.get("handle") or "(none)"
    email = me.get("email") or "(unknown)"
    last_login = _fmt_last_login(me.get("last_login_at"))
    lines = [
        f"Email:  [bold]{email}[/bold]",
        f"Handle: [bold]@{handle}[/bold]",
    ]
    if last_login:
        lines.append(f"Last login: [bold]{last_login}[/bold]")
    Console().print("\n".join(lines))


def _fmt_last_login(iso: str | None) -> str | None:
    """Render a UTC ISO login timestamp in the local timezone (#2583).

    Returns None for a missing or unparseable timestamp so callers can
    omit the line entirely.
    """
    if not iso:
        return None
    try:
        return (
            datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
        )
    except (ValueError, TypeError):
        return None


@account_app.command("passwd")
def account_passwd() -> None:
    """Change your password."""
    context.require_auth()
    url = context.server_url()
    client = context.client()
    current = Prompt.ask("[bold]Current password[/bold]", password=True)
    new = Prompt.ask("[bold]New password[/bold]", password=True)
    confirm = Prompt.ask("[bold]Confirm new password[/bold]", password=True)
    if new != confirm:
        context.err.print("[red]Passwords do not match[/red]")
        raise typer.Exit(code=1)
    policy = account.password_policy(url)
    if len(new) < policy.min_length:
        context.err.print(
            f"[red]Password must be at least {policy.min_length} characters[/red]"
        )
        raise typer.Exit(code=1)
    complexity_error = policy.complexity_error(new)
    if complexity_error:
        context.err.print(f"[red]{complexity_error}[/red]")
        raise typer.Exit(code=1)
    try:
        client.change_password(current, new)
    except httpx.HTTPStatusError as exc:
        # The server surfaces a 401 for a wrong current password and a 400
        # for policy violations (e.g. too short) — the detail is printed
        # verbatim. change-password doesn't itself trigger the /auth/login
        # brute-force lockout; a future global rate limit (429) would show
        # up here as a raw HTTP error until given dedicated handling.
        context.err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    # Success goes to stdout (scripting-friendly: `klangk account passwd &&
    # ...`); errors above go to stderr via context.err.
    Console().print("[green]Password updated.[/green]")


@account_app.command("handle")
def account_handle() -> None:
    """Change your handle (requires password confirmation)."""
    context.require_auth()
    client = context.client()
    current = client.get_me().get("handle") or ""
    new = Prompt.ask("[bold]New handle[/bold]").strip()
    err = account.validate_handle(new)
    if err:
        context.err.print(f"[red]{err}[/red]")
        raise typer.Exit(code=1)
    if not Confirm.ask(
        f"Change your handle from @{current} to @{new}?", default=False
    ):
        context.err.print("[yellow]Cancelled.[/yellow]")
        raise typer.Exit(code=0)
    password = Prompt.ask("[bold]Password (to confirm)[/bold]", password=True)
    try:
        client.change_handle(new, password)
    except httpx.HTTPStatusError as exc:
        context.err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    Console().print(f"[green]Handle updated to @{new}.[/green]")


@account_app.command("email")
def account_email() -> None:
    """Change your email (requires password confirmation)."""
    context.require_auth()
    url = context.server_url()
    client = context.client()
    new = Prompt.ask("[bold]New email[/bold]").strip()
    err = account.validate_email(new)
    if err:
        context.err.print(f"[red]{err}[/red]")
        raise typer.Exit(code=1)
    password = Prompt.ask("[bold]Password (to confirm)[/bold]", password=True)
    try:
        client.change_email(new, password)
    except httpx.HTTPStatusError as exc:
        context.err.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    # The JWT subject is the user id, so the cached token stays valid; only
    # the key it's filed under changes. Re-key it rather than dropping it.
    state = context.state()
    old = state.get_email(url)
    if old is not None and old != new:
        state.rename_user(url, old, new)
        state.save()
    Console().print(
        "[green]Email updated.[/green] Check your inbox to verify the new"
        " address."
    )
