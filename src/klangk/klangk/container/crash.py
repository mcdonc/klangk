"""Crash recovery: unexpected-death detection, classification, restart.

``CrashRecoveryMonitor`` (#2524) is the third registry collaborator
alongside :class:`~klangk.container.idle.IdleMonitor` and
:class:`~klangk.container.health.HealthMonitor`. Every sweep it
``podman inspect``s each tracked workspace container; a container that is
gone or not running — while its registry state is still live and no
stop is in flight — died *unexpectedly*, and is handled:

1. **Classification** — the inspect dict distinguishes an OOM kill
   (``State.OOMKilled``, reported against the workspace's effective
   memory limit) from a non-zero exit from external removal.
2. **Death events** — the terminal ``service_health`` death frame
   carries the cause; a ``container_died`` custom event is broadcast to
   the workspace session.
3. **Restart** (opt-in via ``KLANGKD_CONTAINER_RESTART_ENABLED``) — the
   workspace is restarted after an exponential backoff
   (``base * 2^(n-1)``, capped at :data:`RESTART_BACKOFF_CAP`) with a
   bounded retry count; exhaustion leaves a visible ``crash-loop``
   terminal state instead of spinning forever.

Expected deaths (user stop, idle stop, delete, logout, shutdown) all
route through :meth:`ContainerRegistry.stop_and_remove_container
<klangk.container.registry.ContainerRegistry.stop_and_remove_container>`,
which marks the workspace as stopping and cancels any pending restart —
they never enter the restart path. Because workspace state lives in
named volumes and the home bind mount, a restart loses nothing but
running processes.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from .. import podman
from ..model.container_events import (
    CAUSE_CRASH_RESTART,
    CAUSE_CRASH_TEARDOWN,
)
from .sidecar import container_ident
from .spec import resolve_memory_limit

logger = logging.getLogger(__name__)

# Seconds between liveness sweeps. Detection latency for an unexpected
# death is bounded by this interval (plus one podman inspect per tracked
# workspace). Kept well under the restart backoff so a death is
# classified before the first restart attempt fires.
LIVENESS_SWEEP_INTERVAL = 15.0

# k8s-style backoff reset: a container that stays up this long after a
# restart has its retry counter cleared, so three crashes in three months
# do not accumulate into a spurious crash-loop terminal state.
RESTART_RESET_WINDOW = 600.0

# Ceiling for the exponential backoff (``base * 2^(n-1)``).
RESTART_BACKOFF_CAP = 60.0

# Exit codes >= 128 encode a fatal signal (128 + n). Only the common
# workspace-relevant ones get a name in the death message; the rest fall
# back to the bare code.
_SIGNAL_NAMES = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    6: "SIGABRT",
    9: "SIGKILL",
    11: "SIGSEGV",
    13: "SIGPIPE",
    15: "SIGTERM",
}

# ``podman ps --format json`` reports these states for a container that
# is no longer running. Anything else (``running``, ``created`` — a
# container between create and start, ``paused``, ``restarting``) counts
# as alive: the sweep only acts on a container that has actually stopped.
# A container absent from the listing entirely was removed externally.
_DEAD_STATES = frozenset({"exited", "stopped", "dead"})


def _oom_death(memory_limit: str | None, exit_code) -> tuple[str, str]:
    """(cause, message) for an OOM kill, naming the limit when known."""
    if memory_limit:
        return (
            "oom",
            f"OOM-killed at {memory_limit} memory limit "
            f"(exit code {exit_code})",
        )
    return "oom", f"OOM-killed (exit code {exit_code})"


def _signal_death(exit_code: int) -> tuple[str, str] | None:
    """(cause, message) when the exit code is a signal death (128+n),
    else None."""
    if exit_code <= 128:
        return None
    signal_name = _SIGNAL_NAMES.get(exit_code - 128)
    if signal_name is None:
        return None
    return "exited", f"killed by {signal_name} (exit code {exit_code})"


def _exit_death(exit_code) -> tuple[str, str]:
    """(cause, message) for a plain (non-OOM) process exit."""
    if exit_code is None:
        return "exited", "main process exited (no exit code recorded)"
    if exit_code == 0:
        return "exited", "main process exited cleanly (code 0)"
    if isinstance(exit_code, int):
        signal = _signal_death(exit_code)
        if signal is not None:
            return signal
    return "exited", f"main process exited with code {exit_code}"


def classify_death(
    info: dict | None, memory_limit: str | None = None
) -> tuple[str, str]:
    """Classify a dead container into ``(cause, message)`` (#2524).

    *info* is the last successful ``podman inspect`` result (``None``
    when the container no longer exists — external removal), and
    *memory_limit* the workspace's effective ``--memory`` value (used to
    make an OOM kill name the limit it hit instead of surfacing as a
    generic death). The returned *cause* is one of ``"oom"``,
    ``"exited"``, or ``"removed"``; *message* is a short human-readable
    reason that rides the death events and logs.
    """
    if info is None:
        return "removed", "container removed externally (not found)"
    state = info.get("State") or {}
    exit_code = state.get("ExitCode")
    if state.get("OOMKilled"):
        return _oom_death(memory_limit, exit_code)
    return _exit_death(exit_code)


class RestartTracker:
    """Per-workspace restart bookkeeping (#2524).

    Lives on the :class:`CrashRecoveryMonitor` (in-memory, like the rest
    of the registry state — a klangkd restart reaps all containers
    anyway, so there is nothing to persist). ``attempts`` counts restarts
    *scheduled* (a scheduled attempt that has not fired yet is already
    committed against the bounded budget). ``last_started_at`` anchors
    the stability window that resets the counter.
    """

    def __init__(self) -> None:
        self.attempts: int = 0
        self.last_cause: str | None = None
        self.last_started_at: float | None = None
        self.next_attempt_at: float | None = None
        self.gave_up_at: float | None = None

    def _phase(self) -> str:
        """The tracker's display state."""
        if self.gave_up_at is not None:
            return "crash-loop"
        if self.next_attempt_at is not None:
            return "backing-off"
        if self.last_started_at is not None:
            return "recovering"
        return "dead"

    def status(self) -> dict:
        """API shape for the /status endpoint and list annotation."""
        out: dict = {
            "state": self._phase(),
            "attempts": self.attempts,
            "last_cause": self.last_cause,
        }
        if self.next_attempt_at is not None:
            out["next_attempt_at"] = _iso(self.next_attempt_at)
        if self.gave_up_at is not None:
            out["gave_up_at"] = _iso(self.gave_up_at)
        return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class CrashRecoveryMonitor:
    """Detects unexpectedly-dead containers and (opt-in) restarts them.

    Composed into :class:`~klangk.container.registry.ContainerRegistry`
    as ``registry.crash`` (#2524). Settings are read live off
    ``app.state.settings`` so a SIGHUP reload applies to the next sweep
    (and pending restarts re-check ``enabled`` when they fire).
    """

    def __init__(self, app) -> None:
        self.app = app
        self.crash_task: asyncio.Task | None = None
        # ws_id -> RestartTracker; survives the death teardown so the
        # status API can show why a workspace is down.
        self.trackers: dict[str, RestartTracker] = {}
        # ws_id -> in-flight delayed-restart asyncio.Task. A workspace
        # with a pending restart is skipped by the sweep (its registry
        # state is gone, but the guard also covers a restart that has
        # re-created the state mid-task).
        self.pending: dict[str, asyncio.Task] = {}

    def reconfigure(self, app) -> None:
        self.app = app

    # --- settings (read live, #1608) ---

    @property
    def enabled(self) -> bool:
        return bool(self.app.state.settings.container_restart_enabled)

    @property
    def max_retries(self) -> int:
        return self.app.state.settings.container_restart_max_retries

    @property
    def backoff_base(self) -> float:
        return self.app.state.settings.container_restart_backoff_seconds

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff for 1-based *attempt*: 5s -> 10s -> 20s ... capped."""
        return min(
            self.backoff_base * (2 ** (attempt - 1)), RESTART_BACKOFF_CAP
        )

    # --- lifecycle hooks (called by the registry) ---

    def on_start(self, workspace_id: str) -> None:
        """Reset crash bookkeeping for a workspace being started.

        Called at the top of
        :meth:`~klangk.container.registry.ContainerRegistry.start_container`
        (the single start choke point), so every user-driven start —
        API /start, /restart, a WebSocket connect, autostart — gets a
        fresh slate. The monitor's own restart path also lands there,
        running *inside* a pending task; that case is detected by task
        identity and must NOT reset the counter it is counting on.
        """
        pending = self.pending.get(workspace_id)
        if pending is not None and pending is asyncio.current_task():
            return
        self.clear(workspace_id)

    def on_expected_stop(self, workspace_id: str) -> None:
        """Cancel any pending restart for an expected stop.

        User stop, idle stop, delete, logout, and shutdown all land
        here: an expected death never triggers the restart path, and a
        pending backoff for a workspace the user just acted on must not
        fire afterwards.
        """
        self.clear(workspace_id)

    def clear(self, workspace_id: str) -> None:
        task = self.pending.pop(workspace_id, None)
        if task is not None and not task.done():
            task.cancel()
        self.trackers.pop(workspace_id, None)

    def status(self, workspace_id: str) -> dict | None:
        """Restart/crash state for the status API, or None when clean."""
        tracker = self.trackers.get(workspace_id)
        return tracker.status() if tracker is not None else None

    # --- background loop ---

    def start(self) -> None:
        if self.crash_task is None:
            self.crash_task = asyncio.create_task(self.run_loop())

    async def stop(self) -> None:
        if self.crash_task is not None:
            self.crash_task.cancel()
            try:
                await self.crash_task
            except asyncio.CancelledError:
                pass
            self.crash_task = None
        # Cancelling a pending restart must not leave a stale
        # ``backing-off`` status behind: the task's ``next_attempt_at``
        # was set before its sleep and will never fire again, so
        # /status would report a restart that is never coming
        # (#2524 review). The tracker (cause, attempts) survives for
        # diagnosis; only the pending attempt is cleared.
        for ws_id, task in list(self.pending.items()):
            task.cancel()
            tracker = self.trackers.get(ws_id)
            if tracker is not None:
                tracker.next_attempt_at = None
        self.pending.clear()

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(LIVENESS_SWEEP_INTERVAL)
            try:
                await self.sweep_once()
            except Exception as e:  # pragma: no cover - defensive
                logger.error("Crash-recovery sweep failed: %s", e)

    async def sweep_once(self) -> None:
        """Check every tracked container for liveness and handle deaths.

        Liveness is ONE batched ``podman ps`` for the whole instance (a
        per-workspace ``inspect`` every 15s would be ~7 subprocess
        spawns/sec at 100 workspaces, #2524 review); the per-container
        ``inspect`` runs only to *classify* an actual death. The label
        filter is complete because the instance id is persisted in the
        data dir, so every tracked workspace container — including one
        adopted across a klangkd restart — carries this instance's label.

        Everything the guards depend on is captured BEFORE the await and
        revalidated after it (review #2625): a user stop can begin and
        complete, or a user start can rebind ``state.container_id``, while
        the listing is in flight. Acting on post-await registry state
        would (a) restart a workspace the user just stopped, or (b) tear
        down the freshly-started container — both demonstrated in review.
        """
        snapshot = self._liveness_snapshot()
        if not snapshot:
            return
        liveness = await self._listed_liveness()
        if liveness is None:
            return
        for ws_id, state, cid, epoch in snapshot:
            await self._sweep_one(ws_id, state, cid, epoch, liveness)

    def _liveness_snapshot(self) -> list:
        """(ws_id, state, container_id, stop_epoch) for every container to
        check, excluding in-flight expected stops and pending restarts —
        captured BEFORE any await (review #2625)."""
        registry = self.app.state.container_registry
        return [
            (
                ws_id,
                state,
                state.container_id,
                registry.stop_epoch.get(ws_id, 0),
            )
            for ws_id, state in registry.states.items()
            if ws_id not in registry.stopping and ws_id not in self.pending
        ]

    async def _listed_liveness(self) -> dict | None:
        """container ident -> ps State for this instance, or None when the
        batched listing failed."""
        try:
            listed = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
        except (podman.PodmanError, OSError) as e:
            logger.debug("Crash-recovery liveness listing failed: %s", e)
            return None
        return {container_ident(c): (c.get("State") or "") for c in listed}

    async def _sweep_one(
        self,
        ws_id: str,
        state,
        cid: str,
        epoch: int,
        liveness: dict,
    ) -> None:
        """Revalidate one snapshot row post-await, then classify a death or
        reset the tracker of a stable survivor."""
        registry = self.app.state.container_registry
        # Post-await revalidation: skip anything the world moved under.
        if not self._snapshot_still_current(
            registry, ws_id, state, cid, epoch
        ):
            return
        observed = liveness.get(cid)
        if self._liveness_alive(observed):
            # Alive (running / created / paused / ...): nothing to do.
            self.maybe_reset_tracker(ws_id)
            return
        # Absent from the listing (removed externally) or listed in
        # a dead state — classify via one per-container inspect.
        try:
            info = await self.app.state.podman.inspect_container(cid)
        except (podman.PodmanError, OSError) as e:
            logger.debug(
                "Crash-recovery inspect failed for workspace %s: %s",
                ws_id,
                e,
            )
            return
        try:
            await self.handle_death(ws_id, cid, info, epoch=epoch)
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                "Crash-recovery death handling failed for workspace %s: %s",
                ws_id,
                e,
            )

    def _snapshot_still_current(
        self, registry, ws_id: str, state, cid: str, epoch: int
    ) -> bool:
        """Whether the sweep snapshot row is still the live truth after
        the liveness listing's await."""
        if registry.states.get(ws_id) is not state:
            return False  # state replaced/removed (rebind, user start/stop)
        if state.container_id != cid:
            return False  # rebound to a fresh container — not our death
        if ws_id in registry.stopping:
            return False  # an expected stop is now in flight
        if registry.stop_epoch.get(ws_id, 0) != epoch:
            return False  # a stop began AND completed during the listing
        return True

    @staticmethod
    def _liveness_alive(observed) -> bool:
        """True when an observed liveness state means alive (running /
        created / paused / ...) rather than dead or absent."""
        return observed is not None and observed not in _DEAD_STATES

    def maybe_reset_tracker(self, ws_id: str) -> None:
        """Drop the retry counter once a restarted container is stable.

        A container that has stayed up for :data:`RESTART_RESET_WINDOW`
        after a restart has recovered; keeping the counter would let
        unrelated crashes months apart accumulate into a spurious
        crash-loop terminal state (k8s resets its backoff the same way).
        """
        tracker = self.trackers.get(ws_id)
        if tracker is None or tracker.last_started_at is None:
            return
        if time.time() - tracker.last_started_at >= RESTART_RESET_WINDOW:
            self.trackers.pop(ws_id, None)

    # --- death handling ---

    async def handle_death(
        self,
        ws_id: str,
        container_id: str,
        info: dict | None,
        *,
        epoch: int | None = None,
    ) -> None:
        """Classify an unexpected death, emit events, maybe restart.

        Order matters: the terminal frames fire while the registry state
        still exists (:meth:`notify_workspace_killed` reads it), then the
        teardown removes the (dead) container and its state, then the
        restart is scheduled against the fresh DB workspace row.

        Race closure (review #2625): *epoch* is the workspace's stop
        counter as captured by the sweep before its liveness await. The
        entry guards re-verify the death is still ours to handle (state
        present and bound to this container, no stop in flight, no stop
        having begun-and-completed since), and the post-teardown guard —
        run synchronously, with NO await between it and the
        ``create_task`` inside :meth:`schedule_restart` — catches a stop
        that began while the teardown awaits ran. Any stop beginning
        after ``create_task`` cancels the pending task at its entry
        (:meth:`ContainerRegistry.stop_and_remove_container` calls
        ``on_expected_stop` before its first await). Together these make
        "expected deaths never restart" hold for every interleaving.
        """
        registry = self.app.state.container_registry
        # Entry guards: if the world moved between the sweep's detection
        # and now, this death is not ours to handle.
        state = registry.states.get(ws_id)
        if not self._death_guards_pass(
            registry, ws_id, state, container_id, epoch
        ):
            return
        memory_limit = await self._effective_memory_limit(ws_id)
        # Re-validate after the await (#331): a user-driven reconnect
        # (start_container -> handle_existing_container removes the dead
        # container with a direct podman rm -- no ``stopping`` marker, no
        # epoch bump -- then create_and_start re-binds the state to a
        # fresh container) can complete entirely while the limit read is
        # in flight. The entry guards above ran before that await; acting
        # on the post-await state would tear down the freshly-started
        # container's registry state and network sidecar.
        if not self._death_guards_pass(
            registry, ws_id, state, container_id, epoch
        ):
            return  # the world moved during the limit read (#331)
        cause, message = classify_death(info, memory_limit)
        tracker = self.trackers.get(ws_id) or RestartTracker()
        tracker.last_cause = message
        tracker.next_attempt_at = None
        tracker.last_started_at = None
        logger.warning(
            "Workspace %s container %s died unexpectedly: %s",
            ws_id,
            container_id[:12],
            message,
        )
        # The service_health death frame carries the cause so consumers
        # can tell an OOM kill from a crash from external removal.
        await registry.notify_workspace_killed(
            ws_id, cause=message, container_id=container_id
        )
        # Teardown under the expected-stop marker (this stop is on
        # purpose — the restart, if any, is scheduled after it).
        await registry.stop_and_remove_container(
            container_id, workspace_id=ws_id, cause=CAUSE_CRASH_TEARDOWN
        )
        # Post-teardown sync guards (#2524 review): NO await between these
        # checks and either the tracker insert or the create_task inside
        # schedule_restart, so the single-threaded loop cannot interleave
        # here. A stop that began during the awaits above bumped the
        # epoch (its entry is synchronous); a user start that raced us
        # left a fresh container running (registry state present). In
        # both cases the user action owns the workspace now — record the
        # death event for viewers, but drop the tracker and never
        # restart.
        self._finalize_death(registry, ws_id, cause, message, tracker, epoch)

    def _finalize_death(
        self,
        registry,
        ws_id: str,
        cause,
        message: str,
        tracker: RestartTracker,
        epoch: int | None,
    ) -> None:
        """Post-teardown disposition: interleaved user action wins (no
        restart), else disabled-recovery / crash-loop / schedule-restart."""
        if self._user_action_interleaved(registry, ws_id, epoch):
            logger.info(
                "Workspace %s: user action interleaved with death "
                "handling; not recording crash state or restarting",
                ws_id,
            )
            self.trackers.pop(ws_id, None)  # fresh slate; the stop owns it
            self.broadcast_death_event(ws_id, cause, message, None)
            return
        if not self.enabled:
            # Recovery stays manual (the pre-#2524 behavior); keep the
            # tracker so /status still shows why the workspace died.
            self.trackers[ws_id] = tracker
            self.broadcast_death_event(ws_id, cause, message, tracker)
            return
        if tracker.attempts >= self.max_retries:
            tracker.gave_up_at = time.time()
            self.trackers[ws_id] = tracker
            logger.warning(
                "Workspace %s: crash-loop — %d restart attempt(s) "
                "exhausted (last death: %s); leaving stopped",
                ws_id,
                tracker.attempts,
                message,
            )
            self.broadcast_death_event(ws_id, cause, message, tracker)
            return
        self.schedule_restart(ws_id, tracker)
        self.broadcast_death_event(ws_id, cause, message, tracker)

    @staticmethod
    def _stop_epoch_moved(registry, ws_id: str, epoch: int | None) -> bool:
        """True when a stop began and completed since *epoch* was
        captured."""
        return epoch is not None and registry.stop_epoch.get(ws_id, 0) != epoch

    def _user_action_interleaved(
        self, registry, ws_id: str, epoch: int | None
    ) -> bool:
        """True when a user action raced the death handling and owns the
        workspace now: a stop began/completed during the awaits, or a
        start re-created registry state."""
        if self._stop_epoch_moved(registry, ws_id, epoch):
            return True
        return registry.states.get(ws_id) is not None

    @staticmethod
    def _death_guards_pass(
        registry, ws_id: str, state, container_id: str, epoch: int | None
    ) -> bool:
        """Whether the death is still ours to handle: the registry state is
        present and bound to this container, no expected stop is in flight,
        and no stop began-and-completed since the epoch was captured
        (review #2625). ``state`` may be None (already gone)."""
        if state is None or state.container_id != container_id:
            return False  # workspace state gone or rebound (user action raced)
        if ws_id in registry.stopping:
            return False  # an expected stop is in flight
        if CrashRecoveryMonitor._stop_epoch_moved(registry, ws_id, epoch):
            return False  # a stop began and completed during detection
        return True

    async def _effective_memory_limit(self, ws_id: str) -> str | None:
        """The workspace's effective ``--memory`` (bag override or deploy)."""
        try:
            ws = await self.app.state.model.workspaces.get_workspace_by_id(
                ws_id
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(
                "Crash-recovery workspace lookup failed for %s: %s", ws_id, e
            )
            ws = None
        if ws is not None:
            return resolve_memory_limit(self.app, ws.get("settings"))
        return self.app.state.settings.container_memory_limit

    def schedule_restart(self, ws_id: str, tracker: RestartTracker) -> None:
        """Start the delayed-restart task for *ws_id*."""
        tracker.next_attempt_at = time.time() + self.backoff_delay(
            tracker.attempts + 1
        )
        self.trackers[ws_id] = tracker
        task = asyncio.create_task(self.delayed_restart(ws_id, tracker))
        self.pending[ws_id] = task

        def done(t: asyncio.Task) -> None:
            if self.pending.get(ws_id) is t:
                self.pending.pop(ws_id, None)

        task.add_done_callback(done)

    async def delayed_restart(
        self, ws_id: str, tracker: RestartTracker
    ) -> None:
        """Backoff loop: sleep, restart, retry on failure, give up bounded.

        Aborts (without restarting) when the tracker was superseded — a
        user started or stopped the workspace, or it was deleted — or
        when the feature was disabled mid-flight (SIGHUP).
        """
        while True:
            attempt = tracker.attempts + 1
            delay = self.backoff_delay(attempt)
            tracker.attempts = attempt
            tracker.next_attempt_at = time.time() + delay
            logger.info(
                "Workspace %s: restart attempt %d/%d in %.0fs",
                ws_id,
                attempt,
                self.max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            if self._restart_abort(ws_id, tracker):
                return
            ws = await self._restartable_workspace(ws_id)
            if ws is None:
                return
            if await self._restart_or_retry(ws_id, tracker, attempt, ws):
                return
            # Loop: next backoff (the counter doubling continues).

    def _restart_abort(self, ws_id: str, tracker: RestartTracker) -> bool:
        """True when the pending restart must abort now: superseded by a
        user action (silent, marker left), disabled mid-flight (marker
        cleared), or start-refused by a drain (#2527, logged + marker
        cleared)."""
        if self.trackers.get(ws_id) is not tracker:
            return True  # superseded by a user action
        if not self.enabled:
            tracker.next_attempt_at = None
            return True
        blocked = self.app.state.container_registry.new_starts_blocked_reason()
        if blocked:
            tracker.next_attempt_at = None
            logger.info(
                "Workspace %s: restart suppressed (%s)",
                ws_id,
                blocked,
            )
            return True
        return False

    async def _restartable_workspace(self, ws_id: str) -> dict | None:
        """The workspace row to restart, or None when it was deleted (or
        unreadable) — the tracker is dropped and the restart abandoned."""
        try:
            ws = await self.app.state.model.workspaces.get_workspace_by_id(
                ws_id
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Workspace %s restart: lookup failed: %s", ws_id, e)
            return None
        if ws is None:
            self.trackers.pop(ws_id, None)
            logger.info(
                "Workspace %s no longer exists; abandoning restart",
                ws_id,
            )
        return ws

    async def _restart_or_retry(
        self, ws_id: str, tracker: RestartTracker, attempt: int, ws: dict
    ) -> bool:
        """Start the restarted workspace once; True when the loop should
        stop (restarted, or crash-loop terminal), False to loop into the
        next backoff. Reraises CancelledError."""
        try:
            await self._start_restarted_workspace(ws_id, tracker, attempt, ws)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return self._on_restart_failure(ws_id, tracker, attempt, e)

    async def _start_restarted_workspace(
        self, ws_id: str, tracker: RestartTracker, attempt: int, ws: dict
    ) -> None:
        """One successful restart: record the stability anchor and log."""
        cid, _status = await self.app.state.workspaces.start_workspace(
            ws, cause=CAUSE_CRASH_RESTART
        )
        tracker.last_started_at = time.time()
        tracker.next_attempt_at = None
        logger.info(
            "Workspace %s restarted after unexpected death "
            "(attempt %d, container %s)",
            ws_id,
            attempt,
            cid[:12],
        )

    def _on_restart_failure(
        self,
        ws_id: str,
        tracker: RestartTracker,
        attempt: int,
        exc: Exception,
    ) -> bool:
        """Log a failed attempt; True when the loop should stop
        (crash-loop terminal), False to retry with the next backoff."""
        logger.warning(
            "Workspace %s restart attempt %d failed: %s",
            ws_id,
            attempt,
            exc,
        )
        if tracker.attempts < self.max_retries:
            return False
        self._give_up_restart(ws_id, tracker)
        return True

    def _give_up_restart(self, ws_id: str, tracker: RestartTracker) -> None:
        """Mark the tracker crash-loop terminal; the workspace stays
        stopped."""
        tracker.gave_up_at = time.time()
        tracker.next_attempt_at = None
        logger.warning(
            "Workspace %s: crash-loop — %d restart attempt(s) "
            "exhausted; leaving stopped",
            ws_id,
            tracker.attempts,
        )

    # --- events ---

    def broadcast_death_event(
        self,
        ws_id: str,
        cause: str,
        message: str,
        tracker: RestartTracker | None,
    ) -> None:
        """Broadcast a ``container_died`` custom event to the workspace session.

        Mirrors the ``container_stopped`` event the /stop endpoint sends,
        so connected viewers see *why* the container went down and
        whether a restart is coming. No-op when nobody is connected.
        """
        session = self.app.state.sockets.get_session(ws_id)
        if not session:
            return
        value: dict = {"cause": cause, "message": message}
        if tracker is not None:
            value["restart_attempts"] = tracker.attempts
            value["restart_scheduled"] = tracker.next_attempt_at is not None
            if tracker.next_attempt_at is not None:
                value["restart_in_seconds"] = max(
                    0.0, round(tracker.next_attempt_at - time.time(), 1)
                )
            value["gave_up"] = tracker.gave_up_at is not None
        session.broadcast(
            {
                "type": "event",
                "event": {
                    "type": "CUSTOM",
                    "name": "container_died",
                    "value": value,
                },
            }
        )
