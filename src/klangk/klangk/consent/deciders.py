"""Runtime registry of live consent deciders (#2308).

A workspace is treated as "interactive" (its blocked egress is held for a
human decision, #2311) exactly while **at least one consent decider** is
registered for it. Interactivity is therefore *runtime
state*, not the stored ``egress_mode`` flag: a decider connects (#2310, over
the decider WebSocket) -> the workspace becomes interactive; the decider
disconnects -> it reverts to static allow-list behavior. With no decider,
blocked egress just fails (clean denial, no hanging connection -- the #2308
model). Deciders are strictly workspace-scoped (#2976): consent has no
deploy-wide flavor.

The registry supports **N concurrent deciders** per workspace (several CLI
sessions + Flutter clients at once): it is a collection keyed by decider id,
``has_decider`` is true when >= 1 is live, and a pending request is fanned out
to all of them (first decision wins -- wired in #2244).

Liveness is driven by the decider WebSocket: connect -> ``register``, client
ping -> ``touch``, disconnect -> ``deregister``; a background reaper (sweeping
at half the timeout) drops any decider whose last ping exceeds
``settings.consent_decider_timeout``, so a crashed/half-open client is reaped
within ~1.5× the timeout -- a workspace can be stranded in interactive mode
for at most that long, not indefinitely.

Owns only ``app`` (the app-ownership rule); the timeout is read live via
property so a SIGHUP reload propagates.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..wshandler.safe_websocket import WS_ERRORS

logger = logging.getLogger(__name__)


class ConsentDeciderRegistry:
    """Tracks live consent deciders; the gate for interactive egress (#2308).

    Constructed once in :func:`build_app` and stored on ``app.state``; the
    reaper is started in the lifespan and stopped on shutdown.
    """

    def __init__(self, app) -> None:
        self.app = app
        # decider_id -> {"ws": workspace_id, "seen": monotonic,
        #                 "email": str, "sock": SafeWebSocket}
        self._deciders: dict[str, dict] = {}
        self._reaper: asyncio.Task | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def timeout(self) -> float:
        return self.app.state.settings.consent_decider_timeout

    def register(
        self,
        decider_id: str,
        workspace_id: str,
        email: str | None,
        sock,
    ) -> None:
        """Register a live decider (connect). Idempotent on decider_id.

        ``sock`` is the decider's :class:`SafeWebSocket`; the endpoint owns its
        lifecycle (start/stop sender) -- the registry holds only a reference for
        :meth:`broadcast` fanout.
        """
        self._deciders[decider_id] = {
            "ws": workspace_id,
            "seen": time.monotonic(),
            "email": email,
            "sock": sock,
        }
        logger.info(
            "consent decider registered: scope=%s decider=%s",
            workspace_id[:8],
            decider_id[:8],
        )

    def deregister(self, decider_id: str) -> None:
        """Drop a decider (disconnect). No-op if unknown."""
        if self._deciders.pop(decider_id, None) is not None:
            logger.info(
                "consent decider deregistered: decider=%s", decider_id[:8]
            )

    def touch(self, decider_id: str) -> None:
        """Mark a decider live (client ping)."""
        entry = self._deciders.get(decider_id)
        if entry is not None:
            entry["seen"] = time.monotonic()

    def has_decider(self, workspace_id: str) -> bool:
        """True iff >= 1 live decider is registered for this workspace."""
        now = time.monotonic()
        cutoff = self.timeout
        for entry in self._deciders.values():
            if entry["ws"] == workspace_id and (now - entry["seen"] <= cutoff):
                return True
        return False

    def deciders_for(self, workspace_id: str) -> list[str]:
        """Ids of live deciders for this workspace (#2244 fanout)."""
        now = time.monotonic()
        cutoff = self.timeout
        return [
            did
            for did, entry in self._deciders.items()
            if entry["ws"] == workspace_id and (now - entry["seen"] <= cutoff)
        ]

    def broadcast(self, workspace_id: str, message: dict) -> int:
        """Send *message* to every live decider for this workspace.

        Used by the coordinator to fan out ``egress_request`` (new hold) and
        ``egress_resolved`` (verdict/timeout) frames. A decider whose socket is
        dead/slow is pruned immediately (its endpoint's ``finally`` deregister
        is then a no-op). Returns the number of deciders delivered to.
        Non-blocking: ``SafeWebSocket.send_json`` enqueues on a bounded queue.
        """
        now = time.monotonic()
        cutoff = self.timeout
        dead: list[str] = []
        delivered = 0
        for did, entry in self._deciders.items():
            if entry["ws"] != workspace_id or now - entry["seen"] > cutoff:
                continue
            try:
                entry["sock"].send_json(message)
                delivered += 1
            except WS_ERRORS:
                dead.append(did)
        for did in dead:
            self._deciders.pop(did, None)
        return delivered

    def start(self) -> None:
        """Start the liveness reaper (idempotent). Runs until :meth:`stop`."""
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        """Stop the reaper and clear all registrations."""
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        self._deciders.clear()

    async def _reap_loop(self) -> None:
        """Periodically drop deciders not pinged within the timeout.

        Cancelled by :meth:`stop`; the CancelledError propagates out of the
        sleep and is retrieved by stop()'s await (the standard cancelled-task
        pattern), so stop()'s catch fires.
        """
        while True:
            await asyncio.sleep(max(self.timeout / 2, 0.05))
            now = time.monotonic()
            stale = [
                did
                for did, entry in self._deciders.items()
                if now - entry["seen"] > self.timeout
            ]
            for did in stale:
                self._deciders.pop(did, None)
                logger.info(
                    "consent decider reaped (no ping within %.0fs): %s",
                    self.timeout,
                    did[:8],
                )
