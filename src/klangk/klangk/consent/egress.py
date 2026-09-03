"""Egress-consent retention sweep.

The event half of the original #2242 design (sidecar POSTs each blocked
destination to an HTTP endpoint; ``EgressConsentMonitor.submit`` → persist →
expire) never shipped. #2311 replaced it with the sidecar's WebSocket
(``/ws/egress-sidecar``): each egress event goes through
:meth:`klangk.consent.ConsentCoordinator.hold`, which records static
allows/denials, creates pending requests, arms timeouts, and fans out to
deciders — synchronously, returning a verdict Future so the sidecar can
hold the connection SYN in the kernel. A queued POST could not answer a
held connection, which is why the coordinator design won. The
runtime-interactivity predicate lives on the coordinator itself
(``_ws_is_interactive``) since #3083 folded it into the single
workspace-row read; :func:`workspace_opted_in` (the workspace-side half,
#3080) stays here for the pause paths.

What remains here is periodic retention (:class:`EgressConsentSweeper`,
#2303) — bounding ``egress_consent`` table growth past the retention
window / per-workspace cap — since #2924 joined by the
``container_events`` prune (retention window + deploy-wide row cap) —
the one hourly housekeeping loop for every bounded table.
"""

from __future__ import annotations

import logging

from ..interval import IntervalWorker
from ..model.workspaces import EGRESS_MODE_INTERACTIVE

logger = logging.getLogger(__name__)

# Retention sweep interval (#2303): how often rows past a retention
# window / over a cap are pruned from the bounded tables (egress_consent
# since #2303, container_events since #2924). Pruning is day-scale
# housekeeping, so an hour between sweeps is plenty. The deadline is
# wall-clock (a monotonic ``next_prune`` compared at every loop top), NOT
# an idle timeout: the sweep fires on schedule regardless of any other
# traffic. Mirrors the idle monitor's throttled piggyback sweeps.
PRUNE_INTERVAL = 3600.0


async def workspace_opted_in(app, workspace_id: str) -> bool:
    """True iff the workspace exists and its ``egress_mode`` is interactive.

    The workspace-side half of interactivity (#2308) -- the runtime half is
    a live decider. Also the gate for the consent pause (#2332): the pause
    is a decider prompting affordance, honored only in interactive mode so
    a pause left over from an interactive epoch cannot auto-allow egress
    from a workspace since switched to static/allow (#3080, fail-closed).
    """
    ws = await app.state.model.workspaces.get_workspace(workspace_id)
    return bool(ws) and ws.get("egress_mode") == EGRESS_MODE_INTERACTIVE


class EgressConsentSweeper(IntervalWorker):
    """Prune the bounded tables on a wall-clock interval.

    Constructed once in :func:`build_app` and stored on ``app.state``;
    started in the lifespan and stopped on shutdown. A sweep prunes every
    bounded table — ``egress_consent`` (#2303) and ``container_events``
    (#2924) — each target independently, so a failure in one is logged
    without skipping the others. A sweep fires once immediately on
    startup (an upgrade over a bloated table trims right away), then every
    :data:`PRUNE_INTERVAL`. A failed sweep is logged and retried an
    interval later — housekeeping, not a correctness path.

    Owns only ``app``; settings are read live inside the model calls so a
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
        """One retention sweep over every bounded table.

        Settings are read live inside each model call (SIGHUP reload-safe:
        a reload applies on the next sweep).
        """
        model = self.app.state.model
        await self._prune_target("egress consent", model.egress_consent)
        await self._prune_target("container events", model.container_events)

    async def _prune_target(self, label: str, target) -> None:
        """Prune one bounded table; a failure is logged and the remaining
        targets still run this sweep (every target retries next interval)."""
        try:
            deleted = await target.prune()
        except Exception:
            logger.warning("%s: prune failed", label, exc_info=True)
            return
        if deleted:
            logger.info(
                "%s: pruned %d row(s) past retention/cap", label, deleted
            )
