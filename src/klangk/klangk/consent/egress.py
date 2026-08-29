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

import logging

from ..interval import IntervalWorker
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


class EgressConsentSweeper(IntervalWorker):
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

    # Live-read (a property, not a captured constant) so a patched/test
    # module global applies on the next cycle.
    @property
    def interval(self) -> float:
        return PRUNE_INTERVAL

    log_label = "egress consent: retention sweep"

    async def sweep(self) -> None:
        """One retention sweep: prune rows past retention / over the cap.

        Settings are read live inside the model call (SIGHUP reload-safe:
        a reload applies on the next sweep).
        """
        deleted = await self.app.state.model.egress_consent.prune()
        if deleted:
            logger.info(
                "egress consent: pruned %d row(s) past retention/cap", deleted
            )
