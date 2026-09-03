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

        Re-registers if the sidecar reconnects: the workspace's entry is
        repointed at the fresh socket, and the stale socket's later
        ``deregister`` is identity-guarded (see below) so it cannot drop
        the replacement's registration (#3069).
        """
        self._conns[workspace_id] = sock
        logger.info(
            "sidecar connection registered: ws=%s", str(workspace_id)[:8]
        )

    def _stale_ack_ids(self, workspace_id: str) -> list[str]:
        """Ids of the pending drop-acks waiting on this sidecar."""
        return [
            aid
            for aid, entry in self._pending.items()
            if entry["ws"] == workspace_id
        ]

    @staticmethod
    def _fail_ack(entry) -> None:
        """Resolve one pending drop-ack to False (sidecar gone)."""
        if entry is not None and not entry["future"].done():
            entry["future"].set_result(False)

    def _fail_pending_acks(self, workspace_id: str) -> list[str]:
        """Fail the sidecar's pending drop-acks and return their ids.

        Failing the acks (rather than leaving them to time out) lets an
        in-flight ``revoke`` learn at once that the sidecar is gone.
        """
        stale = self._stale_ack_ids(workspace_id)
        for aid in stale:
            self._fail_ack(self._pending.pop(aid, None))
        return stale

    def deregister(self, workspace_id: str, sock) -> None:
        """Drop a sidecar socket (disconnect) + fail its pending drop-acks.

        Identity-guarded (#3069): only the socket that currently owns the
        workspace's registration may drop it. A reconnect registers a fresh
        socket under the same workspace id; the stale socket's teardown must
        leave the replacement's registration alone — otherwise every later
        ``send_drop`` finds "no sidecar" and revocations proceed unenforced
        while a live sidecar is connected. A pending drop-ack enqueued over
        the stale socket is no longer failed here either; the revoke
        caller's timeout (see ``send_drop``) is the fail-closed backstop.
        """
        registered = self._conns.get(workspace_id)
        if registered is not None and registered is not sock:
            return  # a newer socket owns the registration now
        dropped = self._conns.pop(workspace_id, None) is not None
        stale = self._fail_pending_acks(workspace_id)
        if dropped or stale:
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
        ``False`` on disconnect/shutdown; or ``None`` if there is no live
        sidecar or the enqueue failed -- in the ``None`` case the caller
        proceeds (the rule isn't enforced while the sidecar is down, so
        there's nothing to drop).

        The registered socket is a :class:`SafeWebSocket`, whose ``send_json``
        is a non-blocking queue enqueue that raises only ``SlowClientError``
        (queue full / sender stopped). So a dead-but-not-yet-stopped socket
        accepts the enqueue and the Future stays pending; the caller's
        ``wait_for`` timeout (in ``ConsentCoordinator.revoke``) is the backstop
        and resolves fail-closed. The done-callback below pops the pending
        entry on resolve OR cancel (the timeout cancels the Future), so a
        timed-out revoke against a hung-but-connected sidecar does not leak.
        """
        sock = self._conns.get(workspace_id)
        if sock is None:
            return None
        ack_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[ack_id] = {"future": fut, "ws": workspace_id}
        fut.add_done_callback(
            lambda _f, aid=ack_id: self._pending.pop(aid, None)
        )
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
            # Queue full / sender stopped -- treat as "no sidecar": nothing
            # to drop, caller proceeds.
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
