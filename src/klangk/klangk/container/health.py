"""Service health polling via podman exec (#2542 split).

``HealthMonitor`` mirrors ``IdleMonitor`` in shape; see #1015 for the
design rationale (external polling beats a container-side WS agent).
"""

import asyncio
import logging
import time

from .. import podman
from .spec import SHARED_HOME
from .state import ContainerState

logger = logging.getLogger(__name__)

HEALTH_MESSAGE_MAX_BYTES = 512


def unhealthy_message(rc: int, out: str, err: str) -> str:
    """Build a bounded failure reason from a check's exit code/output.

    Prefers stderr (where shells/diagnostics write their failures);
    falls back to a tail of stdout; if both are empty, reports just
    the exit code.  Truncated to ``HEALTH_MESSAGE_MAX_BYTES`` so a
    verbose check can't grow memory unbounded across workspaces --
    the goal is "why did it fail", not a full transcript (#1088).
    """
    body = (err or "").strip() or (out or "").strip()
    if body and len(body) > HEALTH_MESSAGE_MAX_BYTES:
        body = "..." + body[-HEALTH_MESSAGE_MAX_BYTES:]
    return f"exited {rc}: {body}" if body else f"exited {rc}"


class HealthMonitor:
    """Periodically poll container service health via ``podman exec``.

    Mirrors :class:`IdleMonitor` in shape.  For each workspace with a
    running container and a configured ``health_check`` command, runs
    the command inside the container as the creating user with their
    HOME set.  Exit 0 -> healthy; anything else (non-zero, timeout,
    error) -> unhealthy.  Status transitions are broadcast to
    connected clients as ``service_health`` events.

    See #1015 for the design rationale (external polling beats a
    container-side WS agent here).
    """

    def __init__(self, app) -> None:
        self.app = app
        self.health_task: asyncio.Task | None = None

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def connections(self):
        """The WebSocketState instance the monitor broadcasts through (#1464).

        Reached via ``app_state.sockets`` (owned instance, #1426) — no
        post-construction wiring needed.
        """
        return self.app.state.sockets

    def _setup_complete(self, state: ContainerState) -> bool:
        """True if health checks may run for this workspace.

        Checks are skipped until setup has finished (setup_state ==
        "complete"); running them during setup would report false
        negatives (the service isn't running yet because setup.sh
        hasn't installed it).
        """
        return state.setup_state == "complete"

    def _in_startup_grace(self, state: ContainerState) -> bool:
        """True while the service is still within its startup grace window.

        Mirrors Docker's HEALTHCHECK ``--start-period``: while the
        service command is booting, a failing check is expected rather
        than a real outage, so :meth:`_check_workspace` ignores
        unhealthy results here (but still records a *healthy* result so
        a fast-booting service is marked up immediately).  Anchored to
        ``service_started_at`` (when the command fired, or container
        creation as a fallback).
        """
        return (
            time.time() - state.service_started_at
            < self.app.state.container_registry.health_check_startup_grace
        )

    async def _run_one(self, state: ContainerState) -> tuple[str, str]:
        """Run a single workspace's health check.

        Returns ``(status, message)`` where *status* is ``"healthy"`` or
        ``"unhealthy"`` and *message* is a short, human-readable reason
        for an unhealthy result (a bounded tail of the check's
        stderr/stdout, or the exception text) -- empty when healthy.
        Surfacing the reason turns an ``unhealthy`` status from a black
        box into a diagnosable failure instead of "good luck" (#1088).

        Resolves the owner's container home (same logic as
        ``start_workspace``) and invokes the check via
        ``podman exec`` as the creating user with HOME set.  The check
        runs as a **non-login** bash shell (``bash -c``) on purpose: it
        is an operational probe, not a user session, so it deliberately
        sources *no* startup file -- not ``~/.profile``, ``~/.bashrc``,
        nor ``/etc/profile.d/*``.  This keeps the probe deterministic
        and decoupled from the owning user's interactive setup: a slow
        ``nvm`` load, a broken ``~/.profile`` edit, or a stray ``read``
        prompt must never make an unattended 30s poll flap "unhealthy".

        The flip side is that the check command must not rely on the
        user's PATH or env.  It inherits only the container's image
        ``PATH`` (so ``/opt/klangk/bin`` and system tools like
        ``grep``/``curl`` resolve) plus ``HOME``.  Anything the checked
        service needs -- a sandbox-installed binary, ``OPENCLAW_HOME`` /
        ``HERMES_HOME``, a custom ``PATH`` -- must be referenced by
        **absolute path** in the check command, or wrapped in an
        executable script whose shebang and ``export`` lines bake those
        in (the recommended pattern for non-trivial checks; see
        ``docs/features/health-check.md``).  Errors and timeouts count
        as ``"unhealthy"``.
        """
        owner_id = state.owner_id
        if owner_id is None:
            return "unhealthy", "no owner recorded for workspace"
        if state.per_handle_home:
            handle = await self.app.state.model.users.get_user_handle(owner_id)
            if not handle:
                return "unhealthy", f"owner {owner_id} has no handle"
            # Resolve the owner's container home the same way
            # start_workspace does, so the check runs in the right
            # HOME rather than as root in /.
            ws = self.app.state.workspaces
            ws_home = ws.home_path(state.workspace_id)
            user_home, _created = await ws.ensure_home_symlink(
                ws_home, handle, owner_id
            )
        else:
            # Shared layout (#2169 chunk 2, #2720): one home for every
            # connection — the check probes the workspace's shared
            # /home/klangk; no per-owner symlink to resolve.
            user_home = SHARED_HOME
        cid_short = state.container_id[:12]
        logger.debug(
            "Health check: container %s (workspace %s) running %r",
            cid_short,
            state.workspace_id,
            state.health_check,
        )
        try:
            rc, out, err = await self.app.state.podman.exec_container(
                state.container_id,
                # bash -c (NON-login): sources nothing, so the probe is
                # deterministic and insulated from the user's interactive
                # shell setup. Only the image PATH + HOME are visible,
                # so health_check commands must use absolute paths (or a
                # wrapper script). See docs/features/health-check.md.
                # Skipped until setup_state == complete.
                ["bash", "-c", state.health_check],
                user="klangk",
                extra_env={"HOME": user_home},
                timeout=self.app.state.container_registry.health_check_timeout,
            )
        except (podman.PodmanError, asyncio.TimeoutError, OSError) as e:
            return "unhealthy", f"{type(e).__name__}: {e}"
        if rc == 0:
            return "healthy", ""
        return "unhealthy", unhealthy_message(rc, out, err)

    async def _check_workspace(self, state: ContainerState) -> None:
        """Poll one workspace, record the reason, and broadcast on change."""
        new_status, message = await self._run_one(state)
        # Startup grace window: the service command may still be
        # booting, so an unhealthy result here is expected, not a real
        # outage.  Don't transition to unhealthy, broadcast, or log a
        # failure (mirrors Docker HEALTHCHECK --start-period).  A
        # healthy result is still recorded below so a fast-booting
        # service is marked up the moment it actually responds.
        if new_status == "unhealthy" and self._in_startup_grace(state):
            logger.debug(
                "Health check for workspace %s (container %s) failing "
                "but within startup grace (%.0fs elapsed); not flagging "
                "unhealthy",
                state.workspace_id,
                state.container_id[:12],
                time.time() - state.service_started_at,
            )
            return
        old_status = state.health_status
        state.health_status = new_status
        # Clear the reason once healthy again so a stale failure message
        # can't linger next to a "healthy" status (#1088).
        state.health_message = message if new_status == "unhealthy" else None
        state.health_checked_at = time.time()
        if new_status == "unhealthy":
            # Log the reason at info on a fresh transition (so it's
            # visible without debug logs), debug on steady-state polls
            # so a persistently-broken check doesn't spam at info (#1088).
            log = logger.info if old_status != "unhealthy" else logger.debug
            log(
                "Health check for workspace %s (container %s) unhealthy: %s",
                state.workspace_id,
                state.container_id[:12],
                message,
            )
        if new_status != old_status:
            self._broadcast(state, new_status, state.health_message)

    def _emit(
        self,
        state: ContainerState,
        *,
        healthy: bool,
        message: str | None,
        running: bool,
    ) -> None:
        """Single emit point for ``service_health`` frames (#2548).

        Both the transition (:meth:`_broadcast`) and death
        (:meth:`broadcast_death`) paths bump ``health_seq`` and fan out
        the same payload shape; keeping one emit means the #1175 contract
        fields can never diverge between them again.
        """
        state.health_seq += 1
        self.connections.notify_service_health(
            state.workspace_id,
            healthy=healthy,
            message=message,
            running=running,
            health_checked_at=state.health_checked_at,
            seq=state.health_seq,
        )

    def _broadcast(
        self,
        state: ContainerState,
        status: str,
        message: str | None = None,
    ) -> None:
        """Emit a ``service_health`` transition event to all connections.

        Fanned out via :meth:`WsState.notify_service_health` so the
        workspace list page learns about health transitions for
        auto-started services even when nobody is connected to the
        workspace's terminal session (#1015).  The failure *reason*
        rides along as ``health_message`` so operators can see *why*
        it's unhealthy without digging through logs (#1088).

        Also forwards the additive contract fields (#1175):
        ``running=True`` (this is a live-container frame), the last
        ``health_checked_at`` (#1175 item 3a), and a per-workspace
        ``seq`` (#1175 item 4) bumped on every emit so a reconnecting
        consumer can detect a missed transition.
        """
        self._emit(
            state,
            healthy=status == "healthy",
            message=message,
            running=True,
        )

    def broadcast_death(
        self, state: ContainerState, *, message: str | None = None
    ) -> None:
        """Emit the terminal ``service_health`` frame for a dying container.

        When a container dies the server emits
        ``container_status{running: false}`` and then *silence* on the
        ``service_health`` stream, because the health loop only polls
        ``registry.states`` and a dead container's state is removed.  A
        consumer watching ``service_health`` therefore believes the
        last-known status (possibly healthy) still holds while the
        container is gone (#1175 item 2).  This closes the hole by
        emitting one unambiguous terminal frame with ``running=False``
        and ``healthy=False`` *before* the state is dropped, so a single
        stream is a single source of truth.

        *message* (#2524) carries the classified death cause (e.g.
        "OOM-killed at 8g memory limit") when the death was detected by
        the crash monitor; expected stops leave it None.
        """
        self._emit(
            state,
            healthy=False,
            message=message,
            running=False,
        )

    async def run_health_loop(self) -> None:
        """Background loop: every interval, poll eligible workspaces.

        After each poll sweep, emits liveness heartbeats to connections
        that opted in (#1175 item 3b).  Emitting from *this* loop (rather
        than a standalone task) ties heartbeat presence to the health
        loop being alive -- if the loop stalls, the heartbeats stop.
        """
        while True:
            registry = self.app.state.container_registry
            await asyncio.sleep(registry.health_check_interval)
            for state in list(registry.states.values()):
                if not state.health_check:
                    continue
                if not self._setup_complete(state):
                    continue
                try:
                    await self._check_workspace(state)
                except Exception as e:  # pragma: no cover - defensive
                    logger.error(
                        "Health check error for workspace %s: %s",
                        state.workspace_id,
                        e,
                    )
            self._send_heartbeats()

    def _send_heartbeats(self) -> None:
        """Fan health heartbeats to opt-in connections."""
        self.connections.send_health_heartbeats()

    def start_health_loop(self) -> None:
        if self.health_task is None:
            self.health_task = asyncio.create_task(self.run_health_loop())
