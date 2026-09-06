"""Identity and privilege audit events (#3205).

The structured audit stream for account, identity, and privilege
actions — everything that is *not* a container lifecycle transition
(those live in ``container_events``, #2915). One append-only row per
event, each carrying the acting principal (``actor_id`` /
``actor_email``, denormalized so attribution survives the actor's own
deletion), the target it acted on, a JSON ``detail`` blob for
action-specific context (never secrets: no passwords, no tokens), and
the per-request HTTP metadata the issue called out — the effective
client IP, user agent (#3205), HTTP method, and Referer (#3255,
SV-222447). Rows written before #3255 read NULL for method/referer,
and so does the workstation-binding ``session.revoke`` row, which
records only the presenting workstation pair — the binding violation
(#3194) is judged on workstation identity alone, which the WebSocket
and HTTP paths present identically.

Event coverage (#3205):

- **Account CRUD** — ``user.register``, ``user.create`` (admin),
  ``user.update`` (admin), ``user.delete`` (admin), ``user.unlock``,
  ``user.password.change``, ``user.email.change``,
  ``user.handle.change``.
- **Privilege changes** — ``group.create`` / ``group.update`` /
  ``group.delete``, ``group.member.add`` / ``group.member.remove``,
  ``acl.replace`` (both the admin resource tree and a workspace's own
  ACE list), ``workspace.member.add`` / ``workspace.member.remove``,
  ``workspace.group.add`` / ``workspace.group.remove``,
  ``workspace.role.add`` / ``workspace.role.remove`` /
  ``workspace.role.change``, ``workspace.transfer``.
- **Login/logout** — ``login`` (every session mint, with a ``via``
  detail naming the path: password, oidc, invite, email-verify,
  password-reset, expired-password, local, register),
  ``login.failed`` (a bad credential check, with the attempted
  identifier), ``logout``.
- **Session revocation** — ``session.revoke`` (password-change
  revocation, the max-sessions-per-user eviction, and the
  workstation-binding violation, #3194 — whose row carries the
  presenting workstation as its source IP and no actor, the trigger
  being an unknown presenter).
- **Data-level file operations** (#3257, ASD-STIG SV-222471/472) —
  ``file.download`` (a workspace archive export, and a per-file or
  per-directory download through the files API), ``file.upload``
  (a workspace archive import), ``file.write`` (in-workspace writes
  through the files API: upload, rename, delete). Each row carries
  the workspace as its target and the path and byte size in
  ``detail`` (the size is omitted where meaningless — rename, delete,
  directory downloads). Terminal I/O and container-internal changes
  stay out (#3257 out-of-scope: the PTY byte stream is the user's
  own session; the container filesystem is opaque to the daemon).

Writes are best-effort by design (the #2915 posture): a failed audit
write is logged and never fails the action it annotates — see
:meth:`AuditEventsModel.record_best_effort`. Integrity protection is
the shared opt-in HMAC tagging (#3174): with
``KLANGKD_AUDIT_HMAC_KEY`` configured every row is tagged at insert
time by :func:`klangk.model.audit_hmac.compute_audit_event_hmac`.

Retention/bounding mirrors ``container_events`` (#2924):
:meth:`AuditEventsModel.prune` deletes rows past a retention window
(``audit_events_retention_days``) and trims overflow past a
deploy-wide row cap (``audit_events_row_cap``), keeping the newest —
swept hourly by the consent sweeper. The row cap is applied **per
class**: unauthenticated ``login.failed`` rows (the only class an
anonymous caller can mint) and the high-frequency ``file.*`` rows
(#3257) each get their own bucket, so neither can evict the genuine
account/privilege history an incident review starts from.
An admin-facing paged view is
``GET /api/v1/events/audit`` (``manage-events``).
"""

import json
import logging
import time

from .audit_hmac import compute_audit_event_hmac
from .base import Submodel, resolve_prune_now
from ..notifier import notify_event

logger = logging.getLogger(__name__)

