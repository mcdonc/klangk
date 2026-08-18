"""Interactive egress-consent monitor (#2239, #2242).

Receives blocked-destination events from the network sidecar's NFQUEUE
consumer and persists each as a pending consent request, auto-expiring it
after a timeout if no human decides (#2244 wires the decide/notify UI; this
component owns the receive → persist → expire loop).

The sidecar is the netns owner with ``NET_ADMIN``, so it consumes its own
NFQUEUE (iptables ``-j NFQUEUE`` in interactive mode) and POSTs each blocked
destination here, authenticating with the workspace's own JWT. That JWT is
validated by Caddy's egress-port ``forward_auth`` and re-decoded by the
receive endpoint, so this monitor gets the workspace id straight from the
request — no tag-to-id resolution, and the workspace can't forge events for
other workspaces (its JWT is workspace-scoped).

The monitor owns only ``app`` (the app-ownership rule) and reads
``settings.egress_consent_*`` live via property.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..model.workspaces import EGRESS_MODE_INTERACTIVE

logger = logging.getLogger(__name__)

# Retention sweep interval (#2303): how often egress_consent rows past the
# retention window / over the per-workspace cap are pruned. Pruning is
# day-scale housekeeping, so an hour between sweeps is plenty. The deadline
# is wall-clock (a monotonic ``next_prune`` compared at every loop top), NOT
# a queue-idle timeout: event traffic must never postpone the sweep -- a
# flooding workspace that keeps the queue busy is exactly the case the row
# cap exists for. Mirrors the idle monitor's throttled piggyback sweeps.
PRUNE_INTERVAL = 3600.0


async def workspace_is_interactive(app, workspace_id: str) -> bool:
    # #2308: interactivity is runtime state -- a workspace is interactive
    # only while a live consent decider is registered for it (or
    # deploy-wide), AND the workspace has opted in (egress_mode). No
    # decider -> static behavior (clean denial, no held connection).
    ws = await app.state.model.workspaces.get_workspace(workspace_id)
    if not ws or ws.get("egress_mode") != EGRESS_MODE_INTERACTIVE:
        return False
    return app.state.consent_deciders.has_decider(workspace_id)


class EgressConsentMonitor:
    """Receive sidecar egress events and persist consent requests.

    Constructed once in :func:`build_app` and stored on ``app.state``; started
    in the lifespan and stopped on shutdown. Mirrors the monitor pattern used
    by :class:`HealthMonitor`. Events arrive via :meth:`submit` (called by the
    receive endpoint) and are processed serially by the ``_run`` loop.

    Owns only ``app``; rate-limit / timeout are read live off settings so a
    SIGHUP reload propagates (:meth:`reconfigure` swaps ``app``).
    """

    def __init__(self, app) -> None:
        self.app = app
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._timeouts: set[asyncio.Task] = set()

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def rate_limit(self) -> int:
        return self.app.state.settings.egress_consent_rate_limit

    @property
    def timeout(self) -> float:
        return self.app.state.settings.egress_consent_timeout

    def submit(
        self, workspace_id: str, dst_ip: str, dst_port: int | None
    ) -> None:
        """Enqueue an observed blocked destination (called by the endpoint)."""
        self._queue.put_nowait((workspace_id, dst_ip, dst_port))

    def start(self) -> None:
        """Start the processing loop (idempotent). Runs until :meth:`stop`."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop and any pending timeout tasks."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._timeouts):
            task.cancel()

    async def _run(self) -> None:
        # Wall-clock sweep deadline: 0.0 sweeps once immediately on startup
        # (a prior run may have left the table past the window / over the
        # cap), then every PRUNE_INTERVAL regardless of event traffic.
        next_prune = 0.0
        try:
            while True:
                if time.monotonic() >= next_prune:
                    # Retention sweep: bounded table growth (#2303). A failed
                    # sweep logs and retries an interval later -- housekeeping,
                    # not a correctness path.
                    try:
                        await self._prune()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "egress consent: retention sweep failed",
                            exc_info=True,
                        )
                    next_prune = time.monotonic() + PRUNE_INTERVAL
                # Wait for the next event, but never past the deadline: a
                # busy queue must not postpone the sweep (see PRUNE_INTERVAL).
                timeout = max(0.0, next_prune - time.monotonic())
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    continue  # deadline passed -> sweep at loop top
                workspace_id, dst_ip, dst_port = item
                try:
                    await self._handle_event(workspace_id, dst_ip, dst_port)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad event must not kill the monitor (matches
                    # HealthMonitor's per-sweep isolation).
                    logger.exception("egress consent: event handling failed")
        except asyncio.CancelledError:
            pass

    async def _prune(self) -> None:
        """One retention sweep: prune rows past retention / over the cap.

        Settings are read live inside the model call (SIGHUP reload-safe:
        a reload applies on the next sweep).
        """
        deleted = await self.app.state.model.egress_consent.prune()
        if deleted:
            logger.info(
                "egress consent: pruned %d row(s) past retention/cap", deleted
            )

    async def _handle_event(
        self, workspace_id: str, dst_ip: str, dst_port: int | None
    ) -> None:
        # static (default): the sidecar observed a denial -> record it as
        # denied-by-policy (no human), immediately. interactive: a denial
        # becomes a pending request a human can decide (#2244).
        if await self._is_interactive(workspace_id):
            await self._handle_interactive(workspace_id, dst_ip, dst_port)
        else:
            request = await (
                self.app.state.model.egress_consent.record_static_denial(
                    workspace_id, dst_ip, dst_port
                )
            )
            if request is not None:
                self._notify(request)

    async def _is_interactive(self, workspace_id: str) -> bool:
        return await workspace_is_interactive(self.app, workspace_id)

    async def _handle_interactive(
        self, workspace_id: str, dst_ip: str, dst_port: int | None
    ) -> None:
        consent = self.app.state.model.egress_consent
        if await consent.count_pending(workspace_id) >= self.rate_limit:
            logger.warning(
                "egress consent: workspace %s pending cap reached (%d); "
                "dropping %s:%s",
                workspace_id[:8],
                self.rate_limit,
                dst_ip,
                dst_port,
            )
            return
        request = await consent.create_request(workspace_id, dst_ip, dst_port)
        if request is None:
            return  # a pending request for this (ws, ip, port) already exists
        self._notify(request)
        task = asyncio.create_task(self._timeout(request["id"]))
        self._timeouts.add(task)
        task.add_done_callback(self._timeouts.discard)

    async def _timeout(self, request_id: str) -> None:
        """Auto-expire a pending request after the configured timeout."""
        try:
            await asyncio.sleep(self.timeout)
            await self.app.state.model.egress_consent.expire_pending(
                request_id
            )
        except asyncio.CancelledError:
            pass

    def _notify(self, request: dict) -> None:
        """Notify connected frontends of a new pending request.

        Stub: #2244 wires the WebSocket event. The request is persisted and
        auto-expires on timeout regardless of notification, so the consent
        loop is correct end-to-end without it.
        """
        logger.info(
            "egress consent request: ws=%s %s:%s id=%s",
            str(request.get("workspace_id"))[:8],
            request.get("dest_host"),
            request.get("dest_port"),
            request.get("id"),
        )
