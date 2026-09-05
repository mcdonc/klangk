"""Container lifecycle audit events (#2915).

Every container start and stop is recorded with the acting principal
(the user / agent who fired it, or ``system`` for autonomous causes
like idle timeouts, evictions, drains, and the boot auto-start) plus
the identifiers an operator needs to correlate the event with podman:
the workspace container id and, for egress-filtered workspaces, the
network sidecar container whose netns the workspace shares.

Recording is best-effort: an audit write failure is logged and never
fails the start/stop path it annotates (see
``ContainerRegistry.record_container_event``), and every failure
bumps a counter surfaced on ``/audit`` (#3154). With
``KLANGKD_AUDIT_FAIL_CLOSED`` the *interactive* API transitions
instead write their row before acting and refuse the request (503)
when it cannot be written — see ``INTERACTIVE_START_CAUSES`` /
``INTERACTIVE_STOP_CAUSES`` and the registry's pre-write helpers.

Retention/bounding (#2924) mirrors the egress-consent pruning design
(#2303): :meth:`ContainerEventsModel.prune` deletes rows past a
retention window and trims overflow past a deploy-wide row cap,
keeping the newest. An admin-facing paged view is tracked separately.
"""

import logging
import time

from .audit_hmac import compute_container_event_hmac
from .base import Submodel, resolve_prune_now
from .users import AGENT_USER_ID

logger = logging.getLogger(__name__)

# Event kinds.
EVENT_START = "start"
EVENT_STOP = "stop"

# Actor classification, derived from ``actor_id`` at record time:
# the fixed system-agent identity is its own class; a missing actor is
# an autonomous (system) cause.
ACTOR_USER = "user"
ACTOR_AGENT = "agent"
ACTOR_SYSTEM = "system"

# Canonical causes. Start causes:
CAUSE_API = "api"  # POST /start
CAUSE_CREATE = "create"  # eager start at workspace create (auto_start body)
CAUSE_WS_CONNECT = "ws_connect"  # first WS connection started the container
CAUSE_AUTO_START = "auto_start"  # boot-time auto_start_workspaces sweep
CAUSE_CRASH_RESTART = "crash_restart"  # crash monitor's delayed restart
# Stop causes:
CAUSE_STOP = "stop"  # POST /stop
CAUSE_RESTART = "restart"  # either half of POST /restart (stop + fresh start)
CAUSE_DELETE = "delete"  # workspace deletion cascade
CAUSE_IDLE_TIMEOUT = "idle_timeout"  # inactivity stop (container/idle.py)
CAUSE_EVICTION = "eviction"  # memory-pressure eviction (container/eviction.py)
CAUSE_LOGOUT = "logout"  # owner logged out (stop_user_containers)
CAUSE_CRASH_TEARDOWN = "crash_teardown"  # crash monitor removing a corpse
CAUSE_DRAIN = "drain"  # graceful-restart / scheduled drain
CAUSE_SHUTDOWN = "shutdown"  # klangkd shutdown orphan sweep
CAUSE_SIDECAR_START = "sidecar_start"  # network sidecar created for a start
CAUSE_SIDECAR_STOP = "sidecar_stop"  # network sidecar torn down
CAUSE_SIDECAR_DEPENDENT = (
    "sidecar_dependent"  # workspace container force-removed to free a sidecar
)
CAUSE_REAP = "reap"  # boot reaps (instance leftovers / dead owners)

# Which klangk-managed container a row describes. Workspace rows carry
# actor attribution; sidecar rows are always system-caused (their
# lifecycle is slaved to the workspace's).
ROLE_WORKSPACE = "workspace"
ROLE_SIDECAR = "network-sidecar"

# Interactive (API-request) causes (#3154, security finding):
# the only transitions eligible for audit fail-closed. These causes are
# fired exclusively by user-initiated HTTP endpoints; everything else
# (ws_connect, auto_start, idle_timeout, eviction, drain, shutdown,
# crash_teardown, logout, reap, the sidecar causes) is autonomous or
# non-API and never refuses to act on an audit failure — see
# ``AuditWriteError``.
INTERACTIVE_START_CAUSES = frozenset({CAUSE_API, CAUSE_CREATE, CAUSE_RESTART})
INTERACTIVE_STOP_CAUSES = frozenset({CAUSE_STOP, CAUSE_RESTART, CAUSE_DELETE})

