"""Host memory-pressure eviction of idle workspaces (#2526).

A sibling loop to :class:`~klangk.container.idle.IdleMonitor`. When
host memory runs low — sustained over several polls, to avoid flapping
on transient spikes — the loop stops the least-recently-active workspace
that has **no connected clients**: the k8s node-pressure-eviction
analogue. Reclaim gracefully before the kernel OOM killer picks a
victim at random (which can be klangkd itself or Caddy).

Evicted workspaces take the normal graceful stop path (idle-stop
semantics: container removed, state preserved in volumes, next connect
restarts), plus a ``workspace_evicted`` WS event distinct from idle
stops and a log line naming the cause.

Availability measurement is platform-aware (#2526):

- **Linux, no container limit**: ``MemAvailable``/``MemTotal`` from
  ``/proc/meminfo``.
- **Linux inside a container with a cgroup memory limit** (Docker
  ``-m 2g`` etc.): ``/proc/meminfo`` still shows the **host**, so the
  container can sit at its cgroup ceiling while meminfo reads healthy.
  The cgroup's own headroom (``memory.max - working set`` over the
  limit) is measured too, and the **more pressured** of the two
  fractions wins — either dimension can evict. Caveat: when klangkd
  runs under its own systemd slice with ``MemoryMax`` (and rootless
  podman's workspaces sit outside that slice), the cgroup dimension
  measures klangkd itself — pressure there may not be relievable by
  evicting workspaces.
- **macOS**: ``vm_stat`` pages (free + inactive + speculative) over
  ``sysctl -n hw.memsize`` — there is no meminfo.

All thresholds are read live off ``app.state.settings`` on every poll,
so a SIGHUP config reload (#1587) re-arms the loop without a restart.
"""

import asyncio
import logging
import platform

logger = logging.getLogger(__name__)

# Floor for the poll interval so a misconfigured tiny interval cannot
# spin the loop hot (the /proc read is cheap, but podman stops are not).
MIN_POLL_INTERVAL_SECONDS = 1.0


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    """Parse ``/proc/meminfo`` into a ``{field: bytes}`` mapping.

    Kernel values are in kB; converted to bytes here so callers never
    mix units. Raises ``OSError`` if the file cannot be read (non-Linux
    host, permission) — the eviction loop treats that as "cannot
    measure" and skips the cycle.
    """
    values: dict[str, int] = {}
    with open(path) as fh:
        for line in fh:
            # "MemTotal:       16384000 kB" → name, value, unit
            parts = line.split()
            if len(parts) == 3 and parts[2] == "kB":
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
    return values


def available_fraction(meminfo: dict[str, int]) -> float:
    """Fraction (0..1+) of host memory immediately available.

    Prefers ``MemAvailable`` (kernel-reclaimed + free, the number the
    OOM killer effectively watches); falls back to
    ``MemFree + Cached`` for old kernels without MemAvailable (same
    fallback the Linux OOM docs suggest as an approximation).
    ``MemTotal`` of 0 (a truncated/bogus file) raises — callers treat
    unmeasurable as "skip this cycle".
    """
    total = meminfo.get("MemTotal", 0)
    if total <= 0:
        raise ValueError("meminfo has no MemTotal — cannot measure pressure")
    available = meminfo.get("MemAvailable")
    if available is None:
        available = meminfo.get("MemFree", 0) + meminfo.get("Cached", 0)
    return available / total


def _read_int_file(path: str) -> int:
    """Read a whole-file integer (cgroup files have no kB suffix)."""
    with open(path) as fh:
        return int(fh.read().strip())


# A cgroup v1 "unlimited" limit is a huge sentinel (2^63-ish); anything
# at or beyond this is "no limit" (also catches "max" parsed as inf).
_CGROUP_LIMIT_UNLIMITED = 1 << 60


