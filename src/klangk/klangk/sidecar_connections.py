"""Live network-sidecar WebSocket connections, keyed by workspace (#2339).

The network sidecar opens one socket per workspace to ``/ws/egress-sidecar``
(``wshandler/sidecar.py``). That channel carries blocked-egress events
(sidecar -> klangkd) and verdict relays (klangkd -> sidecar). Revocation
(#2339) needs the *reverse*: klangkd PUSHING a rule-drop command to a specific
workspace's sidecar and correlating the sidecar's ack back to the awaiting
``revoke``. This registry holds those live connections (so a revoke can find
the right socket) plus the pending ack map.

Owns only ``app`` (the app-ownership rule); no background task, so
:meth:`start` is lifespan-symmetry only and :meth:`stop` clears state on
shutdown (failing any in-flight revoke acks).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from .wshandler.safe_websocket import WS_ERRORS

logger = logging.getLogger(__name__)


class SidecarConnections:
    """Tracks live sidecar sockets by workspace + pending drop-rule acks (#2339)."""

    def __init__(self, app) -> None:
        self.app = app
        # workspace_id -> SafeWebSocket
        self._conns: dict[str, object] = {}
        # ack_id -> {"future": Future, "ws": workspace_id}
        self._pending: dict[str, dict] = {}

    def reconfigure(self, app) -> None:
        self.app = app

    def start(self) -> None:
        """Lifespan symmetry only -- connections register on demand."""

    def register(self, workspace_id: str, sock) -> None:
        """Record a sidecar's live socket (on /ws/egress-sidecar connect).

        Re-registers if the sidecar reconnects (drops the stale socket).
        """
        self._conns[workspace_id] = sock
        logger.info(
            "sidecar connection registered: ws=%s", str(workspace_id)[:8]
        )

    def deregister(self, workspace_id: str) -> None:
        """Drop a sidecar socket (disconnect) + fail its pending drop-acks.

        Failing the acks (rather than leaving them to time out) lets an
        in-flight ``revoke`` learn at once that the sidecar is gone.
        """
        self._conns.pop(workspace_id, None)
        stale = [
            aid
            for aid, entry in self._pending.items()
            if entry["ws"] == workspace_id
        ]
        for aid in stale:
            entry = self._pending.pop(aid, None)
            if entry is not None and not entry["future"].done():
                entry["future"].set_result(False)
        if stale or workspace_id in self._conns:
            logger.info(
                "sidecar connection deregistered: ws=%s", str(workspace_id)[:8]
            )

    def get(self, workspace_id: str):
        """The live socket for a workspace, or None."""
        return self._conns.get(workspace_id)

    def send_drop(
        self, workspace_id: str, host: str, decision: str
    ) -> asyncio.Future | None:
        """Push a ``drop_rule`` frame to the workspace's sidecar (#2339).

        Returns a Future that resolves ``True`` on a matching ``drop_ack``,
        ``False`` on disconnect; or ``None`` if there is no live sidecar (or
        the send failed) -- in the None case the caller proceeds (the rule
        isn't enforced while the sidecar is down, so there's nothing to drop).
        """
        sock = self._conns.get(workspace_id)
        if sock is None:
            return None
        ack_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[ack_id] = {"future": fut, "ws": workspace_id}
        try:
            sock.send_json(
                {
                    "type": "drop_rule",
                    "id": ack_id,
                    "host": host,
                    "decision": decision,
                }
            )
        except WS_ERRORS:
            # Socket died between the get() and the send -- treat as "no
            # sidecar": nothing to drop, caller proceeds.
            self._pending.pop(ack_id, None)
            return None
        return fut

    def resolve_ack(self, ack_id: str, ok: bool) -> None:
        """Resolve a pending drop-rule ack (the sidecar handled drop_rule).

        No-op if unknown (a late ack after deregister/timeout).
        """
        entry = self._pending.pop(ack_id, None)
        if entry is not None and not entry["future"].done():
            entry["future"].set_result(bool(ok))

    async def stop(self) -> None:
        """Clear connections + fail every pending drop-ack (shutdown)."""
        for entry in list(self._pending.values()):
            if not entry["future"].done():
                entry["future"].set_result(False)
        self._pending.clear()
        self._conns.clear()
