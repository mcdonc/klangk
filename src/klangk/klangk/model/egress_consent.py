"""Egress consent CRUD for interactive egress filtering (#2239).

Tracks per-workspace consent requests (blocked outbound connections that
need human approval) and their decisions.  Each row represents a single
destination (host + optional port) that a workspace process tried to
reach while in ``egress_mode='interactive'``.
"""

import time
import uuid

from .audit_hmac import (
    compute_egress_consent_hmac,
    integrity_report,
)
from .base import Submodel, resolve_prune_now


# Decision lifecycle values.
DECISION_PENDING = "pending"
DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"  # auto-denied on timeout, distinct from user deny
DECISION_REVOKED = "revoked"  # a prior allow/deny undone by a decider (#2339)
DECISIONS = frozenset(
    {
        DECISION_PENDING,
        DECISION_ALLOWED,
        DECISION_DENIED,
        DECISION_EXPIRED,
        DECISION_REVOKED,
    }
)

# Duration values for an allow/deny decision (#2328): how long the sidecar
# honors it (allow learns the IP for T; deny REJECTs for T). `once` = this
# connection only; `tilrestart` ("until restart") = the workspace container's
# lifetime (the sidecar's in-memory rules); `forever` = the workspace's lifetime
# -- persists across
# container/sidecar restarts: an allow persists via an `allowed_domains`
# mutation the sidecar re-reads on start (#2368) -- best-effort, so a failed
# mutation means the allow won't survive restart despite the audit row here;
# the deny counterpart (`rejected_domains`) is #2369.
DURATION_ONCE = "once"
# Test-only short duration (#2363, subsumed by #2392): accepted by the
# validator + honored by the sidecar so a timed verdict's expiry can be
# exercised in seconds. TEMPORARILY exposed in the human-facing duration
# selectors (CLI TUI + Flutter) for manual testing (#2465); remove it from
# those selectors to hide it again -- it stays valid for programmatic/test
# callers either way.
DURATION_5S = "5s"
DURATION_5M = "5m"
DURATION_15M = "15m"
DURATION_1H = "1h"
DURATION_1D = "1d"
DURATION_1W = "1w"
DURATION_TILRESTART = "tilrestart"
DURATION_FOREVER = "forever"
DURATIONS = frozenset(
    {
        DURATION_ONCE,
        DURATION_5S,
        DURATION_5M,
        DURATION_15M,
        DURATION_1H,
        DURATION_1D,
        DURATION_1W,
        DURATION_TILRESTART,
        DURATION_FOREVER,
    }
)
DURATION_DEFAULT = DURATION_TILRESTART

# Canonical column list for SELECTs + _row_to_dict, so the read shape cannot
# drift from the schema (a column added to the table is added here once).
_EC_COLUMNS = (
    "id, workspace_id, dest_host, dest_port, pid, process_name,"
    " decision, duration, requested_at, decided_at, decided_by,"
    " revoked_at, revoked_by, hmac"
)


