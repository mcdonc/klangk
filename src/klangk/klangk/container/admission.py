"""Admission control for workspace starts (#2525).

The k8s-scheduler/ResourceQuota analogue: before a workspace container
is created, klangkd checks that the start is *admissible* —

1. **Host memory fit** (:meth:`AdmissionControl.check_host_memory`):
   available host memory (``MemAvailable`` from ``/proc/meminfo`` on
   Linux — plus the cgroup limit when klangkd itself runs in a
   memory-limited container; ``sysctl``/``vm_stat`` on macOS, shared
   with the eviction measurement, #2526, and capped by the podman
   machine's configured memory — containers live in that VM, whose
   default 2048 MiB is far below the Mac's RAM) against the workspace's
   resolved ``container_memory_limit`` (#34 / #864) plus a deploy-wide
   reserve for the server itself. A start that does not fit fails fast
   with a clear operator- and user-facing message instead of deferring
   the failure to the kernel OOM killer picking a random victim.
2. **Per-user quota** (:meth:`AdmissionControl.check_user_quota`): a
   deploy-wide ``max_running_workspaces_per_user`` cap on concurrently
   *running* workspaces per owner. A user who hits the cap gets a
   "stop a workspace first" error rather than an overloaded host.

Both checks are advisory against the **limit**, not a live usage gauge
— limits are what scheduler-style accounting can rely on, and the read
stays cheap (one ``/proc`` read + one DB query per start). Workspaces
whose memory limit is unset (unbounded) skip the fit check: there is no
limit to admit against.

The check runs at the single container-start choke point
(``ContainerRegistry.start_container_inner``, right after the #2527
drain gate), so every start path — API start/restart, WS connect,
create's eager start, boot auto-start, crash-recovery restart — is
covered, while a reconnect to an already-running workspace (container
adoption) is never re-admitted: its capacity is already committed.

Failure posture: a host whose memory cannot be measured fails **open**
(start allowed, one-time warning) — the same "unmeasurable is skip"
posture the eviction loop uses (#2526) — because bricking starts on an
exotic platform is worse than admitting without the check. A quota
refusal is exact (registry + DB state, no measurement involved).

All settings are read live off ``app.state.settings`` so a SIGHUP
reload (#1587) re-arms the gates without a restart.
"""

import asyncio
import json
import logging
import platform
import re
import time

from ..exceptions import WorkspaceCapacityError
from .eviction import (
    available_fraction,
    cgroup_memory_headroom,
    macos_measure,
    read_meminfo,
)
from .spec import resolve_memory_limit

logger = logging.getLogger(__name__)

# Podman size-string grammar (``2g`` / ``2gb`` / ``512m`` / ``1024`` /
# ``1.5g``), mirroring settings' ``KLANGKD_CONTAINER_MEMORY_LIMIT``
# validation (docker/go-units ParseSize: single base unit b/k/m/g/t/p,
# case-insensitive, optional trailing b; no IEC i-forms). Captures the
# numeric portion and the unit so the admission check can turn a limit
# string into bytes. Values reaching here are already validated at
# construction (settings validator / workspace-settings schema); a
# malformed string raises ``ValueError`` — surfaced as a config error,
# never silently treated as "fits".
_SIZE_RE = re.compile(r"^(?P<num>\d+(\.\d+)?)(?P<unit>[kKmMgGtTpP]?)[bB]?$")

_UNIT_BYTES = {
    "": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
    "p": 1024**5,
}


def parse_size_bytes(value: str) -> int:
    """Parse a podman size string (``2g`` / ``512mb`` / ``1024``) to bytes.

    Same grammar as ``KLANGKD_CONTAINER_MEMORY_LIMIT`` (see
    :data:`_SIZE_RE`). Raises ``ValueError`` on anything else.
    """
    m = _SIZE_RE.match(value.strip())
    if m is None:
        raise ValueError(
            f"{value!r} is not a valid size string (expected a positive "
            "number with an optional b/k/m/g/t/p unit suffix, e.g. 2g, "
            "512mb, 1024)."
        )
    return int(float(m.group("num")) * _UNIT_BYTES[m.group("unit").lower()])


def format_size(num_bytes: int) -> str:
    """Human-readable size for capacity messages (#2525).

    ``1.2 GB`` / ``512 MB`` — binary units rendered with the decimal
    abbreviation, matching how operators read ``free -h`` and the
    issue's example message ("1.2 GB available, workspace wants 4 GB").
    """
    gib = num_bytes / (1024**3)
    if gib >= 1.0:
        return f"{gib:.1f} GB"
    return f"{max(0, num_bytes) / (1024**2):.0f} MB"


