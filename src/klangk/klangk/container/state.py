"""Per-workspace container lifecycle state (#2542 split)."""

import time


class ContainerState:
    """Per-workspace container lifecycle state."""

    def __init__(self, workspace_id: str, container_id: str, app):
        self.workspace_id = workspace_id
        self.container_id = container_id
        self.app = app
        self.last_activity = time.time()
        self.idle_timeout: int | None = None
        self.idle_callbacks: list = []
        # Health-monitoring state (#1015).  Populated at container start
        # time so HealthMonitor can poll without a DB lookup per tick.
        self.health_status: str | None = None  # "healthy" | "unhealthy"
        self.health_checked_at: float | None = None  # time.time() of last
        # Short, human-readable reason for the last unhealthy result
        # (stderr/stdout tail or exception text).  None when healthy or
        # not yet checked.  Surfaced via the status API + service_health
        # event so an unhealthy workspace isn't a black box (#1088).
        self.health_message: str | None = None
        # Per-workspace monotonic counter carried on every service_health
        # frame so a reconnecting consumer can detect a missed transition
        # against the connect-time snapshot (#1175 item 4).  Increments on
        # each emitted frame (transition and death); resets when the state
        # is recreated (container restart) -- the snapshot reconciles.
        self.health_seq: int = 0
        self.health_check: str | None = None  # shell command, None = disabled
        self.owner_id: str | None = None
        self.setup_state: str | None = None
        # Home layout (#2169 chunk 2, #2720): the health check resolves
        # the owner's per-handle HOME only when this is True; False means
        # the shared /home/klangk. Default True (the pre-#2720 layout and
        # the safe fallback if a start path forgets to set it).
        self.per_handle_home: bool = True
        # Anchor for the startup grace window
        # (HEALTH_CHECK_STARTUP_GRACE_SECONDS): the moment the monitored
        # service began starting.  Defaults to now (container-state
        # creation) so health-checked workspaces with no service command
        # still get a grace window; reset to "now" by
        # ``mark_service_started`` when the service command actually
        # fires, which is the precise point the service begins booting.
        self.service_started_at: float = time.time()

    def record_activity(self) -> None:
        self.last_activity = time.time()

    def mark_service_started(self) -> None:
        """Record that the service command just fired.

        Resets the startup-grace anchor to now.  Called from
        ``terminal.ensure_service_session`` right after it launches the
        service command, so the grace window is measured from the real
        start of the service rather than from the (earlier) container
        creation -- important for the per-connection fire path where a
        freshly-created workspace launches its service command on first
        ``terminal_start``, possibly long after the container started.
        """
        self.service_started_at = time.time()

    def get_idle_timeout(self) -> int:
        if self.idle_timeout is not None:
            return self.idle_timeout
        return self.app.state.container_registry.idle_timeout_seconds
