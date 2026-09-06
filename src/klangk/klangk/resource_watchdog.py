"""Operational resource detection: disk capacity and audit degradation
(#3206).

The STIG detection layer on top of #3250's delivery layer. Two
surfaces, one poll loop:

- **Disk capacity** (SV-222483 rule 96, SV-222668 rule 280) — every
  poll, ``statvfs`` the filesystems holding the data directory (the
  audit records storage), the podman container-storage root, and any
  operator-configured extra paths, deduplicated by device. Usage
  crossing the warn / critical thresholds emits
  ``resource.disk.warn`` / ``resource.disk.critical`` for the admin
  notifier; falling back below the recovery floor emits
  ``resource.disk.recovered``. Events fire on state transitions,
  with hysteresis bands below both thresholds, so usage hovering at
  a boundary produces one alert per episode, not one per poll — an
  undelivered dispatch is retried on later polls (while the event
  could ever dispatch: with no channel configured or the event off
  the allowlist there is nothing to wait for), a still-degraded
  filesystem refreshes its alert once per throttle window, so an
  episode edge is late in the worst case, never lost.
- **Audit pipeline degradation** (SV-222484 rule 97) — the
  audit-write-failure counters the write sites bump
  (``container_events`` on the container registry, ``audit_events``
  on the model — the fail-closed refusals pass the same counters) are
  watched for growth. A poll window with new failures emits one
  ``audit.failure`` (throttled per table by the notifier) and logs
  loudly; a clean window re-arms detection.

Detection is best-effort and loud: a failed measurement is logged
(once per condition, re-armed on recovery) and never blocks or kills
the loop — a watchdog that dies silently would disable the STIG
alerting without a trace. All settings are read live off
``app.state.settings`` every poll, so a SIGHUP reload (#1587)
re-arms the loop without a restart.
"""

import asyncio
import logging
import os
import time

from .notifier import THROTTLE_SECONDS, notify_event

logger = logging.getLogger(__name__)

# Threshold states for one monitored filesystem.
OK = "ok"
WARN = "warn"
CRITICAL = "critical"

# The notification event for each *entered* degraded state; entering
# OK from either is the recovered event (emit_disk_event).
EVENT_BY_STATE = {
    WARN: "resource.disk.warn",
    CRITICAL: "resource.disk.critical",
}
RECOVERED_EVENT = "resource.disk.recovered"

# Usage must fall this far below the warn threshold (percentage
# points) before a degraded filesystem reports recovered — the
# hysteresis band that keeps usage hovering at a boundary from
# flapping events every poll.
RECOVERY_GAP_PERCENT = 5.0

# A persisting warn/critical state re-notifies at most this often — the
# same window the notifier throttles delivery to, so a still-degraded
# filesystem refreshes its alert once per window instead of relying
# solely on the edge transition (whose single dispatch the throttle
# can swallow). The widest degraded-event window: a refresh must not
# fire before the notifier's throttle would admit it.
REFRESH_SECONDS = max(
    THROTTLE_SECONDS[name]
    for name in ("resource.disk.warn", "resource.disk.critical")
)

# After a failed storage-root query, wait this long before retrying
# (one ``podman info`` subprocess per cooldown, not per poll).
GRAPH_ROOT_RETRY_SECONDS = 300.0

# Floor for the poll interval so a misconfigured tiny value cannot
# spin the loop hot (mirrors the eviction loop's floor).
MIN_POLL_INTERVAL_SECONDS = 1.0


def usage_percent(path: str) -> float:
    """Percent of the filesystem holding *path* that is used (0..100).

    Availability is ``f_bavail`` — the space the unprivileged user can
    actually allocate, the number ``df`` reports; it reaches zero
    before the kernel's reserved blocks do. Raises ``OSError`` /
    ``ValueError`` when the path cannot be measured; the watchdog
    skips that path (never alerts blind).
    """
    vfs = os.statvfs(path)
    total = vfs.f_blocks * vfs.f_frsize
    if total <= 0:
        raise ValueError(f"statvfs({path!r}) reports no capacity")
    available = vfs.f_bavail * vfs.f_frsize
    return (total - available) / total * 100.0


