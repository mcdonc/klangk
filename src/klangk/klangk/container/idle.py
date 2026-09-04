"""Idle-timeout tracking and cleanup loop (#2542 split).

``IdleMonitor`` watches ``ContainerState.last_activity`` for all tracked
workspaces and kills containers that exceed their idle timeout.  Extracted
from ``ContainerRegistry`` (issue #972).
"""

import asyncio
import logging
import time

from ..model.container_events import CAUSE_IDLE_TIMEOUT
from .sidecar import ORPHAN_TOKEN_SWEEP_INTERVAL

logger = logging.getLogger(__name__)


class IdleMonitor:
    """Idle-timeout tracking and cleanup loop.

    Monitors ``ContainerState.last_activity`` for all tracked
    workspaces and kills containers that exceed their idle timeout.

    Extracted from ``ContainerRegistry`` (issue #972).
    """

    def __init__(self, app) -> None:
        self.app = app
        self.cleanup_task: asyncio.Task | None = None
        self.cleanup_wake: asyncio.Event | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    def get_cleanup_wake(self) -> asyncio.Event:
        if self.cleanup_wake is None:
            self.cleanup_wake = asyncio.Event()
        return self.cleanup_wake

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            state.idle_callbacks.append(callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state and callback in state.idle_callbacks:
            state.idle_callbacks.remove(callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            state.idle_timeout = seconds
            self.get_cleanup_wake().set()

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        state = self.app.state.container_registry.states.get(workspace_id)
        if state:
            return state.get_idle_timeout()
        return self.app.state.container_registry.idle_timeout_seconds

    async def cleanup_idle_containers(self) -> None:
        registry = self.app.state.container_registry
        last_token_sweep = 0.0
        last_volume_sweep = 0.0
        while True:
            wake = self.get_cleanup_wake()
            wake.clear()
            try:
                await asyncio.wait_for(
                    wake.wait(), timeout=self._cleanup_interval()
                )
            except asyncio.TimeoutError:
                pass
            now = time.time()
            for cid, wid in self._idle_overdue(now):
                await self._stop_idle_workspace(registry, cid, wid)
            # Periodic orphan sidecar-token sweep (#2309): reclaim
            # ws-tokens/<id> files whose workspace row is gone. Piggybacks
            # on this loop (no separate task); self-throttled to scan at
            # most every ORPHAN_TOKEN_SWEEP_INTERVAL.
            last_token_sweep = await self._sweep_tokens_if_due(
                registry, last_token_sweep, now
            )
            # Same cadence for orphaned workspace-owned volumes (#3153):
            # a volume whose workspace row is gone can never be mounted
            # again, so the sweep reclaims what a crashed delete leaves.
            last_volume_sweep = await self._sweep_volumes_if_due(
                registry, last_volume_sweep, now
            )

    async def _stop_idle_workspace(self, registry, cid: str, wid: str) -> None:
        """Notify + stop one idle workspace's container."""
        logger.info(
            "Stopping idle container %s (workspace %s)",
            cid,
            wid,
        )
        state = registry.states.get(wid)
        if state:
            await self._run_idle_callbacks(state, wid)
        await registry.notify_workspace_killed(wid, container_id=cid)
        await registry.stop_and_remove_container(cid, cause=CAUSE_IDLE_TIMEOUT)

    async def _sweep_tokens_if_due(
        self, registry, last_sweep: float, now: float
    ) -> float:
        """Run the orphan sidecar-token sweep when due; returns the
        (possibly advanced) last-sweep timestamp. A failing sweep still
        advances it."""
        if now - last_sweep >= ORPHAN_TOKEN_SWEEP_INTERVAL:
            try:
                await registry.sweep_orphaned_sidecar_tokens()
            except Exception as e:
                logger.warning("Orphan sidecar-token sweep failed: %s", e)
            return now
        return last_sweep

    async def _sweep_volumes_if_due(
        self, registry, last_sweep: float, now: float
    ) -> float:
        """Run the orphan volume sweep when due; returns the (possibly
        advanced) last-sweep timestamp. A failing sweep still advances
        it."""
        if now - last_sweep >= ORPHAN_TOKEN_SWEEP_INTERVAL:
            try:
                await registry.sweep_orphaned_volumes()
            except Exception as e:
                logger.warning("Orphan volume sweep failed: %s", e)
            return now
        return last_sweep

    def _cleanup_interval(self) -> float:
        """Half the smallest active idle timeout (floor 2s), else the
        configured check interval."""
        registry = self.app.state.container_registry
        timeouts = [
            s.idle_timeout
            for s in registry.states.values()
            if s.idle_timeout is not None
        ]
        if timeouts:
            return max(2, min(timeouts) // 2)
        return registry.check_interval_seconds

    def _idle_overdue(self, now: float) -> list[tuple[str, str]]:
        """(container_id, ws_id) for workspaces idle past their timeout."""
        registry = self.app.state.container_registry
        to_stop = []
        for ws_id, state in list(registry.states.items()):
            timeout = state.get_idle_timeout()
            idle_secs = now - state.last_activity
            logger.debug(
                "Idle check: %s idle %.0fs / %ds",
                state.container_id[:12],
                idle_secs,
                timeout,
            )
            if timeout > 0 and idle_secs > timeout:
                to_stop.append((state.container_id, ws_id))
        return to_stop

    @staticmethod
    async def _run_idle_callbacks(state, wid: str) -> None:
        """Fire a workspace's idle callbacks; one raising callback must not
        block the rest or the stop itself."""
        for cb in list(state.idle_callbacks):
            try:
                await cb(wid)
            except Exception as e:
                logger.error("Idle callback error: %s", e)

    def start_cleanup_loop(self) -> None:
        registry = self.app.state.container_registry
        logger.info(
            "Instance: %s, idle timeout: %ds, check interval: %ds",
            self.app.state.util.instance_id(),
            registry.idle_timeout_seconds,
            registry.check_interval_seconds,
        )
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                self.cleanup_idle_containers()
            )
