"""Operational resource detection: disk capacity, host memory and
CPU pressure, and audit degradation (#3206, #3309).

The STIG detection layer on top of #3250's delivery layer. Four
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
- **Host memory** (#3309) — every poll, the memory utilization of
  the machine where containers actually run, as ``100 × (1 −
  availability)``. On a Linux host that is ``MemAvailable``/
  ``MemTotal`` from ``/proc/meminfo`` (the more pressed of meminfo and
  the cgroup limit when klangkd itself runs memory-capped, shared
  with the eviction subsystem #2526). On macOS containers run inside
  the podman machine Linux VM, so the VM's own ``/proc/meminfo`` is
  read via ``podman machine ssh`` — the Mac host's numbers are the
  wrong machine. Crossing ``memory_watchdog_warn_percent`` /
  ``memory_watchdog_critical_percent`` emits ``resource.memory.warn``
  / ``resource.memory.critical``; the same hysteresis and refresh
  semantics as disk.
- **CPU pressure** (#3309) — PSI ``some avg60`` from
  ``/proc/pressure/cpu``: the share of time at least one task was
  stalled on CPU over the last minute, already a 60-second average, so
  a single-poll threshold crossing is signal, not a scheduler spike.
  On macOS the file is read inside the podman machine VM the same
  way. A kernel, container runtime, or VM without PSI disables the
  check with one logged warning (re-armed if the file appears);
  events ``resource.cpu.warn`` / ``resource.cpu.critical`` /
  ``resource.cpu.recovered``.
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
import math
import os
import platform
import time
from typing import Callable, NamedTuple

from .notifier import THROTTLE_SECONDS, notify_event

# The eviction subsystem's measurement helpers (meminfo parsing,
# availability, the cgroup-aware fraction) are imported *inside*
# :meth:`ResourceWatchdog._measure_memory_fraction` (the
# allow-deferred-import pattern, see llm_router): a module-level
# import cycles — klangk.container.__init__ pulls the registry, which
# pulls settings, which imports this module for RECOVERY_GAP_PERCENT.

logger = logging.getLogger(__name__)

# Threshold states for one monitored metric.
OK = "ok"
WARN = "warn"
CRITICAL = "critical"


class ThresholdEvents(NamedTuple):
    """The notification events for one threshold-watched metric: the
    event for entering ``warn`` / ``critical``, and the recovered
    event for entering ``ok`` from either."""

    warn: str
    critical: str
    recovered: str

    def by_state(self, state: str) -> str:
        """The event for one threshold state (``ok`` → recovered)."""
        if state == CRITICAL:
            return self.critical
        if state == WARN:
            return self.warn
        return self.recovered


DISK_EVENTS = ThresholdEvents(
    warn="resource.disk.warn",
    critical="resource.disk.critical",
    recovered="resource.disk.recovered",
)
MEMORY_EVENTS = ThresholdEvents(
    warn="resource.memory.warn",
    critical="resource.memory.critical",
    recovered="resource.memory.recovered",
)
CPU_EVENTS = ThresholdEvents(
    warn="resource.cpu.warn",
    critical="resource.cpu.critical",
    recovered="resource.cpu.recovered",
)

# The threshold-state dicts hold one entry per monitored metric —
# disk entries keyed by the filesystem's ``st_dev``, the two
# host-level metrics by these fixed keys.
MEMORY_KEY = "memory"
CPU_KEY = "cpu-psi"

# Usage must fall this far below the warn threshold (percentage
# points) before a degraded filesystem reports recovered — the
# hysteresis band that keeps usage hovering at a boundary from
# flapping events every poll.
RECOVERY_GAP_PERCENT = 5.0

# A persisting warn/critical state re-notifies at most this often — the
# same window the notifier throttles delivery to, so a still-degraded
# metric refreshes its alert once per window instead of relying
# solely on the edge transition (whose single dispatch the throttle
# can swallow). The widest degraded-event window across every
# threshold-watched metric: a refresh must not fire before the
# notifier's throttle would admit it.
REFRESH_SECONDS = max(
    THROTTLE_SECONDS[name]
    for name in (
        "resource.disk.warn",
        "resource.disk.critical",
        "resource.memory.warn",
        "resource.memory.critical",
        "resource.cpu.warn",
        "resource.cpu.critical",
    )
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


def psi_field(line: str, name: str) -> float | None:
    """One ``name=value`` field of a PSI line, or None when the line
    does not carry it (a missing or non-finite ``value`` counts as
    absent — a truncated or ``nan`` reading must not read as zero
    pressure; a non-finite value would poison every threshold
    comparison)."""
    for token in line.split():
        if token.startswith(f"{name}="):
            try:
                value = float(token.split("=", 1)[1])
            except ValueError:
                return None
            return value if math.isfinite(value) else None
    return None


def read_local_text(path: str) -> str:
    """One local file's content — the Linux-host ``/proc`` read
    (a named helper so tests can intercept the path)."""
    with open(path) as fh:
        return fh.read()


def parse_cpu_psi(text: str) -> float:
    """The CPU ``some avg60`` PSI percentage (0..100) from
    ``/proc/pressure/cpu`` **content**.

    ``some`` — at least one task stalled — is the operator-relevant
    line (``full`` counts only periods where *every* runnable task
    stalled, which CPU PSI does not even report). ``avg60`` — a
    60-second average — is the signal: short spikes average out, so a
    threshold crossing is sustained pressure, not a scheduler burst.
    Raises ``ValueError`` when the text carries no parseable ``some``
    line — callers treat unmeasurable as "skip with one warning",
    never as zero pressure.
    """
    for line in text.splitlines():
        if line.startswith("some "):
            value = psi_field(line, "avg60")
            if value is not None:
                return value
    raise ValueError("no parseable `some avg60` PSI line")


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
        # metric key (st_dev for disk; MEMORY_KEY / CPU_KEY for the
        # host metrics) -> threshold state / the monotonic clock of
        # the last event dispatch (transitions and persistence
        # refreshes both stamp it) / the state of a transition whose
        # dispatch the notifier throttled away, retried on later polls.
        self._states: dict[int | str, str] = {}
        self._emitted_at: dict[int | str, float] = {}
        self._pending: dict[int | str, str] = {}
        # audit table -> last-seen failure count / alerted-this-episode.
        self._audit_counts: dict[str, int] = {}
        self._audit_alerted: dict[str, bool] = {}
        # Conditions warned about as unmeasurable (disk paths and the
        # memory/CPU metrics alike; re-armed on recovery).
        self._unmeasurable: set[str] = set()
        # Podman container-storage root, resolved once and cached
        # (podman info is too slow to run every poll); a failed query
        # retries after a cooldown.
        self._graph_root: str | None = None
        self._graph_root_retry_at = 0.0
        # Last-seen thresholds (instance snapshot): reconfigure
        # compares against these because by the time it runs, the
        # app's settings have already been swapped in place
        # (apply_reloaded_settings assigns app.state.settings before
        # calling reconfigure) — reading "old" off the app would
        # compare new-against-new and never fire.
        self._thresholds = self._thresholds_of(app)

    def reconfigure(self, app) -> None:
        """Swap the app reference (SIGHUP reload). The cached
        container-storage root and its cooldown always reset (a
        changed podman configuration re-resolves immediately), and
        the unmeasurable-condition warnings re-arm. The threshold
        states reset only when a threshold actually changed — an
        unrelated reload must not re-alert already-degraded metrics
        (the notifier's throttle clocks reset on reload too, so
        nothing else would suppress the re-alert)."""
        thresholds_changed = self._thresholds != self._thresholds_of(app)
        self.app = app
        self._thresholds = self._thresholds_of(app)
        self._graph_root = None
        self._graph_root_retry_at = 0.0
        self._unmeasurable.clear()
        if thresholds_changed:
            self._states.clear()
            self._emitted_at.clear()
            self._pending.clear()

    @staticmethod
    def _thresholds_of(app) -> tuple[tuple[float, float], ...]:
        """The (warn, critical) pair of every threshold-watched metric
        off an app's live settings — the snapshot reconfigure compares
        against (see ``__init__``)."""
        settings = app.state.settings
        return (
            (
                settings.disk_watchdog_warn_percent,
                settings.disk_watchdog_critical_percent,
            ),
            (
                settings.memory_watchdog_warn_percent,
                settings.memory_watchdog_critical_percent,
            ),
            (
                settings.cpu_watchdog_warn_percent,
                settings.cpu_watchdog_critical_percent,
            ),
        )

    # --- settings (read live) ---

    @property
    def _enabled(self) -> bool:
        return self.app.state.settings.resource_watchdog_enabled

    @property
    def _warn_percent(self) -> float:
        return self.app.state.settings.disk_watchdog_warn_percent

    @property
    def _critical_percent(self) -> float:
        return self.app.state.settings.disk_watchdog_critical_percent

    @property
    def _memory_enabled(self) -> bool:
        return self.app.state.settings.memory_watchdog_enabled

    @property
    def _memory_warn_percent(self) -> float:
        return self.app.state.settings.memory_watchdog_warn_percent

    @property
    def _memory_critical_percent(self) -> float:
        return self.app.state.settings.memory_watchdog_critical_percent

    @property
    def _cpu_enabled(self) -> bool:
        return self.app.state.settings.cpu_watchdog_enabled

    @property
    def _cpu_warn_percent(self) -> float:
        return self.app.state.settings.cpu_watchdog_warn_percent

    @property
    def _cpu_critical_percent(self) -> float:
        return self.app.state.settings.cpu_watchdog_critical_percent

    @property
    def _poll_interval(self) -> float:
        return max(
            self.app.state.settings.resource_watchdog_poll_interval,
            MIN_POLL_INTERVAL_SECONDS,
        )

    @property
    def _extra_paths(self) -> list[str]:
        return self.app.state.settings.disk_watchdog_paths or []

    # --- loop lifecycle (the eviction-loop pattern) ---

    def start(self) -> None:
        """Start the detection loop (idempotent). Sweeps once
        immediately — a host restarted *because* its resources filled
        should alert on the first poll, not one interval later."""
        if self._task is None:
            settings = self.app.state.settings
            logger.info(
                "Resource watchdog armed: disk warn %.1f%%/critical "
                "%.1f%%, memory warn %.1f%%/critical %.1f%%, CPU "
                "pressure warn %.1f%%/critical %.1f%% (recovery %d "
                "points below each threshold), interval %.1fs (floor "
                "%.1fs), enabled=%s",
                settings.disk_watchdog_warn_percent,
                settings.disk_watchdog_critical_percent,
                settings.memory_watchdog_warn_percent,
                settings.memory_watchdog_critical_percent,
                settings.cpu_watchdog_warn_percent,
                settings.cpu_watchdog_critical_percent,
                int(RECOVERY_GAP_PERCENT),
                settings.resource_watchdog_poll_interval,
                MIN_POLL_INTERVAL_SECONDS,
                settings.resource_watchdog_enabled,
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
        """One poll: disk thresholds, memory, CPU pressure, then audit
        counters. Each surface's failure is logged and skipped — the
        others still run, and the loop never dies (#3206: detection
        failure is loud but never blocks the monitored operation)."""
        for label, check in self._surfaces():
            try:
                await check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Resource-watchdog %s check failed (skipped)",
                    label,
                    exc_info=True,
                )

    def _surfaces(self) -> list[tuple[str, Callable[[], object]]]:
        """The sweep's checks, in run order."""
        return [
            ("disk", self.check_disk),
            ("memory", self.check_memory),
            ("cpu", self.check_cpu),
            ("audit", self._check_audit_surface),
        ]

    async def _check_audit_surface(self) -> None:
        """Awaitable adapter: the audit check is synchronous."""
        self.check_audit()

    # --- disk capacity surface ---

    async def check_disk(self) -> None:
        """One disk-capacity pass over every monitored filesystem."""
        for device, path, usage in await self.monitored_filesystems():
            self.step_filesystem(device, path, usage)

    async def monitored_filesystems(self) -> list[tuple[int, str, float]]:
        """``(device, path, usage%)`` for every monitored filesystem.

        Monitored: the data directory (the audit records storage),
        any ``disk_watchdog_paths`` entries, and the podman
        container-storage root. The configured paths are measured
        first (synchronous statvfs, before the storage-root query's
        await), the root last; the evaluation still runs over the full
        set, so the first sweep waits out the root query (bounded by
        its short timeout). Deduplicated by device — several paths on
        one filesystem are one monitored filesystem, reported under
        the first of them in that order (data directory, extras,
        storage root).
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
            self._note_unmeasurable(f"disk usage of {path}", e)
            return None
        self._rearm_unmeasurable(f"disk usage of {path}")
        return device, path, usage

    def _note_unmeasurable(self, what: str, error: Exception) -> None:
        """Warn once per condition that cannot be measured (re-armed
        on recovery) — detection must be loud about its own blind
        spots but not spam them every poll."""
        key = f"unmeasurable:{what}"
        if key not in self._unmeasurable:
            self._unmeasurable.add(key)
            logger.warning(
                "Resource watchdog cannot measure %s (%s); skipping it "
                "until it is measurable",
                what,
                error,
            )

    def _rearm_unmeasurable(self, what: str) -> None:
        """Re-arm a condition's unmeasurable warning after a
        successful measurement."""
        self._unmeasurable.discard(f"unmeasurable:{what}")

    def step_filesystem(self, device: int, path: str, usage: float) -> None:
        """One disk threshold evaluation (see
        :meth:`_step_threshold`)."""
        self._step_threshold(
            device,
            usage,
            self._warn_percent,
            self._critical_percent,
            DISK_EVENTS,
            lambda state, u, retry: self.emit_disk_event(
                state, path, u, retry=retry
            ),
        )

    def _step_threshold(
        self,
        key: int | str,
        usage: float,
        warn: float,
        critical: float,
        events: ThresholdEvents,
        emit: Callable[[str, float, bool], bool],
    ) -> None:
        """One threshold evaluation for any metric; emits on a state
        transition, and — while a degraded state persists — once per
        refresh window (see :meth:`refresh_due`). A dispatch the
        notifier throttled away is retried on later polls
        (:meth:`retry_pending`). The recovery floors sit
        :data:`RECOVERY_GAP_PERCENT` points below the thresholds — the
        settings validator guarantees the band fits."""
        state = self._states.get(key, OK)
        new = classify(
            usage,
            state,
            warn,
            critical,
            warn - RECOVERY_GAP_PERCENT,
            critical - RECOVERY_GAP_PERCENT,
        )
        self._states[key] = new
        if new != state or self.refresh_due(key, new):
            self._emitted_at[key] = time.monotonic()
            self._record_dispatch(key, new, emit(new, usage, False), events)
            return
        self.retry_pending(key, usage, events, emit)

    def _record_dispatch(
        self,
        key: int | str,
        state: str,
        dispatched: bool,
        events: ThresholdEvents,
    ) -> None:
        """Track an undelivered transition for retry: episode edges
        must land — a swallowed warn entry is refreshed by persistence,
        but a swallowed recovery would otherwise never be re-sent. A
        dispatch that can never succeed (see :meth:`_event_dispatchable`)
        is treated as done — retrying it every poll would only log."""
        if dispatched or not self._event_dispatchable(events.by_state(state)):
            self._pending.pop(key, None)
        else:
            self._pending[key] = state

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

    def retry_pending(
        self,
        key: int | str,
        usage: float,
        events: ThresholdEvents,
        emit: Callable[[str, float, bool], bool],
    ) -> None:
        """Re-dispatch a transition the notifier throttled away.

        The throttle admits at most one delivery per event per window,
        so an episode edge that landed inside another episode's window
        is dropped with no retry of its own — until this retries it
        into an expired window (worst case: one window late). Only the
        still-current state is retried; a newer transition has already
        rewritten the pending entry, and an event that could never
        dispatch (allowlist/channels) is dropped rather than retried.
        """
        pending = self._pending.get(key)
        if pending is None:
            return
        dispatched = emit(pending, usage, True)
        if dispatched or not self._event_dispatchable(
            events.by_state(pending)
        ):
            self._pending.pop(key, None)
        if dispatched:
            self._emitted_at[key] = time.monotonic()

    def refresh_due(self, key: int | str, state: str) -> bool:
        """True when a persisting degraded state should re-notify.

        Transitions are edge-triggered and the notifier throttles
        delivery with a stamp-at-dispatch window — a transition whose
        dispatch fell inside another episode's window is retried
        (:meth:`retry_pending`), and a still-degraded metric
        refreshes its alert once per window — the worst case is a late
        alert, never a permanently lost one.
        """
        if state == OK:
            return False
        last = self._emitted_at.get(key, 0.0)
        return time.monotonic() - last >= REFRESH_SECONDS

    def emit_disk_event(
        self, state: str, path: str, usage: float, *, retry: bool = False
    ) -> bool:
        """Notify + log one disk event (transition, refresh, or
        retry). Returns whether the notification dispatched (#3206
        retry semantics — dispatched, not delivered). Retry attempts
        log at DEBUG: the transition already said it at WARNING, and
        an edge swallowed by the throttle retried at the poll floor
        would otherwise repeat the line hundreds of times inside one
        window."""
        event = DISK_EVENTS.by_state(state)
        self._log_disk_event(state, path, usage, retry)
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

    def _log_disk_event(
        self, state: str, path: str, usage: float, retry: bool
    ) -> None:
        """The disk event's log line (see :meth:`_log_metric_event`)."""
        self._log_metric_event(
            f"Disk usage {usage:.1f}% on {path}",
            state,
            retry,
            self._warn_percent,
            self._critical_percent,
        )

    def _log_metric_event(
        self,
        subject: str,
        state: str,
        retry: bool,
        warn: float,
        critical: float,
    ) -> None:
        """The log line for one threshold event — WARNING for
        degradations, INFO for recoveries, DEBUG for retry attempts
        (the transition already said it at WARNING, and an edge
        swallowed by the throttle retried at the poll floor would
        otherwise repeat the line hundreds of times inside one
        window)."""
        if retry:
            logger.debug(
                "%s: %s (retry; warn %.1f%%, critical %.1f%%)",
                subject,
                state,
                warn,
                critical,
            )
            return
        level = logging.INFO if state == OK else logging.WARNING
        logger.log(
            level,
            "%s: %s (warn %.1f%%, critical %.1f%%)",
            subject,
            "recovered" if state == OK else state,
            warn,
            critical,
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

    # --- host memory / CPU pressure surfaces (#3309) ---

    async def check_memory(self) -> None:
        """One memory pass: the utilization of the machine containers
        run on, as ``100 × (1 − availability)`` (see the module
        docstring for the macOS podman-machine path)."""
        if not self._memory_enabled:
            self._forget(MEMORY_KEY)
            return
        try:
            fraction = await self._measure_memory_fraction()
        except Exception as e:  # noqa: BLE001 — best-effort measurement
            self._note_unmeasurable("host memory", e)
            return
        self._rearm_unmeasurable("host memory")
        self._step_threshold(
            MEMORY_KEY,
            (1.0 - fraction) * 100.0,
            self._memory_warn_percent,
            self._memory_critical_percent,
            MEMORY_EVENTS,
            self._emit_memory,
        )

    async def _measure_memory_fraction(self) -> float:
        """Memory availability (0..1) of the machine containers run
        on. Linux host: the eviction subsystem's platform-aware
        measurement (meminfo, pressed by the cgroup limit when klangkd
        itself runs capped). macOS: the podman machine VM's own
        meminfo, read over ``podman machine ssh``. Raises when
        unmeasurable — the caller warns once and skips."""
        # Deferred import (see the module top): breaks the
        # settings → resource_watchdog → container.eviction cycle.
        from .container.eviction import (  # allow-deferred-import (see module top)
            available_fraction,
            measure_available_fraction,
            parse_meminfo,
        )

        if platform.system() != "Darwin":
            return await measure_available_fraction()
        meminfo = parse_meminfo(await self._read_machine_proc("meminfo"))
        return available_fraction(meminfo)

    async def _read_machine_proc(self, name: str) -> str:
        """Read ``/proc/<name>`` from inside the podman machine VM
        (macOS: containers live there, so its /proc is the gauge that
        matters; the Mac host's is the wrong machine). Raises
        ``OSError`` when the VM is unreachable — a stopped machine, no
        podman subsystem, or a command failure."""
        podman = getattr(self.app.state, "podman", None)
        if podman is None:
            raise OSError("no podman subsystem on the app state")
        rc, out, _err = await podman.run(
            ["machine", "ssh", "--", "cat", f"/proc/{name}"],
            check=False,
            timeout=5.0,
        )
        if rc != 0:
            raise OSError(f"podman machine ssh cat /proc/{name} exited {rc}")
        return out

    def _emit_memory(self, state: str, usage: float, retry: bool) -> bool:
        """Notify + log one memory event (see ``emit_disk_event`` for
        the return/retry semantics)."""
        self._log_metric_event(
            f"Memory usage {usage:.1f}%",
            state,
            retry,
            self._memory_warn_percent,
            self._memory_critical_percent,
        )
        return notify_event(
            self.app,
            MEMORY_EVENTS.by_state(state),
            detail={
                "metric": "memory",
                "usage_percent": round(usage, 1),
                "state": state,
                "warn_percent": self._memory_warn_percent,
                "critical_percent": self._memory_critical_percent,
            },
        )

    async def check_cpu(self) -> None:
        """One CPU-pressure pass: PSI ``some avg60`` (see the module
        docstring for the macOS podman-machine path)."""
        if not self._cpu_enabled:
            self._forget(CPU_KEY)
            return
        try:
            psi = parse_cpu_psi(await self._read_cpu_psi())
        except Exception as e:  # noqa: BLE001 — best-effort measurement
            self._note_unmeasurable("CPU pressure (PSI)", e)
            return
        self._rearm_unmeasurable("CPU pressure (PSI)")
        self._step_threshold(
            CPU_KEY,
            psi,
            self._cpu_warn_percent,
            self._cpu_critical_percent,
            CPU_EVENTS,
            self._emit_cpu,
        )

    async def _read_cpu_psi(self) -> str:
        """``/proc/pressure/cpu`` content from where containers run:
        directly on a Linux host, inside the podman machine VM on
        macOS. Raises ``OSError`` when unmeasurable."""
        if platform.system() == "Darwin":
            return await self._read_machine_proc("pressure/cpu")
        return read_local_text("/proc/pressure/cpu")

    def _emit_cpu(self, state: str, psi: float, retry: bool) -> bool:
        """Notify + log one CPU-pressure event (see ``emit_disk_event``
        for the return/retry semantics)."""
        self._log_metric_event(
            f"CPU pressure {psi:.1f}% (PSI some avg60)",
            state,
            retry,
            self._cpu_warn_percent,
            self._cpu_critical_percent,
        )
        return notify_event(
            self.app,
            CPU_EVENTS.by_state(state),
            detail={
                "metric": "cpu",
                "psi_avg60_percent": round(psi, 1),
                "state": state,
                "warn_percent": self._cpu_warn_percent,
                "critical_percent": self._cpu_critical_percent,
            },
        )

    def _forget(self, key: int | str) -> None:
        """Drop every remembered condition for one metric, so a
        re-enabled check evaluates fresh (a metric that degraded while
        its check was off alerts on the first poll after)."""
        self._states.pop(key, None)
        self._emitted_at.pop(key, None)
        self._pending.pop(key, None)

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