class EgressConsentModel(Submodel):
    """CRUD for the ``egress_consent`` table."""

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

        The row is re-read inside the same transaction (the :meth:`decide`
        pattern) and returned via :func:`_row_to_dict`, so the returned dict
        carries the full column set -- the exact shape ``list_requests``
        rows have. The live ``egress_request`` frame (``_fanout`` in the
        coordinator) frames this dict, so live and replayed
        (``snapshot``) frames cannot drift apart (#3082).
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
            return await _restamp(db, self.app.state.settings, request_id)

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
        return await self._record_static_row(
            DECISION_DENIED, workspace_id, dest_host, dest_port
        )

    async def record_static_allow(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None = None,
    ) -> dict | None:
        """Insert an allow-mode allow: permitted by policy, no human
        (``decided_by`` NULL), immediately -- no pending state, no timeout.

        ``allow`` egress mode (#2406) is default-permit: every off-list
        destination is recorded (logged) + auto-allowed with no consent prompt,
        mirroring how :meth:`record_static_denial` records a static mode denial.
        Dedup: at most one allow-mode row per (workspace, host, port) via
        ``idx_egress_consent_static_allow_dedup`` (INSERT OR IGNORE), so a
        flooding workspace can't spam allow rows. Returns the row, or None if
        one already exists.
        """
        return await self._record_static_row(
            DECISION_ALLOWED, workspace_id, dest_host, dest_port
        )

    async def _record_static_row(
        self,
        decision: str,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None,
    ) -> dict | None:
        """INSERT OR IGNORE one static-mode consent row; the new row, or
        None when the mode's dedup index already had it. Re-read inside the
        transaction (like :meth:`create_request`, #3082) so every returned
        row dict carries the full :func:`_row_to_dict` column set."""
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
                    decision,
                    now,
                    now,
                    None,
                ),
            )
            if cursor.rowcount == 0:
                return None
            return await _restamp(db, self.app.state.settings, request_id)

    async def get_request(self, request_id: str) -> dict | None:
        """Get a single consent request by ID."""
        row = await self.app.state.db.fetchone(
            f"SELECT {_EC_COLUMNS} FROM egress_consent WHERE id = ?",
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
        if decision is not None:
            rows = await self.app.state.db.fetchall(
                f"SELECT {_EC_COLUMNS} FROM egress_consent"
                " WHERE workspace_id = ? AND decision = ?"
                " ORDER BY requested_at DESC LIMIT ?",
                (workspace_id, decision, limit),
            )
        else:
            rows = await self.app.state.db.fetchall(
                f"SELECT {_EC_COLUMNS} FROM egress_consent"
                " WHERE workspace_id = ?"
                " ORDER BY requested_at DESC LIMIT ?",
                (workspace_id, limit),
            )
        return [_row_to_dict(row) for row in rows]

    # Duration string -> seconds, for the time-bounded in-effect window
    # (#2328). `once`/`tilrestart`/`forever` are not time-bounded (handled
    # separately in :meth:`_duration_in_effect`), so they're absent here.
    _DURATION_SECONDS = {
        DURATION_5S: 5,
        DURATION_5M: 300,
        DURATION_15M: 900,
        DURATION_1H: 3600,
        DURATION_1D: 86400,
        DURATION_1W: 604800,
    }

    async def active_verdict_for(
        self,
        workspace_id: str,
        dest_host: str,
        dest_port: int | None,
    ) -> dict | None:
        """The newest IN-EFFECT verdict for an exact (host, port), or None (#2332).

        Used by the consent-pause path (:meth:`ConsentCoordinator.hold`) to
        respect a recorded verdict while prompting is paused: a destination
        with an in-effect DENY is still blocked (not auto-allowed); everything
        else is auto-allowed. Returns the newest verdict whose duration has
        not elapsed, skipping elapsed ones, so a newer-but-elapsed allow does
        NOT mask an older in-effect deny (mirrors :meth:`list_active`'s
        in-effect filtering, scoped to one destination). Only verdict decisions
        (``decided_by`` not NULL) are considered -- static policy denials are
        the complement of the allow-list and not actionable here. Port is
        matched exactly (NULL port matches NULL port).
        """
        if dest_port is not None:
            query = (
                f"SELECT {_EC_COLUMNS} FROM egress_consent"
                " WHERE workspace_id = ? AND dest_host = ?"
                " AND dest_port = ? AND decision IN (?, ?)"
                " AND decided_by IS NOT NULL"
                " ORDER BY decided_at DESC"
            )
            params = (
                workspace_id,
                dest_host,
                dest_port,
                DECISION_ALLOWED,
                DECISION_DENIED,
            )
        else:
            query = (
                f"SELECT {_EC_COLUMNS} FROM egress_consent"
                " WHERE workspace_id = ? AND dest_host = ?"
                " AND dest_port IS NULL AND decision IN (?, ?)"
                " AND decided_by IS NOT NULL"
                " ORDER BY decided_at DESC"
            )
            params = (
                workspace_id,
                dest_host,
                DECISION_ALLOWED,
                DECISION_DENIED,
            )
        now = time.time()
        rows = await self.app.state.db.fetchall(query, params)
        # Newest-first: return the first verdict still in effect. An elapsed
        # newer verdict (e.g. an expired timed allow) is skipped so it can't
        # hide an older in-effect deny -- the pause must keep blocking a host
        # the user previously denied.
        for row in rows:
            d = _row_to_dict(row)
            if self._duration_in_effect(d["duration"], d["decided_at"], now):
                return d
        return None

    async def list_active(self, workspace_id: str) -> list[dict]:
        """Consent verdicts still in effect for a workspace (#2335 slice A).

        Returns allowed/denied rows whose duration hasn't elapsed, so a
        rule-management view can show "what's currently affecting networking".
        Each row carries its ``decision`` so the caller can group allows vs
        denies.

        - ``once`` -> excluded (consumed by the single connection).
        - ``5m``/``15m``/``1h``/``1d``/``1w`` -> in effect iff
          ``decided_at + duration`` > now.
        - ``tilrestart`` -> in effect (container lifetime). Reaped when the
          workspace container (re)starts (:meth:`clear_tilrestart_duration`,
          #2346), so the recorded set matches what the sidecar enforces across
          container restarts; a sidecar-only restart (no container restart)
          is the one residual gap.
        - ``forever`` -> in effect (workspace lifetime). An allow's
          cross-restart enforcement is via ``allowed_domains`` (#2368); the
          row itself is the audit record.

        Only **verdict** decisions are returned (``decided_by`` not NULL):
        static policy denials (``record_static_denial``, ``decided_by`` NULL)
        are the complement of the allow-list, infinite in number, and not
        actionable in the rules view. Expired / pending / revoked rows are
        never in effect.
        """
        now = time.time()
        rows = await self.app.state.db.fetchall(
            f"SELECT {_EC_COLUMNS} FROM egress_consent"
            " WHERE workspace_id = ? AND decision IN (?, ?)"
            " AND decided_by IS NOT NULL"
            " ORDER BY decided_at DESC",
            (workspace_id, DECISION_ALLOWED, DECISION_DENIED),
        )
        out = []
        for row in rows:
            if self._duration_in_effect(
                row["duration"], row["decided_at"], now
            ):
                out.append(_row_to_dict(row))
        return out

    @classmethod
    def _duration_in_effect(
        cls, duration: str | None, decided_at: float | None, now: float
    ) -> bool:
        """Whether a decision is still in effect at ``now`` (#2335 slice A)."""
        if decided_at is None:
            return False
        if duration in (DURATION_TILRESTART, DURATION_FOREVER):
            return True
        if duration == DURATION_ONCE:
            return False
        secs = cls._DURATION_SECONDS.get(duration)
        if secs is None:
            # Unknown / NULL duration: a verdict always sets one, so this only
            # hits a NULL (e.g. a future migration). Treat as not in effect
            # (fail-safe: don't claim an effect we can't bound).
            return False
        return decided_at + secs > now

    async def count_pending(self, workspace_id: str) -> int:
        """Count pending requests for a workspace.

        Gates the coordinator's per-workspace pending cap (flood bound).
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
        decided_by: str,
        duration: str = DURATION_DEFAULT,
    ) -> dict | None:
        """Record a decision on a pending request.

        Returns the updated row dict, or ``None`` if the request doesn't
        exist or is no longer pending. Raises ``ValueError`` for invalid
        decision/duration values.
        """
        if decision not in (DECISION_ALLOWED, DECISION_DENIED):
            raise ValueError(
                f"Invalid decision: {decision!r}"
                f" (must be {DECISION_ALLOWED!r} or {DECISION_DENIED!r})"
            )
        if duration not in DURATIONS:
            raise ValueError(f"Invalid duration: {duration!r}")
        decided_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, duration = ?,"
                " decided_at = ?, decided_by = ?"
                " WHERE id = ? AND decision = ?",
                (
                    decision,
                    duration,
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
            return await _restamp(db, self.app.state.settings, request_id)

    async def revoke(self, request_id: str, revoked_by: str) -> dict | None:
        """Mark a prior allow/deny verdict revoked (#2339).

        Only flips a row that is currently an active verdict (``allowed`` or
        ``denied``); pending/expired/already-revoked rows are untouched
        (returns None). Stamps ``revoked_at`` + ``revoked_by`` for audit (the
        original ``decided_*`` is preserved as the verdict's provenance).
        Returns the updated row, or None.
        """
        now = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, revoked_at = ?, revoked_by = ?"
                " WHERE id = ? AND decision IN (?, ?)",
                (
                    DECISION_REVOKED,
                    now,
                    revoked_by,
                    request_id,
                    DECISION_ALLOWED,
                    DECISION_DENIED,
                ),
            )
            if cursor.rowcount == 0:
                return None
            return await _restamp(db, self.app.state.settings, request_id)

    async def expire_pending(
        self,
        request_id: str,
    ) -> bool:
        """Auto-expire a pending request (timeout). Returns True if updated.

        Uses ``DECISION_EXPIRED`` so the audit trail distinguishes a
        human deny from an unattended timeout. The duration is ``once`` -- a
        timeout is a non-persistent deny (just this connection), distinct from
        an active deny (which defaults to `tilrestart`).
        """
        decided_at = time.time()
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, duration = ?, decided_at = ?"
                " WHERE id = ? AND decision = ?",
                (
                    DECISION_EXPIRED,
                    DURATION_ONCE,
                    decided_at,
                    request_id,
                    DECISION_PENDING,
                ),
            )
            if cursor.rowcount > 0:
                await _restamp(db, self.app.state.settings, request_id)
                return True
            return False

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
            # Read the full rows once up front: after the bulk UPDATE the
            # only mutated columns are decision/decided_at (both known),
            # so each tag can be re-stamped without a re-read per row.
            cursor = await db.execute(
                f"SELECT {_EC_COLUMNS} FROM egress_consent WHERE decision = ?",  # noqa: S608
                (DECISION_PENDING,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return 0
            await db.execute(
                "UPDATE egress_consent"
                " SET decision = ?, decided_at = ?"
                " WHERE decision = ?",
                (DECISION_EXPIRED, decided_at, DECISION_PENDING),
            )
            settings = self.app.state.settings
            for row in rows:
                d = _row_to_dict(row)
                d["decision"] = DECISION_EXPIRED
                d["decided_at"] = decided_at
                await _stamp_hmac(db, settings, d)
            return len(rows)

    async def clear_tilrestart_duration(self, workspace_id: str) -> int:
        """Delete decided ``tilrestart``-duration verdicts for a workspace (#2346).

        A ``tilrestart`` ("until restart") verdict means "for the workspace
        container's lifetime" --
        the sidecar honors it via an in-memory rule (a learned ACCEPT for an
        allow, a REJECT for a deny) that dies when the sidecar/container
        restarts. But this row persists, so without reaping :meth:`list_active`
        would keep returning stale ``restart`` verdicts as in effect after a
        restart (the rule-management view would show rules no longer enforced).
        Called from the container (re)start path.

        - Clears **both** allows and denies (a ``restart`` deny's REJECT dies
          with the container too).
        - Leaves ``forever`` (intended to survive restarts -- an allow
          persists via ``allowed_domains`` which the sidecar re-reads on
          start, #2368), time-bounded (``5m``..``1w``) and ``once"
          rows (governed by their own expiry), ``pending`` rows, and static
          policy denials (``decided_by`` NULL, duration NULL). Returns count.
        """
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "DELETE FROM egress_consent"
                " WHERE workspace_id = ? AND duration = ?"
                " AND decision IN (?, ?)",
                (
                    workspace_id,
                    DURATION_TILRESTART,
                    DECISION_ALLOWED,
                    DECISION_DENIED,
                ),
            )
            return cursor.rowcount

    # Pruning (#2303): what the retention sweep may delete. Rows still in
    # effect are *enforcement state*, not history -- deleting a forever or
    # tilrestart (or un-elapsed timed) verdict would silently stop it working
    # (e.g. the consent-pause path reads recorded denies via
    # :meth:`active_verdict_for`; pruning one would auto-allow the host). They
    # leave via their own lifecycle instead: workspace deletion cascades, and
    # :meth:`clear_tilrestart_duration` drops tilrestart rows at container
    # restart. Stale ``pending`` rows (older than the retention window) and
    # all terminal-but-not-in-effect rows -- static policy records, expired,
    # revoked, elapsed verdicts -- are fair game.
    _PRUNE_KEEP_DECISIONS = (DECISION_ALLOWED, DECISION_DENIED)

    def _prune_eligible(self, row, now: float) -> bool:
        """Whether a consent row may be pruned (see the block comment above)."""
        decision = row["decision"]
        if decision not in self._PRUNE_KEEP_DECISIONS:
            # pending (stale by age), expired, revoked -- never in effect.
            return True
        if row["decided_by"] is None:
            # Static policy record (audit-only; enforcement is the iptables
            # allow-list, and the dedup index re-records on the next hit).
            return True
        return not self._duration_in_effect(
            row["duration"], row["decided_at"], now
        )

    async def verify_integrity(self) -> dict:
        """Re-compute every row's HMAC and report mismatches (#3174).

        Rows written before the HMAC migration carry no tag and are
        counted as ``no_hmac``, not ``tampered``.  The ``tampered`` id
        list is capped at :data:`audit_hmac.TAMPER_REPORT_CAP`; the
        full count travels in ``tampered_total``.
        """
        rows = await self.app.state.db.fetchall(
            f"SELECT {_EC_COLUMNS} FROM egress_consent ORDER BY id"
        )
        return integrity_report(
            self.app.state.settings,
            rows,
            _row_to_dict,
            compute_egress_consent_hmac,
        )

    async def prune(self, now: float | None = None) -> int:
        """Bound the table: delete rows past retention / over the per-workspace
        cap (#2303). Returns the number of rows deleted.

        Two passes, both skipping rows still in effect (see
        :meth:`_prune_eligible`):

        - **Retention** (``egress_consent_retention_days`` > 0): delete rows
          whose terminal timestamp (``revoked_at`` / ``decided_at`` /
          ``requested_at``, first non-NULL) is older than the window.
        - **Row cap** (``egress_consent_row_cap`` > 0): per workspace, if the
          row count exceeds the cap, delete the oldest eligible rows down to
          it -- belt-and-suspenders against a flood of decided requests
          outpacing age-based pruning. Live ``pending`` rows are never
          deleted by this pass (only stale-by-retention ones are).
        """
        settings = self.app.state.settings
        retention_days = settings.egress_consent_retention_days
        row_cap = settings.egress_consent_row_cap
        if retention_days <= 0 and row_cap <= 0:
            return 0
        when = resolve_prune_now(now)
        deleted = 0
        if retention_days > 0:
            deleted += await self._prune_retention(
                when - retention_days * 86400.0, when
            )
        if row_cap > 0:
            deleted += await self._prune_row_cap(row_cap, when)
        return deleted

    async def _prune_retention(self, cutoff: float, now: float) -> int:
        """Delete rows whose terminal timestamp predates *cutoff*.

        TOCTOU guard: the snapshot and the deletes run in separate
        transactions, so a row snapshotted as pending may have been
        decided in between -- deleting it would silently drop a fresh
        verdict. Pending candidates are therefore re-checked against
        their decision at DELETE time. Non-pending candidates need no
        re-check: their eligibility is monotonic (allowed/denied can
        only transition to revoked, still eligible; expired/revoked/
        static rows never change decision).
        """
        rows = await self.app.state.db.fetchall(
            "SELECT id, decision, duration, decided_at, decided_by"
            " FROM egress_consent"
            " WHERE COALESCE(revoked_at, decided_at, requested_at) < ?",
            (cutoff,),
        )
        pending_ids, decided_ids = self._retention_split_ids(rows, now)
        deleted = await self._delete_ids(
            pending_ids, require_decision=DECISION_PENDING
        )
        deleted += await self._delete_ids(decided_ids)
        return deleted

    def _retention_split_ids(
        self, rows: list, now: float
    ) -> tuple[list[str], list[str]]:
        """Split prunable rows into ``(pending, decided)`` id lists."""
        pending: list[str] = []
        decided: list[str] = []
        for r in rows:
            if not self._prune_eligible(r, now):
                continue
            bucket = pending if r["decision"] == DECISION_PENDING else decided
            bucket.append(r["id"])
        return pending, decided

    async def _prune_row_cap(self, row_cap: int, now: float) -> int:
        """Per workspace over the cap, delete the oldest eligible rows down
        to it (belt-and-suspenders against a flood of decided requests
        outpacing age-based pruning; live ``pending`` rows are never deleted
        by this pass)."""
        over = await self.app.state.db.fetchall(
            "SELECT workspace_id, COUNT(*) AS cnt"
            " FROM egress_consent GROUP BY workspace_id HAVING cnt > ?",
            (row_cap,),
        )
        deleted = 0
        for entry in over:
            excess = entry["cnt"] - row_cap
            rows = await self.app.state.db.fetchall(
                "SELECT id, decision, duration, decided_at, decided_by"
                " FROM egress_consent"
                " WHERE workspace_id = ? AND decision != ?"
                " ORDER BY COALESCE(revoked_at, decided_at,"
                " requested_at) ASC",
                (entry["workspace_id"], DECISION_PENDING),
            )
            ids = []
            for r in rows:
                if excess <= 0:
                    break
                if self._prune_eligible(r, now):
                    ids.append(r["id"])
                    excess -= 1
            deleted += await self._delete_ids(ids)
        return deleted

    async def _delete_ids(
        self, ids: list[str], *, require_decision: str | None = None
    ) -> int:
        """DELETE the given ids in bounded chunks; returns rows removed.

        ``require_decision`` re-checks the row's decision at DELETE time --
        the TOCTOU guard for pending candidates (see :meth:`prune`): a row
        snapshotted as pending but decided before the DELETE must not be
        removed. Chunks of 100 keep well under SQLite's parameter limit.
        """
        removed = 0
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            placeholders = ",".join("?" * len(chunk))
            sql = f"DELETE FROM egress_consent WHERE id IN ({placeholders})"  # noqa: S608
            params: tuple = tuple(chunk)
            if require_decision is not None:
                sql += " AND decision = ?"
                params = params + (require_decision,)
            async with self.app.state.db.transaction() as db:
                cursor = await db.execute(sql, params)
                removed += cursor.rowcount
        return removed


async def _select_row(db, request_id: str) -> dict | None:
    """Re-read one full row inside an open transaction (the #3082 shape
    guarantee: an inserted row is returned via the same
    :func:`_row_to_dict` mapping the read paths use, so a returned dict can
    never carry a different key set than a ``list_requests`` row)."""
    cursor = await db.execute(
        f"SELECT {_EC_COLUMNS} FROM egress_consent WHERE id = ?",  # noqa: S608
        (request_id,),
    )
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def _restamp(db, settings, request_id: str) -> dict | None:
    """Re-read a just-mutated row and (re)stamp its HMAC in the same
    transaction (#3174). Returns None when the row no longer exists
    (a ``_select_row`` miss) so callers surface it as "not found"."""
    row = await _select_row(db, request_id)
    if row is not None:
        await _stamp_hmac(db, settings, row)
    return row


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "dest_host": row["dest_host"],
        "dest_port": row["dest_port"],
        "pid": row["pid"],
        "process_name": row["process_name"],
        "decision": row["decision"],
        "duration": row["duration"],
        "requested_at": row["requested_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "hmac": row["hmac"],
    }


async def _stamp_hmac(db, settings, row: dict) -> None:
    """Compute and persist the HMAC tag for a just-written row (#3174)."""
    tag = compute_egress_consent_hmac(settings, row)
    await db.execute(
        "UPDATE egress_consent SET hmac = ? WHERE id = ?",
        (tag, row["id"]),
    )
    row["hmac"] = tag
