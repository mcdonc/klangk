"""Container lifecycle management: start, stop, bringup, reaps (#2542 split).

``ContainerRegistry`` composes :class:`PortAllocator`,
:class:`BrowserRouter`, :class:`IdleMonitor`, :class:`HealthMonitor`,
and :class:`CrashRecoveryMonitor` as collaborators, and mixes in
:class:`NetworkSidecarMixin` for the FQDN egress sidecar.  Spec
assembly (env/mounts/volumes/limits/create kwargs, plus the
:class:`ContainerStartSpec` start-parameter object) lives in
:mod:`.spec` (#2566).  Constructed once in :func:`build_app` and stored
on ``app.state.container_registry`` (#1426).
"""

import asyncio
import logging
import os
import time

from .. import podman
from .. import fips as fips_mod
from ..exceptions import AuditWriteError, NodeDrainingError
from ..model.workspaces import EGRESS_MODE_ALLOW, EGRESS_MODE_INTERACTIVE
from ..model.container_events import (
    CAUSE_DRAIN,
    CAUSE_LOGOUT,
    CAUSE_REAP,
    CAUSE_SHUTDOWN,
    EVENT_START,
    EVENT_STOP,
    INTERACTIVE_START_CAUSES,
    INTERACTIVE_STOP_CAUSES,
    ROLE_SIDECAR,
    ROLE_WORKSPACE,
)
from ..podman import PodmanError, bringup_timeout
from ..ssl_trust import SSL_MOUNT_DEST as _SSL_MOUNT_DEST
from ..settings import parse_bool_setting
from ..workspace_settings import resolve_allow_sudo
from .admission import AdmissionControl
from .crash import CrashRecoveryMonitor
from .health import HealthMonitor
from .idle import IdleMonitor
from .basics import (
    CONTAINER_PORT_START,
    DEFAULT_PORTS_PER_WORKSPACE,
    BrowserRouter,
    ContainerState,
    PortAllocator,
    workspace_container_name,
    workspace_name_slug,
)
from .sidecar import (
    NetworkSidecarMixin,
    container_ident,
    labeled_workspace_id,
)
from .spec import (
    ContainerStartSpec,
    SHARED_HOME,
    is_named_volume,
    split_csv,
    valid_volume_name,
    build_create_kwargs,
    build_env,
    build_mounts,
    ensure_volumes,
    image_pull_policy,
    nix_binds,
)

logger = logging.getLogger(__name__)


_VALID_MOUNT_OPTIONS = {
    "ro",
    "rw",
    "z",
    "Z",
    "nocopy",
    "consistent",
    "cached",
    "delegated",
}

_PROTECTED_PATHS = [
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
]


def pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` is currently running.

    Used by the dead-owner container reap
    (:meth:`ContainerRegistry.reap_dead_owner_containers`) to decide whether
    the klangkd that created a container (its PID recorded in the
    ``klangk.pid`` label) is still alive.

    ``os.kill(pid, 0)`` sends no signal — it only checks existence:
    success ⇒ alive; ``ProcessLookupError`` (ESRCH) ⇒ no such process ⇒
    dead; ``PermissionError`` (EPERM) ⇒ the process exists but is owned by
    another user (e.g. a sibling klangkd run under a different account) ⇒
    treat as alive so we leave its containers alone.

    Deliberately a plain liveness check, **not** a process-identity check.
    PIDs recycle, so a dead owner's PID can be reused by an unrelated
    process and read falsely as "alive" — but that failure mode only ever
    *misses a reap* (the leaked container keeps running, the pre-feature
    behavior); it can never reap a live owner's containers, because a live
    owner always holds its own PID and so always reads alive (#2342, #1556).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not ours — another user's process; assume alive.
        return True
    return True


def reap_sort_key(c: dict) -> int:
    """Sort key: remove netns *dependents* (workspaces) before sidecars.

    A workspace joins its network sidecar's netns via
    ``--network container:<sidecar>``, so the sidecar cannot be removed
    while the workspace still references it — podman refuses with "has
    dependent containers", and ``rm -f`` does **not** override that check.
    ``list_containers`` returns containers in no guaranteed role order, so an
    unsorted reap may hit a sidecar before its workspace, skip it (logged
    ``PodmanError``), remove the workspace, and orphan the sidecar until the
    next startup (#2476). Ordering workspaces (``klangk.role=workspace``)
    ahead of everything else lets each sidecar removal succeed in the same
    pass regardless of the list order.
    """
    return (
        0 if (c.get("Labels") or {}).get("klangk.role") == "workspace" else 1
    )


async def safe_remove(podman_inst, container_id: str, *, what: str) -> bool:
    """Remove a container, 404-tolerant, logging failures as warnings.

    The shared tail of every reap/sweep path (#2548):
    ``remove_container`` itself is 404-tolerant; any other error is
    logged with *what* context and swallowed so one bad container never
    aborts a sweep. Returns True on success.
    """
    try:
        await podman_inst.remove_container(container_id)
        return True
    except podman.PodmanError as e:
        logger.warning("Failed to reap %s %s: %s", what, container_id[:12], e)
        return False


def _parse_host_port(entry) -> int | None:
    """Host port from one PortBindings entry, or None when unreadable."""
    try:
        return int(entry["HostPort"])
    except (KeyError, ValueError, TypeError):
        return None


def _host_port_entries(bindings: dict):
    """Flatten PortBindings' {port: [entries]} into the entries."""
    for ports_list in bindings.values():
        for entry in ports_list or []:
            yield entry


def host_bound_ports(info: dict) -> set[int]:
    """Host ports published by an inspected container (from HostConfig
    PortBindings)."""
    bindings = info.get("HostConfig", {}).get("PortBindings") or {}
    ports = map(_parse_host_port, _host_port_entries(bindings))
    return {port for port in ports if port is not None}


async def remove_stale_container(
    podman, stale_id: str, bound: set[int], wanted_ports: set[int]
) -> None:
    """Remove a stale container holding a wanted port; best-effort."""
    try:
        await podman.remove_container(stale_id)
        logger.info(
            "Removed stale container %s (ports %s)",
            stale_id[:12],
            bound & wanted_ports,
        )
    except PodmanError as del_exc:
        logger.warning(
            "Could not remove stale container %s: %s",
            stale_id[:12],
            del_exc,
        )


def _resolved_under_root(resolved: str, roots) -> bool:
    """True when a resolved path equals or lives under an allowed root."""
    return any(
        resolved == root or resolved.startswith(root + "/") for root in roots
    )


def _labeled_workspace_ident(c: dict) -> str | None:
    """Ident of a listed container when it is a workspace container
    (the network sidecar shares the workspace label)."""
    labels = c.get("Labels") or {}
    if labels.get("klangk.role") != "workspace":
        return None
    return container_ident(c) or None


def _owner_pid_label(c: dict) -> int | None:
    """Parsed ``klangk.pid`` label, or None when absent/unparseable."""
    label = (c.get("Labels") or {}).get("klangk.pid")
    if not label:
        return None
    try:
        return int(label)
    except (TypeError, ValueError):
        return None


def _dead_owner_pid(c: dict) -> int | None:
    """Owner pid when the container's ``klangk.pid`` label names a dead
    process, else None (liveness undecidable or owner alive: leave it)."""
    owner_pid = _owner_pid_label(c)
    if owner_pid is None or owner_pid <= 0 or pid_alive(owner_pid):
        return None
    return owner_pid


def _wanted_host_ports(publish: list[tuple[int, int]]) -> set[int]:
    """Host ports from a publish list of (host, container) pairs."""
    return {host_port for host_port, _container_port in publish}


class ContainerRegistry(NetworkSidecarMixin):
    """Manages all container state and podman interactions.

    Composes :class:`PortAllocator`, :class:`BrowserRouter`,
    :class:`IdleMonitor`, and :class:`HealthMonitor` as collaborators.
    Backward-compatible proxy methods delegate to the collaborators so
    existing callers are unchanged.

    Constructed once in :func:`build_app` and stored on
    ``app.state.container_registry`` (#1426). The module-level ``registry``
    is a transitional shim for callers not yet migrated to explicit
    threading.
    """

    def __init__(self, app):
        self.app = app

        # Runtime-mutable state (initialized from settings but overridable
        # at runtime via set_idle_timeout — NOT a live settings read).
        self.idle_timeout_seconds, self.check_interval_seconds = (
            self._parse_idle_timeout()
        )

        self.states: dict[str, ContainerState] = {}
        self._cid_to_wsid: dict[str, str] = {}
        # Workspaces with a live network sidecar (#2254) — only these get a
        # best-effort network sidecar teardown on stop, so a non-filtered workspace
        # stop doesn't fire a speculative remove.
        self._ws_with_network_sidecar: set[str] = set()
        # The live network sidecar's container id per workspace (#2915):
        # the netns owner a filtered workspace joins via
        # ``--network container:<id>``. Recorded on container_events rows
        # (start: right off the bringup; stop: captured before teardown).
        # Populated only for sidecars started by this process — a stop of
        # a prior-session workspace records NULL for the namespace.
        self._ws_netns_owner: dict[str, str] = {}
        # Workspaces with an expected stop in flight (#2524): the crash
        # monitor skips these so a user/idle/logout stop — which holds
        # this marker across its slow podman remove — is never misread
        # as an unexpected death. Discarded when the stop completes (the
        # marker alone cannot distinguish "a stop completed while the
        # monitor's liveness call was in flight" from "no stop ever
        # happened"), so each stop also bumps ``stop_epoch`` — a
        # monotonic per-workspace counter the crash monitor snapshots
        # around its awaits and re-checks before scheduling a restart,
        # closing the completed-during-detection race (review #2625).
        self.stopping: set[str] = set()
        self.stop_epoch: dict[str, int] = {}
        # Audit-write failure counter (#3154, security finding V-222486):
        # every container_events write that fails — the best-effort paths
        # included — bumps this so /audit exposes an operator-visible
        # (and assessor-visible) signal that the audit trail is losing
        # rows, beyond the warning log line.
        self.audit_write_failures: int = 0
        # In-memory drain flag (#2527): while a SIGHUP graceful restart
        # quiesces the node, every container-start path refuses new
        # starts. Deliberately NOT persisted — it must self-clear when
        # the restart completes, and a crashed restart must not leave
        # the node refusing starts.
        self.draining: bool = False
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._service_session_locks: dict[str, asyncio.Lock] = {}
        # Containers whose service-command fire half-completed and whose
        # cleanup ALSO failed, leaving a command-less ``service-cmd``
        # window behind (#2740). Keyed like the locks above; cleared on
        # successful fire/retry, cleanup, and container teardown.
        self._service_fire_pending: set[str] = set()
        self.on_workspace_killed = None
        self.on_container_status_changed = None

        # Collaborators
        self.ports = PortAllocator(app)
        self.browsers = BrowserRouter()
        self.idle = IdleMonitor(app)
        self.health = HealthMonitor(app)
        self.crash = CrashRecoveryMonitor(app)
        # Admission control (#2525): host-capacity fit + per-user quota,
        # checked at the start choke point below. A registry collaborator
        # (like idle/health/crash) — no independent lifespan of its own.
        self.admission = AdmissionControl(app)

        # The Podman instance is reached via self.app.state.podman (owned
        # instance, #1426) — no post-construction wiring needed.

    def reconfigure(self, app) -> None:
        self.app = app
        self.ports.reconfigure(app)
        self.idle.reconfigure(app)
        self.health.reconfigure(app)
        self.crash.reconfigure(app)
        self.admission.reconfigure(app)

    # --- settings-derived config (read live off app_state, #1608) ---

    @property
    def image_name(self) -> str:
        return self.app.state.settings.image_name or "klangk-workspace"

    @property
    def allowed_images(self) -> set[str]:
        imgs = set(split_csv(self.app.state.settings.allowed_images))
        imgs.add(self.image_name)
        return imgs

    @property
    def allowed_mount_roots(self) -> list[str]:
        return [
            os.path.realpath(p)
            for p in split_csv(self.app.state.settings.allowed_mount_roots)
        ]

    @property
    def port_range_start(self) -> int:
        return self.app.state.settings.port_range_start or 9000

    @property
    def health_check_interval(self) -> float:
        return self.app.state.settings.health_check_interval or 30.0

    @property
    def health_check_timeout(self) -> float:
        return self.app.state.settings.health_check_timeout or 10.0

    @property
    def health_check_startup_grace(self) -> float:
        return self.app.state.settings.health_check_startup_grace or 30.0

    # --- Proxy: CrashRecoveryMonitor (#2524) ---

    def start_crash_loop(self) -> None:
        self.crash.start()

    # --- settings-derived methods (were module functions, #1487) ---

    def container_dns_config(self) -> list[str]:
        """Return DNS server list from settings.dns_servers."""
        return split_csv(self.app.state.settings.dns_servers)

    def container_dns_search_config(self) -> list[str]:
        """Return DNS search-domain list from settings.dns_search (#2055)."""
        return split_csv(self.app.state.settings.dns_search)

    def image_pull_policy(self) -> str:
        """Resolve the workspace-image pull policy from settings."""
        return image_pull_policy(self.app)

    def _is_protected(self, source: str) -> bool:
        """True if source is a protected host path that must never be mounted."""
        resolved = os.path.realpath(source)
        data_dir = os.path.realpath(self.app.state.settings.data_dir)
        for blocked in [*_PROTECTED_PATHS, data_dir]:
            blocked = os.path.realpath(blocked)
            if resolved == blocked or resolved.startswith(blocked + "/"):
                return True
        return False

    def _validate_mount_shape(self, spec: str, parts: list[str]) -> str | None:
        """Structural checks: 2-3 parts, non-empty source, absolute dest."""
        if len(parts) < 2 or len(parts) > 3:
            return (
                f"Invalid mount {spec!r}: expected source:dest or "
                "source:dest:options"
            )
        if not parts[0]:
            return f"Invalid mount {spec!r}: source is empty"
        if not parts[1].startswith("/"):
            return (
                f"Invalid mount {spec!r}: container path must be absolute "
                "(start with /)"
            )
        return None

    def _validate_mount_options(self, spec: str, options: str) -> str | None:
        """The 3rd field must be a comma list of known options."""
        for opt in options.split(","):
            if opt and opt not in _VALID_MOUNT_OPTIONS:
                return f"Invalid mount {spec!r}: unknown option {opt!r}"
        return None

    def validate_mount_spec(self, spec: str) -> str | None:
        """Validate a container mount spec string."""
        parts = spec.split(":")
        error = self._validate_mount_shape(spec, parts)
        if error:
            return error
        if len(parts) == 3:
            error = self._validate_mount_options(spec, parts[2])
            if error:
                return error
        source = parts[0]
        if is_named_volume(source):
            return self._validate_named_volume_source(spec, source)
        return self._validate_bind_source(spec, source)

    def _validate_named_volume_source(
        self, spec: str, source: str
    ) -> str | None:
        """A named-volume source must be podman-safe (#3018): it is
        appended verbatim to the podman argv at container start, so a
        leading dash would be parsed as a flag — the same rule as the
        volumes API (``VOLUME_NAME_PATTERN``, #2971)."""
        if valid_volume_name(source):
            return None
        return (
            f"Invalid mount {spec!r}: named-volume source {source!r} "
            "is not podman-safe (must start alphanumeric, contain only "
            "[a-zA-Z0-9_.-], and be at most 64 chars)"
        )

    def _validate_bind_source(self, spec: str, source: str) -> str | None:
        """A bind source must not be a protected host path, and —
        deny-by-default (#3153) — must live under a configured
        `KLANGKD_ALLOWED_MOUNT_ROOTS` root. With no roots configured,
        user bind mounts are disabled entirely; only named volumes
        may be mounted."""
        if self._is_protected(source):
            return f"Invalid mount {spec!r}: source is a protected host path"
        roots = self.allowed_mount_roots
        if not roots:
            return (
                f"Invalid mount {spec!r}: bind mounts are disabled on "
                "this deploy (KLANGKD_ALLOWED_MOUNT_ROOTS is unset); "
                "only named volumes may be mounted"
            )
        resolved = os.path.realpath(source)
        if _resolved_under_root(resolved, roots):
            return None
        allowed = ", ".join(roots)
        return (
            f"Invalid mount {spec!r}: bind mount source must be "
            f"under an allowed root ({allowed})"
        )

    def validate_mounts(self, mounts: list[str]) -> str | None:
        """Validate a list of mount specs. Returns first error or None."""
        for spec in mounts:
            error = self.validate_mount_spec(spec)
            if error:
                return error
        return None

    def _parse_idle_timeout(self) -> tuple[int, int]:
        default = 60 * 60
        env_val = self.app.state.settings.idle_timeout_seconds
        if env_val is not None:
            try:
                timeout = int(env_val)
            except ValueError:
                logger.warning(
                    "KLANGKD_IDLE_TIMEOUT_SECONDS=%r is not a valid integer, "
                    "using default %d",
                    env_val,
                    default,
                )
                timeout = default
        else:
            timeout = default
        interval = max(10, min(60, timeout // 3))
        return timeout, interval

    def ports_per_workspace_cap(self) -> int:
        """Server-wide ceiling on hosted-app ports per workspace."""
        # int-typed + validated at construction since #2603; None
        # (explicitly emptied) means the default. A legitimate 0 (which
        # disables hosted ports) must not be swallowed by an `or` — test
        # against None explicitly.
        raw = self.app.state.settings.hosted_ports_per_workspace
        if raw is None:
            return DEFAULT_PORTS_PER_WORKSPACE
        return raw

    def set_idle_timeout(self, seconds: int) -> None:
        """Set the global idle timeout (replaces api mutating module globals)."""
        self.idle_timeout_seconds = seconds
        self.check_interval_seconds = max(10, min(60, seconds // 3))

    # --- Service-session locks (#1188, #1478) ---
    # The per-container firing-lock dict lives here on the registry. It used
    # to live at module scope in terminal.py (terminal.py couldn't import
    # container — circular); #1477 removed that constraint by threading
    # app_state through ensure_service_session, so the registry now owns the
    # dict and terminal reaches it via app_state.container_registry.

    def get_service_session_lock(self, container_id: str) -> asyncio.Lock:
        """Get or create the per-container lock for service-command firing."""
        if container_id not in self._service_session_locks:
            self._service_session_locks[container_id] = asyncio.Lock()
        return self._service_session_locks[container_id]

    def clear_service_session_lock(self, container_id: str) -> None:
        """Drop the per-container firing lock for a torn-down container.

        Called from container teardown so the lock dict does not grow
        unbounded with container churn. Safe to call when no lock exists
        for the id.
        """
        self._service_session_locks.pop(container_id, None)

    def prune_service_session_locks(
        self, active_container_ids: set[str]
    ) -> int:
        """Remove lock entries for containers no longer tracked (#1351).

        Bounds the ``_service_session_locks`` dict against unbounded growth
        from container churn: explicit :func:`clear_service_session_lock`
        calls cover the normal teardown path, but a racing re-bind in
        ``stop_and_remove_container`` can leave an entry whose container is
        gone. This opportunistic sweep removes any entry whose container id
        is no longer in *active_container_ids*.

        Entries whose lock is currently held are skipped: recreating a fresh
        ``asyncio.Lock`` for an in-flight service-command fire would not
        serialize against the held one, reopening the duplicate-window race
        the lock exists to prevent. Returns the number of entries pruned.
        """
        stale = [
            cid
            for cid, lock in self._service_session_locks.items()
            if cid not in active_container_ids and not lock.locked()
        ]
        for cid in stale:
            del self._service_session_locks[cid]
        return len(stale)

    def mark_service_fire_pending(self, container_id: str) -> None:
        """Record a half-completed service-command fire (#2740).

        Set when the ``service-cmd`` window exists but the command may
        never have been typed into it (killed/timed-out send-keys whose
        kill-window cleanup also failed, or a cancellation mid-sequence).
        The next :meth:`terminal.ensure_service_session` call sees the
        window plus this flag and retries only the send, instead of
        suppressing the fire forever on the window-exists check.
        """
        self._service_fire_pending.add(container_id)

    def clear_service_fire_pending(self, container_id: str) -> None:
        """Drop the pending-fire marker (fire/retry/cleanup succeeded,
        or the container was torn down). Safe when not set."""
        self._service_fire_pending.discard(container_id)

    def service_fire_pending(self, container_id: str) -> bool:
        """True if a service-command fire is awaiting a send retry."""
        return container_id in self._service_fire_pending

    def _get_workspace_lock(self, workspace_id: str) -> asyncio.Lock:
        """Get or create a per-workspace lock for container operations."""
        if workspace_id not in self._workspace_locks:
            self._workspace_locks[workspace_id] = asyncio.Lock()
        return self._workspace_locks[workspace_id]

    def workspace_operation_in_flight(self, workspace_id: str) -> bool:
        """True while a start/stop holds this workspace's lock (#2527).

        The signal the memory-pressure evictor uses to skip a workspace
        whose container is mid-(re)create: the container is tracked from
        ``podman create`` but its WS subscriber only registers after
        ``container_ready``, so without this a connecting workspace is
        briefly eviction-eligible and its fresh container is stopped
        under the connecting client.
        """
        lock = self._workspace_locks.get(workspace_id)
        return lock is not None and lock.locked()

    async def _instance_volumes(self) -> list[dict]:
        """This instance's managed volumes; [] when the list fails
        (best-effort callers log-and-continue)."""
        try:
            return await self.app.state.podman.list_volumes(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
        except (podman.PodmanError, OSError) as e:
            logger.warning("volume list failed: %s", e)
            return []

    async def _try_remove_volume(self, name: str, context: str) -> bool:
        """Best-effort remove; a refusal is logged (the orphan sweep
        retries later), never raised."""
        try:
            await self.app.state.podman.remove_volume(name)
            return True
        except podman.PodmanError as e:
            logger.warning(
                "%s: removing volume %s failed: %s", context, name, e
            )
            return False

    async def remove_workspace_volumes(self, workspace_id: str) -> int:
        """Remove every volume owned by *workspace_id* (best-effort).

        Volumes are workspace-owned (#3153), so the workspace delete
        cascade calls this: a volume whose workspace is gone can never
        be mounted again. A removal that fails (e.g. the volume is
        briefly still attached to a dying container) is logged and
        skipped — the periodic orphan sweep reclaims stragglers.
        Returns the number of volumes removed.
        """
        removed = 0
        for volume in await self._instance_volumes():
            labels = volume.get("Labels") or {}
            if labels.get("klangk.workspace-id") != workspace_id:
                continue
            if await self._try_remove_volume(
                volume["Name"], f"volume cleanup for workspace {workspace_id}"
            ):
                removed += 1
        return removed

    def _orphan_ws_id(self, volume: dict, existing: set[str]) -> str | None:
        """The volume's workspace id when it names a workspace with no
        row; None when the volume is live or has no workspace label
        (label-less volumes are not ours to judge)."""
        ws_id = (volume.get("Labels") or {}).get("klangk.workspace-id")
        if ws_id and ws_id not in existing:
            return ws_id
        return None

    async def sweep_orphaned_volumes(self) -> int:
        """Remove managed volumes whose workspace no longer exists.

        The delete-time cascade is best-effort, so a crash mid-delete
        (or a removal refused by an attached container) can strand a
        workspace-owned volume that — by design — no other workspace
        can ever mount. This sweep reclaims them (#3153).
        Returns the number of volumes removed.
        """
        volumes = await self._instance_volumes()
        try:
            existing = await self.app.state.workspaces.existing_workspace_ids()
        except Exception as e:
            logger.warning("Orphan volume sweep skipped: %s", e)
            return 0
        removed = 0
        for volume in volumes:
            if self._orphan_ws_id(volume, existing) is None:
                continue
            if await self._try_remove_volume(
                volume["Name"], "Orphan volume sweep"
            ):
                removed += 1
        return removed

    def prune_workspace_registry_entries(self, workspace_id: str) -> None:
        """Drop the per-workspace lock and stop-epoch entries (#2912).

        Called **only** from workspace *delete* paths (the workspace
        delete endpoint and the admin user-delete cascade), after the
        DB row is gone: no *new* start can pass its existence check,
        so the #1258 reason the lock entry outlives every stop (a
        racing start must serialize against the in-flight stop's lock
        object) no longer applies to fresh starts. A start that
        already passed its DB check before the delete committed may
        still hold or wait on the popped lock — the pop removes the
        dict entry, not the lock object, so the holder keeps a working
        lock; the orphaned container it may leave behind is the
        pre-existing delete-vs-start race outcome, not a new one.

        The stop-epoch pop resets the workspace to the implicit 0
        (``get``'s default, i.e. "never stopped"). The #2625 crash
        *entry* guards compare epochs by strict inequality and run
        before any crash-teardown bump, so a delete mid-detection still
        reads as "changed" and skips. The *post-teardown* guard
        (#3074) allows the crash teardown's own single bump, and a
        popped epoch (0) does not read as moved — so a delete landing
        during death handling's awaits can reach the restart scheduler;
        the scheduled task's workspace-row check
        (``CrashRecoveryMonitor._restartable_workspace``) abandons it,
        because the prune callers run after the DB row is gone. The
        ``crash.clear`` below drops any retained crash tracker and
        cancels a pending restart at delete time; the narrow window
        where a delete completes *between* the crash teardown and
        ``_finalize_death`` may still leave one stale tracker entry
        until process exit (invisible — no row). Bounds
        ``_workspace_locks`` and ``stop_epoch`` against unbounded
        growth from workspace create/delete churn, mirroring how
        :meth:`prune_service_session_locks` bounds the per-container
        lock dict (#1351). A no-op when neither entry exists (the
        workspace was never started in this process).
        """
        self._workspace_locks.pop(workspace_id, None)
        self.stop_epoch.pop(workspace_id, None)
        # Delete-time crash cleanup (#3074 review): the workspace row is
        # gone, so any crash state or pending restart for it is obsolete.
        self.crash.clear(workspace_id)

    # --- State tracking ---

    def track_activity(
        self,
        container_id: str,
        workspace_id: str,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
        per_handle_home: bool | None = None,
    ) -> None:
        state = self.states.get(workspace_id)
        was_new = state is None
        if was_new:
            state = ContainerState(workspace_id, container_id, self.app)
            self.states[workspace_id] = state
        else:
            # Remove old reverse mapping if container changed
            if state.container_id != container_id:
                self._cid_to_wsid.pop(state.container_id, None)
            state.container_id = container_id
        self._cid_to_wsid[container_id] = workspace_id
        state.record_activity()
        self._apply_track_metadata(
            state,
            health_check=health_check,
            owner_id=owner_id,
            setup_state=setup_state,
            per_handle_home=per_handle_home,
        )
        if was_new:
            self._notify_status_changed(workspace_id, True)

    def _apply_track_metadata(
        self,
        state: ContainerState,
        *,
        health_check: str | None,
        owner_id: str | None,
        setup_state: str | None,
        per_handle_home: bool | None,
    ) -> None:
        """Refresh health-monitoring metadata (#1015). Always refreshed
        so a config change (or a recreated container) is picked up."""
        state.health_check = health_check
        if owner_id is not None:
            state.owner_id = owner_id
        if setup_state is not None:
            state.setup_state = setup_state
        if per_handle_home is not None:
            state.per_handle_home = per_handle_home

    def record_activity(self, container_id: str) -> None:
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            state = self.states.get(ws_id)
            if state:
                state.record_activity()

    def mark_service_started(self, container_id: str) -> None:
        """Reset the startup-grace anchor for a container's service.

        Called by ``terminal.ensure_service_session`` right after it
        launches the service command, so the health monitor's grace
        window is measured from the real start of the service.  No-op
        if the container isn't tracked (e.g. the service session fired
        before the state was registered).
        """
        ws_id = self._cid_to_wsid.get(container_id)
        if ws_id:
            state = self.states.get(ws_id)
            if state:
                state.mark_service_started()

    def get_state(self, workspace_id: str) -> ContainerState | None:
        return self.states.get(workspace_id)

    def set_on_workspace_killed(self, callback) -> None:
        self.on_workspace_killed = callback

    def set_on_container_status_changed(self, callback) -> None:
        self.on_container_status_changed = callback

    def _notify_status_changed(self, workspace_id: str, running: bool) -> None:
        if self.on_container_status_changed:
            state = self.states.get(workspace_id)
            started_at = state.service_started_at if state else None
            self.on_container_status_changed(workspace_id, running, started_at)

    async def remove_state(
        self, workspace_id: str, *, expect_container_id: str | None = None
    ) -> None:
        """Remove tracked state for a workspace.

        Serialized under the per-workspace lock (the same one
        :meth:`start_container` holds) so a racing start cannot observe a
        half-cleaned registry (#1258). The per-workspace lock entry is
        deliberately *not* removed here -- see :meth:`stop_and_remove_container`
        for why popping it would reopen the race.

        *expect_container_id* is the re-bind guard (#331): when the caller
        names the container whose state it believes it is removing (a
        death/stop teardown keyed to a specific dead container), the state
        is popped only if it still belongs to that container. A racing
        user-driven start may have removed the dead container and
        re-bound the workspace to a fresh one (``start_container`` ->
        ``handle_existing_container`` removes a stopped container with a
        direct ``podman rm`` -- never marking ``stopping`` or bumping the
        stop epoch -- then ``track_activity`` re-binds the state) while
        this teardown was between its guard checks and the lock; popping
        the fresh state would orphan a RUNNING container. The check runs
        under the workspace lock, so it is authoritative against
        ``start_container``'s re-track: whichever side acquires the lock
        first completes atomically.
        """
        async with self._get_workspace_lock(workspace_id):
            state = self.states.get(workspace_id)
            if (
                expect_container_id is not None
                and state is not None
                and state.container_id != expect_container_id
            ):
                # Re-bound to a fresh container by a racing start: the
                # live state is not ours to remove.
                return
            state = self.states.pop(workspace_id, None)
            if state:
                self._cid_to_wsid.pop(state.container_id, None)
                # Drop the per-container service-firing lock (#1188), then
                # sweep any other entries orphaned by container churn (#1351).
                self.clear_service_session_lock(state.container_id)
                self.clear_service_fire_pending(state.container_id)
                self.prune_service_session_locks(set(self._cid_to_wsid))

    # --- Proxy: PortAllocator ---

    @property
    def port_lock(self) -> asyncio.Lock:
        return self.ports.port_lock

    async def allocate_ports(self, workspace_id: str, count: int) -> list[int]:
        return await self.ports.allocate_ports(workspace_id, count)

    async def get_workspace_ports(self, workspace_id: str) -> list[int]:
        return await self.ports.get_workspace_ports(workspace_id)

    # --- Proxy: BrowserRouter ---

    @property
    def browser_routes(self) -> dict:
        return self.browsers.browsers

    def register_browser(
        self, browser_id: str, workspace_id: str, sock: object
    ) -> None:
        self.browsers.register_browser(browser_id, workspace_id, sock)

    def resolve_browser(self, browser_id: str) -> tuple[str, object] | None:
        return self.browsers.resolve_browser(browser_id)

    def revoke_workspace_browsers(self, workspace_id: str) -> None:
        self.browsers.revoke_workspace_browsers(workspace_id)

    def revoke_browser(self, sock: object) -> None:
        self.browsers.revoke_browser(sock)

    # --- Proxy: IdleMonitor ---

    @property
    def cleanup_task(self) -> asyncio.Task | None:
        return self.idle.cleanup_task

    @cleanup_task.setter
    def cleanup_task(self, value: asyncio.Task | None) -> None:
        self.idle.cleanup_task = value

    @property
    def cleanup_wake(self) -> asyncio.Event | None:
        return self.idle.cleanup_wake

    @cleanup_wake.setter
    def cleanup_wake(self, value: asyncio.Event | None) -> None:
        self.idle.cleanup_wake = value

    def get_cleanup_wake(self) -> asyncio.Event:
        return self.idle.get_cleanup_wake()

    def on_idle_stop(self, workspace_id: str, callback) -> None:
        self.idle.on_idle_stop(workspace_id, callback)

    def remove_idle_callback(self, workspace_id: str, callback) -> None:
        self.idle.remove_idle_callback(workspace_id, callback)

    def set_workspace_idle_timeout(
        self, workspace_id: str, seconds: int
    ) -> None:
        self.idle.set_workspace_idle_timeout(workspace_id, seconds)

    def get_workspace_idle_timeout(self, workspace_id: str) -> int:
        return self.idle.get_workspace_idle_timeout(workspace_id)

    async def cleanup_idle_containers(self) -> None:
        await self.idle.cleanup_idle_containers()

    def start_cleanup_loop(self) -> None:
        self.idle.start_cleanup_loop()

    # --- Proxy: HealthMonitor ---

    @property
    def health_task(self) -> asyncio.Task | None:
        return self.health.health_task

    def start_health_loop(self) -> None:
        self.health.start_health_loop()

    # --- Container lifecycle ---

    async def bringup(
        self,
        workspace_id: str,
        container_id: str,
        service_command: str | None,
        setup_state: str | None,
    ) -> None:
        """Populate the shared home and fire the service command.

        Called at the single choke point: every freshly-created container
        (the tail of :meth:`start_container`).

        The shared home (``/home/klangk``) is ensured + populated HERE,
        under both layouts (#2717): the image has no ``/home/klangk``
        (uid 1000's passwd home is ``/home`` itself), and the home volume
        mounts at ``/home`` shadowing the image's own content — so a
        fresh volume has nothing at ``/home/klangk`` (no
        ``.profile``/``.bashrc``) until this writes it. Sequenced BEFORE
        ``ensure_service_session`` -- the service session's login shell
        sources ``/home/klangk/.profile`` (#2169's environment-parity
        motivation) -- and before any user's first shell, including on
        the boot/autostart path where no user ever connects first. For
        pre-#2718 per-user volumes this materializes ``/home/klangk``
        where it never existed; orphaned ``.users/{AGENT_USER_ID}``
        agent dirs are simply abandoned. No layout provisions an
        agent-private home anymore. The sandbox ``setup.sh`` contract
        (``KLANGKWS_AGENT_HOME``, baked as the same constant in
        :meth:`.spec.build_env`) keeps working under both layouts and is
        a no-op under shared.

        The service command itself is idempotent via
        :meth:`terminal.ensure_service_session` (per-container lock +
        window-exists check), so calling this on every fresh create is safe:
        after the first fire it is a no-op. The create-time deferral for
        workspaces whose ``setup.sh`` has not run yet is handled by gating
        on ``setup_state`` -- the CLI sandbox driver marks such workspaces
        ``"pending"`` at create, and the fire lands later once setup
        completes and the WS connect path runs.
        """
        await self.app.state.workspaces.ensure_shared_home(
            workspace_id, container_id
        )
        if not service_command:
            return
        await self.app.state.terminal.ensure_service_session(
            container_id,
            service_command,
            setup_state=setup_state,
        )

    async def start_container(
        self, spec: ContainerStartSpec
    ) -> tuple[str, str]:
        """Start (or restart) a Pi container for a workspace.

        Returns (container_id, status) where status is one of:
        'connected' (already running), 'restarted', or 'created'.

        Serialized per workspace so concurrent WebSocket connections
        don't race to create the same container. All parameters travel
        on the :class:`ContainerStartSpec` (#2566) shared with
        :meth:`start_container_inner`, so adding a start parameter is a
        single spec field instead of a two-signature edit.

        Applies the per-workspace idle-timeout override from the
        settings bag (#864) at this single start choke point, so
        EVERY start path gets it -- a workspace started by a
        WebSocket connect (the normal web-UI flow,
        wshandler.connection) lands here just like POST /start
        (Workspaces.start_workspace), and previously only the
        latter applied the bag, so a WS-started workspace silently
        ignored its override (found by the idle fuzz harness,
        #2514). Only when actually declared: an absent key leaves
        the container state's ``idle_timeout`` at None so
        ``get_idle_timeout()`` lazily follows the live deploy
        default (a SIGHUP settings reload stays effective for
        running containers). The auto_start boot path pins 0 after
        this returns, so a service workspace never idles out
        regardless of its bag (#1244).
        """
        async with self._get_workspace_lock(spec.workspace_id):
            # Crash bookkeeping reset (#2524): a user-driven start gives
            # the workspace a fresh retry slate. The monitor's own restart
            # runs inside a pending task and is exempt (task identity —
            # resetting there would erase the counter it is counting on).
            self.crash.on_start(spec.workspace_id)
            # Materialize <home>/klangk on the HOST before podman start:
            # the image WORKDIR is /home/klangk (#2725) but the home volume
            # mounts at /home, so a missing dir means podman either
            # auto-creates it as container-root (unwritable) or — for a
            # legacy dangling `klangk` symlink — refuses to start (chdir
            # ENOENT). Idempotent; the skel populate itself stays in
            # bringup where a container already exists to run it in.
            await self.app.state.workspaces.ensure_shared_home_dir(
                spec.workspace_id
            )
            result = await self.start_container_inner_or_audited(spec)
            bag = spec.workspace_settings or {}
            if "idle_timeout" in bag:
                self.idle.set_workspace_idle_timeout(
                    spec.workspace_id, bag["idle_timeout"]
                )
            return result

    async def start_container_inner_or_audited(
        self, spec: ContainerStartSpec
    ) -> tuple[str, str]:
        """Run the under-lock start, backstopping the audit row (#2915
        review; fail-closed pre-write #3154).

        If the inner start raises *after* the container was created and
        tracked (e.g. the bringup exec fails), the container is real and
        running — the crash monitor can later tear it down, and a stop
        row with no matching start row would be a lie by omission. On
        failure a start row is recorded (best-effort) for the tracked
        container, then the exception propagates unchanged. Failures
        before any container exists (drain gate, admission, port
        allocation) leave no state and record nothing — no container,
        no start.

        With ``audit_fail_closed`` and an interactive cause, the start
        row is pre-written BEFORE the attempt (#3154): a write failure
        raises :class:`AuditWriteError` out of this method before any
        container is created, and the row is then finalized with the
        podman ids on success or retracted when no transition happened
        ('connected', or a failure before any container existed).
        """
        pending = await self._prewrite_start_event(spec)
        try:
            result = await self.start_container_inner(spec)
        except BaseException:
            state = self.states.get(spec.workspace_id)
            if state is not None and state.container_id:
                await self._settle_failed_start(
                    spec, pending, state.container_id
                )
            else:
                await self.retract_prewritten_event(pending)
            raise
        # Success: a real transition (created / restarted) is recorded;
        # 'connected' attached to an already-running container and is no
        # transition at all.
        if result[1] == "connected":
            await self.retract_prewritten_event(pending)
            return result
        await self._settle_completed_start(spec, pending, result[0])
        return result

    async def _prewrite_start_event(
        self, spec: ContainerStartSpec
    ) -> int | None:
        """Audit-before-act start row (#3154) — None unless the
        interactive fail-closed gate applies."""
        if (
            not self.audit_fail_closed
            or spec.audit_cause not in INTERACTIVE_START_CAUSES
        ):
            return None
        return await self.prewrite_audit_event(
            spec.workspace_id,
            None,
            EVENT_START,
            cause=spec.audit_cause,
            actor_id=spec.audit_actor_id,
        )

    async def _settle_completed_start(
        self, spec: ContainerStartSpec, pending: int | None, container_id: str
    ) -> None:
        """Post-start audit settlement: finalize the pre-written row
        with the podman ids (#3154), or record the transition
        best-effort (the non-fail-closed path)."""
        if pending is not None:
            await self._finalize_prewritten_event(
                pending,
                container_id=container_id,
                network_namespace=self._ws_netns_owner.get(spec.workspace_id),
            )
            return
        await self.record_container_event(
            spec.workspace_id,
            container_id,
            EVENT_START,
            cause=spec.audit_cause,
            actor_id=spec.audit_actor_id,
        )

    async def _settle_failed_start(
        self, spec: ContainerStartSpec, pending: int | None, container_id: str
    ) -> None:
        """Failed-start backstop (#2915 review): a tracked container is
        real, so its start row must exist — finalize the pre-written row
        (#3154, with the live netns owner for row-shape parity with the
        best-effort path) or record one best-effort. "Tracked" means a
        ``states`` entry seen by THIS attempt: the guarded routes guard
        against a pre-existing entry *before* taking the per-workspace
        lock, so except for a concurrent start racing in through that
        window (same false-attribution class as the legacy best-effort
        arm, #2915) the id belongs to a container this attempt made
        real."""
        if pending is not None:
            await self._finalize_prewritten_event(
                pending,
                container_id=container_id,
                network_namespace=self._ws_netns_owner.get(spec.workspace_id),
            )
            return
        await self.record_container_event(
            spec.workspace_id,
            container_id,
            EVENT_START,
            cause=spec.audit_cause,
            actor_id=spec.audit_actor_id,
        )

    async def record_container_event(
        self,
        workspace_id: str,
        container_id: str | None,
        event: str,
        *,
        cause: str,
        actor_id: str | None = None,
        network_namespace: str | None = None,
        container_role: str = ROLE_WORKSPACE,
    ) -> None:
        """Best-effort container_events write (#2915).

        Auditing must never fail the start/stop path it annotates: a
        DB error is logged, counted in ``audit_write_failures`` (the
        /audit signal, #3154), and swallowed. Netns defaults to the
        live sidecar mapping when the caller didn't capture one
        (workspace starts); sidecar rows own their netns, so theirs
        stays NULL. The fail-closed interactive paths use the
        audit-before-act pre-write helpers below instead of / in
        addition to this.
        """
        if container_role == ROLE_WORKSPACE:
            netns = network_namespace or self._ws_netns_owner.get(workspace_id)
        else:
            netns = None
        try:
            await self.app.state.model.container_events.record(
                workspace_id,
                event,
                cause,
                actor_id=actor_id,
                container_id=container_id,
                network_namespace=netns,
                container_role=container_role,
            )
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            self._count_audit_write_failure()
            logger.warning(
                "container_events audit write failed for %s (%s): %s",
                workspace_id[:8],
                event,
                e,
            )

    @property
    def audit_fail_closed(self) -> bool:
        """Live ``KLANGKD_AUDIT_FAIL_CLOSED`` read (#3154) — a SIGHUP
        settings reload applies to transitions started after it."""
        return self.app.state.settings.audit_fail_closed

    def _count_audit_write_failure(self) -> None:
        """Bump the /audit audit-write-failure counter (#3154)."""
        self.audit_write_failures += 1

    async def prewrite_audit_event(
        self,
        workspace_id: str,
        container_id: str | None,
        event: str,
        *,
        cause: str,
        actor_id: str | None = None,
        network_namespace: str | None = None,
        container_role: str = ROLE_WORKSPACE,
    ) -> int:
        """Audit-before-act container_events write (#3154).

        Writes the row BEFORE the transition it will annotate and
        returns its id, so the caller can refuse the transition up
        front when the audit trail is unavailable — never
        start-then-rollback. Only called on the interactive paths
        (``INTERACTIVE_START_CAUSES`` / ``INTERACTIVE_STOP_CAUSES``)
        with ``audit_fail_closed`` on: a write failure raises
        :class:`AuditWriteError` (the API layer maps it to a 503) after
        being counted and logged. The id feeds
        ``_finalize_prewritten_event`` (the transition happened — fill
        in the podman ids) or ``retract_prewritten_event`` (it did
        not — delete the row).
        """
        try:
            return await self.app.state.model.container_events.record(
                workspace_id,
                event,
                cause,
                actor_id=actor_id,
                container_id=container_id,
                network_namespace=network_namespace,
                container_role=container_role,
            )
        except Exception as e:  # noqa: BLE001 — counted, then refused
            self._count_audit_write_failure()
            logger.warning(
                "container_events audit prewrite failed for %s (%s): %s",
                workspace_id[:8],
                event,
                e,
            )
            raise AuditWriteError(
                f"container_events audit write failed for {workspace_id[:8]}"
                f" ({event}); refusing the {cause} transition"
                " (KLANGKD_AUDIT_FAIL_CLOSED)"
            ) from e

    async def _finalize_prewritten_event(
        self,
        event_id: int,
        *,
        container_id: str | None = None,
        network_namespace: str | None = None,
    ) -> None:
        """Fill the post-transition podman ids into a pre-written row
        (#3154). Best-effort: the transition already happened, so a
        failure is counted and logged, never fatal — the row stands as
        the record of an event that did occur, just without the ids."""
        try:
            await self.app.state.model.container_events.finalize_event(
                event_id,
                container_id=container_id,
                network_namespace=network_namespace,
            )
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            self._count_audit_write_failure()
            logger.warning(
                "container_events audit finalize failed for row %s: %s",
                event_id,
                e,
            )

    async def retract_prewritten_event(self, event_id: int | None) -> None:
        """Delete a pre-written row whose transition never happened
        (#3154) — no-op for None (the non-fail-closed paths). Best-effort:
        the refusal/failure already happened; if the delete fails the
        row stays as an over-record of an attempted transition, which
        is the safe direction for an audit trail.

        Route-facing (#3154 review): the /stop route calls this when a
        step BETWEEN its pre-write and the stop raises (the
        notify_workspace_killed fan-out does DB reads), so no phantom
        stop row survives a stop that never began."""
        if event_id is None:
            return
        try:
            await self.app.state.model.container_events.retract_event(event_id)
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            self._count_audit_write_failure()
            logger.warning(
                "container_events audit retract failed for row %s: %s",
                event_id,
                e,
            )

    async def handle_existing_container(
        self,
        existing_container_id: str,
        workspace_id: str,
        t_start: float,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
        per_handle_home: bool = False,
    ) -> tuple[str, str] | None:
        """Check an existing container and reuse/remove it.

        Returns ``(container_id, "connected")`` if the container is
        still running, or ``None`` if it was removed (or not found)
        and a new one should be created.
        """
        info = await self.app.state.podman.inspect_container(
            existing_container_id
        )
        t_inspect = time.monotonic()
        logger.info(
            "workspace-open: check if old container still exists "
            "(podman inspect): %.3fs",
            t_inspect - t_start,
        )
        if info is None:
            logger.info(
                "Could not find container %s, creating new one",
                existing_container_id,
            )
            # #2676: the id can be stale (an unclean host shutdown/restart,
            # or another connection recreated the container without this
            # caller's DB snapshot updating). Reconcile against live podman
            # state by label before creating: a running workspace container
            # is adopted exactly like a matching id above (this is the path
            # a reconnect takes and why it self-heals), and a stopped one is
            # removed. Without this, the create path would race the live
            # pair — the sidecar pre-remove is refused with "dependent
            # containers" while its workspace container is attached.
            adopted = await self._adopt_labeled_container(
                workspace_id,
                t_start,
                health_check=health_check,
                owner_id=owner_id,
                setup_state=setup_state,
                per_handle_home=per_handle_home,
            )
            if adopted is not None:
                return adopted
            return None
        if info["State"]["Running"]:
            # FIPS gate on adoption (#2626 review): a container started
            # before the mode was enabled (or left adoptable by a
            # best-effort-failed startup reap) must not serve unprobed.
            # Runs only with the mode on, and only on this adopt path
            # (fresh creates are gated in create_and_start). Raises on
            # failure after removing the container + state — the caller
            # (start_container) surfaces it; the workspace is not left
            # half-tracked.
            if self.app.state.settings.fips_mode:
                await self._fips_gate(workspace_id, existing_container_id)
            self.track_activity(
                existing_container_id,
                workspace_id,
                health_check=health_check,
                owner_id=owner_id,
                setup_state=setup_state,
                per_handle_home=per_handle_home,
            )
            logger.info(
                "workspace-open: DONE — container was already running, "
                "no work needed: %.3fs",
                time.monotonic() - t_start,
            )
            return existing_container_id, "connected"
        await self.app.state.podman.remove_container(existing_container_id)
        logger.info(
            "workspace-open: delete old stopped container (podman rm): %.3fs",
            time.monotonic() - t_inspect,
        )
        logger.info(
            "Removed stopped container %s for workspace %s, will recreate",
            existing_container_id,
            workspace_id,
        )
        return None

    async def _adopt_labeled_container(
        self,
        workspace_id: str,
        t_start: float,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
        per_handle_home: bool = False,
    ) -> tuple[str, str] | None:
        """Adopt a live workspace container found by label (#2676).

        The id passed to :meth:`start_container` can be stale (an unclean
        host shutdown/restart, or another connection recreated the container
        without the caller's snapshot updating). This reconcile step looks
        up the workspace's container by its ``klangk.workspace`` label —
        the same identity the reaper and sidecar sweeps key on — and:

        - running  -> adopt it: same semantics as the matching-id branch of
          :meth:`handle_existing_container` (FIPS gate, re-track), plus the
          DB ``container_id`` is re-persisted so the staleness heals for
          every later caller;
        - stopped  -> remove it (so the create path's sidecar pre-remove
          can't be refused for a still-attached dependent), then fall
          through to create.

        Returns ``(container_id, "connected")`` on adoption, or ``None``
        when no labeled container exists (a plain fresh create).
        """
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.workspace={workspace_id}"
            )
        except (podman.PodmanError, OSError, ValueError):
            # Best-effort reconcile: a failed ps must not break a start
            # that may legitimately create a fresh container.
            return None
        for c in containers:
            ident = _labeled_workspace_ident(c)
            if ident is None:
                continue
            if str(c.get("State", "")).lower() != "running":
                await self._remove_stopped_labeled(workspace_id, ident)
                continue
            return await self._adopt_running_labeled(
                workspace_id,
                t_start,
                ident,
                health_check=health_check,
                owner_id=owner_id,
                setup_state=setup_state,
                per_handle_home=per_handle_home,
            )
        return None

    async def _remove_stopped_labeled(
        self, workspace_id: str, ident: str
    ) -> None:
        """Remove a stopped labeled workspace container so the create
        path's sidecar pre-remove can't be refused for a still-attached
        dependent."""
        await self.app.state.podman.remove_container(ident)
        logger.info(
            "workspace-open: removed stopped labeled container %s "
            "for workspace %s, will recreate",
            ident[:12],
            workspace_id[:8],
        )

    async def _adopt_running_labeled(
        self,
        workspace_id: str,
        t_start: float,
        ident: str,
        *,
        health_check: str | None,
        owner_id: str | None,
        setup_state: str | None,
        per_handle_home: bool,
    ) -> tuple[str, str]:
        """Adopt the running labeled container — exactly what a fresh
        connect does with a current id, and why a reconnect after a
        failed restart self-heals today (#2676)."""
        if self.app.state.settings.fips_mode:
            await self._fips_gate(workspace_id, ident)
        await self.app.state.model.workspaces.update_workspace_container(
            workspace_id, ident
        )
        self.track_activity(
            ident,
            workspace_id,
            health_check=health_check,
            owner_id=owner_id,
            setup_state=setup_state,
            per_handle_home=per_handle_home,
        )
        logger.info(
            "workspace-open: DONE — adopted running labeled container "
            "%s for stale-id workspace %s: %.3fs",
            ident[:12],
            workspace_id[:8],
            time.monotonic() - t_start,
        )
        return ident, "connected"

    async def _reconcile_ports(
        self, workspace_id: str, num_ports: int
    ) -> list[int]:
        """Allocate or trim host ports under the port lock.

        ``num_ports`` is clamped down to the server-wide cap
        (``KLANGKD_HOSTED_PORTS_PER_WORKSPACE``). At cap 0 every workspace
        releases all of its allocations; the returned empty list then
        suppresses the hosting env in :func:`klangk.container.spec.build_env`
        (#1237).
        """
        num_ports = min(num_ports, self.ports_per_workspace_cap())
        async with self.port_lock:
            host_ports = await self.app.state.model.ports.get_workspace_ports(
                workspace_id
            )
            if len(host_ports) < num_ports:
                new_ports = (
                    await self.app.state.model.ports.find_and_allocate_ports(
                        workspace_id,
                        num_ports - len(host_ports),
                        self.port_range_start,
                    )
                )
                host_ports.extend(new_ports)
            elif len(host_ports) > num_ports:
                excess = host_ports[num_ports:]
                await self.app.state.model.ports.remove_port_allocations(
                    workspace_id, excess
                )
                host_ports = host_ports[:num_ports]
        return host_ports

    async def create_and_start(
        self,
        container_name: str,
        resolved_image: str,
        workspace_id: str,
        publish: list[tuple[int, int]],
        allow_sudo: bool,
        create_kwargs: dict,
        *,
        health_check: str | None = None,
        owner_id: str | None = None,
        setup_state: str | None = None,
        per_handle_home: bool = False,
    ) -> str:
        """Create the container, persist it, start it, and configure it.

        Handles port-conflict retries by removing stale containers
        that hold conflicting ports.
        """
        t_create = time.monotonic()
        cid = await self.app.state.podman.create_container(
            container_name, resolved_image, **create_kwargs
        )
        logger.info(
            "workspace-open: create container image (podman create): %.3fs",
            time.monotonic() - t_create,
        )
        await self.app.state.model.workspaces.update_workspace_container(
            workspace_id, cid
        )
        self.track_activity(
            cid,
            workspace_id,
            health_check=health_check,
            owner_id=owner_id,
            setup_state=setup_state,
            per_handle_home=per_handle_home,
        )
        # --hooks-dir is a podman global flag that must be present on the
        # start invocation — podman does not persist it from create. No
        # caller sets create_kwargs["hooks_dir"] since the egress filter
        # moved into the network sidecar (#2255); the passthrough is kept
        # for any future --hooks-dir consumer.
        _hooks_dirs = create_kwargs.get("hooks_dir")
        t_podman_start = time.monotonic()
        await self.start_with_port_conflict_retry(
            cid, publish, container_name, hooks_dir=_hooks_dirs
        )
        logger.info(
            "workspace-open: boot container (podman start): %.3fs",
            time.monotonic() - t_podman_start,
        )

        # Backend gateway: the network sidecar statically allow-lists
        # host.containers.internal on the klangkd backend port in its
        # entrypoint (KLANGKNETWORK_EGRESS_BACKEND_PORT) — it's a
        # /etc/hosts entry the FQDN proxy can't learn. No post-start
        # allow step is needed (#2255).

        # Configure sudo inside the container.
        if allow_sudo:
            sudoers_rule = "klangk ALL=(ALL) NOPASSWD:ALL"
        else:
            sudoers_rule = "klangk ALL=(ALL) !ALL"
        await self.app.state.podman.exec_container(
            cid,
            ["klangk-configure-sudo", sudoers_rule],
            user="root",
        )

        # Write the workspace token so container processes can
        # authenticate without an env-var restart.
        workspace_token = self.app.state.auth.create_workspace_token(
            workspace_id
        )
        await self.app.state.terminal.set_workspace_token(cid, workspace_token)

        # Block until the entrypoint's one-time setup is done. ``podman
        # start`` returns when the entrypoint has *begun*, not finished;
        # the sentinel below is created only after the on-entrypoint hooks
        # complete. Waiting here means every caller of start_container —
        # terminals, exec, agent, health check — gets a genuine readiness
        # guarantee regardless of shell, closing the race that previously
        # only the in-bashrc gate covered (and only for bash).
        # Readiness budget is CI-aware too (#3064): this wait sits inside
        # the same bring-up chain as create/start, and a client POST that
        # can legally spend 240s on CI must not be capped by a flat 60s
        # here.
        await self.app.state.podman.wait_for_container_ready(
            cid, timeout=bringup_timeout(60.0, 240.0)
        )

        # FIPS enforcement (#2570, #2591): the gate runs at this single
        # create choke point (fail closed — see _fips_gate).
        await self._fips_gate(workspace_id, cid)

        return cid

    async def _fips_gate(self, workspace_id: str, cid: str) -> None:
        """Refuse a workspace container that cannot prove FIPS (#2570).

        With ``KLANGKD_FIPS_MODE`` on, a freshly-created container must
        pass the distro-agnostic probe (klangk.fips) before it is handed
        to any user; failure or non-verifiability fails CLOSED — the
        container is removed and the start raises, so a misbuilt image
        can never serve. No-op when the mode is off.

        Ordering (#2626 review): the gate runs after
        ``wait_for_container_ready`` (the probe needs ``podman exec``,
        which needs a started container) and BEFORE every user handoff
        — it is the last step of ``create_and_start``, so the create-
        time service-command fire (``bringup`` →
        ``ensure_service_session``, #1244) and any WS connect/
        terminal/exec that could fire one happen strictly after the
        gate. Residual exposure in the pre-gate window: published host
        ports are already bound, but the entrypoint's one-time setup
        binds nothing user-facing, and the only execs klangkd itself
        makes (sudo config, workspace token) are container-internal —
        a refused container briefly exists but never serves.

        Cleanup is inline (not stop_and_remove_container / remove_state,
        which take the workspace lock this path already holds —
        asyncio.Lock is not reentrant); the DB container_id going stale
        is fine: the next start's handle_existing_container treats an
        uninspectable container as recreate-me.
        """
        if not self.app.state.settings.fips_mode:
            return
        ok, detail = await fips_mod.probe_container(self.app.state.podman, cid)
        if not ok:
            # Expected-stop protocol (#2524/#2625): this removal is on
            # purpose. Marking stopping + bumping the stop epoch keeps
            # the crash monitor's sweep from misreading the in-flight
            # removal as an unexpected death (which would broadcast a
            # false death event and, with auto-restart on, schedule
            # create→probe→remove cycles ending in a bogus crash-loop —
            # #2626 review).
            self.stopping.add(workspace_id)
            self.stop_epoch[workspace_id] = (
                self.stop_epoch.get(workspace_id, 0) + 1
            )
            self.crash.on_expected_stop(workspace_id)
            try:
                await safe_remove(
                    self.app.state.podman,
                    cid,
                    what="non-FIPS workspace container",
                )
                state = self.states.get(workspace_id)
                if state is not None and state.container_id == cid:
                    self.states.pop(workspace_id, None)
                    self._cid_to_wsid.pop(cid, None)
                self.clear_service_session_lock(cid)
                self.clear_service_fire_pending(cid)
                self._notify_status_changed(workspace_id, False)
            finally:
                self.stopping.discard(workspace_id)
            raise podman.PodmanError(
                500,
                "KLANGKD_FIPS_MODE is enabled but the workspace "
                f"container {cid[:12]} failed its FIPS verification: "
                f"{detail}. The image must run an OpenSSL with the "
                "FIPS provider active (see docs/deployment/fips.md).",
            )

    @staticmethod
    def _is_port_conflict(exc: podman.PodmanError) -> bool:
        """True if the error indicates a port bind / allocation conflict."""
        if exc.status == 409:
            return True
        low = exc.message.lower()
        return "port" in low and "already" in low

    async def _resolve_port_conflict(
        self,
        cid: str,
        container_name: str,
        publish: list[tuple[int, int]],
        podman,
    ) -> None:
        """Remove stale containers and orphaned pasta processes
        holding conflicting ports."""
        logger.warning(
            "Port conflict starting %s, cleaning stale containers",
            container_name,
        )
        wanted_ports = _wanted_host_ports(publish)
        stale = await podman.list_containers(
            f"klangk.instance={self.app.state.util.instance_id()}"
        )
        for c in stale:
            stale_id = container_ident(c)
            if stale_id == cid:
                continue
            info = await podman.inspect_container(stale_id)
            if info is None:
                continue
            bound = host_bound_ports(info)
            if bound & wanted_ports:
                await remove_stale_container(
                    podman, stale_id, bound, wanted_ports
                )

    async def start_with_port_conflict_retry(
        self,
        cid: str,
        publish: list[tuple[int, int]],
        container_name: str,
        *,
        hooks_dir: list[str] | None = None,
    ) -> None:
        """Start a container, recovering from host-port bind conflicts (#2293).

        On a port-conflict ``PodmanError`` (a TOCTOU between the DB
        allocator's ``socket.bind`` probe and pasta's bind), remove stale
        instance containers holding the conflicting host ports
        (:meth:`_resolve_port_conflict`, which skips ``cid`` itself) and retry
        with back-off — ports may linger in TIME_WAIT after the previous
        container's pasta process exits. Shared by the workspace create path
        (:meth:`create_and_start`) and the network sidecar start, so a
        filtered workspace — whose host ports are published on the sidecar
        (#2291) — self-heals the same way a non-filtered one does. Fails
        closed: an unresolved conflict re-raises.
        """
        try:
            await self.app.state.podman.start_container(
                cid, hooks_dir=hooks_dir
            )
        except podman.PodmanError as exc:
            if not self._is_port_conflict(exc):
                raise
            await self._resolve_port_conflict(
                cid, container_name, publish, self.app.state.podman
            )
            # Retry with back-off; ports may linger in TIME_WAIT after
            # the previous container's pasta process exits.
            await self._retry_start_after_conflict(
                cid, hooks_dir=hooks_dir, last_exc=exc
            )

    async def _retry_start_after_conflict(
        self, cid: str, *, hooks_dir: list[str] | None, last_exc: Exception
    ) -> None:
        """Retry a port-conflicted start with back-off, re-raising the
        conflict when every retry fails (fails closed)."""
        for delay in (0.5, 1.5):
            await asyncio.sleep(delay)
            try:
                await self.app.state.podman.start_container(
                    cid, hooks_dir=hooks_dir
                )
                return
            except podman.PodmanError as retry_exc:
                if not self._is_port_conflict(retry_exc):
                    raise
                last_exc = retry_exc
        raise last_exc

    def _sidecar_requirements(
        self,
        egress_mode: str,
        allowed_domains: list[str] | None,
        rejected_domains: list[str] | None,
    ) -> tuple[bool, bool]:
        """(sidecar_required, needs_sidecar) for a start (#2325, #2406).

        #2325: a workspace needs the FQDN network sidecar whenever it is
        egress-filtered. That's either (a) it declares an allow/reject list
        (the sidecar NXDOMAINs/holds those names), or (b) it is in
        interactive mode, which holds EVERY not-yet-approved egress for a
        consent decision even with empty lists (the "ask first" default
        posture). Static mode with no lists stays unrestricted (no point
        filtering nothing). Used by both the reconnect re-track in
        start_container_inner and the create-path sidecar start.

        #2406: ``allow`` mode requests permissiveness, not filtering -- it
        runs the sidecar WHEN one is configured (so off-list egress is logged
        via the consent pipeline and ``rejected_domains`` is enforced at the
        sidecar DNS layer), but degrades to unrestricted when filtering isn't
        set up. Fail-closing an allow-mode workspace would be wrong (it never
        asked to be locked down), so allow is best-effort, not mandatory --
        unlike interactive / list-declaring workspaces, which fail-closed.
        """
        sidecar_required = egress_mode == EGRESS_MODE_INTERACTIVE or bool(
            allowed_domains or rejected_domains
        )
        sidecar_optional = self._sidecar_optional_egress(
            egress_mode, sidecar_required
        )
        return sidecar_required, sidecar_required or sidecar_optional

    def _sidecar_optional_egress(
        self, egress_mode: str, sidecar_required: bool
    ) -> bool:
        """Whether allow-mode egress wants the sidecar when one is
        configured (#2406): ``allow`` requests permissiveness, not
        filtering — it runs the sidecar when one is set up (off-list
        egress logged via the consent pipeline, ``rejected_domains``
        enforced at the sidecar DNS layer) but degrades to unrestricted
        when filtering isn't configured. Fail-closing an allow-mode
        workspace would be wrong (it never asked to be locked down), so
        allow is best-effort, not mandatory — unlike interactive /
        list-declaring workspaces, which fail-closed."""
        return (
            egress_mode == EGRESS_MODE_ALLOW
            and not sidecar_required
            and self.network_sidecar_enabled()
            and bool(self.app.state.settings.userns)
        )

    async def _build_start_config(
        self, spec: ContainerStartSpec, host_ports: list[int]
    ) -> tuple[dict, str, bool]:
        """Build the create kwargs (+ slug and sudo posture) for a start.

        Returns (create_kwargs, slug, allow_sudo)."""
        # Build environment and mounts.
        # Every exec process inherits KLANGKWS_AGENT_HOME (#1157). The
        # agent home is the constant shared home (#2720): the agent's
        # handle is fixed (#2718), so this is no longer resolved from the
        # DB at this seam.
        agent_home = SHARED_HOME
        ssl_dir = self.app.state.ssl_trust.ssl_cert_dir()
        if ssl_dir:
            logger.info(
                "Runtime SSL trust enabled: mounting %s at %s",
                ssl_dir,
                _SSL_MOUNT_DEST,
            )
        env_vars = build_env(
            self.app,
            spec.workspace_id,
            host_ports,
            spec.hosting_hostname,
            spec.hosting_proto,
            spec.hosting_base_path,
            agent_home,
            spec.extra_env,
            ssl_dir,
        )
        await ensure_volumes(
            self.app,
            spec.extra_mounts,
            spec.workspace_id,
            self.app.state.podman,
        )
        binds = build_mounts(
            spec.home_path, spec.config_path, spec.extra_mounts, ssl_dir
        )
        # #2201: when nix is enabled, bind the workspace's btrfs-snapshot /nix
        # (and the seed's nix.conf) into the container, and signal the baked
        # /etc/profile.d activation (KLANGKWS_NIX) so nix is on PATH by default.
        nix_bind_specs, nix_env = await nix_binds(
            self.app, spec.workspace_id, spec.workspace_settings
        )
        binds += nix_bind_specs
        env_vars += nix_env

        iid = self.app.state.util.instance_id()
        # #2286: embed the slugified workspace name in the container + sidecar
        # names so `podman ps | grep <partial-name>` finds a workspace and its
        # sidecar together. The slug is decorative (uniqueness is iid + id); a
        # missing/empty name falls back to an id-only name. Both names use the
        # same workspace_id[:8] tail so an id-prefix grep matches the pair.
        ws_row = await self.app.state.model.workspaces.get_workspace_by_id(
            spec.workspace_id
        )
        slug = workspace_name_slug((ws_row or {}).get("name") or "")
        # #2017/#3047: sudo posture. The bag value is the sole source
        # (absent = off, true opts in); the deploy-wide allow_sudo flag
        # is only a ceiling — a workspace true can never grant sudo on a
        # deploy that forbids it. Read live off settings (the
        # app-ownership rule); applies to newly-created containers.
        allow_sudo = resolve_allow_sudo(
            ws_row, parse_bool_setting(self.app.state.settings.allow_sudo)
        )
        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]
        create_kwargs = build_create_kwargs(
            self.app,
            workspace_id=spec.workspace_id,
            iid=iid,
            slug=slug,
            binds=binds,
            env_vars=env_vars,
            publish=publish,
            workspace_settings=spec.workspace_settings,
        )
        return create_kwargs, slug, allow_sudo

    async def _attach_network_sidecar(
        self,
        spec: ContainerStartSpec,
        sidecar_required: bool,
        publish: list,
        slug: str,
        create_kwargs: dict,
    ) -> None:
        """Egress filtering (#1365): start the FQDN network sidecar and
        rewire create_kwargs onto its netns.

        The FQDN network sidecar is the only egress model. The OCI-hook
        "static" model was dropped (#2254 review) -- maintaining two
        complete models was more complexity than value. A sidecar'd
        workspace (needs_sidecar: interactive mode, or an allow/reject list
        set) runs --network container:<network sidecar> (the network
        sidecar's proxy owns the rules; the workspace is unprivileged).
        Fail-CLOSED (#2254 review B2): a workspace that needs filtering
        never starts unrestricted -- silently ignoring it would disable a
        security control the user requested (an interactive workspace asked
        for "ask first"; an allow-listed one asked for filtering), so a
        missing/unstartable network sidecar raises instead. Static mode
        with no lists is the one case that starts unrestricted (no
        filtering requested) (#2325)."""
        workspace_id = spec.workspace_id
        # Interactive / list-declaring workspaces REQUEST filtering, so a
        # missing/unstartable sidecar is fail-closed (silently starting
        # unrestricted would disable a security control the user asked
        # for). ``allow`` mode is best-effort (sidecar_optional already
        # gated on these), so it never reaches this fail-close (#2406).
        if sidecar_required:
            if not self.network_sidecar_enabled():
                raise podman.PodmanError(
                    500,
                    f"workspace {workspace_id[:8]} is egress-filtered "
                    "(interactive mode, or allowed_domains/rejected_domains "
                    "set) but egress filtering is disabled "
                    "(netfilter_enabled is off or network_sidecar_image is "
                    "unset); refusing to start "
                    "unfiltered. Switch the workspace to static mode with no "
                    "lists, or enable egress filtering.",
                )
            # The #2264 SO_MARK-bypass guard is user-namespace isolation: the
            # workspace must run in a user namespace DISTINCT from the one that
            # owns the network sidecar's netns. The sidecar is launched with no
            # --userns (podman's default), so an empty KLANGKD_USERNS here would
            # emit no --userns either, putting the workspace in that same default
            # userns and reopening the bypass (review #2). Fail-closed: require
            # an explicit, non-empty userns. (Default keep-id:uid=1000,gid=1000
            # satisfies this; pinned by test_filtered_workspace_userns_isolates_netns.)
            if not self.app.state.settings.userns:
                raise podman.PodmanError(
                    500,
                    f"workspace {workspace_id[:8]} is egress-filtered "
                    "(interactive mode, or allowed_domains/rejected_domains "
                    "set), which requires a "
                    "non-empty "
                    "KLANGKD_USERNS so the workspace runs in a user namespace "
                    "distinct from the network sidecar's. An empty userns would "
                    "share the sidecar's userns and reopen the SO_MARK egress "
                    "bypass. Set KLANGKD_USERNS (default "
                    "keep-id:uid=1000,gid=1000) or switch the workspace to "
                    "static mode with no lists.",
                )
        network_sidecar_id = await self.start_network_sidecar(
            workspace_id,
            spec.allowed_domains,
            spec.egress_mode,
            rejected_domains=spec.rejected_domains,
            publish=publish,
            slug=slug,
        )
        create_kwargs["network"] = f"container:{network_sidecar_id}"
        # --dns/--dns-search, --add-host, and --publish are all invalid
        # under --network container: podman rejects --add-host outright
        # ("cannot set extra host entries when ... joined to another
        # containers network namespace"), dns/dns-search are
        # incompatible, and --publish is silently discarded. The workspace
        # still resolves host.containers.internal via the network sidecar's
        # /etc/hosts (podman populates it), and the network sidecar's iptables
        # statically allow-lists the backend port (entrypoint.sh, B1).
        # #2267: the workspace's host ports are instead published on the
        # network sidecar (passed to start_network_sidecar above). The
        # workspace shares the sidecar's netns, so the sidecar's --publish
        # forwards into that netns and reaches the workspace's listener,
        # letting filtered workspaces host apps (which --publish on the
        # workspace itself cannot, under --network container:).
        create_kwargs.pop("dns", None)
        create_kwargs.pop("dns_search", None)
        create_kwargs.pop("add_hosts", None)
        create_kwargs.pop("publish", None)
        # Remember this workspace has a live network sidecar so its stop
        # tears the network sidecar down (#2254), and keep its id as the
        # netns owner for container_events attribution (#2915).
        self._ws_with_network_sidecar.add(workspace_id)
        self._ws_netns_owner[workspace_id] = network_sidecar_id

    async def _create_start_shielded(
        self,
        spec: ContainerStartSpec,
        container_name: str,
        resolved_image: str,
        allow_sudo: bool,
        create_kwargs: dict,
    ) -> str:
        """Create + start the workspace container, shielded from
        cancellation so a dropped WebSocket doesn't orphan a running
        container; tears a just-started sidecar down on failure (#2255)."""
        workspace_id = spec.workspace_id
        try:
            container_id = await asyncio.shield(
                self.create_and_start(
                    container_name,
                    resolved_image,
                    workspace_id,
                    # The workspace's OWN publish (what _resolve_port_conflict
                    # scans for stale holders). A filtered workspace publishes
                    # nothing (its ports moved to the sidecar, #2267), so pass
                    # create_kwargs's actual publish (empty for filtered) rather
                    # than the stale local -- otherwise a conflict on a filtered
                    # workspace would scan the sidecar's ports and tear the
                    # sidecar (and its netns) down.
                    create_kwargs.get("publish", []),
                    allow_sudo,
                    create_kwargs,
                    health_check=spec.health_check,
                    owner_id=spec.user_id,
                    setup_state=spec.setup_state,
                    per_handle_home=spec.per_handle_home,
                )
            )
        except BaseException:
            # If the workspace container fails to create/start after a
            # network sidecar was started for it, tear the sidecar down so
            # it doesn't leak (NET_ADMIN + proxy) until the next startup
            # reap. The klangk.instance label would eventually cull it, but
            # cleaning up immediately matches how workspace containers are
            # treated and avoids an unfiltered netns lingering (#2255).
            if workspace_id in self._ws_with_network_sidecar:
                await self.stop_network_sidecar(workspace_id)
                self._ws_with_network_sidecar.discard(workspace_id)
                # The sidecar's id is no longer a live netns owner —
                # drop it or a later stop row would name a removed
                # sidecar (#2915 review).
                self._ws_netns_owner.pop(workspace_id, None)
            raise
        return container_id

    async def start_container_inner(
        self, spec: ContainerStartSpec
    ) -> tuple[str, str]:
        """Inner implementation of start_container (called under lock).

        Unpacks the spec once (#2566); the body reads plain locals, same
        as the pre-spec signature. Mounts/env building reads the spec
        directly in _build_start_config; only the locals this body uses
        are unpacked.
        """
        workspace_id = spec.workspace_id
        num_ports = spec.num_ports
        image = spec.image
        setup_state = spec.setup_state
        service_command = spec.service_command
        allowed_domains = spec.allowed_domains
        rejected_domains = spec.rejected_domains
        egress_mode = spec.egress_mode
        t_start = time.monotonic()
        resolved_image = self._resolved_start_image(image)

        sidecar_required, needs_sidecar = self._sidecar_requirements(
            egress_mode, allowed_domains, rejected_domains
        )

        # Reuse a running container or remove a stopped one.
        result = await self._reuse_existing_container(
            spec, workspace_id, t_start, needs_sidecar
        )
        if result is not None:
            return result

        # Start-refusal gate (#2527): while a graceful restart's drain
        # flag is set, every *new* container creation through this single
        # choke point is refused. Placed after the existing-container
        # reuse above so a client connecting to a still-running workspace
        # keeps working — existing workspaces keep running, only fresh
        # starts are blocked — and applies immediately across all start
        # paths (API start/restart, WS connect, create eager start, boot
        # auto-start, crash-recovery restart).
        blocked_reason = self.new_starts_blocked_reason()
        if blocked_reason:
            raise NodeDrainingError(blocked_reason)

        # Admission control (#2525): host-memory fit + per-user running
        # quota, checked at this single choke point so every start path
        # (API start/restart, WS connect, create eager start, boot
        # auto-start, crash-recovery restart) is covered. Like the drain
        # gate it sits after the running-container adoption check above —
        # a workspace that is already running keeps its committed
        # capacity and is never re-admitted on reconnect. Raises
        # WorkspaceCapacityError (503 / WS error frame upstream) with an
        # actionable message instead of deferring the failure to the
        # kernel OOM killer.
        await self.admission.admit(spec)

        # A fresh container (re)start means no `restart`-duration consent
        # verdict can still be in effect -- the sidecar's in-memory rules
        # (learned ACCEPT for an allow, REJECT for a deny) died with the
        # previous container. Reap those rows so list_active (#2335) matches
        # the enforced set (#2346). Safe on a first-ever start (no rows yet);
        # `forever`/time-bounded/`once`/pending/static rows are left alone.
        await self.app.state.model.egress_consent.clear_tilrestart_duration(
            workspace_id
        )

        # Allocate host ports.
        t_ports = time.monotonic()
        host_ports = await self._reconcile_ports(workspace_id, num_ports)
        logger.info(
            "workspace-open: allocate host ports from DB: %.3fs",
            time.monotonic() - t_ports,
        )

        t_env = time.monotonic()
        create_kwargs, slug, allow_sudo = await self._build_start_config(
            spec, host_ports
        )
        publish = [
            (host_port, CONTAINER_PORT_START + i)
            for i, host_port in enumerate(host_ports)
        ]

        if needs_sidecar:
            await self._attach_network_sidecar(
                spec, sidecar_required, publish, slug, create_kwargs
            )

        # #2347: the workspace never holds CAP_NET_RAW, under any
        # configuration. The old enable_ping grant (#2045) — cap_add net_raw
        # so a setuid ping binary could bridge the cap to the non-root klangk
        # user — is removed: the workspace is where untrusted user code runs,
        # and CAP_NET_RAW there lets it open raw/packet sockets in the netns
        # (forge packets, emit arbitrary ICMP). The #2276 (B) filtered+sudo
        # drop is folded into this unconditional cap_drop. Unprivileged
        # ``ping`` stops working as a result (the setuid-ping /
        # ping_group_range / setcap alternatives all fail rootless, #2045);
        # the egress sidecar owns the netns plumbing. NET_ADMIN is never
        # granted either, so dropping net_raw alone also closes
        # setsockopt(SO_MARK) (which needs NET_ADMIN or NET_RAW) even for
        # root-in-workspace — a defense-in-depth second line behind the
        # PRIMARY guard, user-namespace isolation (the workspace's keep-id
        # userns differs from the netns-owning userns, #2264; enforced above
        # and pinned by test_filtered_workspace_userns_isolates_netns).
        # Applied to newly-created containers only: a container already
        # running keeps its existing cap set until recreated.
        create_kwargs["cap_drop"] = ["net_raw"]

        logger.info(
            "workspace-open: build env vars, volumes, and "
            "container config: %.3fs",
            time.monotonic() - t_env,
        )

        container_name = workspace_container_name(
            self.app.state.util.instance_id(), workspace_id, slug
        )

        container_id = await self._create_start_shielded(
            spec, container_name, resolved_image, allow_sudo, create_kwargs
        )

        # Fresh create: provision the agent home and fire the service
        # command (#1244). This is the single choke point -- every
        # start path (boot autostart, create, connect, klangk restart)
        # routes through start_container, so the bring-up runs once per
        # fresh container regardless of caller. ensure_service_session
        # is idempotent, and setup_state gates the create-time deferral
        # for workspaces whose setup.sh has not run yet.
        await self.bringup(
            workspace_id, container_id, service_command, setup_state
        )
        logger.info(
            "workspace-open: DONE — new container created and started: %.3fs",
            time.monotonic() - t_start,
        )
        logger.info(
            "Started container %s for workspace %s (ports %s)",
            container_id,
            workspace_id,
            host_ports,
        )
        return container_id, "created"

    def _resolved_start_image(self, image: str | None) -> str:
        """The image to start, validated against the allowed list."""
        resolved = image or self.image_name
        if resolved not in self.allowed_images:
            raise ValueError(
                f"Image {resolved!r} is not in the allowed "
                f"list: {sorted(self.allowed_images)}"
            )
        return resolved

    async def _reuse_existing_container(
        self,
        spec: ContainerStartSpec,
        workspace_id: str,
        t_start: float,
        needs_sidecar: bool,
    ) -> tuple[str, str] | None:
        """Reuse a running container (or remove a stopped one); None
        means no existing container / not reusable — continue with a
        fresh create."""
        if not spec.existing_container_id:
            return None
        result = await self.handle_existing_container(
            spec.existing_container_id,
            workspace_id,
            t_start,
            health_check=spec.health_check,
            owner_id=spec.user_id,
            setup_state=spec.setup_state,
            per_handle_home=spec.per_handle_home,
        )
        if result is None:
            return None
        # Re-track a sidecar'd workspace's network sidecar on reconnect.
        # _ws_with_network_sidecar is in-memory and lost on a process
        # restart; without this, a reconnect-then-stop would skip
        # stop_network_sidecar (only the create path added it before)
        # and leak the sidecar until the next start's force-remove or
        # the instance reaper. A sidecar'd workspace always has a live
        # sidecar (fail-closed), so needs_sidecar => re-track.
        if needs_sidecar:
            self._ws_with_network_sidecar.add(workspace_id)
        return result

    async def stop_and_remove_container(
        self,
        container_id: str,
        workspace_id: str | None = None,
        *,
        cause: str,
        actor_id: str | None = None,
        pending_event: int | None = None,
        prewrite_decided: bool = False,
    ) -> bool:
        """Stop and remove a container.

        Returns True when the container ended up gone via this call (the
        podman remove succeeded, or it was already gone — 404); False
        when the stop/remove failed (logged at warning) or a racing
        start re-bound the workspace and the fresh container was left
        alone (#2527 review: drains count only True results, so the
        reported stop count never overstates).

        The slow ``self.app.state.podman.remove_container`` call for the
        workspace container runs *outside* the workspace lock; the
        registry-state teardown *and* the network sidecar teardown are
        serialized -- under the same per-workspace lock
        :meth:`start_container` uses -- so a concurrent start for the same
        workspace cannot observe a half-cleaned registry (#1258) and cannot
        have its freshly-started sidecar removed out from under it (#2265).
        Under the lock we re-check (via the live registry state) that this is
        still the workspace's container: a racing ``start_container`` may
        already have re-bound the workspace to a fresh container, in which case
        we must not tear down the new state (or revoke its browsers, or remove
        its sidecar). ``workspace_id`` lets a caller that knows the workspace
        (the /stop, /delete endpoints) supply it directly, so a container
        started by autostart or a prior klangkd session -- not in the in-memory
        registry -- still gets its network sidecar torn down on stop (#2286).

        The per-workspace lock entry is deliberately *not* popped. Popping
        it while another coroutine is waiting on (or holding) that exact
        ``asyncio.Lock`` would let a subsequent ``_get_workspace_lock``
        create a brand-new lock object that does not serialize against the
        in-flight one -- reopening the very race this method exists to
        prevent. The retained entry is a single small object per workspace
        ever seen and is cleared on process restart, or on workspace
        *delete* (:meth:`prune_workspace_registry_entries`, #2912) -- the
        only path after which no new start can begin for the id.

        Every caller of this method is an *expected* stop (user stop, idle
        stop, delete, logout, shutdown — plus the crash monitor's own
        teardown of an already-dead container): the workspace is marked in
        ``self.stopping`` for the duration so the crash monitor's sweep
        cannot misread the in-flight removal as an unexpected death, and
        any pending crash-restart for it is cancelled (#2524).

        ``cause`` (required, #2915) labels the container_events audit row —
        every caller states why it stops the container (api/stop,
        restart, delete, idle_timeout, eviction, logout, drain, shutdown,
        crash_teardown). ``actor_id`` is the acting principal when a user
        (or the agent) fired the stop; None records a system actor.
        ``pending_event`` (#3154) is a stop row already pre-written via
        :meth:`prewrite_stop_event` — pass it so the row is written once
        (the /stop route does this to put the audit refusal before its
        terminal death frames). ``prewrite_decided`` (#3154 review)
        says the CALLER already evaluated the fail-closed gate for this
        stop (its pre-write may legitimately have been skipped with the
        mode off); the in-method pre-write then never re-arms on a
        mid-request SIGHUP flip, so a refusal cannot fire after side
        effects the caller already emitted.
        """
        ws_id = workspace_id or self._cid_to_wsid.get(container_id)
        # Netns owner captured before teardown pops it (#2915).
        netns = self._ws_netns_owner.get(ws_id)
        # Audit-before-act (#3154): on the interactive fail-closed paths
        # the stop row is written HERE — a write failure raises
        # AuditWriteError and the stop is refused before any teardown
        # (before even the expected-stop bookkeeping below). The /stop
        # route pre-writes earlier still (via prewrite_stop_event) so the
        # refusal also precedes notify_workspace_killed, whose production
        # callback tears down registry state (#3154 review B1); it passes
        # the row id back in as ``pending_event``.
        pending = await self.prewrite_stop_event(
            ws_id,
            container_id,
            cause=cause,
            actor_id=actor_id,
            netns=netns,
            prewritten=pending_event,
            decided=prewrite_decided,
        )
        if ws_id:
            self._begin_expected_stop(ws_id)
        stopped = False
        torn_down = False
        try:
            stopped = await self._try_remove_container(container_id)
            # The caller (/stop, /delete) knows the workspace_id even when
            # this container isn't tracked in the in-memory registry
            # (started by autostart or a prior klangkd session, stopped
            # without a connect in this process).
            if ws_id:
                torn_down = await self._teardown_registry_state(
                    ws_id, container_id
                )
            # Drop the per-container service-firing lock (#1188), then sweep
            # any other entries orphaned by container churn (e.g. a racing
            # re-bind that popped this container's mapping before teardown)
            # (#1351).
            self.clear_service_session_lock(container_id)
            self.clear_service_fire_pending(container_id)
            self.prune_service_session_locks(set(self._cid_to_wsid))
            # Gone via this call AND (untracked, or our registry state torn
            # down — i.e. not left alone by the rebind guard).
            result = self._stop_outcome(ws_id, stopped, torn_down)
            await self._settle_stop_audit(
                ws_id,
                container_id,
                result,
                pending,
                cause=cause,
                actor_id=actor_id,
                netns=netns,
            )
            return result
        finally:
            if ws_id:
                self.stopping.discard(ws_id)
                # Clear again (#2524): a stop that began BEFORE the crash
                # monitor scheduled a restart (but completes after — podman
                # removes are slow and interleave) must still cancel it.
                # Expected deaths never restart, no matter the ordering.
                self.crash.on_expected_stop(ws_id)

    def _begin_expected_stop(self, ws_id: str) -> None:
        """Mark the workspace as stopping for the duration of the
        removal below, so the crash monitor's sweep cannot misread the
        in-flight removal as an unexpected death."""
        self.stopping.add(ws_id)
        # Bumped synchronously at stop ENTRY, before any await — the
        # fail-closed audit pre-write (#3154) may precede this method's
        # caller, but a refusal raises before _begin_expected_stop runs
        # at all, so the epoch invariant holds for every stop that
        # actually begins. The crash monitor snapshots the epoch around
        # its awaits and re-checks it before scheduling a restart, so a
        # stop that begins at any point during death detection/handling
        # invalidates the restart — even if the stop fully completes
        # before the scheduler re-checks (#2524 review).
        self.stop_epoch[ws_id] = self.stop_epoch.get(ws_id, 0) + 1
        self.crash.on_expected_stop(ws_id)

    async def _try_remove_container(self, container_id: str) -> bool:
        """Remove the container (stop + rm). A podman failure is logged
        as a warning, not raised — the registry teardown still runs."""
        try:
            await self.app.state.podman.remove_container(container_id)
            logger.info("Stopped container %s", container_id)
            return True
        except podman.PodmanError as e:
            logger.warning(
                "Failed to stop container %s: %s",
                container_id,
                e,
            )
            return False

    async def _teardown_registry_state(
        self, ws_id: str, container_id: str
    ) -> bool:
        """Under the per-workspace lock, tear down the registry state we
        still own.

        Re-check (via the live registry state) that this is still the
        workspace's container: a racing ``start_container`` may already
        have re-bound the workspace to a fresh container, in which case
        we must not tear down the new state (or revoke its browsers, or
        remove its sidecar). The check uses the live registry state (not
        the reverse cid map) so it can tell a re-bound workspace
        (state's container_id differs — leave the fresh sidecar
        generation alone, #2265) from an untracked one (no state — no
        racing start possible, so its sidecar is safe to remove by label
        even though it isn't in the in-memory set).

        Returns True when the registry state was torn down; False when a
        racing start re-bound the workspace and the fresh container was
        left alone.
        """
        async with self._get_workspace_lock(ws_id):
            current = self.states.get(ws_id)
            if current is None or current.container_id == container_id:
                # Remove the network sidecar (label-based, idempotent --
                # a no-op for non-filtered workspaces or when egress is
                # disabled). Done for every non-rebound stop so a sidecar
                # started by autostart / a prior session is cleaned up
                # even if it isn't tracked in _ws_with_network_sidecar
                # (#2286).
                self._ws_with_network_sidecar.discard(ws_id)
                self._ws_netns_owner.pop(ws_id, None)
                await self.stop_network_sidecar(ws_id)
                self._cid_to_wsid.pop(container_id, None)
                self.revoke_workspace_browsers(ws_id)
                self.states.pop(ws_id, None)
                return True
            # Re-bound to a fresh container by a racing start: the fresh
            # container is not ours to stop.
            return False

    @staticmethod
    def _stop_outcome(
        ws_id: str | None, stopped: bool, torn_down: bool
    ) -> bool:
        """True when the container ended up gone via this call (see the
        ``stop_and_remove_container`` docstring for why a re-bind counts
        as not-gone)."""
        if ws_id is None:
            return stopped
        return stopped and torn_down

    def _stop_prewrite_gated(self, ws_id: str | None, cause: str) -> bool:
        """True when the interactive fail-closed stop pre-write applies
        (#3154)."""
        return (
            self.audit_fail_closed
            and bool(ws_id)
            and cause in INTERACTIVE_STOP_CAUSES
        )

    async def prewrite_stop_event(
        self,
        ws_id: str | None,
        container_id: str,
        *,
        cause: str,
        actor_id: str | None,
        netns: str | None = None,
        prewritten: int | None = None,
        decided: bool = False,
    ) -> int | None:
        """Audit-before-act stop row (#3154) — route-facing form.

        Writes the row and returns its id so a caller that sequences
        side effects around the stop (the /stop route's terminal death
        frames) can put the refusal point FIRST:
        ``notify_workspace_killed``'s production callback tears down
        registry state, which a refused stop must not lose for a
        still-running container (#3154 review B1). Pass the returned id
        back via ``stop_and_remove_container(pending_event=...)`` so the
        row is written exactly once. None unless the interactive
        fail-closed gate applies; raises :class:`AuditWriteError` to
        refuse the stop before any side effect when the row cannot be
        written. ``decided`` (#3154 review) says the caller already
        evaluated the gate for THIS stop (possibly off); skip the
        in-method re-evaluation so a mid-request SIGHUP flip cannot
        arm a refusal after the caller's side effects.
        """
        if prewritten is not None:
            return prewritten
        if decided or not self._stop_prewrite_gated(ws_id, cause):
            return None
        return await self.prewrite_audit_event(
            ws_id,
            container_id,
            EVENT_STOP,
            cause=cause,
            actor_id=actor_id,
            network_namespace=netns or self._ws_netns_owner.get(ws_id),
        )

    async def _settle_stop_audit(
        self,
        ws_id: str | None,
        container_id: str,
        gone: bool,
        pending: int | None,
        *,
        cause: str,
        actor_id: str | None,
        netns: str | None,
    ) -> None:
        """Post-stop audit settlement (#3154): a pre-written row is
        already final when the stop happened (every field was known at
        pre-write) and retracted when it did not; without one, the
        legacy best-effort post-hoc write applies."""
        if pending is not None:
            if not gone:
                await self.retract_prewritten_event(pending)
            return
        await self.audit_stop(
            ws_id,
            container_id,
            gone,
            cause=cause,
            actor_id=actor_id,
            netns=netns,
        )

    async def audit_stop(
        self,
        ws_id: str | None,
        container_id: str,
        gone: bool,
        *,
        cause: str,
        actor_id: str | None,
        netns: str | None,
    ) -> None:
        """Record a stop event when the container is a resolvable,
        actually-stopped workspace (#2915).

        Sweep/orphan stops without a workspace id are skipped — that
        population includes network sidecars, which are not workspaces.
        """
        if ws_id and gone:
            await self.record_container_event(
                ws_id,
                container_id,
                EVENT_STOP,
                cause=cause,
                actor_id=actor_id,
                network_namespace=netns,
            )

    async def notify_workspace_killed(
        self,
        workspace_id: str,
        *,
        cause: str | None = None,
        container_id: str | None = None,
    ) -> None:
        """Call the on_workspace_killed callback, logging any errors.

        Must be called **before** ``stop_and_remove_container`` so that
        ``self.states`` still contains the workspace state needed to emit
        the terminal ``service_health`` death frame. *cause* (#2524) is
        the classified death reason (e.g. "OOM-killed at 8g memory
        limit"); it rides the death frame's ``message`` field so
        consumers can tell an OOM kill from a crash from external
        removal.

        *container_id* (#331) is the re-bind guard: the id of the
        container the caller believes died. When the workspace is now
        tracked under a DIFFERENT container (a racing user-driven start
        removed the dead one and re-bound the workspace), this death is
        not ours to act on -- no terminal death frame for the live
        container, no spurious ``running=False`` status, and the reset
        chain carries the expected id down to :meth:`remove_state`, which
        re-checks authoritatively under the workspace lock. Callers that
        know the dead container (crash monitor, idle stop, eviction,
        /stop, drain, logout) always have it at hand.
        """
        state = self.states.get(workspace_id)
        if self._killed_container_rebound(state, container_id):
            # Re-bound to a fresh container by a racing start: the
            # workspace is live; a death teardown would corrupt it.
            logger.info(
                "Workspace %s: killed-container teardown skipped — "
                "re-bound to %s (dead was %s)",
                workspace_id,
                state.container_id[:12],
                container_id[:12],
            )
            return
        self._notify_status_changed(workspace_id, False)
        await self._broadcast_service_death(state, cause)
        await self._fire_workspace_killed(workspace_id, container_id)

    def _killed_container_rebound(
        self, state: ContainerState | None, container_id: str | None
    ) -> bool:
        """True when the workspace is now tracked under a DIFFERENT
        container (a racing user-driven start removed the dead one and
        re-bound the workspace): this death is not ours to act on."""
        return (
            container_id is not None
            and state is not None
            and state.container_id != container_id
        )

    async def _broadcast_service_death(
        self, state: ContainerState | None, cause: str | None
    ) -> None:
        """Emit a terminal ``running=False`` frame so consumers watching
        only service_health learn the service is down (#1175 item 2).
        Only health-checked workspaces ever appeared on the stream, so
        only those get a terminal frame."""
        if state is not None and state.health_check is not None:
            await self.health.broadcast_death(state, message=cause)

    async def _fire_workspace_killed(
        self, workspace_id: str, container_id: str | None
    ) -> None:
        """Call the on_workspace_killed callback, logging any errors."""
        if self.on_workspace_killed:
            try:
                await self.on_workspace_killed(workspace_id, container_id)
            except Exception as e:
                logger.error(
                    "Workspace killed callback error for %s: %s",
                    workspace_id,
                    e,
                )

    async def stop_user_containers(self, user_id: str) -> None:
        """Stop all containers for a user (called on logout)."""
        workspaces = await self.app.state.model.workspaces.get_user_workspaces_with_containers(
            user_id
        )
        for ws in workspaces:
            if ws["container_id"]:
                await self.notify_workspace_killed(
                    ws["id"], container_id=ws["container_id"]
                )
                await self.stop_and_remove_container(
                    ws["container_id"],
                    workspace_id=ws["id"],
                    cause=CAUSE_LOGOUT,
                    actor_id=user_id,
                )

    def new_starts_blocked_reason(self) -> str | None:
        """Why new container starts are refused, or None if allowed (#2527).

        The single refuser is the in-memory drain flag: while a SIGHUP
        graceful restart quiesces the node, every container-start path
        (API start/restart, WS connect, create eager start, boot
        auto-start, crash-recovery restart) refuses new starts through
        this shared reason at the single start choke point. Never
        persisted — it must self-clear when the restart completes, and a
        crashed restart must not leave the node refusing starts.
        """
        if self.draining:
            return (
                "node is draining: new workspace starts are disabled "
                "(a restart or shutdown is in progress)"
            )
        return None

    async def drain_all_containers(self, *, reason: str = "node drain") -> int:
        """Gracefully stop every running workspace (#2527 drain).

        The k8s ``drain`` half: same path as logout's
        :meth:`stop_user_containers` and the idle-timeout cleanup —
        :meth:`notify_workspace_killed` emits the terminal
        status/service-death frames so connected clients see a clean
        "stopped" instead of a dropped WebSocket, then
        :meth:`stop_and_remove_container` runs the graceful podman stop
        (``podman stop -t 5`` then ``rm -f`` — a fixed 5s kill grace,
        #2527 review). The container_stopped broadcast carries the drain
        reason so the UI shows it.

        Workspaces drain **concurrently** (per-workspace stops are
        independent and already serialize on the per-workspace lock —
        the same concurrency ``shutdown()`` uses), so a node with many
        workspaces does not pay N sequential 5s stops.

        After the tracked workspaces, a **label sweep** re-lists this
        instance's containers and removes any stragglers — a start that
        passed the refusal gate just before it closed (its
        ``track_activity`` had not landed when the states snapshot was
        taken) is caught here rather than left running (#2527 review).

        Returns the number of workspaces/containers verifiably stopped
        (``stop_and_remove_container`` returning False — failed stop or
        a racing re-bind — is logged and not counted).
        """

        # Snapshot so a container exiting mid-drain cannot mutate the
        # dict under us.
        tracked = [
            (ws_id, state.container_id)
            for ws_id, state in list(self.states.items())
            if state.container_id
        ]
        results = await asyncio.gather(
            *(self._drain_one(ws_id, cid, reason) for ws_id, cid in tracked),
            return_exceptions=True,
        )
        stopped = self._count_drained(tracked, results)
        # Label sweep: catch starts that raced the gate (created after
        # the snapshot) and anything else instance-labelled still alive.
        stopped += await self._sweep_drain_leftovers()
        return stopped

    def tracked_container_count(self) -> int:
        """Running containers a drain is expected to stop (V-222585,
        #3176): the drain's own snapshot baseline, counted up front so
        the caller can detect an under-stopping drain by outcome —
        per-container stop failures never raise out of
        ``drain_all_containers``."""
        return sum(1 for state in self.states.values() if state.container_id)

    async def leftover_containers(self) -> list[str]:
        """Idents of this instance's containers still listed as
        running (V-222585, #3176) — ground truth for verifying a
        forced-backstop stop actually left nothing behind (the stop
        path swallows per-container failures). Raises on a listing
        failure so the caller can treat "cannot verify" as insecure.
        """
        containers = await self.app.state.podman.list_containers(
            f"klangk.instance={self.app.state.util.instance_id()}"
        )
        return [ident for ident in map(container_ident, containers) if ident]

    async def _drain_one(self, ws_id: str, cid: str, reason: str) -> bool:
        """Notify + stop one workspace for a drain, with the same
        "stopped on purpose" broadcast as the /stop endpoint (clients
        re-render as stopped, not disconnected)."""
        await self.notify_workspace_killed(ws_id, container_id=cid)
        ok = await self.stop_and_remove_container(
            cid, workspace_id=ws_id, cause=CAUSE_DRAIN
        )
        if not ok:
            logger.warning(
                "Drain: workspace %s container %s not stopped "
                "(failed or re-bound by a racing start)",
                ws_id,
                cid[:12],
            )
            return False
        session = self.app.state.sockets.get_session(ws_id)
        if session:
            session.broadcast(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {"reason": reason},
                    },
                }
            )
        return True

    @staticmethod
    def _count_drained(tracked: list[tuple[str, str]], results: list) -> int:
        """Count the successful drains; log the raising ones."""
        stopped = 0
        for (ws_id, _cid), result in zip(tracked, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Drain: workspace %s stop raised: %r", ws_id, result
                )
            elif result:
                stopped += 1
        return stopped

    async def _sweep_drain_leftovers(self) -> int:
        """Label sweep: catch starts that raced the drain gate (created
        after the snapshot) and anything else instance-labelled still
        alive. Returns the count additionally stopped."""
        try:
            leftovers = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
        except (podman.PodmanError, OSError) as e:
            logger.warning("Drain: error sweeping leftover containers: %s", e)
            leftovers = []
        stopped = 0
        for c in leftovers:
            stopped += await self.sweep_stop_audited(c, cause=CAUSE_DRAIN)
        return stopped

    async def sweep_stop_audited(self, c: dict, *, cause: str) -> bool:
        """Stop one sweep/orphan container, recording its audit row for
        labeled containers of either role (#2915).

        Workspace-labeled containers pass their label-resolved
        workspace_id (stop row + #2286 sidecar teardown); sidecar-labeled
        ones route through the label-based removal choke point
        (:meth:`_remove_network_sidecar`) so the ``sidecar_stop`` row is
        written exactly once — a sidecar already torn down by its
        workspace's stop is simply absent from that listing, instead of
        producing a second row from a 404-tolerant sweep stop (#2915
        review). Label-less containers record nothing.
        """
        cid = container_ident(c)
        if not cid:
            return False
        labels = c.get("Labels") or {}
        ws_id = labels.get("klangk.workspace")
        if labels.get("klangk.role") == "network-sidecar":
            if not ws_id:
                logger.info(
                    "Sweep: stopping label-less sidecar container %s",
                    cid[:12],
                )
                return await self.stop_and_remove_container(cid, cause=cause)
            logger.info(
                "Sweep: stopping leftover sidecar %s for %s",
                cid[:12],
                ws_id[:8],
            )
            return await self._remove_network_sidecar(ws_id)
        logger.info("Sweep: stopping leftover klangk container %s", cid[:12])
        return await self.stop_and_remove_container(
            cid, workspace_id=labeled_workspace_id(c), cause=cause
        )

    async def record_reap(self, c: dict) -> None:
        """Audit row for a reaped labeled container (#2915 review).

        The boot reaps are the mass teardown an audit trail exists for:
        after an unclean klangkd death, every reaped container's start
        row would otherwise dangle with no matching stop. Label-less
        containers record nothing (nothing to correlate them to).
        """
        labels = c.get("Labels") or {}
        ws_id = labels.get("klangk.workspace")
        cid = container_ident(c)
        if not ws_id or not cid:
            return
        role = (
            ROLE_SIDECAR
            if labels.get("klangk.role") == "network-sidecar"
            else ROLE_WORKSPACE
        )
        await self.record_container_event(
            ws_id, cid, EVENT_STOP, cause=CAUSE_REAP, container_role=role
        )

    # --- Pre-warm ---

    async def prewarm_podman(self) -> None:
        """Run a throwaway container create+rm to warm podman caches.

        The very first ``podman create`` with ``--userns=keep-id`` in a
        session can take ~20-30s while podman initialises storage,
        user-namespace mappings, and network helpers.  Paying that cost
        here (during backend startup) keeps it off the path where a
        user is waiting.
        """
        t0 = time.monotonic()
        try:
            # Resource limits (container_{cpu,memory,pids}_limit) are
            # deliberately NOT applied here: this is a throwaway create+rm
            # to warm podman caches, removed immediately, so capping its
            # CPU/memory/PIDs serves no purpose. The limits are applied to
            # real workspaces in start_container's create_kwargs (#34).
            cid = await self.app.state.podman.create_container(
                "klangk-prewarm",
                self.image_name,
                pull="never",
                userns=self.app.state.settings.userns,
            )
            await self.app.state.podman.remove_container(cid)
            logger.info("Podman pre-warmed in %.3fs", time.monotonic() - t0)
        except podman.PodmanError as e:
            logger.warning(
                "Podman pre-warm failed (%.3fs): %s", time.monotonic() - t0, e
            )

    # --- Startup reap ---

    async def reap_instance_containers(self) -> None:
        """Remove every container labelled with this instance's ID.

        Runs early in :func:`startup`, before any workspace is tracked, so
        every leftover container from a crashed/killed previous run is
        reaped unconditionally -- there is nothing to "adopt" because the
        in-memory registry starts empty.  ``auto_start_workspaces`` then
        recreates the ones that should be running.

        Safe even when klangkd itself runs inside a container: the
        ``klangk.instance`` label filter scopes removal to containers
        *this instance* created, so an unrelated host container (or a
        container created by an outer klangkd with a different instance
        ID) is never touched (#1556).
        """
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
        except (podman.PodmanError, OSError) as e:
            logger.warning("Error scanning for leftover containers: %s", e)
            return
        # Remove dependents (workspaces) before the sidecars they reference,
        # or every sidecar is skipped this pass (#2476 — see reap_sort_key).
        containers.sort(key=reap_sort_key)
        for c in containers:
            cid = container_ident(c)
            if not cid:
                continue
            logger.info("Reaping leftover container %s on startup", cid[:12])
            if await safe_remove(
                self.app.state.podman, cid, what="leftover container"
            ):
                await self.record_reap(c)

    async def reap_dead_owner_containers(self) -> None:
        """Reap managed containers whose owning klangkd is no longer running.

        The companion to :meth:`reap_instance_containers`. That one removes
        *this* instance's own leftovers (always safe — the in-memory registry
        starts empty, so there is nothing to adopt). This one removes
        containers created by **other** klangkd instances whose owner process
        has since died, which is the #2342 leak: an uncleanly-killed klangkd's
        containers carry an instance ID no live instance matches, so they
        would otherwise run forever (consuming memory, CPU, and the
        NFQUEUE/iptables state in their netns).

        Runs once at :func:`startup`, right after the per-instance reap, so
        this instance's own leftovers are already gone before this scan. The
        decision per container (listed by ``klangk.managed=true``, which spans
        all instances):

        - **No ``klangk.pid`` label → skip.** The container predates this
          feature (an older klangkd that did not stamp the label — possibly
          still running) or the label is unreadable; either way liveness
          cannot be decided, so it is left alone. Tolerating label-less
          containers keeps a new klangkd from culling an older sibling's live
          work on a mixed-version host (#2342).
        - **``klangk.pid`` names a live process → skip.** The owning klangkd
          is still running — possibly a sibling, possibly mid-``shutdown``
          (still alive, still owns its containers; its own ``shutdown()``
          finishes the job). A live owner always holds its own PID, so its
          containers always read alive and are never reaped here (#1556).
        - **``klangk.pid`` names no live process → reap.** The owner is dead.
          ``remove_container(force=True)`` stops-then-removes gracefully and
          treats an already-gone container (404) as success; any other error
          (e.g. a 409 from a concurrent removal) is caught and logged below,
          so it never aborts the sweep. A container whose owner died
          mid-shutdown — leaving podman finishing a stop — is thus handled
          without racing conmon (at worst it surfaces as a logged warning on
          one of the racers).

        Liveness is a plain "is there a process with this PID?" check
        (:func:`pid_alive`), not a process-identity check. PID recycling can
        make a dead owner's PID read falsely alive, but that only ever
        *misses* a reap; it never reaps a live owner's containers. See #2342
        for the full rationale.
        """
        try:
            containers = await self.app.state.podman.list_containers(
                "klangk.managed=true"
            )
        except (podman.PodmanError, OSError) as e:
            logger.warning("Error scanning for dead-owner containers: %s", e)
            return
        # Remove dependents (workspaces) before the sidecars they reference,
        # or every sidecar is skipped this pass (#2476 — see reap_sort_key).
        containers.sort(key=reap_sort_key)
        for c in containers:
            cid = container_ident(c)
            if not cid:
                continue
            owner_pid = _dead_owner_pid(c)
            if owner_pid is None:
                continue  # undecidable or owner alive → leave it
            await self._reap_dead_owner_container(c, cid, owner_pid)

    async def _reap_dead_owner_container(
        self, c: dict, cid: str, owner_pid: int
    ) -> None:
        """Gracefully remove one dead-owner container, recording its
        audit row on success (#2915 review)."""
        logger.info(
            "Reaping dead-owner container %s (owner pid %d no longer running)",
            cid[:12],
            owner_pid,
        )
        if await safe_remove(
            self.app.state.podman, cid, what="dead-owner container"
        ):
            await self.record_reap(c)

    # --- Shutdown ---

    async def shutdown(self) -> None:
        await self._cancel_and_await(self.cleanup_task)
        self.cleanup_task = None
        await self._cancel_and_await(self.health_task)
        self.health.health_task = None
        # Crash monitor (#2524): stop the sweep and cancel any pending
        # delayed restarts — a shutdown-time restart would race the
        # container teardown below.
        await self.crash.stop()
        tracked_ids = set(self._cid_to_wsid.keys())
        tasks = [
            self.stop_and_remove_container(cid, cause=CAUSE_SHUTDOWN)
            for cid in tracked_ids
        ]
        tasks += await self._shutdown_orphan_tasks(tracked_ids)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _cancel_and_await(task) -> None:
        """Cancel a background task and await its termination."""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _shutdown_orphan_tasks(self, tracked_ids: set[str]) -> list:
        """Stop tasks for labeled containers beyond the tracked set:
        a prior-session workspace or its sidecar stopped at shutdown
        must not vanish from the audit trail (#2915 review)."""
        tasks = []
        try:
            containers = await self.app.state.podman.list_containers(
                f"klangk.instance={self.app.state.util.instance_id()}"
            )
            for c in containers:
                cid = container_ident(c)
                if cid not in tracked_ids:
                    logger.info(
                        "Removing orphaned klangk container %s",
                        cid,
                    )
                    tasks.append(
                        self.sweep_stop_audited(c, cause=CAUSE_SHUTDOWN)
                    )
        except (
            podman.PodmanError,
            OSError,
        ) as e:
            logger.warning("Error listing orphaned containers: %s", e)
        return tasks
