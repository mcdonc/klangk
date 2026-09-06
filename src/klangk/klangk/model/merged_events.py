"""Time-correlated merged audit stream (#3251).

A read-only merge of the three audit tables — ``audit_events``
(identity/privilege, #3205), ``container_events`` (lifecycle, #2915),
and ``egress_consent`` (#2239) — into one time-ordered stream, so a
reviewer can replay "what did this actor do, in order" across all
components (ASD-STIG SV-222439: an attack trail crosses components —
a login, a workspace start, an egress-consent decision — and reading
each table separately loses the interleaving).

Each merged row names its origin table in ``source`` (``audit`` /
``container`` / ``egress``), carries the common correlation fields
(timestamp, event name, actor, workspace — an audit row whose target
*is* a workspace carries that workspace, an account-scoped one
none), and embeds the full origin row in ``data`` minus the
verification-internal HMAC tag (#3174 — integrity verification stays
with the separate per-table work). One merged row per origin row: an
egress row is named ``egress.<decision>`` after its current state,
timestamped by ``requested_at``, and attributed to the revoker or
decider when a human has acted on it.

Filters (all optional, :class:`MergedEventFilters`): an inclusive
time window (``since`` / ``until``, epoch seconds, applied to each
table's own event timestamp), an ``actor`` substring (actor id, or
email where the row's id resolves through the ``users`` table), a
``workspace`` id-or-name, and an ``event``-name substring.
"""

from dataclasses import dataclass

from .base import Submodel

# Origin-table discriminators, as shipped in each merged row's
# ``source`` field.
SOURCE_AUDIT = "audit"
SOURCE_CONTAINER = "container"
SOURCE_EGRESS = "egress"

# The uniform projection every branch of the union emits. ``row_id``
# keeps its native type per branch (INTEGER for the two event tables,
# the TEXT uuid for consent rows); the page ordering never compares
# ids across branches because the ``source`` tiebreak runs first.
# ``actor_type`` is the container table's native classification;
# audit and egress actors are always human (``user``) when present.
_PAGE_COLUMNS = (
    "source",
    "row_id",
    "created_at",
    "event",
    "actor_id",
    "actor_email",
    "workspace_id",
    "actor_type",
)

_AUDIT_SELECT = (
    "SELECT 'audit' AS source, id AS row_id, created_at, event,"
    " actor_id, actor_email,"
    " CASE WHEN target_type = 'workspace' THEN target_id END"
    " AS workspace_id,"
    " CASE WHEN actor_id IS NOT NULL THEN 'user' END AS actor_type"
    " FROM audit_events"
)
_CONTAINER_SELECT = (
    "SELECT 'container' AS source, id AS row_id, created_at, event,"
    " actor_id, NULL AS actor_email, workspace_id, actor_type"
    " FROM container_events"
)
_EGRESS_SELECT = (
    "SELECT 'egress' AS source, id AS row_id, requested_at AS"
    " created_at, 'egress.' || decision AS event,"
    " COALESCE(revoked_by, decided_by) AS actor_id,"
    " NULL AS actor_email, workspace_id,"
    " CASE WHEN COALESCE(revoked_by, decided_by) IS NOT NULL"
    " THEN 'user' END AS actor_type"
    " FROM egress_consent"
)

# A workspace filter narrows workspace-carrying tables by exact id or
# by a workspace whose *name* contains the text (the ``workspace``
# convention of the container-events view, #3006).
_WORKSPACE_ID_OR_NAME = (
    "(workspace_id = ? OR workspace_id IN"
    " (SELECT id FROM workspaces WHERE name LIKE '%' || ? || '%'))"
)

# An actor substring matches the row's stored actor id directly, or an
# actor whose email matches through the users table (audit rows carry
# a denormalized email, so they need no join).
_ACTOR_ID_OR_EMAIL = (
    "(actor_id LIKE '%' || ? || '%' OR actor_id IN"
    " (SELECT id FROM users WHERE email LIKE '%' || ? || '%'))"
)


@dataclass(frozen=True)
class MergedEventFilters:
    """The shared filter set of the merged stream's list/count reads.

    Every field is optional; ``None`` (or an empty string) means "no
    filter on that axis". ``since`` / ``until`` are inclusive epoch
    seconds compared against each table's own event timestamp
    (``created_at`` for the event tables, ``requested_at`` for
    consent rows).
    """

    since: float | None = None
    until: float | None = None
    actor: str | None = None
    workspace: str | None = None
    event: str | None = None


def time_window_conditions(conditions, params, column, filters) -> None:
    """Append the inclusive since/until bounds on one timestamp column."""
    if filters.since is not None:
        conditions.append(f"{column} >= ?")
        params.append(filters.since)
    if filters.until is not None:
        conditions.append(f"{column} <= ?")
        params.append(filters.until)


def where_clause(conditions) -> str:
    """Join already-built conditions into a WHERE clause ('' when none)."""
    return f" WHERE {' AND '.join(conditions)}" if conditions else ""


def audit_branch_clause(filters: MergedEventFilters) -> tuple[str, list]:
    """The audit_events branch's WHERE clause + params.

    The workspace axis matches rows whose *target* is the workspace:
    audit rows name a workspace through ``target_type='workspace'``
    (the share/role/ACL/transfer events), not a workspace column.
    """
    conditions: list[str] = []
    params: list = []
    time_window_conditions(conditions, params, "created_at", filters)
    if filters.event:
        conditions.append("event LIKE '%' || ? || '%'")
        params.append(filters.event)
    if filters.actor:
        conditions.append(
            "(actor_id LIKE '%' || ? || '%' OR actor_email LIKE"
            " '%' || ? || '%')"
        )
        params.extend((filters.actor, filters.actor))
    if filters.workspace:
        conditions.append(
            "(target_type = 'workspace' AND (target_id = ? OR target_id IN"
            " (SELECT id FROM workspaces WHERE name LIKE '%' || ? || '%')))"
        )
        params.extend((filters.workspace, filters.workspace))
    return where_clause(conditions), params


