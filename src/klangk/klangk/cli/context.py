"""Shared CLI context: config/state caches, server resolution, the auth gate.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import websockets
from rich.console import Console

from .auth import fetch_config, local_login, seed_config, UNREACHABLE
from .client import KlangkClient
from .config import CLIConfig, CLIState, default_server_uds_path


cfg_cache: CLIConfig | None = None
state_cache: CLIState | None = None

# The shared typer app lives here (not in main.py) so command modules can
# decorate against it at import time without a circular import; main.py
# imports it back as the CLI's entrypoint app (#2542 split).
app = typer.Typer(
    name="klangk",
    help="Klangk Client",
    rich_markup_mode="rich",
    # Run the callback even with no subcommand so bare `klangk` can launch
    # the interactive TUI (see main._maybe_launch_tui). `--help`/`--version`
    # are still handled by click before the callback runs, so they keep
    # precedence.
    invoke_without_command=True,
)

err = Console(stderr=True)


def run_ws_command(body):
    """Run an async ws command body, surfacing failures as a clean exit.

    Returns whatever *body* returns (e.g. an exit code). Server ``error``
    frames — e.g. the "Permission denied" a member without
    ``share-terminals`` gets — raise ``ConnectionError`` fast out of the
    ``frame_is`` predicates (#2633); a silent server drop raises
    ``TimeoutError``, carrying the failing wait's detail when it has
    any. Both, plus a handshake rejection, reach the user as a one-line
    message and a nonzero exit instead of a raw traceback (#2876) —
    the same presentation ``klangk shell`` uses (shellcmd.py),
    including the 4001/4002 session-expired special case.
    """
    try:
        return asyncio.run(body())
    except ConnectionError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    except asyncio.TimeoutError as e:
        detail = str(e) or "Timed out waiting for the server to respond"
        err.print(f"[red]{detail}[/red]")
        raise typer.Exit(code=1) from None
    except websockets.InvalidStatus as e:
        if e.response.status_code in (4001, 4002):
            err.print(
                "[red]Session expired. Run `klangk login`"
                " to re-authenticate.[/red]"
            )
        else:
            err.print(f"[red]Connection rejected: {e}[/red]")
        raise typer.Exit(code=1) from None


def cfg() -> CLIConfig:
    global cfg_cache
    if cfg_cache is None:
        cfg_cache = CLIConfig.load()
    return cfg_cache


def state() -> CLIState:
    global state_cache
    if state_cache is None:
        state_cache = CLIState.load()
    return state_cache


server_override: str | None = None


def server_url() -> str:
    if server_override is not None:
        return server_override
    active = state().active_server
    if active is not None:
        return active
    # Single-host convenience (#1676): if a co-located klangkd has bound
    # its default UDS, talk to it without forcing a `klangk login` step.
    # Gated on existence so a host with no klangkd keeps the helpful
    # "not configured" error. Note this is ``exists()``, not a connect
    # probe: a *stale* socket left behind by a crashed klangkd still
    # passes, and the unreachable connect is then surfaced by
    # ``require_auth`` as a clear "Cannot connect to klangkd" message
    # rather than a misleading "Not logged in".
    default_uds = default_server_uds_path()
    if Path(default_uds).exists():
        return default_uds
    err.print(
        "[red]No server configured[/red] — run"
        " [bold]klangk login <server>[/bold] first,"
        " or pass [bold]--server[/bold]."
    )
    raise typer.Exit(code=1)


def client() -> KlangkClient:
    return KlangkClient(server_url(), state().get_token(server_url()))


def resolve_or_exit(client, name: str):
    """Resolve a workspace by name/id or exit with the standard error.

    The shared form of the resolve-then-Exit preamble repeated across the
    command modules (#2546): on ``WorkspaceNotFoundError`` prints
    ``No workspace named '<name>'`` to stderr and raises ``typer.Exit(1)``.
    """
    # Deferred: context is imported by every command module; this keeps
    # client.py's httpx/websockets import weight off commands that never
    # build a client (check_deferred_imports allowlist).
    # allow-deferred-import (httpx weight)
    from .client import WorkspaceNotFoundError

    try:
        return client.resolve_workspace(name)
    except WorkspaceNotFoundError:
        err.print(f"[red]No workspace named[/red] '{name}'")
        raise typer.Exit(code=1) from None


def session_token() -> str:
    """The active server's token, after require_auth.

    Collapses the repeated ``state().get_token(server_url())`` preamble
    (#2546). Callers pair it with ``require_auth()`` (which guarantees a
    token exists), so an empty return is the caller's pragma-nocover
    guard, unchanged from the inline form.
    """
    return state().get_token(server_url())


def ws_max_size() -> int:
    return cfg().get_ws_max_size(server_url())


def require_auth() -> None:
    """Ensure the active server has a usable token.

    In ``none`` (no-auth) mode the server freely issues a token for the
    seeded default user, so any command auto-logs in on first run rather
    than demanding a prior ``klangk login`` (#1374). The server's mode is
    probed live (not cached) so a mode switch takes effect immediately:
    flipping none->password after a command auto-logged in still leaves
    that token valid until it expires, but a *fresh* command with no
    stored token will see the new mode and not auto-login.
    """
    cli_state = state()
    url = server_url()
    if cli_state.get_token(url):
        return
    config = fetch_config(url)
    if maybe_none_login(cli_state, url, config):
        return
    # A UDS server that's down (e.g. a stale default socket left behind
    # by a crashed klangkd, #1676) must not be reported as "not logged
    # in" — running `klangk login` against it would just fail to connect
    # the same way. Tell the user the server is unreachable instead.
    # (TCP servers keep the existing "Not logged in" path; a reachability
    # hint for them is a separate UX change.)
    if config == UNREACHABLE and url.startswith("/"):
        err.print(
            f"[red]Cannot connect to klangkd[/red] at {url} — is it running?"
        )
        raise typer.Exit(code=1)
    err.print(
        "[red]Not logged in[/red] — run [bold]klangk login[/bold] first."
    )
    raise typer.Exit(code=1)


def maybe_none_login(
    state: CLIState, url: str, config: dict | str | None = None
) -> bool:
    """If the server is in ``none`` mode, fetch a free token and store it.

    Returns True on success (token stored, ``require_auth`` proceeds).
    Returns False if the server is not in ``none`` mode or unreachable,
    leaving the caller to emit the normal "Not logged in" error. The
    mode is probed live via /config (no cache) — cheap for a single
    command entry point, and the only way to stay correct across a mode
    switch. ``require_auth`` passes the config it already fetched so we
    don't probe twice; a caller without one pays the single fetch here.
    """
    if config is None:
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