# A podman machine with a configured memory below this (as parsed
# bytes) is treated as unparseable rather than a real ceiling: no real
# machine ships with < 64 MiB, and a MiB-scale integer (2048, 4096, …)
# is exactly what a unit mismatch would look like — capping
# availability to ~2 KB would refuse every start (#2525).
_MIN_MACHINE_MEMORY_BYTES = 64 * 1024 * 1024

# How long a parsed podman-machine memory cap stays fresh. The value
# only changes via ``podman machine set`` / re-init — rare enough that
# a 5-minute cache keeps the per-start cost at one ``vm_stat`` pair
# plus an occasional short ``podman machine ls``.
MACHINE_MEMORY_TTL_SECONDS = 300.0

# (podman_bin) -> (monotonic_ts, cap_bytes | None); None results are
# cached too (a machine-less Mac shouldn't shell out per start).
machine_memory_cache: dict[str, tuple[float, int | None]] = {}


def _pick_machine_memory(machines) -> int | None:
    """Pick the machine whose configured memory caps the measurement.

    Prefers the entry flagged ``"Default": true`` (what podman talks to
    without a ``--connection``), then the canonical default name, then
    exactly-one-machine. An ambiguous multi-machine setup with no
    default returns None (no cap) rather than guessing wrong. Memory
    is bytes in the ``machine ls --format json`` output (the
    podman-machine-list(1) example ships 2048 MiB as 2147483648).
    """
    if not isinstance(machines, list) or not machines:
        return None

    def memory_of(entry) -> int | None:
        try:
            value = int(entry["Memory"])
        except (KeyError, TypeError, ValueError):
            return None
        if value < _MIN_MACHINE_MEMORY_BYTES:
            return None
        return value

    for entry in machines:
        if entry.get("Default") is True:
            mem = memory_of(entry)
            if mem is not None:
                return mem
    for entry in machines:
        if entry.get("Name") == "podman-machine-default":
            mem = memory_of(entry)
            if mem is not None:
                return mem
    if len(machines) == 1:
        return memory_of(machines[0])
    return None


