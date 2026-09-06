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
  ``resource.disk.recovered``. Events fire on state transitions only,
  with a hysteresis band below the warn threshold, so usage hovering
  at a boundary produces one alert per episode, not one per poll.
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

from .notifier import notify_event

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
    usage: float, state: str, warn: float, critical: float, floor: float
) -> str:
    """The threshold state for one usage reading (percent used).

    Entering ``warn`` / ``critical`` is immediate. Recovery to ``ok``
    requires usage at or below *floor*; a reading between floor and
    warn keeps the current state, so a boundary-hovering usage level
    cannot flap events every poll — a degraded filesystem reports
    ``ok`` only after genuinely recovering, not from an intermediate
    dip. A reading between the thresholds eases ``critical`` to
    ``warn`` (a real improvement, reported as such).
    """
    if usage >= critical:
        return CRITICAL
    if usage >= warn:
        return WARN
    if usage <= floor:
        return OK
    return state


class ResourceWatchdog:
    """App-state owned detection loop for operational resource
    conditions (#3206). Follows the state-object ownership rule:
    constructed with ``app`` only, every setting read live off
    ``self.app.state.settings``.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        # st_dev -> threshold state (deduplicated by device).
        self._states: dict[int, str] = {}
        # audit table -> last-seen failure count / alerted-this-episode.
        self._audit_counts: dict[str, int] = {}
        self._audit_alerted: dict[str, bool] = {}
        # Paths warned about as unmeasurable (re-armed on recovery).
        self._warned_paths: set[str] = set()
        # Podman container-storage root, resolved once and cached
        # (podman info is too slow to run every poll).
        self._graph_root: str | None = None
        self._graph_root_failed = False

    def reconfigure(self, app) -> None:
        """Swap the app reference (SIGHUP reload). Clears the cached
        container-storage root so a changed podman configuration
        re-resolves."""
        self.app = app
        self._graph_root = None
        self._graph_root_failed = False

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
        """Recovery floor: usage at or below this reports recovered."""
        return self._warn_percent - RECOVERY_GAP_PERCENT

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
        kept — degradation that happened while disabled is detected on
        the next growth after re-enabling."""
        self._states.clear()
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
        ``disk_watchdog_paths`` entries, and the podman
        container-storage root (resolved once, cached). Deduplicated by
        device — several paths on one filesystem are one monitored
        filesystem, reported under the first path (the data directory
        wins over the storage root when they share a filesystem).
        """
        paths = [self.app.state.settings.data_dir, *self._extra_paths]
        root = await self.resolve_graph_root()
        if root:
            paths.append(root)
        filesystems: dict[int, tuple[int, str, float]] = {}
        for path in paths:
            entry = self._measure(path)
            if entry is not None and entry[0] not in filesystems:
                filesystems[entry[0]] = entry
        return list(filesystems.values())

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
        """One threshold evaluation; emits only on a state transition."""
        state = self._states.get(device, OK)
        new = classify(
            usage,
            state,
            self._warn_percent,
            self._critical_percent,
            self._floor,
        )
        self._states[device] = new
        if new != state:
            self.emit_disk_event(new, path, usage)

    def emit_disk_event(self, state: str, path: str, usage: float) -> None:
        """Notify + log one disk state transition (#3250 fan-out)."""
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
        notify_event(
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

        One ``podman info`` subprocess per process lifetime (re-tried
        after a settings reload — reconfigure clears the cache). An
        unresolvable root (no podman state, a remote machine whose
        storage path does not exist on this host, a failed query) is
        logged once and means the configured paths alone are
        monitored."""
        if self._graph_root or self._graph_root_failed:
            return self._graph_root
        podman = getattr(self.app.state, "podman", None)
        if podman is None:
            self._graph_root_failed = True
            return None
        root = await self._query_graph_root(podman)
        if root is None:
            self._graph_root_failed = True
            logger.info(
                "Resource watchdog: podman storage root unavailable; "
                "monitoring the configured paths only"
            )
            return None
        self._graph_root = root
        return root

    async def _query_graph_root(self, podman) -> str | None:
        """One ``podman info`` query for the storage root; None on any
        failure."""
        try:
            rc, out, _err = await podman.run(
                ["info", "--format", "{{.Store.GraphRoot}}"], check=False
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
        for table, count in self._audit_counters():
            self.step_audit(table, count)

    def _audit_counters(self):
        """``(table, count)`` for each audit-write-failure counter.
        Guarded for minimal app states (no registry / model wired) —
        an absent counter is simply not watched."""
        registry = getattr(self.app.state, "container_registry", None)
        if registry is not None:
            yield "container_events", registry.audit_write_failures
        model = getattr(self.app.state, "model", None)
        events = getattr(model, "audit_events", None)
        if events is not None:
            yield "audit_events", events.write_failures

    def step_audit(self, table: str, count: int) -> None:
        """Edge-detect counter growth. The first poll is a baseline;
        new failures in a later window emit one ``audit.failure`` (the
        notifier throttles per table, shared with the write sites'
        own events); continued growth stays quiet until a clean window
        re-arms detection."""
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
            "this window (%d total)",
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