def cgroup_memory_headroom(
    cgroup_dir: str = "/sys/fs/cgroup",
) -> tuple[int, int] | None:
    """Return ``(limit_bytes, working_set_bytes)`` for this cgroup, or None.

    The Docker/container case (#2526): ``/proc/meminfo`` inside a
    container shows the **host**, so a container capped at ``-m 2g`` can
    OOM while meminfo reads healthy. ``memory.max`` (cgroup v2) or
    ``memory.limit_in_bytes`` (v1) is the container's real ceiling.

    Returns None when the cgroup has no finite limit (bare metal, a
    Docker container without ``-m``, rootless podman's default) or the
    files are unreadable — in those cases meminfo alone governs. The
    working set is ``current - inactive_file`` (the kernel-reclaimable
    page cache is excluded), the same approximation k8s uses for node
    pressure. A limit smaller than the floor is ignored.
    """
    v2_max = None
    try:
        text = open(f"{cgroup_dir}/memory.max").read().strip()
        if text != "max":
            v2_max = int(text)
    except (OSError, ValueError):
        pass
    try:
        if v2_max is not None:
            limit = v2_max
            current = _read_int_file(f"{cgroup_dir}/memory.current")
            inactive_file = 0
            try:
                for line in open(f"{cgroup_dir}/memory.stat"):
                    name, _, value = line.partition(" ")
                    if name == "inactive_file":
                        inactive_file = int(value)
                        break
            except (OSError, ValueError):
                pass
        else:
            limit = _read_int_file(
                f"{cgroup_dir}/memory/memory.limit_in_bytes"
            )
            if limit >= _CGROUP_LIMIT_UNLIMITED:
                return None
            current = _read_int_file(
                f"{cgroup_dir}/memory/memory.usage_in_bytes"
            )
            inactive_file = 0
            try:
                for line in open(f"{cgroup_dir}/memory/memory.stat"):
                    name, _, value = line.partition(" ")
                    if name in ("total_inactive_file", "inactive_file"):
                        inactive_file = int(value)
                        break
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        return None
    # "0" (or a bogus negative) is not a limit — e.g. a transient
    # memory.max=0 during container teardown. Ignore, meminfo governs.
    if limit is None or limit <= 0:
        return None
    working_set = max(0, current - inactive_file)
    if working_set > limit:
        # Over the ceiling (limit hit, accounted with lag): no headroom.
        return limit, limit
    return limit, working_set


def parse_vm_stat(
    vm_stat_output: str, page_size: int, total_bytes: int
) -> float:
    """Fraction (0..1) of macOS memory immediately available.

    ``vm_stat`` reports page counts; "available" is free + inactive +
    speculative pages (the same approximation psutil uses — pages the
    pager can reclaim without swapping anything out). Capped at 1.0.
    Raises ``ValueError`` when the page counters are missing — callers
    treat unmeasurable as "skip this cycle".
    """
    pages: dict[str, int] = {}
    for line in vm_stat_output.splitlines():
        # "Pages free:                               12345."
        name, colon, value = line.partition(":")
        if not colon:
            continue
        value = value.strip().rstrip(".")
        if value.isdigit():
            pages[name.strip()] = int(value)
    free = pages.get("Pages free")
    inactive = pages.get("Pages inactive")
    speculative = pages.get("Pages speculative")
    if free is None or inactive is None or speculative is None:
        raise ValueError("vm_stat output lacks page counters")
    if total_bytes <= 0 or page_size <= 0:
        raise ValueError("vm_stat parse got non-positive total/page size")
    available = (free + inactive + speculative) * page_size
    return min(available / total_bytes, 1.0)


def vm_stat_page_size(vm_stat_output: str) -> int:
    """Extract the page size from ``vm_stat``'s header line.

    Raises ``ValueError`` when the header is missing or unparseable:
    guessing 4096 on a 16 KiB-page Apple-Silicon host would mis-state
    availability 4×, and a wrong guess in the "more pressured"
    direction would evict on healthy memory. Unmeasurable (skip the
    cycle) is the safe failure.
    """
    # "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
    header = vm_stat_output.splitlines()[0] if vm_stat_output else ""
    page_size = 0
    if "page size of" in header and "bytes" in header:
        try:
            page_size = int(
                header.split("page size of")[1].split("bytes")[0].strip()
            )
        except ValueError:
            page_size = 0
    if page_size <= 0:
        raise ValueError("vm_stat header lacks a parseable page size")
    return page_size


