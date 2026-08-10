"""Egress consent CRUD for interactive egress filtering (#2239).

Tracks per-workspace consent requests (blocked outbound connections that
need human approval) and their decisions.  Each row represents a single
destination (host + optional port) that a workspace process tried to
reach while in ``egress_mode='interactive'``.
"""

import time
import uuid


# Decision lifecycle values.
DECISION_PENDING = "pending"
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"  # auto-denied on timeout, distinct from user deny
DECISIONS = frozenset(
    {DECISION_PENDING, DECISION_ALLOWED, DECISION_DENIED, DECISION_EXPIRED}
)

# Scope values for an allow/deny decision.
SCOPE_ONCE = (
    "once"  # container lifetime only, not persisted to allowed_domains
)
SCOPE_WORKSPACE = "workspace"  # persisted to workspace's allowed_domains
SCOPE_DEPLOY = "deploy"  # promoted to deploy-wide default
SCOPES = frozenset({SCOPE_ONCE, SCOPE_WORKSPACE, SCOPE_DEPLOY})


class EgressConsentModel:
    """CRUD for the ``egress_consent`` table."""

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def create_request(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None = None,
        pid: int | None = None,
        process_name: str | None = None,
    ) -> dict | None:
        """Insert a pending consent request, or return None if one already
        exists for this (workspace, host, port).

        Uses ``INSERT OR IGNORE`` against the partial unique index
        ``idx_egress_consent_pending_dedup`` to atomically deduplicate —
        no TOCTOU between a separate has_pending() check and the insert.
        """
        request_id = str(uuid.uuid4())
        requested_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO egress_consent"
                " (id, workspace_id, dest_host, dest_port,"
                "  pid, process_name, decision, requested_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    workspace_id,
                    dest_host,
                    dest_port,
                    pid,
                    process_name,
                    DECISION_PENDING,
                    requested_at,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return {
            "id": request_id,
            "workspace_id": workspace_id,
            "dest_host": dest_host,
            "dest_port": dest_port,
            "pid": pid,
            "process_name": process_name,
            "decision": DECISION_PENDING,
            "scope": None,
            "requested_at": requested_at,
            "decided_at": None,
            "decided_by": None,
        }

    async def record_static_denial(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None = None,
    ) -> dict | None:
        """Insert a static-mode denial: denied by policy, no human
        (``decided_by`` NULL), immediately -- no pending state, no timeout.

        Static mode only ever records denials (the sidecar observes only
        blocked/NXDOMAIN'd traffic; allowed traffic passes through
        unobserved). Dedup: at most one static denial per (workspace, host,
        port) via ``idx_egress_consent_static_dedup`` (INSERT OR IGNORE), so
        a flooding workspace can't spam denial rows. Returns the row, or
        None if one already exists.
        """
        request_id = str(uuid.uuid4())
        now = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO egress_consent"
                " (id, workspace_id, dest_host, dest_port,"
                "  decision, requested_at, decided_at, decided_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    workspace_id,
                    dest_host,
                    dest_port,
                    DECISION_DENIED,
                    now,
                    now,
                    None,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return {
            "id": request_id,
            "workspace_id": workspace_id,
            "dest_host": dest_host,
            "dest_port": dest_port,
            "pid": None,
            "process_name": None,
            "decision": DECISION_DENIED,
            "scope": None,
            "requested_at": now,
            "decided_at": now,
            "decided_by": None,
        }

    async def get_request(self, request_id: str) -> dict | None:
        """Get a single consent request by ID."""
        row = await self.app.state.db.fetchone(
            "SELECT id, workspace_id, dest_host, dest_port,"
            " pid, process_name,"
            " decision, scope, requested_at, decided_at, decided_by"
            " FROM egress_consent WHERE id = ?",
            (request_id,),
        )
        if row is None:
            return None
        return _row_to_dict(row)

    async def list_requests(
        self,
        workspace_id: str,
        decision: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List consent requests for a workspace, optionally filtered by decision."""
        async with self.app.state.db.transaction() as db:
            if decision is not None:
                cursor = await db.execute(
                    "SELECT id, workspace_id, dest_host, dest_port,"
                    " pid, process_name,"
                    " decision, scope, requested_at, decided_at, decided_by"
                    " FROM egress_consent"
                    " WHERE workspace_id = ? AND decision = ?"
                    " ORDER BY requested_at DESC LIMIT ?",
                    (workspace_id, decision, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, workspace_id, dest_host, dest_port,"
                    " pid, process_name,"
                    " decision, scope, requested_at, decided_at, decided_by"
                    " FROM egress_consent"
                    " WHERE workspace_id = ?"
                    " ORDER BY requested_at DESC LIMIT ?",
                    (workspace_id, limit),
                )
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]

    async def count_pending(self, workspace_id: str) -> int:
        """Count pending requests for a workspace.

        No caller gates on this today (the interactive-mode consent flow
        — NFLOG listener, API, daemon — is not yet wired, #2242/#2254); the
        method is foundation for a future rate-limit on consent-request
        creation.
        """
        row = await self.app.state.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM egress_consent"
            " WHERE workspace_id = ? AND decision = ?",
            (workspace_id, DECISION_PENDING),
        )
        return row["cnt"] if row else 0

    async def has_pending(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None,
    ) -> bool:
        """Check if a pending request already exists for this destination."""
        if dest_port is not None:
            row = await self.app.state.db.fetchone(
                "SELECT 1 FROM egress_consent"
                " WHERE workspace_id = ? AND dest_host = ?"
                " AND dest_port = ? AND decision = ?",
                (workspace_id, dest_host, dest_port, DECISION_PENDING),
            )
        else:
            row = await self.app.state.db.fetchone(
                "SELECT 1 FROM egress_consent"
                " WHERE workspace_id = ? AND dest_host = ?"
                " AND dest_port IS NULL AND decision = ?",
                (workspace_id, dest_host, DECISION_PENDING),
            )
        return row is not None

    async def decide(
        self,
        request_id: str,
        decision: str,
        scope: str | None,
        decided_by: str,
    ) -> dict | None:
        """Record a decision on a pending request.

        Returns the updated row dict, or ``None`` if the request doesn't
        exist or is no longer pending. Raises ``ValueError`` for invalid
        decision/scope values.
        """
        if decision not in (DECISION_ALLOWED, DECISION_DENIED):
            raise ValueError(
                f"Invalid decision: {decision!r}"
                f" (must be {DECISION_ALLOWED!r} or {DECISION_DENIED!r})"
            )
        if scope is not None and scope not in SCOPES:
            raise ValueError(f"Invalid scope: {scope!r}")
        decided_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, scope = ?, decided_at = ?, decided_by = ?"
                " WHERE id = ? AND decision = ?",
                (
                    decision,
                    scope,
                    decided_at,
                    decided_by,
                    request_id,
                    DECISION_PENDING,
                ),
            )
            if cursor.rowcount == 0:
                return None
            # Re-read inside the same transaction so the result is
            # consistent even if the row is deleted concurrently.
            cursor = await db.execute(
                "SELECT id, workspace_id, dest_host, dest_port,"
                " pid, process_name,"
                " decision, scope, requested_at, decided_at, decided_by"
                " FROM egress_consent WHERE id = ?",
                (request_id,),
            )
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None

    async def expire_pending(
        self,
        request_id: str,
    ) -> bool:
        """Auto-expire a pending request (timeout). Returns True if updated.

        Uses ``DECISION_EXPIRED`` so the audit trail distinguishes a
        human deny from an unattended timeout.
        """
        decided_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, decided_at = ?"
                " WHERE id = ? AND decision = ?",
                (DECISION_EXPIRED, decided_at, request_id, DECISION_PENDING),
            )
            return cursor.rowcount > 0

    async def expire_all_pending(self) -> int:
        """Expire EVERY pending request (startup reaping).

        On startup the in-memory holds are gone, so any row still ``pending``
        from a prior run is an orphan with no live hold to resolve it. Mark
        them ``expired`` so :meth:`list_requests` (the decider snapshot) does
        not replay them to a freshly-connected decider. Returns the count
        reaped.
        """
        decided_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, decided_at = ?"
                " WHERE decision = ?",
                (DECISION_EXPIRED, decided_at, DECISION_PENDING),
            )
            return cursor.rowcount

    async def delete_for_workspace(self, workspace_id: str) -> int:
        """Delete all consent records for a workspace. Returns count."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM egress_consent WHERE workspace_id = ?",
                (workspace_id,),
            )
            return cursor.rowcount


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "dest_host": row["dest_host"],
        "dest_port": row["dest_port"],
        "pid": row["pid"],
        "process_name": row["process_name"],
        "decision": row["decision"],
        "scope": row["scope"],
        "requested_at": row["requested_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
    }
