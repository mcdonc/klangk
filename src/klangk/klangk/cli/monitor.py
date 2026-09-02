"""The `klangk monitor` command: event stream, reconnect backoff, token refresh.

Part of the #2542 split of the former 3061-line
``klangk/cli/main.py``; commands and helpers live in
single-responsibility modules, all imported back into
``klangk.cli.main`` for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys

import typer
import websockets

from .auth import local_login, refresh_token
from .client import server_mode_is_none
from . import context
from .transport import ws_connect


def truthy_env(msg: dict, key: str, env_name: str) -> dict[str, str]:
    """{env_name: value} when *key* is present and truthy."""
    value = msg.get(key)
    return {env_name: str(value)} if value else {}


def present_env(msg: dict, key: str, env_name: str) -> dict[str, str]:
    """{env_name: value} when *key* is present (even falsy, e.g. seq 0)."""
    value = msg.get(key)
    return {env_name: str(value)} if value is not None else {}


def health_event_env(msg: dict) -> dict[str, str]:
    """Env vars for a ``service_health`` event."""
    env = {
        "KLANGK_HEALTHY": "true" if msg.get("healthy") else "false",
        # ``running`` distinguishes "unhealthy check" from "container
        # stopped" -- both have healthy=false, but a death frame carries
        # running=false (#1175 item 2).  Defaults to true for older
        # servers that don't send the field.
        "KLANGK_RUNNING": "true" if msg.get("running", True) else "false",
    }
    env.update(truthy_env(msg, "health_message", "KLANGK_HEALTH_MESSAGE"))
    env.update(
        truthy_env(msg, "health_checked_at", "KLANGK_HEALTH_CHECKED_AT")
    )
    env.update(present_env(msg, "seq", "KLANGK_HEALTH_SEQ"))
    return env


def dispatch_monitor_event(msg: dict, command: list[str]) -> None:
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
        env.update(health_event_env(msg))
    # FileNotFoundError (missing binary) propagates to the caller.
    subprocess.run(command, input=payload.encode(), env=env, check=False)


async def monitor_connection(
    server_spec: str,
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
    async with ws_connect(server_spec, token=token, max_size=max_size) as conn:
        async for raw in conn:
            msg = parse_monitor_event(raw, type_filter, ws_filter)
            if msg is not None:
                dispatch_monitor_event(msg, command)


def type_allowed(etype, type_filter: set) -> bool:
    """True when *etype* passes the --type filter."""
    return not type_filter or etype in type_filter


def workspace_allowed(wid, ws_filter: set) -> bool:
    """True when *wid* passes the --workspace filter."""
    if not ws_filter:
        return True
    return wid is not None and wid in ws_filter


def parse_monitor_event(
    raw: str, type_filter: set, ws_filter: set
) -> dict | None:
    """One socket frame as a dispatchable event, or None when it is not an
    event / fails the type / workspace filters."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    etype = msg.get("type")
    if etype is None:
        return None  # control/ack messages aren't events
    if not type_allowed(etype, type_filter):
        return None
    if not workspace_allowed(msg.get("workspace_id"), ws_filter):
        return None
    return msg


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
    if await asyncio.to_thread(server_mode_is_none, server_url):
        try:
            _email, new = await asyncio.to_thread(local_login, server_url)
        except SystemExit:
            return None
        return new
    return None


def classify_monitor_failure(exc: BaseException) -> tuple[bool, str]:
    """``(auth_close, reason)`` for a monitor connection failure."""
    if isinstance(exc, websockets.ConnectionClosed):
        code = exc.rcvd.code if exc.rcvd else None
        return code in (4001, 4002), f"closed (code {code})"
    if isinstance(exc, websockets.InvalidStatus):
        code = exc.response.status_code
        return code in (4001, 4002), f"rejected (HTTP {code})"
    return False, f"network error: {exc}"


async def refresh_on_auth_close(server_spec: str, current_token: str) -> str:
    """A fresh token after an auth close, or the current one.

    A failed refresh still returns the current token so the monitor
    keeps retrying — the server/token may recover.
    """
    new = await refresh_token_threaded(server_spec, current_token)
    if new:
        context.err.print("[green]Token refreshed.[/green]")
    else:
        context.err.print(
            "[yellow]Token refresh failed; retrying with the"
            " current token.[/yellow]"
        )
    return new or current_token


def reconnects_exhausted(max_reconnects: int | None, attempt: int) -> bool:
    """True when the reconnect budget is spent."""
    return max_reconnects is not None and attempt >= max_reconnects


async def monitor_run(
    server_spec: str,
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
    context.err.print(
        "[green]Monitoring events. Press Ctrl+C to stop.[/green]"
    )
    attempt = 0
    while True:
        try:
            await monitor_connection(
                server_spec,
                current_token,
                max_size,
                command,
                types,
                workspaces,
            )
            auth_close = False
            reason = "connection closed"
        except (
            websockets.ConnectionClosed,
            websockets.InvalidStatus,
            OSError,
            asyncio.TimeoutError,
        ) as exc:
            auth_close, reason = classify_monitor_failure(exc)

        # On an auth-related close, try to refresh the JWT. A successful
        # refresh lets the next attempt authenticate cleanly; a failed
        # one still reconnects (the server/token may recover).
        if auth_close:
            current_token = await refresh_on_auth_close(
                server_spec, current_token
            )

        if reconnects_exhausted(max_reconnects, attempt):
            context.err.print(
                f"[red]{reason}; max reconnects ({max_reconnects})"
                " reached, giving up.[/red]"
            )
            raise typer.Exit(code=1)
        attempt += 1
        delay = monitor_backoff(attempt, max_delay)
        context.err.print(
            f"[yellow]{reason}; reconnecting in {delay:.1f}s"
            f" (attempt {attempt})...[/yellow]"
        )
        await asyncio.sleep(delay)


def monitor_options(
    command: list[str] | None, no_reconnect: bool, max_reconnects: int | None
) -> tuple[list[str], int | None]:
    """Resolve the command list and reconnect bound from the CLI flags."""
    return (
        list(command) if command else [],
        0 if no_reconnect else max_reconnects,
    )


@context.app.command()
def monitor(
    command: list[str] = typer.Argument(
        None,
        help=(
            "Optional command to run for each event. Pass it after '--' "
            "so its own flags aren't parsed by klangk."
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
      klangk monitor                                # stream all events
      klangk monitor --type service_health | jq .   # pretty health events
      klangk monitor --type service_health -- sh -c \
        '[ "$KLANGK_HEALTHY" = false ] && notify-send "Service unhealthy"'
      klangk monitor --type service_health -- sh -c \
        '[ "$KLANGK_RUNNING" = false ] && echo "container stopped"'
      klangk monitor --workspace <id> --type service_health
    """
    context.require_auth()
    surl = context.server_url()
    token = context.session_token()
    if (
        not token
    ):  # pragma: no cover  # context.require_auth already guards this
        context.err.print(
            "[red]Not logged in. Run `klangk login` first.[/red]"
        )
        raise typer.Exit(code=1)
    command, effective_max = monitor_options(
        command, no_reconnect, max_reconnects
    )
    try:
        asyncio.run(
            monitor_run(
                surl,
                token,
                max_size=context.ws_max_size(),
                command=command,
                types=event_type,
                workspaces=workspace,
                max_reconnects=effective_max,
                max_delay=max_delay,
            )
        )
    except websockets.InvalidStatus as e:
        # A rejection during the very first connect (before the loop's
        # reconnect path is established).
        context.err.print(f"[red]Connection rejected: {e}[/red]")
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        context.err.print("[dim]Stopped.[/dim]")