async def run_podman_json(*cmd: str):
    """Run a short podman command, return parsed JSON stdout.

    Raises ``OSError`` on a spawn failure / non-zero exit and
    ``ValueError`` on non-JSON output, so
    :func:`podman_machine_memory_bytes` can treat both as "no cap".
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _err = await proc.communicate()
    if proc.returncode != 0:
        raise OSError(f"{' '.join(cmd)} exited {proc.returncode}")
    return json.loads(out.decode(errors="replace"))


async def podman_machine_memory_bytes(
    podman_bin: str = "podman", *, runner=None, _now=None
) -> int | None:
    """Configured memory of the podman machine VM, in bytes, or None.

    On macOS, containers do not run on the Mac — they run inside a
    podman machine Linux VM whose memory is configured at
    ``podman machine init`` time and defaults to a tiny 2048 MiB,
    regardless of the Mac's RAM (#2525). Admission must fit the
    workspace where containers actually live, so the macOS measurement
    is capped by the machine's ceiling. Read via
    ``<podman_bin> machine ls --format json`` (works while the machine
    is stopped; does not start it) and cached for
    :data:`MACHINE_MEMORY_TTL_SECONDS`.

    *runner* injects the command transport (tests; the real one is
    :func:`run_podman_json`). Any failure — no podman, no machine,
    unparseable output — returns None: no cap, the Mac measurement
    governs (the gate's fail-open posture at large).
    """
    if _now is None:
        _now = time.monotonic
    cached = machine_memory_cache.get(podman_bin)
    if cached is not None and _now() - cached[0] < MACHINE_MEMORY_TTL_SECONDS:
        return cached[1]
    if runner is None:
        runner = run_podman_json
    try:
        machines = await runner(
            podman_bin, "machine", "ls", "--format", "json"
        )
    except (OSError, ValueError):
        cap = None
    else:
        cap = _pick_machine_memory(machines)
    machine_memory_cache[podman_bin] = (_now(), cap)
    return cap


async def available_memory_bytes(podman_bin: str = "podman") -> int:
    """Absolute host memory bytes immediately available (#2525).

    The bytes-based sibling of the eviction loop's
    ``measure_available_fraction`` (#2526) — admission compares a
    workspace's byte-sized memory limit against absolute headroom, not a
    fraction of total. Same platform-aware measurement:

    - **Linux**: ``MemAvailable`` from ``/proc/meminfo`` (falling back
      to ``MemFree + Cached`` on old kernels without it, via
      ``available_fraction``); when klangkd runs inside a cgroup with a
      finite memory limit (Docker ``-m``), the cgroup's own headroom
      (``limit - working set``) is measured too and the **smaller**
      absolute value wins — meminfo inside a container shows the host.
    - **macOS**: ``sysctl`` + ``vm_stat`` (shared with eviction),
      **capped by the podman machine's configured memory** — containers
      live in that VM (default 2048 MiB, far below the Mac's RAM), so
      fitting a workspace against the Mac's free memory alone would
      admit starts that cannot fit where containers actually run. The
      VM's *usage* is not measurable from the Mac, so the cap is its
      total — a ceiling, not a live gauge.

    Raises ``OSError``/``ValueError`` when nothing is measurable —
    callers treat that as fail-open.
    """
    if platform.system() == "Darwin":
        _total, available = await macos_measure()
        machine_cap = await podman_machine_memory_bytes(podman_bin)
        if machine_cap is not None:
            available = min(available, machine_cap)
        return available
    meminfo = read_meminfo()
    total = meminfo.get("MemTotal", 0)
    if total <= 0:
        raise ValueError("meminfo has no MemTotal — cannot measure capacity")
    available = int(available_fraction(meminfo) * total)
    headroom = cgroup_memory_headroom()
    if headroom is not None:
        limit, working_set = headroom
        available = min(available, max(0, limit - working_set))
    return available


class AdmissionControl:
    """Admission gates for workspace container starts (#2525).

    Constructed as a :class:`~klangk.container.registry.ContainerRegistry`
    collaborator (``registry.admission``) — like the idle/health/crash
    monitors — because the check runs at the registry's start choke
    point and needs no independent lifespan of its own.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._warned_unmeasurable = False

    def reconfigure(self, app) -> None:
        self.app = app

    # --- settings (read live off app.state, #1608) ---

    @property
    def memory_check_enabled(self) -> bool:
        return bool(self.app.state.settings.admission_memory_enabled)

    @property
    def margin_bytes(self) -> int:
        """Deploy-wide reserve for the server itself, in bytes."""
        raw = self.app.state.settings.admission_memory_margin
        if not raw:
            return 0
        try:
            return parse_size_bytes(raw)
        except ValueError:  # pragma: no cover — validator rejects at boot
            return 0

    @property
    def max_running_per_user(self) -> int | None:
        """Per-user running-workspace cap, or None when unlimited."""
        raw = self.app.state.settings.max_running_workspaces_per_user
        return raw or None

    # --- gates ---

    async def admit(self, spec) -> None:
        """Refuse a fresh container start, or return to admit it (#2525).

        Raises :class:`~klangk.exceptions.WorkspaceCapacityError` with an
        operator- and user-facing message when either gate refuses.
        *spec* is the :class:`~klangk.container.spec.ContainerStartSpec`
        of the start in flight (called under the workspace's start lock,
        after the running-container adoption check, so the workspace
        being started is by definition not currently running).
        """
        ws = await self.app.state.model.workspaces.get_workspace_by_id(
            spec.workspace_id
        )
        if ws is not None:
            await self.check_user_quota(spec.workspace_id, ws)
        await self.check_host_memory(
            spec.workspace_id, spec.workspace_settings, ws
        )

    async def _owned_ids(self, owner_id) -> list[str]:
        """All of *owner_id*'s workspace ids, started or not (#2525).

        Deliberately NOT prefiltered by ``container_id IS NOT NULL": a
        fresh workspace's container id is persisted only after
        ``podman create`` — seconds after admission — so a prefilter
        hides exactly the sibling starts the in-flight counting exists
        to see. Runtime state is joined in by the caller against the
        in-memory registry.
        """
        if owner_id is None:
            return []
        return await self.app.state.model.workspaces.get_user_workspace_ids(
            owner_id
        )

    def _committed(self, registry, ws_id: str) -> bool:
        """True while *ws_id* occupies capacity (#2525).

        Running (tracked in the in-memory registry — the same
        running-truth the idle/eviction monitors key on) or
        mid-start/stop (the per-workspace operation lock held). The
        lock signal is what closes the two-different-workspaces-
        starting-at-once race: the second start sees the first's lock
        and counts it. A workspace whose stop is still in flight
        transiently counts too (conservative, self-clearing in
        seconds).
        """
        return (
            ws_id in registry.states
            or registry.workspace_operation_in_flight(ws_id)
        )

    async def check_user_quota(self, workspace_id: str, ws: dict) -> None:
        """Refuse starts past ``max_running_workspaces_per_user``.

        Counts the owner's workspaces that are running or mid-start/stop
        (see :meth:`_committed`), over ALL of the owner's rows — a
        never-started sibling mid-create holds its operation lock but
        has no ``container_id`` yet, so the DB prefilter would hide it.
        The workspace being admitted is excluded: its own start lock is
        held by definition, and it is not running (a fresh create
        follows the adoption check).
        """
        limit = self.max_running_per_user
        if limit is None:
            return
        registry = self.app.state.container_registry
        owned = await self._owned_ids(ws.get("user_id"))
        running = sum(
            1
            for wid in owned
            if wid != workspace_id and self._committed(registry, wid)
        )
        if running >= limit:
            raise WorkspaceCapacityError(
                f"workspace quota reached: {running} of this user's "
                f"workspaces are already running and the server caps it "
                f"at {limit} (KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER). "
                "Stop a workspace first, or ask the operator to raise "
                "the cap."
            )

    async def _in_flight_sibling_limits(
        self, workspace_id: str, ws: dict | None
    ) -> int:
        """Sum of the memory limits of sibling starts in flight (#2525).

        ``MemAvailable`` cannot see a commitment that has not been
        created yet: N concurrent starts of *different* workspaces all
        measure the same pre-create availability and would each "fit"
        against it. Subtracting each lock-held sibling's resolved limit
        closes that window (the same lock signal the quota gate uses).
        Only siblings mid-start/stop are subtracted — a *running*
        workspace's usage is already reflected in MemAvailable, and
        limit-sum accounting for running workspaces is deliberately not
        this gate's semantics (the #2526 evictor owns overcommit after
        the fact). A mid-stop sibling transiently over-subtracts
        (conservative, self-clearing). Siblings with no limit
        configured (unbounded) contribute nothing: an unbounded
        commitment cannot be quantified, and the deploy chose not to
        bound it.
        """
        registry = self.app.state.container_registry
        committed = 0
        owned = await self._owned_ids((ws or {}).get("user_id"))
        for wid in owned:
            if wid == workspace_id:
                continue
            if not registry.workspace_operation_in_flight(wid):
                continue
            sibling = (
                await self.app.state.model.workspaces.get_workspace_by_id(wid)
            )
            if sibling is None:
                continue
            limit = resolve_memory_limit(self.app, sibling.get("settings"))
            if not limit:
                continue
            try:
                committed += parse_size_bytes(limit)
            except ValueError:  # pragma: no cover — validated upstream
                continue
        return committed

    async def check_host_memory(
        self,
        workspace_id: str,
        workspace_settings: dict | None,
        ws: dict | None = None,
    ) -> None:
        """Refuse a start whose memory limit does not fit the host.

        Compares available host memory against the workspace's resolved
        ``container_memory_limit`` (workspace settings-bag override >
        deploy default, #864) plus ``admission_memory_margin``. Skipped
        when the check is disabled or the limit is unset (unbounded —
        nothing to admit against); fails open (with a one-time warning)
        when memory cannot be measured on this platform.
        """
        if not self.memory_check_enabled:
            return
        limit = resolve_memory_limit(self.app, workspace_settings)
        if not limit:
            return
        # Parsed outside the measurement guard: a malformed limit string
        # is a config error (surfaced as such), not "unmeasurable".
        limit_bytes = parse_size_bytes(limit)
        try:
            available = await available_memory_bytes(
                self.app.state.settings.podman_bin or "podman"
            )
        except (OSError, ValueError) as e:
            if not self._warned_unmeasurable:
                self._warned_unmeasurable = True
                logger.warning(
                    "Admission memory check degraded: cannot measure host "
                    "memory availability (%s); allowing starts without "
                    "the capacity check",
                    e,
                )
            return
        # A measurement that works again re-arms the warning.
        self._warned_unmeasurable = False
        # Sibling starts in flight have not created their containers
        # yet, so MemAvailable cannot see their commitment — subtract
        # their resolved limits before fitting (#2525 review).
        available -= await self._in_flight_sibling_limits(workspace_id, ws)
        margin = self.margin_bytes
        needed = limit_bytes + margin
        if available < needed:
            raise WorkspaceCapacityError(
                f"host at capacity: {format_size(available)} available, "
                f"workspace wants {format_size(needed)} "
                f"(memory limit {format_size(limit_bytes)} + "
                f"{format_size(margin)} reserve). Stop an idle workspace, "
                "free host memory, or lower the workspace memory limit "
                "(KLANGKD_CONTAINER_MEMORY_LIMIT)."
            )