async def _run_command(*cmd: str) -> str:
    """Run a short command, return stdout, raise OSError on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    if proc.returncode != 0:
        raise OSError(f"{' '.join(cmd)} exited {proc.returncode}")
    return out.decode(errors="replace")


async def macos_available_fraction(runner=None) -> float:
    """Measure availability on macOS via ``sysctl`` + ``vm_stat``.

    Two short subprocesses every poll interval; run through
    :mod:`asyncio` so the event loop is never blocked. *runner* injects
    the command transport (tests); the real one is
    :func:`_run_command`. Any failure raises — the caller treats
    unmeasurable as "skip this cycle".
    """
    if runner is None:
        runner = _run_command
    total = int((await runner("sysctl", "-n", "hw.memsize")).strip())
    vm_stat = await runner("vm_stat")
    return parse_vm_stat(vm_stat, vm_stat_page_size(vm_stat), total)


async def measure_available_fraction() -> float:
    """Best availability measurement for this platform, 0..1+.

    Linux: the more pressured of meminfo and the cgroup limit (when the
    process runs in a memory-limited container; no limit → meminfo
    alone). macOS: ``vm_stat``/``sysctl``. Raises when nothing is
    measurable — the eviction loop skips that cycle.
    """
    if platform.system() == "Darwin":
        return await macos_available_fraction()
    meminfo_fraction = available_fraction(read_meminfo())
    headroom = cgroup_memory_headroom()
    if headroom is None:
        return meminfo_fraction
    limit, working_set = headroom
    cgroup_fraction = (limit - working_set) / limit
    return min(meminfo_fraction, cgroup_fraction)


class MemoryPressureEvictor:
    """Evict idle workspaces under sustained host memory pressure (#2526).

    One workspace per poll while pressure persists (each stop frees
    memory asynchronously — podman removal is slow), least-recently-
    active first, and never a workspace with live terminal/browser
    subscribers while an idle one exists. Recovery above the hysteresis
    threshold ends the episode.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._task: asyncio.Task | None = None
        self._warned_no_evictable = False
        # Successful evictions since the episode opened (reset on
        # recovery) — rides the exhausted-candidates warning so an
        # operator can tell "nothing to evict" from "evicted N and it
        # didn't help" (#2627 review).
        self._evicted_this_episode = 0

    def reconfigure(self, app) -> None:
        self.app = app

    @property
    def _threshold(self) -> float:
        """Pressure threshold, percent of MemTotal (live off settings)."""
        return self.app.state.settings.memory_eviction_threshold_percent

    @property
    def _recovery(self) -> float:
        """Recovery (hysteresis) threshold, percent of MemTotal."""
        return self.app.state.settings.memory_eviction_recovery_percent

    @property
    def _sustain_polls(self) -> int:
        """Consecutive below-threshold polls before evictions begin."""
        return self.app.state.settings.memory_eviction_sustain_polls

    def start(self) -> None:
        """Start the eviction loop (idempotent). Runs until :meth:`stop`."""
        if self._task is None:
            settings = self.app.state.settings
            logger.info(
                "Memory-pressure eviction armed: threshold %.1f%%, "
                "recovery %.1f%%, sustain %d polls, interval %.1fs "
                "(effective floor %.1fs), enabled=%s",
                settings.memory_eviction_threshold_percent,
                settings.memory_eviction_recovery_percent,
                settings.memory_eviction_sustain_polls,
                settings.memory_eviction_poll_interval,
                MIN_POLL_INTERVAL_SECONDS,
                settings.memory_eviction_enabled,
            )
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the eviction loop.

        Tolerates a loop that already died: a dead task's exception must
        never re-raise here and break the lifespan shutdown cascade
        that runs after this (main.py finally).
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Memory-eviction loop had died earlier; suppressing "
                    "its exception on shutdown",
                    exc_info=True,
                )
            self._task = None

    def _evictable_workspaces(self) -> list:
        """Tracked workspace states with no connected clients, oldest first.

        Sort key is ``ContainerState.last_activity`` ascending — the
        least-recently-active workspace is evicted first (the same
        activity-recency ordering the idle monitor's reap uses, applied
        as an LRU). Workspaces with any live terminal/browser subscriber
        are never candidates while an idle one exists, and workspaces
        already in a stop path (``registry.stopping``) are skipped so a
        concurrent stop/idle-stop is not double-processed. Every
        workspace client holds the same workspace WebSocket
        (``session.subscribers``) — the subscriber set covers
        terminal and browser clients alike (#2627 review).
        Workspaces pinned "never stop" — per-workspace ``idle_timeout``
        of 0, the pin auto-started boot services use so the idle monitor
        leaves them alone (#1244) — are also skipped: a pin must mean
        something, and a pinned service typically has zero WS
        subscribers and a stale ``last_activity`` (hosted-app traffic
        bypasses klangkd entirely), so without this it would sort to
        the LRU head and die first. A deploy-wide idle timeout of 0
        (idle stopping disabled entirely) likewise disables eviction —
        the conservative reading of "never stop idle workspaces".
        """
        registry = self.app.state.container_registry
        sockets = self.app.state.sockets
        candidates = []
        for ws_id, state in registry.states.items():
            if ws_id in registry.stopping:
                continue
            # A start/stop in flight (the per-workspace lock is held)
            # means the workspace is mid-transition: a reconnecting
            # workspace's container is tracked from podman create but
            # has no subscriber until container_ready, so without this
            # check an armed evictor stops the fresh container under
            # the connecting client (#2527 e2e flake).
            if registry.workspace_operation_in_flight(ws_id):
                continue
            if state.get_idle_timeout() == 0:
                continue
            session = sockets.sessions.get(ws_id)
            if session and (
                session.subscribers or session.browser_subscribers
            ):
                continue
            candidates.append(state)
        candidates.sort(key=lambda s: s.last_activity)
        return candidates

    async def evict_one(self, fraction: float) -> bool:
        """Gracefully stop the least-recently-active idle workspace.

        Returns True if a workspace was evicted, False when nothing is
        evictable (every tracked workspace has connected clients, or the
        registry is empty). The fraction (0..1 available) rides the log
        line and the broadcast so consumers can see *why*.

        Race posture (#2627 review): everything from candidate build to
        the eviction log/broadcast is synchronous — no other task can
        stop the victim inside that prefix — and a workspace already in
        a stop path is excluded by the ``registry.stopping`` check
        (``stop_and_remove_container`` sets it synchronously at entry,
        before its first await). The remaining window is DURING this
        method's two awaits: a concurrent idle-stop of the same victim
        double-notifies and double-removes — benign (both stops are
        state-tolerant, the remove 404-tolerant, and the death frame
        deduplicates by state) — so no guard is needed and none would
        be reachable.
        """
        candidates = self._evictable_workspaces()
        if not candidates:
            # Once per episode (reset on success and on recovery) — a
            # WARNING per poll under sustained all-busy pressure would
            # be ~8.6k lines/day at defaults.
            if not self._warned_no_evictable:
                self._warned_no_evictable = True
                if self._evicted_this_episode:
                    logger.warning(
                        "Host memory pressure (%.1f%% available) and no "
                        "idle workspace left — evicted %d workspace(s) "
                        "this episode without recovery. If this host's "
                        "cgroup limit applies to klangkd itself rather "
                        "than the workspaces (e.g. a systemd slice "
                        "MemoryMax), evicting workspaces cannot relieve "
                        "it; check what the limit actually caps.",
                        fraction * 100,
                        self._evicted_this_episode,
                    )
                else:
                    logger.warning(
                        "Host memory pressure (%.1f%% available) but no "
                        "idle workspace to evict — every tracked "
                        "workspace has connected clients or a never-stop "
                        "pin",
                        fraction * 100,
                    )
            else:
                logger.debug(
                    "Memory pressure persists; still no evictable workspace"
                )
            return False
        self._warned_no_evictable = False
        victim = candidates[0]
        self._evicted_this_episode += 1
        logger.warning(
            "Evicting workspace %s (container %s): host memory pressure "
            "(%.1f%% available < %.1f%% threshold)",
            victim.workspace_id,
            victim.container_id[:12],
            fraction * 100,
            self._threshold,
        )
        sockets = self.app.state.sockets
        registry = self.app.state.container_registry
        sockets.notify_workspace_evicted(
            victim.workspace_id,
            reason="host memory pressure",
        )
        await registry.notify_workspace_killed(victim.workspace_id)
        await registry.stop_and_remove_container(
            victim.container_id, workspace_id=victim.workspace_id
        )
        return True

    async def _handle_measurement(
        self, fraction: float, below: int, pressured: bool
    ) -> tuple[int, bool]:
        """One state-machine step on a fresh availability measurement.

        Returns the new ``(below, pressured)``. Two states:

        - normal: consecutive below-threshold polls build until
          ``sustain_polls`` (a transient spike never evicts), then the
          episode opens with one eviction.
        - pressured: evict one workspace per poll while still below
          threshold; the episode closes only when availability rises to
          the recovery threshold (strictly above threshold — the gap is
          the hysteresis that prevents flap-eviction at the boundary).
        """
        percent = fraction * 100
        if pressured:
            if percent >= self._recovery:
                logger.info(
                    "Host memory recovered (%.1f%% available >= %.1f%%) "
                    "— ending eviction episode",
                    percent,
                    self._recovery,
                )
                self._warned_no_evictable = False
                self._evicted_this_episode = 0
                return 0, False
            if percent < self._threshold:
                await self.evict_one(fraction)
            # Between threshold and recovery: hold — memory is being
            # reclaimed (an eviction may still be in flight); evicting
            # again here is what flap-prevention exists to avoid.
            return below, pressured
        if percent < self._threshold:
            below += 1
            if below >= self._sustain_polls:
                logger.warning(
                    "Sustained host memory pressure (%.1f%% "
                    "available < %.1f%% for %d polls) — evicting "
                    "idle workspaces",
                    percent,
                    self._threshold,
                    below,
                )
                await self.evict_one(fraction)
                return 0, True
            return below, pressured
        return 0, pressured

    async def _run(self) -> None:
        """Poll loop: measure, sustain, evict, hysteresis.

        Sleeps ``memory_eviction_poll_interval`` (floored at
        :data:`MIN_POLL_INTERVAL_SECONDS`), measures host availability,
        and feeds each measurement to :meth:`_handle_measurement`. All
        settings are re-read every cycle (SIGHUP-live, #1587). A host
        whose ``/proc/meminfo`` cannot be read (non-Linux) is warned
        about once and skipped — never evicted blind.
        """
        below = 0
        pressured = False
        warned_unreadable = False
        while True:
            settings = self.app.state.settings
            interval = max(
                settings.memory_eviction_poll_interval,
                MIN_POLL_INTERVAL_SECONDS,
            )
            await asyncio.sleep(interval)
            if not settings.memory_eviction_enabled:
                below = 0
                pressured = False
                continue
            try:
                fraction = await measure_available_fraction()
            except (OSError, ValueError) as e:
                if not warned_unreadable:
                    warned_unreadable = True
                    logger.warning(
                        "Memory-pressure eviction degraded: cannot measure "
                        "memory availability on this platform (%s); "
                        "skipping cycles until measurable",
                        e,
                    )
                continue
            # A measurement that works again re-arms the warning, so a
            # path that breaks *later* (transient today, permanent next
            # week) still says so.
            warned_unreadable = False
            try:
                below, pressured = await self._handle_measurement(
                    fraction, below, pressured
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The eviction action must never kill the loop: a host at
                # <10% available is exactly where fork (podman exec)
                # fails with OSError — skipping one cycle is fine, dying
                # silently for the process lifetime is not (#2627 review).
                logger.warning(
                    "Memory-pressure eviction cycle failed (skipped)",
                    exc_info=True,
                )
