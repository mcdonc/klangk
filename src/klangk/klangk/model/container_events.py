"""Container lifecycle audit events (#2915).

Every container start and stop is recorded with the acting principal
(the user / agent who fired it, or ``system`` for autonomous causes
like idle timeouts, evictions, drains, and the boot auto-start) plus
the identifiers an operator needs to correlate the event with podman:
the workspace container id and, for egress-filtered workspaces, the
network sidecar container whose netns the workspace shares.

Recording is best-effort: an audit write failure is logged and never
fails the start/stop path it annotates (see
``ContainerRegistry.record_container_event``).

Retention/bounding (#2924) mirrors the egress-consent pruning design
(#2303): :meth:`ContainerEventsModel.prune` deletes rows past a
retention window and trims overflow past a deploy-wide row cap,
keeping the newest. An admin-facing paged view is tracked separately.
"""

import logging
import time

from .base import Submodel
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

# Canonical column list so the read shape cannot drift from the schema
# (a column added to the table is added here once).
_EVENT_COLUMNS = (
    "id, workspace_id, event, actor_type, actor_id, cause,"
    " container_id, container_role, network_namespace, created_at"
)


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


def _resolve_prune_now(now: float | None) -> float:
    """The sweep's reference clock (caller-supplied or wall clock)."""
    return time.time() if now is None else now


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
        """
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT INTO container_events"
                " (workspace_id, event, actor_type, actor_id, cause,"
                "  container_id, container_role, network_namespace,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    event,
                    actor_type_for(actor_id),
                    actor_id,
                    cause,
                    container_id,
                    container_role,
                    network_namespace,
                    time.time(),
                ),
            )

    async def list_events(
        self,
        workspace_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest-first event history, optionally filtered to a workspace."""
        sql = f"SELECT {_EVENT_COLUMNS} FROM container_events"
        params: list = []
        if workspace_id is not None:
            sql += " WHERE workspace_id = ?"
            params.append(workspace_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = await self.app.state.db.fetchall(sql, (*params, limit, offset))
        return [row_to_dict(row) for row in rows]

    async def count_events(self, workspace_id: str | None = None) -> int:
        """Row count for paging, with the same optional workspace filter."""
        sql = "SELECT COUNT(*) FROM container_events"
        params: tuple = ()
        if workspace_id is not None:
            sql += " WHERE workspace_id = ?"
            params = (workspace_id,)
        row = await self.app.state.db.fetchone(sql, params)
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
        when = _resolve_prune_now(now)
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
