"""Periodic inactivity sweep: auto-disable dormant accounts (#2588).

The sweeper runs once immediately on startup, then every
:data:`SWEEP_INTERVAL`. Each pass reads
``settings.inactivity_disable_days`` live (a SIGHUP reload applies on
the next sweep) and delegates to
:meth:`klangk.model.users.UsersModel.disable_inactive_users`, which
judges inactivity by the newest of last API access / last login / account
creation and exempts the system agent and admin-group members.

A failed sweep logs and retries an interval later — housekeeping, not a
correctness path.
"""

from __future__ import annotations

import logging

from klangk import wshandler
from klangk.interval import IntervalWorker

logger = logging.getLogger(__name__)

SWEEP_INTERVAL = 3600.0


class InactivitySweeper(IntervalWorker):
    """Disable dormant accounts on a wall-clock interval (#2588).

    Constructed once in :func:`klangk.main.build_app` and stored on
    ``app.state``; started in the lifespan and stopped on shutdown.

    Owns only ``app``; settings are read live inside each sweep so a
    SIGHUP reload applies on the next pass (:meth:`reconfigure` swaps
    ``app``).
    """

    # Live-read (a property, not a captured constant) so a patched/test
    # module global applies on the next cycle.
    @property
    def interval(self) -> float:
        return SWEEP_INTERVAL

    log_label = "inactivity: dormant-account sweep"

    async def sweep(self) -> None:
        """One sweep: disable accounts past the inactivity window."""
        days = self.app.state.settings.inactivity_disable_days
        if days <= 0:
            return
        disabled = await self.app.state.model.users.disable_inactive_users(
            days
        )
        if disabled:
            # Cut live connections for each disabled account (#2588
            # review): the WS is the terminal/control data plane, and a
            # dormant-turned-disabled account must not keep it. 4001 ->
            # the client logs out rather than reconnect-looping.
            for u in disabled:
                await wshandler.disconnect_user(
                    self.app.state.sockets,
                    u["id"],
                    reason="Account disabled",
                )
            logger.info(
                "inactivity: disabled %d account(s) inactive for more than"
                " %d day(s): %s",
                len(disabled),
                days,
                ", ".join(u["email"] for u in disabled),
            )
