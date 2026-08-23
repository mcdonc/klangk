"""Scheduled server stop/recycle (#2661).

:class:`ServerScheduler` is the ``app.state``-owned service that owns the
schedule loop: it watches the persisted ``server_schedules`` rows, keeps
every connected client informed (``server_schedule`` broadcast with the
pending list — clients render the countdown locally from ``fire_at``),
and fires due schedules.

Firing reuses the existing graceful lifecycle paths verbatim — the
scheduler owns no teardown of its own (#2661 scope change: "host
shutdown/restart" and the OS power commands are gone):

* **stop** — send the process SIGTERM: the #2527 graceful-shutdown path
  broadcasts ``host_shutdown``, refuses new starts, quiesces in-flight
  requests, drains every workspace, and exits (code 0). What happens
  next is the service manager's decision, not klangkd's.
* **recycle** — request the SIGHUP graceful restart
  (:meth:`klangk.main.Lifecycle.request_recycle`): quiesce, drain,
  recycle the runtime in-process, ``host_started``. The process never
  exits; a deploy that wants the supervisor to restart klangkd uses a
  scheduled stop instead.

The schedule is persisted in the DB, so it survives a klangkd restart:
on boot the loop simply re-reads the pending rows.
"""

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Poll cadence: how often due schedules are looked for.
_POLL_INTERVAL_SECONDS = 5.0
# Re-broadcast cadence for the pending-schedule snapshot: clients compute
# the countdown locally from fire_at, so this only needs to be frequent
# enough to catch a client that missed a change-driven broadcast.
_BROADCAST_INTERVAL_SECONDS = 30.0


class ServerScheduler:
    """Owns the scheduled server-action loop (app-only ownership, #1563)."""

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        self._last_broadcast: datetime | None = None
        self._last_snapshot: list[dict] | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    # --- lifecycle ---

    def start(self) -> None:
        """Launch the schedule loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        """Cancel the loop and wait for it."""
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # --- snapshot / broadcast ---

    async def snapshot(self) -> list[dict]:
        """The pending schedules as client-facing dicts."""
        return await self.app.state.model.server_schedules.pending_schedules()

    def snapshot_message(self, schedules: list[dict]) -> dict:
        """The ``server_schedule`` WS message for *schedules*."""
        return {"type": "server_schedule", "schedules": schedules}

    async def notify_pending(self) -> None:
        """Broadcast the current pending list to every connection."""
        schedules = await self.snapshot()
        self.notify_pending_sync(schedules)

    def notify_pending_sync(self, schedules: list[dict]) -> None:
        """Broadcast an already-fetched pending list (post-mutation path)."""
        self._last_snapshot = schedules
        self._last_broadcast = datetime.now(timezone.utc)
        self.app.state.sockets.broadcast_to_all(
            self.snapshot_message(schedules)
        )

    async def send_snapshot_to(self, sock) -> None:
        """Send the current pending list to one just-registered socket.

        Called from the WS accept path so a client that connects while a
        schedule is pending learns about it immediately instead of
        waiting for the next periodic broadcast (#2661).
        """
        try:
            sock.send_json(self.snapshot_message(await self.snapshot()))
        except Exception:
            # The just-registered socket is already gone; dispatch.py
            # owns cleanup on disconnect.
            pass

    # --- loop ---

    async def _run(self) -> None:
        logger.info("Server scheduler loop started")
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad tick (DB hiccup, podman error) must not kill
                    # the loop — the schedule stays armed for the next one.
                    logger.exception("Server scheduler tick failed")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Server scheduler loop stopped")
            raise

    async def _tick(self) -> None:
        schedules = await self.snapshot()
        now = datetime.now(timezone.utc)
        due = [s for s in schedules if _parse_fire_at(s["fire_at"]) <= now]
        pending = [s for s in schedules if s not in due]
        # Refresh clients when the set changed or on the periodic cadence.
        changed = pending != (self._last_snapshot or [])
        periodic = (
            self._last_broadcast is None
            or (now - self._last_broadcast).total_seconds()
            >= _BROADCAST_INTERVAL_SECONDS
        )
        if changed or periodic:
            self.notify_pending_sync(pending)
        for schedule in due:
            await self._fire(schedule)

    # --- firing ---

    async def _fire(self, schedule: dict) -> None:
        """Fire one due schedule by handing off to the graceful paths."""
        action = schedule["action"]
        schedule_id = schedule["id"]
        logger.info(
            "Server scheduler: firing scheduled %s (%s)",
            action,
            schedule_id,
        )
        # Remove the row first: the action is happening, and this keeps a
        # klangkd restart (e.g. a crash mid-fire) from re-firing it.
        await self.app.state.model.server_schedules.delete_schedule(
            schedule_id
        )
        self.app.state.sockets.broadcast_to_all(
            {"type": "server_schedule_fired", "action": action}
        )
        if action == "stop":
            # Reuse the #2527 TERM/INT path wholesale: broadcast
            # host_shutdown, refuse starts, quiesce, drain, exit 0. The
            # signal is delivered on this (loop) thread, so the hook
            # creates the graceful-shutdown task on the running loop.
            logger.info(
                "Server scheduler: requesting graceful stop (SIGTERM to self)"
            )
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            # Recycle == the SIGHUP graceful path, always (#2661):
            # quiesce, drain, recycle the runtime in-process. The
            # process never exits — a deploy that wants the supervisor
            # to restart klangkd schedules a stop instead.
            lifecycle = self.app.state.lifecycle
            if lifecycle.shutting_down:
                logger.info(
                    "Server scheduler: recycle skipped; shutdown already "
                    "in progress"
                )
                return
            logger.info("Server scheduler: requesting graceful recycle")
            lifecycle.request_recycle(source="scheduled recycle")


def _parse_fire_at(value: str) -> datetime:
    """Parse a stored fire_at ISO string as timezone-aware UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_fire_at(payload: dict) -> datetime:
    """Turn an API scheduling payload into a timezone-aware fire time.

    Accepts ``{"at": "<ISO-8601>"}`` (absolute) or ``{"in_seconds": N}``
    (relative, > 0). Raises ``ValueError`` with a client-safe message
    otherwise.
    """
    at = payload.get("at")
    if at:
        try:
            parsed = datetime.fromisoformat(str(at))
        except ValueError as e:
            raise ValueError(f"invalid 'at' timestamp: {at!r}") from e
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    in_seconds = payload.get("in_seconds")
    if in_seconds is not None:
        try:
            seconds = float(in_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("'in_seconds' must be a number") from e
        if seconds <= 0:
            raise ValueError("'in_seconds' must be positive")
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    raise ValueError("provide either 'at' or 'in_seconds'")
