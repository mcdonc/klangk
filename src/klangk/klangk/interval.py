"""Shared background-worker scaffold: a cancel-safe interval loop.

Several app-state housekeepers (inactivity disable, egress-consent
retention, the server-action scheduler) are the same shape: start once in
the lifespan, run an interval loop whose unit of work is log-and-continue,
stop cleanly on shutdown. :class:`IntervalWorker` owns that shape;
subclasses implement :meth:`sweep` (one unit of work) and set
:data:`interval` plus the log labels.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class IntervalWorker:
    """App-state background worker: interval loop + start/stop lifecycle.

    The loop sweeps once immediately on start, then every
    :data:`interval`. A raising sweep is logged and retried an interval
    later — housekeeping, never a correctness path (:meth:`sweep` decides
    its own failure posture). Owns only ``app``; settings are read live
    inside the sweep so a SIGHUP reload applies on the next pass
    (:meth:`reconfigure` swaps ``app``).
    """

    #: Seconds between sweeps.
    interval = 3600.0

    #: The failure log line's prefix (``"<log_label> failed"``).
    log_label = "worker"

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    def start(self) -> None:
        """Start the loop (idempotent). Runs until :meth:`stop`."""
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the loop."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run(self) -> None:
        # 0.0 sweeps once immediately on startup, then every interval.
        next_sweep = 0.0
        try:
            while True:
                if time.monotonic() >= next_sweep:
                    try:
                        await self.sweep()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "%s failed", self.log_label, exc_info=True
                        )
                    next_sweep = time.monotonic() + self.interval
                await asyncio.sleep(max(0.0, next_sweep - time.monotonic()))
        except asyncio.CancelledError:
            self.on_stopped()
            raise

    def on_stopped(self) -> None:
        """Hook fired when the loop is cancelled (default: nothing)."""

    async def sweep(self) -> None:
        """One unit of periodic work; failures are logged, not fatal."""
        raise NotImplementedError  # abstract hook
