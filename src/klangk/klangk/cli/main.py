"""Klangk CLI — typer app.

#2542 split: the former 3061-line ``main.py`` is now a thin composition
module. The typer ``app``, its callback, and the TUI launcher live here;
command implementations live in single-responsibility siblings and are
imported below so every ``from klangk.cli.main import X`` (and every
``klangk.cli.main.X`` monkeypatch target that binds a module global)
keeps working unchanged.

Module map:

- :mod:`.context`    — config/state caches, ``server_url``, ``require_auth``
- :mod:`.authcmds`   — ``login`` / ``logout`` / ``status``
- :mod:`.workspaces` — ``ls``/``create``/``dup``/``rm``/``members``/
                       ``restart``/``stop``/``start``/``export``/``import``
- :mod:`.edit`       — ``edit`` (settings editor)
- :mod:`.shellcmd`   — ``shell`` + agent-forwarding / consent-popup helpers
- :mod:`.monitor`    — ``monitor`` + event dispatch / backoff / refresh
- :mod:`.sandboxcmd` — ``sandbox`` + setup helpers
- :mod:`.terminals`  — ``terminal`` ls / share, workspace share/unshare
- :mod:`.execsync`   — ``exec`` / ``sync`` / ``images``
- :mod:`.admin`      — ``admin`` users+invitations / ``volumes`` /
                       ``consent-decide``
"""

from __future__ import annotations


import asyncio  # noqa: F401 (test patch target)
import subprocess  # noqa: F401 (test patch target)
import sys

import httpx
import typer
import websockets
from rich.prompt import Confirm, Prompt  # noqa: F401 (test patch targets)

from .auth import (  # noqa: F401 (test patch targets)
    fetch_config,
    local_login,
    login,
    logout as do_logout,
    refresh_token,
    UNREACHABLE,
)
from .client import (  # noqa: F401
    AuthError,
    KlangkClient,
    WorkspaceNotFoundError,
    decode_token_claims,
    drain_stdin,
    exec_on_ws,
    get_terminal_size,
    reset_terminal,
    send_ignore_closed,
    wait_container_ready,
    ws_exec,
    ws_shell,
    server_mode_is_none,
)
from .config import (
    CLIConfig,  # noqa: F401 (test patch target)
    CLIState,  # noqa: F401 (test patch target)
    default_server_uds_path,  # noqa: F401
    ensure_config,
    seed_config,  # noqa: F401
)
from .shell_popup import (  # noqa: F401 (test patch targets)
    EGRESS_INTERACTIVE,
    OUTER_PREFIX,
    REOPEN_KEY,
    hidden_session_name,
    host_tmux_version,
    run_consent_shell,
    should_use_popup,
    socket_path,
)

# --- context (caches, server resolution, auth gate) -----------------------
from . import context
from .context import (  # noqa: F401
    app,
    cfg,
    cfg_cache,
    client,
    err,
    maybe_none_login,
    state,
    state_cache,
    require_auth,
    server_url,
    ws_max_size,
)

# --- commands + helpers (re-exported for callers/tests) -------------------
from .authcmds import (  # noqa: F401
    account_app,
    account_email,
    account_handle,
    account_passwd,
    account_show,
    login_cmd,
    logout,
    status,
)
from .workspaces import (  # noqa: F401
    create,
    dup,
    export_workspace,
    import_workspace,
    list_workspaces,
    members,
    restart,
    rm,
    short_id,
    start,
    stop,
    workspace_status,
)
from .edit import (  # noqa: F401
    build_settings,
    parse_env_list,
    prompt,
    edit,
)
from .shellcmd import (  # noqa: F401
    consent_popup_enabled,
    klangk_argv,
    popup_decider_argv,
    popup_inner_shell_argv,
    run_consent_popup,
    resolve_forward_agent,
    shell,
)
from .monitor import (  # noqa: F401
    dispatch_monitor_event,
    monitor_backoff,
    monitor_connection,
    monitor,
    monitor_run,
    refresh_token_threaded,
)
from .sandboxcmd import (  # noqa: F401
    resolve_workspace_and_url,
    sandbox,
    sandbox_setup,
    sandbox_setup_only,
)
from .terminals import (  # noqa: F401
    resolve_own_window,
    share_terminal,
    share_workspace,
    terminal_app,
    terminals,
    unshare_terminal,
    unshare_workspace,
)
from .execsync import (  # noqa: F401  # noqa: F811
    exec_cmd,
    images,
    sync,
)
from .admin import (  # noqa: F401
    admin_error,
    resolve_workspace_for_consent,
    admin_app,
    admin_invitations_app,
    admin_users_app,
    consent_decide,
    vol_app,
    volumes_create,
    volumes_list,
    volumes_rm,
)


@app.callback()
def app_callback(
    ctx: typer.Context,
    server: str | None = typer.Option(
        None, "--server", help="Server alias or URL"
    ),
) -> None:
    ensure_config()
    if server is not None:
        # Single mutable cell lives in .context (#2542): write through the
        # module attribute, not a `global` here — main's binding is a
        # re-export, and rebinding it would leave context's copy stale.
        context.server_override = context.cfg().resolve_server(server)
    if ctx.invoked_subcommand is None:
        maybe_launch_tui(ctx)


def is_interactive() -> bool:
    """True when stdin and stdout are both real terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def maybe_launch_tui(ctx: typer.Context) -> None:
    """Launch the interactive TUI for a bare ``klangk`` invocation.

    Only on a real terminal: in non-TTY contexts (pipes, CI, typer's
    ``CliRunner``) the historic "print help" behavior is preserved so the
    command stays scriptable and the CLI test suite isn't surprised by a
    TUI it can't drive. The TUI is imported lazily so the textual dep
    never loads on plain subcommand paths (``klangk ls`` etc.).
    """
    if not is_interactive():
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    from .tui import run_tui  # allow-deferred-import (textual, ~440ms)

    try:
        run_tui(server_url=context.server_override)
    except Exception as exc:  # surface TUI crashes, don't swallow them
        err.print(f"[red]TUI error:[/red] {exc}")
        raise typer.Exit(code=1)


def main() -> None:  # pragma: no cover
    try:
        app()
    except AuthError as exc:
        err.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    except httpx.ConnectError:
        err.print("[red]Cannot connect to server[/red] — is it running?")
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        err.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None
    except websockets.ConnectionClosed:
        err.print("\n[red]Server disconnected[/red]")
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover
    main()
