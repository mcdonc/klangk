"""Egress-consent retention sweep + interactivity predicate.

The event half of the original #2242 design (sidecar POSTs each blocked
destination to an HTTP endpoint; ``EgressConsentMonitor.submit`` → persist →
expire) never shipped. #2311 replaced it with the sidecar's WebSocket
(``/ws/egress-sidecar``): each egress event goes through
:meth:`klangk.consent.ConsentCoordinator.hold`, which records static
allows/denials, creates pending requests, arms timeouts, and fans out to
deciders — synchronously, returning a verdict Future so the sidecar can
hold the connection SYN in the kernel. A queued POST could not answer a
held connection, which is why the coordinator design won.

What remains here is periodic retention (:class:`EgressConsentSweeper`,
#2303) — bounding ``egress_consent`` table growth past the retention
window / per-workspace cap — plus the :func:`workspace_is_interactive`
predicate the coordinator gate-checks with.
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
# an idle timeout: the sweep fires on schedule regardless of any other
# traffic. Mirrors the idle monitor's throttled piggyback sweeps.
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


class EgressConsentSweeper:
    """Prune the egress_consent retention table on a wall-clock interval.

    Constructed once in :func:`build_app` and stored on ``app.state``;
    started in the lifespan and stopped on shutdown. A sweep fires once
    immediately on startup, then every :data:`PRUNE_INTERVAL`. A failed
    sweep logs and retries an interval later — housekeeping, not a
    correctness path.

    Owns only ``app``; settings are read live inside the model call so a
    SIGHUP reload applies on the next sweep (:meth:`reconfigure` swaps
    ``app``).
    """

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    def start(self) -> None:
        """Start the sweep loop (idempotent). Runs until :meth:`stop`."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the sweep loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        # 0.0 sweeps once immediately on startup (a prior run may have left
        # the table past the window / over the cap), then every
        # PRUNE_INTERVAL.
        next_prune = 0.0
        try:
            while True:
                if time.monotonic() >= next_prune:
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
                await asyncio.sleep(max(0.0, next_prune - time.monotonic()))
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