def classify(
    usage: float,
    state: str,
    warn: float,
    critical: float,
    floor: float,
    critical_floor: float,
) -> str:
    """The threshold state for one usage reading (percent used).

    Entering ``warn`` / ``critical`` is immediate. Recovery has
    hysteresis on both edges: a ``critical`` filesystem eases to
    ``warn`` only at or below *critical_floor*, and any degraded
    state reports ``ok`` only at or below *floor* (warn's band). A
    reading inside either band keeps the current state, so usage
    hovering at either boundary cannot flap events every poll — a
    degraded filesystem reports ``ok`` only after genuinely
    recovering, not from an intermediate dip.
    """
    if usage >= critical:
        return CRITICAL
    if state == CRITICAL and usage > critical_floor:
        return CRITICAL
    return classify_below_critical(usage, state, warn, floor)


def classify_below_critical(
    usage: float, state: str, warn: float, floor: float
) -> str:
    """The warn-side classification (usage below the critical
    threshold): warn at or above *warn*, ok at or below *floor*, the
    current state inside the hysteresis band."""
    if usage >= warn:
        return WARN
    if usage <= floor:
        return OK
    return state


def dedup_filesystems(
    entries: list[tuple[int, str, float]],
) -> list[tuple[int, str, float]]:
    """Keep the first entry per device — several paths on one
    filesystem are one monitored filesystem."""
    filesystems: dict[int, tuple[int, str, float]] = {}
    for entry in entries:
        if entry[0] not in filesystems:
            filesystems[entry[0]] = entry
    return list(filesystems.values())


def audit_failure_counts(app) -> dict[str, int]:
    """``{table: count}`` for every audit-write-failure counter on the
    app state (#3206). Guarded for minimal app states (no registry /
    model wired) — an absent subsystem's counter is simply absent. The
    ``/audit`` status surface reports the same numbers."""
    counts: dict[str, int] = {}
    registry = getattr(app.state, "container_registry", None)
    if registry is not None:
        counts["container_events"] = registry.audit_write_failures
    model = getattr(app.state, "model", None)
    events = getattr(model, "audit_events", None)
    if events is not None:
        counts["audit_events"] = events.write_failures
    return counts