# Canonical column list so the read shape cannot drift from the schema
# (a column added to the table is added here once). ``detail`` is stored
# as a JSON string and decoded on read. ``method``/``referer`` are the
# #3255 additions; they stay outside the HMAC column set
# (``_AE_HMAC_COLUMNS``) so the published offsite-verification contract
# (docs/reference/audit-integrity.md) is unchanged by their arrival.
_EVENT_COLUMNS = (
    "id, event, actor_id, actor_email, target_type, target_id,"
    " detail, source_ip, user_agent, method, referer, created_at, hmac"
)


# The row-cap classes (#3205, #3257): each predicate names one bucket
# that is capped independently of the others under
# ``audit_events_row_cap``. ``login.failed`` is the only class an
# anonymous caller can mint, and ``file.*`` rows are the highest-
# frequency class — a flood in either evicts only its own bucket,
# never the account/privilege history an incident review starts from.
_ROW_CAP_CLASSES = (
    "event NOT LIKE 'file.%' AND event != 'login.failed'",
    "event = 'login.failed'",
    "event LIKE 'file.%'",
)


def filter_clause(
    event: str | None, actor: str | None, target: str | None
) -> tuple[str, list]:
    """WHERE clause + params narrowing event reads.

    All three filters are optional substrings (SQLite ``LIKE`` — ASCII
    case-insensitive, ``%``/``_`` wildcards not escaped, the same
    convention as the other admin filters): ``event`` matches the event
    name, ``actor`` matches the actor id *or* email, ``target``
    matches the target id. An empty string means no filter.
    """
    conditions: list[str] = []
    params: list = []
    if event:
        conditions.append("event LIKE '%' || ? || '%'")
        params.append(event)
    if actor:
        conditions.append(
            "(actor_id LIKE '%' || ? || '%' OR actor_email LIKE"
            " '%' || ? || '%')"
        )
        params.extend([actor, actor])
    if target:
        conditions.append("target_id LIKE '%' || ? || '%'")
        params.append(target)
    if not conditions:
        return "", []
    return " WHERE " + " AND ".join(conditions), params


def row_to_dict(row) -> dict:
    """Row-tuple -> dict with the columns of ``_EVENT_COLUMNS``.

    ``detail`` decodes from its stored JSON string into an object for
    the wire (``None`` stays ``None``); everything else passes through.
    """
    keys = [c.strip() for c in _EVENT_COLUMNS.split(",")]
    fields = dict(zip(keys, row, strict=True))
    if fields["detail"] is not None:
        fields["detail"] = json.loads(fields["detail"])
    return fields


