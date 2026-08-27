"""WebSocket status listener for the klangk TUI.

Connects to the server's ``/ws`` and forwards the same broadcast events
the web UI and ``klangk monitor`` consume (``workspaces_changed``,
``container_status``, ``service_health``) to a callback, so the TUI's
status reflects live workspace/container state. Reconnection is the
caller's concern — the ``monitor`` command owns the battle-tested
reconnect loop; this is the lean listener the TUI runs as a worker.

The WS connection is the TUI's single reachability signal (#2052): its
lifecycle (connect / drop) drives the unreachable overlay in
:class:`~klangk.cli.tui.screens.main.MainScreen`, so we ping on a short
interval to detect a wedged / half-open connection fast without any REST
polling. The server side is lowered to match (``main.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from ..transport import ws_connect

logger = logging.getLogger(__name__)

# Protocol-level liveness for the TUI's status WS (#2052). The client pings
# the server every ``_WS_PING_INTERVAL`` seconds and expects a pong within
# ``_WS_PING_TIMEOUT``; a wedged / half-open connection is dropped within
# ~interval+timeout, which ``_status_loop`` turns into the unreachable
# overlay. Tighter than the ``websockets`` default (20/20) so a silent drop
# surfaces in ~20s with no REST polling.
_WS_PING_INTERVAL = 10
_WS_PING_TIMEOUT = 10


async def listen_for_status(
    server_url: str,
    token: str,
    on_event: Callable[[dict], object],
    *,
    on_connect: Callable[[], object] | None = None,
    max_size: int | None = None,
) -> None:
    """Connect to ``/ws`` and call ``on_event(event)`` for each broadcast.

    ``on_connect`` (if given) is invoked once after the connection is
    established, before any frames are read — the TUI uses it to clear the
    unreachable overlay and refresh the workspace list on (re)connect (#2052).

    Non-JSON and non-object frames are skipped (the server occasionally
    sends control/ack frames). Callback exceptions are isolated: a bug in
    ``on_event``/``on_connect`` is logged and swallowed rather than tearing
    down the connection — an exception escaping this listener reads as a
    connection loss to ``_status_loop``, which would churn reconnects and
    replay the failure forever (#2029 audit, same isolation rule as the
    consent decider's pump).
    """
    async with ws_connect(
        server_url,
        token=token,
        max_size=max_size,
        ping_interval=_WS_PING_INTERVAL,
        ping_timeout=_WS_PING_TIMEOUT,
    ) as ws:
        if on_connect is not None:
            try:
                on_connect()
            except Exception:  # noqa: BLE001
                logger.exception("status WS on_connect callback failed")
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            try:
                on_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("status WS event callback failed")