# Canonical column list so the read shape cannot drift from the schema
# (a column added to the table is added here once).
_EVENT_COLUMNS = (
    "id, workspace_id, event, actor_type, actor_id, cause,"
    " container_id, container_role, network_namespace, created_at,"
    " hmac"
)


def workspace_filter_clause(
    workspace: str | None, workspace_id: str | None
) -> tuple[str, list]:
    """WHERE clause + params narrowing event reads to one workspace.

    ``workspace`` (#3006) is the id-or-name query the admin UI sends:
    an exact workspace-id match or a workspace whose *name* contains the
    text, so typing either narrows the history. It wins over the legacy
    exact ``workspace_id`` param when both are sent. Name matching is
    SQLite ``LIKE`` — ASCII case-insensitive, ``%``/``_`` wildcards not
    escaped (the same convention as the other admin filters). An empty
    string for either param means no filter, so a degenerate
    ``?workspace=`` cannot silently narrow the audit history.
    """
    if workspace:
        return (
            " WHERE (workspace_id = ? OR workspace_id IN"
            " (SELECT id FROM workspaces WHERE name LIKE '%' || ? || '%'))",
            [workspace, workspace],
        )
    if workspace_id:
        return " WHERE workspace_id = ?", [workspace_id]
    return "", []


def actor_type_for(actor_id: str | None) -> str:
    """Classify an acting principal id into an ``actor_type``."""
    if actor_id is None:
        return ACTOR_SYSTEM
    if actor_id == AGENT_USER_ID:
        return ACTOR_AGENT
    return ACTOR_USER


def row_to_dict(row) -> dict:
    """Row-tuple -> dict with the column names of ``_EVENT_COLUMNS``."""
    keys = [c.strip() for c in _EVENT_COLUMNS.split(",")]
    return dict(zip(keys, row, strict=True))