def container_branch_clause(filters: MergedEventFilters) -> tuple[str, list]:
    """The container_events branch's WHERE clause + params."""
    conditions: list[str] = []
    params: list = []
    time_window_conditions(conditions, params, "created_at", filters)
    if filters.event:
        conditions.append("event LIKE '%' || ? || '%'")
        params.append(filters.event)
    if filters.actor:
        conditions.append(_ACTOR_ID_OR_EMAIL)
        params.extend((filters.actor, filters.actor))
    if filters.workspace:
        conditions.append(_WORKSPACE_ID_OR_NAME)
        params.extend((filters.workspace, filters.workspace))
    return where_clause(conditions), params


def egress_branch_clause(filters: MergedEventFilters) -> tuple[str, list]:
    """The egress_consent branch's WHERE clause + params.

    The actor axis matches the decider or the revoker; the event axis
    matches the synthesized ``egress.<decision>`` name.
    """
    conditions: list[str] = []
    params: list = []
    time_window_conditions(conditions, params, "requested_at", filters)
    if filters.event:
        conditions.append("('egress.' || decision) LIKE '%' || ? || '%'")
        params.append(filters.event)
    if filters.actor:
        conditions.append(
            "(decided_by LIKE '%' || ? || '%' OR revoked_by LIKE"
            " '%' || ? || '%' OR decided_by IN"
            " (SELECT id FROM users WHERE email LIKE '%' || ? || '%')"
            " OR revoked_by IN"
            " (SELECT id FROM users WHERE email LIKE '%' || ? || '%'))"
        )
        params.extend(
            (filters.actor, filters.actor, filters.actor, filters.actor)
        )
    if filters.workspace:
        conditions.append(_WORKSPACE_ID_OR_NAME)
        params.extend((filters.workspace, filters.workspace))
    return where_clause(conditions), params


def merged_union(filters: MergedEventFilters) -> tuple[str, list]:
    """The three-branch ``UNION ALL`` subquery + params (placeholder
    order: branch order, then each branch's WHERE order)."""
    parts: list[str] = []
    params: list = []
    for select, clause in (
        (_AUDIT_SELECT, audit_branch_clause),
        (_CONTAINER_SELECT, container_branch_clause),
        (_EGRESS_SELECT, egress_branch_clause),
    ):
        where, branch_params = clause(filters)
        parts.append(f"{select}{where}")
        params.extend(branch_params)
    return " UNION ALL ".join(parts), params


class MergedEventsModel(Submodel):
    """Read-only merged view over the three audit tables (#3251)."""

    async def list_events(
        self,
        filters: MergedEventFilters | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest-first merged stream page (the filters of
        :class:`MergedEventFilters`, LIMIT/OFFSET over the merge)."""
        if filters is None:
            filters = MergedEventFilters()
        union, params = merged_union(filters)
        sql = (
            f"SELECT {', '.join(_PAGE_COLUMNS)} FROM ({union})"
            " ORDER BY created_at DESC, source ASC, row_id DESC"
            " LIMIT ? OFFSET ?"
        )
        rows = await self.app.state.db.fetchall(sql, (*params, limit, offset))
        page = [dict(zip(_PAGE_COLUMNS, row, strict=True)) for row in rows]
        full = await self.full_rows(page)
        return [self.merged_row(entry, full) for entry in page]

    async def count_events(
        self, filters: MergedEventFilters | None = None
    ) -> int:
        """Row count for paging, over the same merged union + filters."""
        if filters is None:
            filters = MergedEventFilters()
        union, params = merged_union(filters)
        row = await self.app.state.db.fetchone(
            f"SELECT COUNT(*) FROM ({union})", tuple(params)
        )
        return row[0] if row else 0

    async def full_rows(self, page: list[dict]) -> dict[str, dict]:
        """Full origin rows for one merged page, per source, keyed by id.

        A row pruned between the union read and this fetch is simply
        absent — its merged row keeps the union's columns with an
        empty ``data`` (the prune windows are the same three tables,
        so the race is narrow and the row was history being deleted
        anyway).
        """
        ids = {SOURCE_AUDIT: [], SOURCE_CONTAINER: [], SOURCE_EGRESS: []}
        for entry in page:
            ids[entry["source"]].append(entry["row_id"])
        model = self.app.state.model
        return {
            SOURCE_AUDIT: await model.audit_events.rows_by_ids(
                ids[SOURCE_AUDIT]
            ),
            SOURCE_CONTAINER: await model.container_events.rows_by_ids(
                ids[SOURCE_CONTAINER]
            ),
            SOURCE_EGRESS: await model.egress_consent.rows_by_ids(
                ids[SOURCE_EGRESS]
            ),
        }

    @staticmethod
    def merged_row(entry: dict, full: dict[str, dict]) -> dict:
        """One union entry + its full origin row -> the wire shape.

        ``data`` embeds the origin row whole (minus the
        verification-internal HMAC tag, #3174) so source-specific
        detail stays reachable without widening the common columns.
        """
        data = full[entry["source"]].get(entry["row_id"], {})
        return {
            "source": entry["source"],
            "id": entry["row_id"],
            "created_at": entry["created_at"],
            "event": entry["event"],
            "actor_id": entry["actor_id"],
            "actor_email": entry["actor_email"],
            "workspace_id": entry["workspace_id"],
            "actor_type": entry["actor_type"],
            "data": {k: v for k, v in data.items() if k != "hmac"},
        }