class AuditEventsModel(Submodel):
    """CRUD for the ``audit_events`` table."""

    def __init__(self, app) -> None:
        super().__init__(app)
        # Total failed writes through record_best_effort, in-memory
        # only (zeroed on restart). The degradation counter the
        # resource watchdog watches and /audit reports (#3206) —
        # mirrors container_registry.audit_write_failures.
        self.write_failures = 0

    async def record(
        self,
        event: str,
        *,
        actor_id: str | None = None,
        actor_email: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict | None = None,
        source_ip: str | None = None,
        user_agent: str | None = None,
        method: str | None = None,
        referer: str | None = None,
    ) -> None:
        """Insert one audit event row, HMAC-tagged when configured.

        *method* / *referer* are the #3255 request fields
        (SV-222447); both ``None`` for rows written before #3255 and
        for the workstation-binding violation row. Raises on a DB
        failure; callers that must not fail on an audit problem use
        :meth:`record_best_effort` instead.
        """
        created_at = time.time()
        detail_json = json.dumps(detail) if detail is not None else None
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "INSERT INTO audit_events"
                " (event, actor_id, actor_email, target_type, target_id,"
                "  detail, source_ip, user_agent, method, referer,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event,
                    actor_id,
                    actor_email,
                    target_type,
                    target_id,
                    detail_json,
                    source_ip,
                    user_agent,
                    method,
                    referer,
                    created_at,
                ),
            )
            row_id = cursor.lastrowid
            row = {
                "id": row_id,
                "event": event,
                "actor_id": actor_id,
                "actor_email": actor_email,
                "target_type": target_type,
                "target_id": target_id,
                "detail": detail_json,
                "source_ip": source_ip,
                "user_agent": user_agent,
                "method": method,
                "referer": referer,
                "created_at": created_at,
            }
            tag = compute_audit_event_hmac(self.app.state.settings, row)
            if tag is not None:
                await db.execute(
                    "UPDATE audit_events SET hmac = ? WHERE id = ?",
                    (tag, row_id),
                )

    async def record_best_effort(self, event: str, **kwargs) -> None:
        """Insert one audit event row, swallowing write failures.

        Auditing must never fail the action it annotates (#2915
        posture): a DB error is logged and swallowed. Fail-closed
        auditing is deliberately *not* offered here — unlike container
        transitions there is no single admission choke point to gate,
        and an unwritable audit table must not brick account
        management.
        """
        try:
            await self.record(event, **kwargs)
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            self.write_failures += 1
            logger.warning("audit_events write failed (%s): %s", event, e)
            # SV-222484/485: a degraded identity audit stream must alert
            # the SA in real time, not just log (#3250). This is the one
            # deliberate notifier reach from the model layer — the
            # write's failure site is here, and every API-layer caller
            # funnels through it. The guarded helper is a no-op on
            # minimal test app states; notify_admins never raises (and
            # never writes an audit row, so this cannot recurse).
            notify_event(
                self.app,
                "audit.failure",
                detail={
                    "table": "audit_events",
                    "failed_event": event,
                    "error": str(e),
                },
            )

    async def list_events(
        self,
        event: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Newest-first event history with the optional filters of
        :func:`filter_clause`."""
        where, params = filter_clause(event, actor, target)
        sql = f"SELECT {_EVENT_COLUMNS} FROM audit_events{where}"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        rows = await self.app.state.db.fetchall(sql, (*params, limit, offset))
        return [row_to_dict(row) for row in rows]

    async def count_events(
        self,
        event: str | None = None,
        actor: str | None = None,
        target: str | None = None,
    ) -> int:
        """Row count for paging, with the same optional filters."""
        where, params = filter_clause(event, actor, target)
        row = await self.app.state.db.fetchone(
            f"SELECT COUNT(*) FROM audit_events{where}", tuple(params)
        )
        return row[0] if row else 0

    async def prune(self, now: float | None = None) -> int:
        """Bound the table: delete rows past retention / over the row
        cap. Returns the number of rows deleted.

        Mirrors ``container_events`` (#2924): every row is terminal
        history at write time, so the passes are pure deletion —
        retention window first, then the row cap keeping the newest.
        The cap is applied per class (#3205 review): ``login.failed``
        rows — the only class an unauthenticated caller can mint — are
        capped in their own bucket, so flooding them evicts only other
        ``login.failed`` rows, never the privileged-action history.
        """
        settings = self.app.state.settings
        retention_days = settings.audit_events_retention_days
        row_cap = settings.audit_events_row_cap
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
                "DELETE FROM audit_events WHERE created_at < ?",
                (now - retention_days * 86400.0,),
            )
            return cursor.rowcount

    async def _prune_row_cap(self, row_cap: int) -> int:
        """Deploy-wide cap, applied per class: delete the oldest rows
        over the cap in each bucket of ``_ROW_CAP_CLASSES``, keeping
        the newest (the same tie-break :meth:`list_events` uses). The
        unauthenticated ``login.failed`` class and the ``file.*`` class
        are capped separately from everything else so neither can
        evict privileged-action history."""
        deleted = 0
        for predicate in _ROW_CAP_CLASSES:
            async with self.app.state.db.transaction() as db:
                cursor = await db.execute(
                    "DELETE FROM audit_events WHERE id IN"
                    f" (SELECT id FROM audit_events WHERE {predicate}"
                    " ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?)",
                    (row_cap,),
                )
                deleted += cursor.rowcount
        return deleted