class ResourceWatchdog:
    """App-state owned detection loop for operational resource
    conditions (#3206). Follows the state-object ownership rule:
    constructed with ``app`` only, every setting read live off
    ``self.app.state.settings``.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        # st_dev -> threshold state (deduplicated by device) / the
        # monotonic clock of the last event dispatch (transitions and
        # persistence refreshes both stamp it) / the state of a
        # transition whose dispatch the notifier throttled away,
        # retried on later polls.
        self._states: dict[int, str] = {}
        self._emitted_at: dict[int, float] = {}
        self._pending: dict[int, str] = {}
        # audit table -> last-seen failure count / alerted-this-episode.
        self._audit_counts: dict[str, int] = {}
        self._audit_alerted: dict[str, bool] = {}
        # Paths warned about as unmeasurable (re-armed on recovery).
        self._warned_paths: set[str] = set()
        # Podman container-storage root, resolved once and cached
        # (podman info is too slow to run every poll); a failed query
        # retries after a cooldown.
        self._graph_root: str | None = None
        self._graph_root_retry_at = 0.0

    def reconfigure(self, app) -> None:
        """Swap the app reference (SIGHUP reload). The cached
        container-storage root and its cooldown always reset (a
        changed podman configuration re-resolves immediately), and
        the unmeasurable-path warnings re-arm. The disk states reset
        only when a threshold actually changed — an unrelated reload
        must not re-alert already-degraded filesystems (the notifier's
        throttle clocks reset on reload too, so nothing else would
        suppress the re-alert)."""
        thresholds_changed = self._thresholds_changed(app)
        self.app = app
        self._graph_root = None
        self._graph_root_retry_at = 0.0
        self._warned_paths.clear()
        if thresholds_changed:
            self._states.clear()
            self._emitted_at.clear()
            self._pending.clear()

    def _thresholds_changed(self, new_app) -> bool:
        """Whether the reload moved a disk threshold (the only change
        that needs the remembered states re-evaluated from ``ok``)."""
        old = self.app.state.settings
        new = new_app.state.settings
        return (
            old.disk_watchdog_warn_percent != new.disk_watchdog_warn_percent
            or old.disk_watchdog_critical_percent
            != new.disk_watchdog_critical_percent
        )

    # --- settings (read live) ---

    @property
    def _enabled(self) -> bool:
        return self.app.state.settings.disk_watchdog_enabled

    @property
    def _warn_percent(self) -> float:
        return self.app.state.settings.disk_watchdog_warn_percent

    @property
    def _critical_percent(self) -> float:
        return self.app.state.settings.disk_watchdog_critical_percent

    @property
    def _floor(self) -> float:
        """Recovery floor: usage at or below this reports recovered.
        The validator guarantees warn >= RECOVERY_GAP_PERCENT, so this
        never goes below 0."""
        return self._warn_percent - RECOVERY_GAP_PERCENT

    @property
    def _critical_floor(self) -> float:
        """The critical→warn easing floor (same gap; the validator
        guarantees critical >= warn >= the gap, so never below 0)."""
        return self._critical_percent - RECOVERY_GAP_PERCENT

    @property
    def _poll_interval(self) -> float:
        return max(
            self.app.state.settings.disk_watchdog_poll_interval,
            MIN_POLL_INTERVAL_SECONDS,
        )

    @property
    def _extra_paths(self) -> list[str]:
        return self.app.state.settings.disk_watchdog_paths or []

    # --- loop lifecycle (the eviction-loop pattern) ---

    def start(self) -> None:
        """Start the detection loop (idempotent). Sweeps once
        immediately — a host restarted *because* its disk filled should
        alert on the first poll, not one interval later."""
        if self._task is None:
            settings = self.app.state.settings
            logger.info(
                "Resource watchdog armed: disk warn %.1f%%, critical "
                "%.1f%% (recovery below %.1f%%), interval %.1fs "
                "(effective floor %.1fs), enabled=%s",
                settings.disk_watchdog_warn_percent,
                settings.disk_watchdog_critical_percent,
                settings.disk_watchdog_warn_percent - RECOVERY_GAP_PERCENT,
                settings.disk_watchdog_poll_interval,
                MIN_POLL_INTERVAL_SECONDS,
                settings.disk_watchdog_enabled,
            )
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the detection loop.

        Tolerates a loop that already died: a dead task's exception
        must never re-raise here and break the lifespan shutdown
        cascade that runs after this."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Resource-watchdog loop had died earlier; suppressing "
                    "its exception on shutdown",
                    exc_info=True,
                )
            self._task = None

    async def run(self) -> None:
        """Poll loop: one guarded cycle, then sleep. Settings are read
        live after every sleep (a SIGHUP reload swaps the settings
        object mid-sleep, #1587). A cycle that escapes sweep's own
        guards is logged and retried an interval later — the loop
        itself never dies, and the sleep always happens, so a broken
        cycle cannot spin hot."""
        while True:
            await self.guarded_cycle()
            await asyncio.sleep(self._poll_interval)

    async def guarded_cycle(self) -> None:
        """One enabled/disabled cycle with the loop-survival guard."""
        try:
            if self._enabled:
                await self.sweep()
            else:
                self._reset_states()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Resource-watchdog cycle failed (skipped)", exc_info=True
            )

    def _reset_states(self) -> None:
        """Clear remembered conditions while disabled, so re-enabling
        evaluates fresh: a disk that filled while disabled alerts on
        the first poll after re-enabling. Audit counter baselines are
        kept — failures that accumulated while disabled are detected
        on the first poll after re-enabling (the baseline predates
        them, so the growth is visible immediately)."""
        self._states.clear()
        self._emitted_at.clear()
        self._pending.clear()
        self._audit_alerted.clear()

    async def sweep(self) -> None:
        """One poll: disk thresholds, then audit counters. Each
        surface's failure is logged and skipped — the other surface
        still runs, and the loop never dies (#3206: detection failure
        is loud but never blocks the monitored operation)."""
        try:
            await self.check_disk()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Resource-watchdog disk check failed (skipped)",
                exc_info=True,
            )
        try:
            self.check_audit()
        except Exception:
            logger.warning(
                "Resource-watchdog audit check failed (skipped)",
                exc_info=True,
            )

    # --- disk capacity surface ---

    async def check_disk(self) -> None:
        """One disk-capacity pass over every monitored filesystem."""
        for device, path, usage in await self.monitored_filesystems():
            self.step_filesystem(device, path, usage)

    async def monitored_filesystems(self) -> list[tuple[int, str, float]]:
        """``(device, path, usage%)`` for every monitored filesystem.

        Monitored: the data directory (the audit records storage), any
        ``disk_watchdog_paths`` entries, and the podman container-storage
        root. The configured paths are measured first (synchronous
        statvfs, before the storage-root query's await), the root last;
        the evaluation still runs over the full set, so the first sweep
        waits out the root query (bounded by its short timeout). Deduplicated by device — several paths on one
        filesystem are one monitored filesystem, reported under the
        first path (the data directory wins over the storage root when
        they share a filesystem).
        """
        paths = [self.app.state.settings.data_dir, *self._extra_paths]
        entries = [e for e in map(self._measure, paths) if e is not None]
        root = await self.resolve_graph_root()
        if root:
            entry = self._measure(root)
            if entry is not None:
                entries.append(entry)
        return dedup_filesystems(entries)

    def _measure(self, path: str) -> tuple[int, str, float] | None:
        """``(device, path, usage%)`` for one path; None when the path
        cannot be measured (warned once per path, re-armed when a
        later poll measures it again)."""
        try:
            device = os.stat(path).st_dev
            usage = usage_percent(path)
        except (OSError, ValueError) as e:
            if path not in self._warned_paths:
                self._warned_paths.add(path)
                logger.warning(
                    "Resource watchdog cannot measure disk usage of %s "
                    "(%s); skipping it until it is measurable",
                    path,
                    e,
                )
            return None
        self._warned_paths.discard(path)
        return device, path, usage

    def step_filesystem(self, device: int, path: str, usage: float) -> None:
        """One threshold evaluation; emits on a state transition, and
        — while a degraded state persists — once per refresh window
        (see :meth:`refresh_due`). A dispatch the notifier throttled
        away is retried on later polls (:meth:`retry_pending`)."""
        state = self._states.get(device, OK)
        new = classify(
            usage,
            state,
            self._warn_percent,
            self._critical_percent,
            self._floor,
            self._critical_floor,
        )
        self._states[device] = new
        if new != state or self.refresh_due(device, new):
            self._emitted_at[device] = time.monotonic()
            self._record_dispatch(
                device, new, self.emit_disk_event(new, path, usage)
            )
            return
        self.retry_pending(device, path, usage)

    def _record_dispatch(
        self, device: int, state: str, dispatched: bool
    ) -> None:
        """Track an undelivered transition for retry: episode edges
        must land — a swallowed warn entry is refreshed by persistence,
        but a swallowed recovery would otherwise never be re-sent. A
        dispatch that can never succeed (see :meth:`_event_dispatchable`)
        is treated as done — retrying it every poll would only log."""
        if dispatched or not self._event_dispatchable(
            EVENT_BY_STATE.get(state, RECOVERED_EVENT)
        ):
            self._pending.pop(device, None)
        else:
            self._pending[device] = state

    def _event_dispatchable(self, event: str) -> bool:
        """True when a retry could ever land: the notifier is wired,
        the event is allowlisted, and a channel is configured.

        This deliberately does NOT consult the throttle — waiting out
        the throttle window is exactly what the retry exists for. With
        no channels configured (the default deployment) or the event
        removed from ``admin_notify_events``, an undelivered dispatch
        is an operator decision, not a transient condition; the retry
        loop must not re-log it every poll forever.
        """
        notifier = getattr(self.app.state, "notifier", None)
        if notifier is None:
            return False
        try:
            return (
                event in notifier.notify_events()
                and notifier.channels_configured()
            )
        except Exception:  # noqa: BLE001 — best-effort probe
            return False

    def retry_pending(self, device: int, path: str, usage: float) -> None:
        """Re-dispatch a transition the notifier throttled away.

        The throttle admits at most one delivery per event per window,
        so an episode edge that landed inside another episode's window
        is dropped with no retry of its own — until this retries it
        into an expired window (worst case: one window late). Only the
        still-current state is retried; a newer transition has already
        rewritten the pending entry, and an event that could never
        dispatch (allowlist/channels) is dropped rather than retried.
        """
        pending = self._pending.get(device)
        if pending is None:
            return
        event = EVENT_BY_STATE.get(pending, RECOVERED_EVENT)
        delivered = self.emit_disk_event(pending, path, usage)
        if delivered or not self._event_dispatchable(event):
            self._pending.pop(device, None)
        if delivered:
            self._emitted_at[device] = time.monotonic()

    def refresh_due(self, device: int, state: str) -> bool:
        """True when a persisting degraded state should re-notify.

        Transitions are edge-triggered and the notifier throttles
        delivery with a stamp-at-dispatch window — a transition whose
        dispatch fell inside another episode's window is retried
        (:meth:`retry_pending`), and a still-degraded filesystem
        refreshes its alert once per window — the worst case is a late
        alert, never a permanently lost one.
        """
        if state == OK:
            return False
        last = self._emitted_at.get(device, 0.0)
        return time.monotonic() - last >= REFRESH_SECONDS

    def emit_disk_event(self, state: str, path: str, usage: float) -> bool:
        """Notify + log one disk event (transition, refresh, or retry).
        Returns whether the notification dispatched (#3206 retry
        semantics)."""
        event = EVENT_BY_STATE.get(state, RECOVERED_EVENT)
        level = logging.INFO if state == OK else logging.WARNING
        logger.log(
            level,
            "Disk usage %.1f%% on %s: %s (warn %.1f%%, critical %.1f%%)",
            usage,
            path,
            "recovered" if state == OK else state,
            self._warn_percent,
            self._critical_percent,
        )
        return notify_event(
            self.app,
            event,
            detail={
                "path": path,
                "usage_percent": round(usage, 1),
                "state": state,
                "warn_percent": self._warn_percent,
                "critical_percent": self._critical_percent,
            },
        )

    async def resolve_graph_root(self) -> str | None:
        """The podman container-storage root, resolved once and cached.

        One ``podman info`` subprocess per success (cached for the
        process lifetime; re-resolved after a settings reload —
        reconfigure clears the cache). A failed query (no podman
        state, a remote machine whose storage path does not exist on
        this host, a transient podman startup race) starts a cooldown:
        the configured paths alone are monitored until the next retry
        one :data:`GRAPH_ROOT_RETRY_SECONDS` later — never a permanent
        degradation from one transient failure.
        """
        if self._graph_root:
            return self._graph_root
        if time.monotonic() < self._graph_root_retry_at:
            return None
        podman = getattr(self.app.state, "podman", None)
        if podman is None:
            self._defer_graph_root_retry()
            return None
        root = await self._query_graph_root(podman)
        if root is None:
            self._defer_graph_root_retry()
            logger.info(
                "Resource watchdog: podman storage root unavailable; "
                "monitoring the configured paths only (retrying in "
                "%ds)",
                int(GRAPH_ROOT_RETRY_SECONDS),
            )
            return None
        self._graph_root = root
        return root

    def _defer_graph_root_retry(self) -> None:
        """Schedule the next storage-root query one cooldown out."""
        self._graph_root_retry_at = time.monotonic() + GRAPH_ROOT_RETRY_SECONDS

    async def _query_graph_root(self, podman) -> str | None:
        """One ``podman info`` query for the storage root; None on any
        failure. Short timeout — a wedged podman socket must not delay
        the first sweep's data-directory evaluation by the default
        30s (a failure merely starts the retry cooldown)."""
        try:
            rc, out, _err = await podman.run(
                ["info", "--format", "{{.Store.GraphRoot}}"],
                check=False,
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001 — best-effort resolution
            return None
        if rc != 0:
            return None
        return out.strip() or None

    # --- audit pipeline surface ---

    def check_audit(self) -> None:
        """One audit-pipeline pass: growth in either write-failure
        counter is a detected condition."""
        for table, count in audit_failure_counts(self.app).items():
            self.step_audit(table, count)

    def step_audit(self, table: str, count: int) -> None:
        """Edge-detect counter growth. The first poll is a baseline;
        failures new since the last check emit one ``audit.failure``
        (the notifier throttles per table, shared with the write
        sites' own events); continued growth stays quiet until a
        clean window re-arms detection."""
        previous = self._audit_counts.get(table)
        self._audit_counts[table] = count
        grew = previous is not None and count > previous
        if not grew:
            self._audit_alerted[table] = False
            return
        if self._audit_alerted.get(table, False):
            return
        self._audit_alerted[table] = True
        logger.warning(
            "Audit pipeline degradation: %d new %s write failure(s) "
            "since the last check (%d total)",
            count - previous,
            table,
            count,
        )
        notify_event(
            self.app,
            "audit.failure",
            detail={
                "table": table,
                "failures": count - previous,
                "total": count,
                "source": "watchdog",
            },
        )