class ContainerEventsModel(Submodel):
    """CRUD for the ``container_events`` table."""

    async def record(
        self,
        workspace_id: str,
        event: str,
        cause: str,
        *,
        actor_id: str | None = None,
        container_id: str | None = None,
        network_namespace: str | None = None,
        container_role: str = ROLE_WORKSPACE,
    ) -> None:
        """Insert one lifecycle event row.

        ``actor_type`` is derived from ``actor_id`` (None -> system,
        the agent identity -> agent, anything else -> user).
        ``container_role`` distinguishes workspace containers from their
        network sidecars; sidecar rows never carry a netns owner (they
        ARE the netns owner).

        Returns the new row id so a fail-closed pre-write (#3154) can
        later finalize it (``finalize_event``) once the podman ids are
        known, or retract it (``retract_event``) when the transition it
        predicted never happened.
        """
        created_at = time.time()
        actor_type = actor_type_for(actor_id)
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "INSERT INTO container_events"
                " (workspace_id, event, actor_type, actor_id, cause,"
                "  container_id, container_role, network_namespace,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    event,
                    actor_type,
                    actor_id,
                    cause,
                    container_id,
                    container_role,
                    network_namespace,
                    created_at,
                ),
            )
            row_id = cursor.lastrowid
            row = {
                "id": row_id,
                "workspace_id": workspace_id,
                "event": event,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "cause": cause,
                "container_id": container_id,
                "container_role": container_role,
                "network_namespace": network_namespace,
                "created_at": created_at,
            }
            tag = compute_container_event_hmac(self.app.state.settings, row)
            if tag is not None:
                await db.execute(
                    "UPDATE container_events SET hmac = ? WHERE id = ?",
                    (tag, row_id),
                )
            return row_id

    @staticmethod
    def _finalize_updates(
        container_id: str | None, network_namespace: str | None
    ) -> dict:
        """The non-None field updates a finalize applies (#3154) — a
        None argument means "still unknown", never "clear it"."""
        return {
            col: value
            for col, value in (
                ("container_id", container_id),
                ("network_namespace", network_namespace),
            )
            if value is not None
        }

    async def finalize_event(
        self,
        event_id: int,
        *,
        container_id: str | None = None,
        network_namespace: str | None = None,
    ) -> None:
        """Fill a pre-written row's post-transition fields (#3154).

        Audit-before-act writes the row first; once the transition has
        happened, the podman-assigned ``container_id`` and the live
        netns owner are filled in here. A None argument means "still
        unknown", never "clear the column" — the row keeps whatever
        it already holds.

        #3174: the integrity tag covers the finalized fields, so it is
        recomputed over the row's post-update content — and cleared
        when tagging is off at finalize time, because a stale tag over
        changed fields would read as tampering to an external checker.
        A row pruned between pre-write and finalize settles nothing.
        """
        updates = self._finalize_updates(container_id, network_namespace)
        if not updates:
            return
        row = await self.app.state.db.fetchone(
            f"SELECT {_EVENT_COLUMNS} FROM container_events WHERE id = ?",
            (event_id,),
        )
        if row is None:
            return
        fields = row_to_dict(row)
        fields.update(updates)
        tag = compute_container_event_hmac(self.app.state.settings, fields)
        sets = ", ".join(f"{col} = ?" for col in updates)
        params = (*updates.values(), tag, event_id)
        async with self.app.state.db.transaction() as db:
            await db.execute(
                f"UPDATE container_events SET {sets}, hmac = ? WHERE id = ?",
                params,
            )

    async def retract_event(self, event_id: int) -> None:
        """Delete a pre-written row whose transition never happened
        (#3154).

        Audit-before-act writes the row first; when the action then
        fails before any state changed (admission refusal, failed
        podman stop, 'connected' attach to an already-running
        container), the row describes an event that did not occur and
        is removed so the trail stays truthful.
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "DELETE FROM container_events WHERE id = ?", (event_id,)
            )

    async def list_events(
        self,
        workspace_id: str | None = None,
        *,
        workspace: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest-first event history, optionally filtered to a workspace
        (exact ``workspace_id``, or id-or-name via ``workspace``)."""
        where, params = workspace_filter_clause(workspace, workspace_id)
        sql = f"SELECT {_EVENT_COLUMNS} FROM container_events{where}"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = await self.app.state.db.fetchall(sql, (*params, limit, offset))
        return [row_to_dict(row) for row in rows]

    async def count_events(
        self,
        workspace_id: str | None = None,
        *,
        workspace: str | None = None,
    ) -> int:
        """Row count for paging, with the same optional workspace filter."""
        where, params = workspace_filter_clause(workspace, workspace_id)
        row = await self.app.state.db.fetchone(
            f"SELECT COUNT(*) FROM container_events{where}", tuple(params)
        )
        return row[0] if row else 0

    async def prune(self, now: float | None = None) -> int:
        """Bound the table: delete rows past retention / over the row cap
        (#2924). Returns the number of rows deleted.

        Two passes, both pure history (unlike the egress-consent prune
        there is no in-effect exemption -- every row is terminal at write
        time, so nothing here is enforcement state):

        - **Retention** (``container_events_retention_days`` > 0): delete
          rows whose ``created_at`` is older than the window.
        - **Row cap** (``container_events_row_cap`` > 0, deploy-wide): when
          the total row count exceeds the cap, delete the oldest rows down
          to it, keeping the newest.
        """
        settings = self.app.state.settings
        retention_days = settings.container_events_retention_days
        row_cap = settings.container_events_row_cap
        if retention_days <= 0 and row_cap <= 0:
            return 0
        when = resolve_prune_now(now)
        deleted = 0
        if retention_days > 0:
            deleted += await self._prune_retention(when, retention_days)
        if row_cap > 0:
            deleted += await self._prune_row_cap(row_cap)
        return deleted

    async def _prune_retention(self, now: float, retention_days: int) -> int:
        """Retention pass: delete rows older than the window."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM container_events WHERE created_at < ?",
                (now - retention_days * 86400.0,),
            )
            return cursor.rowcount

    async def _prune_row_cap(self, row_cap: int) -> int:
        """Deploy-wide cap: delete the oldest rows over the cap, keeping
        the newest (``created_at DESC, id DESC`` -- the same tie-break
        :meth:`list_events` uses, so "newest" is stable across equal
        timestamps). ``LIMIT -1 OFFSET n`` is SQLite's "all but the first
        n" -- a single statement, no snapshot, so no TOCTOU."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM container_events WHERE id IN"
                " (SELECT id FROM container_events"
                " ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (row_cap,),
            )
            return cursor.rowcount
