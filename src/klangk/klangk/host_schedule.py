"""Scheduled host shutdown/restart (#2661).

:class:`HostScheduler` is the ``app.state``-owned service that owns the
schedule loop: it watches the persisted ``host_schedules`` rows, keeps
every connected client informed (``host_schedule`` broadcast with the
pending list — clients render the countdown locally from ``fire_at``),
and fires due schedules.

Firing mirrors the graceful TERM/INT shutdown (#2527): broadcast the
action, refuse new container starts, quiesce in-flight HTTP requests,
gracefully drain every workspace, then run the configured OS command
(``KLANGKD_HOST_SHUTDOWN_COMMAND`` / ``_RESTART_``). An empty command is
a dry run — everything up to the OS step still happens (teardown +
notifications), which is the right default for a klangkd that lacks the
privileges to power off its host.

The schedule is persisted in the DB, so it survives a klangkd restart:
on boot the loop simply re-reads the pending rows.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Poll cadence: how often due schedules are looked for.
_POLL_INTERVAL_SECONDS = 5.0
# Re-broadcast cadence for the pending-schedule snapshot: clients compute
# the countdown locally from fire_at, so this only needs to be frequent
# enough to catch a client that missed a change-driven broadcast.
_BROADCAST_INTERVAL_SECONDS = 30.0


class HostScheduler:
    """Owns the scheduled host-action loop (app-only ownership, #1563)."""

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
        return await self.app.state.model.host_schedules.pending_schedules()

    def snapshot_message(self, schedules: list[dict]) -> dict:
        """The ``host_schedule`` WS message for *schedules*."""
        return {"type": "host_schedule", "schedules": schedules}

    async def notify_pending(self) -> None:
        """Broadcast the current pending list to every connection."""
        schedules = await self.snapshot()
        self._last_snapshot = schedules
        self._last_broadcast = datetime.now(timezone.utc)
        self.app.state.sockets.broadcast_to_all(
            self.snapshot_message(schedules)
        )

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
        logger.info("Host scheduler loop started")
        try:
            while True:
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One bad tick (DB hiccup, podman error) must not kill
                    # the loop — the schedule stays armed for the next one.
                    logger.exception("Host scheduler tick failed")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Host scheduler loop stopped")
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
        """Fire one due schedule: teardown, notify, OS command."""
        action = schedule["action"]
        schedule_id = schedule["id"]
        logger.info(
            "Host scheduler: firing scheduled %s (%s)",
            action,
            schedule_id,
        )
        # Remove the row first: the action is happening, and this keeps a
        # klangkd restart (e.g. a crash mid-fire) from re-firing it.
        await self.app.state.model.host_schedules.delete_schedule(schedule_id)
        state = self.app.state
        sockets = state.sockets
        sockets.broadcast_to_all(
            {"type": "host_schedule_fired", "action": action}
        )
        registry = state.container_registry
        registry.draining = True
        try:
            timeout = state.settings.quiesce_timeout
            logger.info(
                "Host scheduler: quiesce (waiting up to %.1fs for "
                "in-flight requests)",
                timeout,
            )
            inflight = state.inflight_requests
            idle = await inflight.wait_for_idle(timeout)
            if not idle:
                logger.warning(
                    "Host scheduler: %d request(s) still in flight after "
                    "%.1fs; proceeding",
                    inflight.count,
                    timeout,
                )
            logger.info("Host scheduler: draining workspaces")
            stopped = await registry.drain_all_containers(
                reason=f"scheduled host {action}"
            )
            logger.info("Host scheduler: drained %d workspace(s)", stopped)
        except Exception:
            logger.exception(
                "Host scheduler: teardown for scheduled %s failed", action
            )
        command = (
            state.settings.host_shutdown_command
            if action == "shutdown"
            else state.settings.host_restart_command
        )
        if not command:
            logger.warning(
                "Host scheduler: no %s command configured "
                "(KLANGKD_HOST_%s_COMMAND is empty); dry run — teardown "
                "complete, host left running",
                action,
                action.upper(),
            )
            return
        logger.info("Host scheduler: running %s command: %s", action, command)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info(
            "Host scheduler: %s command exited rc=%s", action, proc.returncode
        )


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
