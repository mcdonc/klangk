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

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

SWEEP_INTERVAL = 3600.0


class InactivitySweeper:
    """Disable dormant accounts on a wall-clock interval (#2588).

    Constructed once in :func:`klangk.main.build_app` and stored on
    ``app.state``; started in the lifespan and stopped on shutdown.

    Owns only ``app``; settings are read live inside each sweep so a
    SIGHUP reload applies on the next pass (:meth:`reconfigure` swaps
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
        # 0.0 sweeps once immediately on startup, then every
        # SWEEP_INTERVAL.
        next_sweep = 0.0
        try:
            while True:
                if time.monotonic() >= next_sweep:
                    try:
                        await self._sweep()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "inactivity: dormant-account sweep failed",
                            exc_info=True,
                        )
                    next_sweep = time.monotonic() + SWEEP_INTERVAL
                await asyncio.sleep(max(0.0, next_sweep - time.monotonic()))
        except asyncio.CancelledError:
            pass

    async def _sweep(self) -> None:
        """One sweep: disable accounts past the inactivity window."""
        days = self.app.state.settings.inactivity_disable_days
        if days <= 0:
            return
        disabled = await self.app.state.model.users.disable_inactive_users(
            days
        )
        if disabled:
            logger.info(
                "inactivity: disabled %d account(s) inactive for more than"
                " %d day(s): %s",
                len(disabled),
                days,
                ", ".join(u["email"] for u in disabled),
            )
