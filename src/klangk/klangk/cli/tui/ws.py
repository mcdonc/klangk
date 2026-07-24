"""WebSocket status listener for the klangk TUI.

Connects to the server's ``/ws`` and forwards the same broadcast events
the web UI and ``klangk monitor`` consume (``workspaces_changed``,
``container_status``, ``service_health``) to a callback, so the TUI's
status reflects live workspace/container state. Reconnection is the
caller's concern — the ``monitor`` command owns the battle-tested
reconnect loop; this is the lean listener the TUI runs as a worker.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ..transport import ws_connect


async def listen_for_status(
    server_url: str,
    token: str,
    on_event: Callable[[dict], object],
    *,
    max_size: int | None = None,
) -> None:
    """Connect to ``/ws`` and call ``on_event(event)`` for each broadcast.

    Non-JSON and non-object frames are skipped (the server occasionally
    sends control/ack frames).
    """
    async with ws_connect(server_url, token=token, max_size=max_size) as ws:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            on_event(event)
