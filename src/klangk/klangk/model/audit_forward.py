"""Forwarding cursor + row readers for the audit forwarder (#3252).

The ``audit_forward_state`` table holds one row per (source, target)
pair — the id of the last row that target accepted (the *watermark*).
The worker (:mod:`klangk.audit_forward`) reads rows past the
watermark, delivers them, then advances it; a restart resumes right
after the last accepted record, and a re-configured target starts
from zero (at-least-once permits the replay).

The watermark is each table's monotonic **never-reused** row key
(migration 0037): the AUTOINCREMENT ``id`` for the two append-only
event tables, and the trigger-assigned ``forward_seq`` for
``egress_consent`` (whose own ``id`` is a random UUID and whose
implicit rowid SQLite may reuse after a delete). Both are exposed on
every forwarded record as ``forward_cursor``, so a receiver can order
and deduplicate per source.

Every query the forwarder runs lives here (the model-layer SQL
containment rule, #3068) — the worker only orchestrates.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .audit_events import _EVENT_COLUMNS as AUDIT_EVENT_COLUMNS
from .base import Submodel
from .container_events import _EVENT_COLUMNS as CONTAINER_EVENT_COLUMNS
from .egress_consent import _EC_COLUMNS as EGRESS_CONSENT_COLUMNS

logger = logging.getLogger(__name__)

# The three forwarded sources, in sweep order.
SOURCE_AUDIT_EVENTS = "audit_events"
SOURCE_CONTAINER_EVENTS = "container_events"
SOURCE_EGRESS_CONSENT = "egress_consent"
SOURCE_ORDER = (
    SOURCE_AUDIT_EVENTS,
    SOURCE_CONTAINER_EVENTS,
    SOURCE_EGRESS_CONSENT,
)


def base_record(row) -> dict:
    """Row-tuple -> dict with every selected column."""
    return {key: row[key] for key in row.keys()}


def audit_event_record(row) -> dict:
    """Decode the stored JSON ``detail`` blob for the wire; a corrupt
    blob ships raw (warned) so one bad row can never wedge the source
    behind it."""
    record = base_record(row)
    if isinstance(record["detail"], str):
        record["detail"] = decode_detail(record["detail"])
    return record


def decode_detail(text: str):
    """JSON-decode a detail blob, falling back to the raw string."""
    try:
        return json.loads(text)
    except ValueError:
        logger.warning(
            "audit_events row detail is not valid JSON; forwarding raw"
        )
        return text


@dataclass(frozen=True)
class SourceSpec:
    """Per-table read shape: the monotonic cursor expression, the
    canonical column list, and the row decoder."""

    cursor: str
    columns: str
    decode: Callable[[object], dict]


SOURCE_SPECS = {
    SOURCE_AUDIT_EVENTS: SourceSpec(
        cursor="id",
        columns=AUDIT_EVENT_COLUMNS,
        decode=audit_event_record,
    ),
    SOURCE_CONTAINER_EVENTS: SourceSpec(
        cursor="id",
        columns=CONTAINER_EVENT_COLUMNS,
        decode=base_record,
    ),
    SOURCE_EGRESS_CONSENT: SourceSpec(
        cursor="forward_seq",
        columns=EGRESS_CONSENT_COLUMNS,
        decode=base_record,
    ),
}


class AuditForwardModel(Submodel):
    """Watermark reads/advances + bounded row readers for the three
    audit tables, resolved through ``app_state.db``. Watermark keys are
    opaque per-(source, target) strings the worker mints."""

    async def watermark(self, key: str) -> int:
        """The key's cursor — 0 (forward everything) when no row exists
        yet (a fresh install, or a target never forwarded to)."""
        row = await self.app.state.db.fetchone(
            "SELECT watermark FROM audit_forward_state WHERE source = ?",
            (key,),
        )
        return row[0] if row else 0

    async def advance(self, key: str, watermark: int) -> None:
        """Persist the new cursor after a delivered batch (upsert)."""
        async with self.app.state.db.transaction() as db:
            await db.execute(
                "INSERT INTO audit_forward_state"
                " (source, watermark, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(source) DO UPDATE SET"
                " watermark = excluded.watermark,"
                " updated_at = excluded.updated_at",
                (key, watermark, time.time()),
            )

    async def rows_after(
        self, source: str, after: int, limit: int
    ) -> list[dict]:
        """The source's rows past *after*, oldest first, at most
        *limit* — the next batch to deliver. Each record carries its
        ``forward_cursor`` (see :data:`SOURCE_SPECS`)."""
        spec = SOURCE_SPECS[source]
        rows = await self.app.state.db.fetchall(
            f"SELECT {spec.cursor} AS forward_cursor, {spec.columns}"  # noqa: S608
            f" FROM {source} WHERE {spec.cursor} > ?"
            f" ORDER BY {spec.cursor} LIMIT ?",
            (after, limit),
        )
        return [spec.decode(row) for row in rows]

    async def pending_count(self, source: str, after: int) -> int:
        """Rows past the given cursor — the queue depth (used by the
        ``/audit`` forwarding status)."""
        cursor = SOURCE_SPECS[source].cursor
        row = await self.app.state.db.fetchone(
            f"SELECT COUNT(*) FROM {source} WHERE {cursor} > ?",  # noqa: S608
            (after,),
        )
        return row[0] if row else 0
