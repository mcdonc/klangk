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
from klangk.notifier import notify_event

logger = logging.getLogger(__name__)

SWEEP_INTERVAL = 3600.0


def notify_sweep_disables(app, days: int, disabled: list) -> None:
    """One SA/ISSO notification for a sweep's disable batch (#3250).

    One message per sweep — not one per account — so a batch of
    dormant accounts lands as a single SV-222419 notification. No
    actor: this is a system action.
    """
    notify_event(
        app,
        "user.disable",
        detail={
            "via": "inactivity",
            "days": days,
            "users": [u["email"] for u in disabled],
        },
    )


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
                await self._audit_disable(u, days)
                await wshandler.disconnect_user(
                    self.app.state.sockets,
                    u["id"],
                    reason="Account disabled",
                )
                # #3162: the decider surface must not outlive the disable
                # either — it holds egress-consent authority.
                await wshandler.disconnect_deciders_by_user(
                    self.app, u["id"], reason="Account disabled"
                )
            logger.info(
                "inactivity: disabled %d account(s) inactive for more than"
                " %d day(s): %s",
                len(disabled),
                days,
                ", ".join(u["email"] for u in disabled),
            )
            # One SA/ISSO notification for the whole batch (#3250,
            # SV-222419): the sweeper's silent auto-disable is exactly
            # what an ISSO wants to hear about.
            notify_sweep_disables(self.app, days, disabled)

    async def _audit_disable(self, user: dict, days: int) -> None:
        """Write the sweep's ``user.disable`` audit row (#3251 review).

        The admin toggle writes one ``user.disable`` row per account
        (admin.py); the sweep disables the same accounts and must
        leave the same trail — no actor (a system action, like the
        anonymous rows), ``via=inactivity`` in the detail. Best-effort
        like every audit emit: an unwritable table logs and never
        fails the sweep.
        """
        await self.app.state.model.audit_events.record_best_effort(
            "user.disable",
            target_type="user",
            target_id=user["id"],
            detail={"via": "inactivity", "days": days, "email": user["email"]},
        )
