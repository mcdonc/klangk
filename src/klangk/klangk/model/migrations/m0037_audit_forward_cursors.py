"""Migration 0037: no-reuse forwarding cursors on the audit tables (#3252).

The audit forwarder (:mod:`klangk.audit_forward`) advances a persisted
watermark past every row it delivers, so the watermark key must be
monotonic and never reused — a reused key below the watermark would
ship that row to no target, silently (a gap in an at-least-once
stream).

SQLite allocates rowids for a plain ``INTEGER PRIMARY KEY`` table as
``max(rowid) + 1``, so deleting the highest-id row makes the next
insert reuse its id — exactly the gap case: retention pruning a table
to empty, ``clear_tilrestart_duration`` deleting a workspace's newest
consent row, or a workspace cascade delete. Three fixes, one per
source:

- ``audit_events`` and ``container_events`` are rebuilt with
  ``INTEGER PRIMARY KEY AUTOINCREMENT`` — the ``sqlite_sequence``
  table keeps the high-water mark across deletes, so ids are never
  reused (the same reason ``acl_entries`` and ``password_history``
  use AUTOINCREMENT). Existing rows and ids are preserved, and the
  indexes are recreated after the rename.
- ``egress_consent`` (whose primary key is the request UUID, so its
  cursor is the implicit rowid — which SQLite *can* reuse) gains a
  ``forward_seq INTEGER`` column assigned by an AFTER INSERT trigger
  from the dedicated ``audit_forward_sequences`` counter table. The
  counter only ever increments, so deleted rows never free their
  sequence numbers. Existing rows are backfilled with their rowid
  (a one-time assignment, monotonic within the table), and the
  counter starts past the backfill maximum. ``INSERT OR IGNORE``
  (the consent dedup path) never fires the trigger for an ignored
  row, so deduped inserts consume no sequence numbers.
"""

from klangk.model.migrations.base import Migration

_AUDIT_EVENTS_COLUMNS = (
    "id, event, actor_id, actor_email, target_type, target_id,"
    " detail, source_ip, user_agent, created_at, hmac"
)
_CONTAINER_EVENTS_COLUMNS = (
    "id, workspace_id, event, actor_type, actor_id, cause,"
    " container_id, container_role, network_namespace, created_at,"
    " hmac"
)

# (table, new CREATE with AUTOINCREMENT, column list, indexes to
# recreate after the rename). Shapes mirror migrations 0019/0030/0034
# (the historical baseline plus every later ALTER), with the single
# change being AUTOINCREMENT on the id.
_REBUILDS = (
    (
        "audit_events",
        """
        CREATE TABLE audit_events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            actor_id TEXT,
            actor_email TEXT,
            target_type TEXT,
            target_id TEXT,
            detail TEXT,
            source_ip TEXT,
            user_agent TEXT,
            created_at REAL NOT NULL,
            hmac TEXT
        )
        """,
        _AUDIT_EVENTS_COLUMNS,
        (
            "CREATE INDEX IF NOT EXISTS idx_audit_events_time"
            " ON audit_events (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_events_event"
            " ON audit_events (event)",
        ),
    ),
    (
        "container_events",
        """
        CREATE TABLE container_events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            event TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            cause TEXT NOT NULL,
            container_id TEXT,
            container_role TEXT NOT NULL DEFAULT 'workspace'
                CHECK (container_role IN ('workspace', 'network-sidecar')),
            network_namespace TEXT,
            created_at REAL NOT NULL,
            hmac TEXT
        )
        """,
        _CONTAINER_EVENTS_COLUMNS,
        (
            "CREATE INDEX IF NOT EXISTS idx_container_events_ws_time"
            " ON container_events (workspace_id, created_at DESC)",
        ),
    ),
)


async def _rebuild_table(db, name: str, create_sql: str, columns: str) -> None:
    """Copy *name* into an AUTOINCREMENT twin, then swap the names.

    Copying rows with their explicit ids seeds ``sqlite_sequence`` to
    the current maximum, so the no-reuse guarantee starts from the
    existing high-water mark, not from 1.
    """
    await db.execute(create_sql)
    await db.execute(
        f"INSERT INTO {name}_new ({columns})"  # noqa: S608
        f" SELECT {columns} FROM {name}"
    )
    await db.execute(f"DROP TABLE {name}")  # noqa: S608
    await db.execute(
        f"ALTER TABLE {name}_new RENAME TO {name}"  # noqa: S608
    )


async def apply(db) -> None:
    for name, create_sql, columns, indexes in _REBUILDS:
        await _rebuild_table(db, name, create_sql, columns)
        for index_sql in indexes:
            await db.execute(index_sql)
    # egress_consent: the trigger-assigned never-reused sequence.
    await db.execute(
        "ALTER TABLE egress_consent ADD COLUMN forward_seq INTEGER"
    )
    await db.execute("UPDATE egress_consent SET forward_seq = rowid")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_forward_sequences (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
        """
    )
    await db.execute(
        "INSERT INTO audit_forward_sequences (name, value)"
        " SELECT 'egress_consent', COALESCE(MAX(forward_seq), 0)"
        " FROM egress_consent"
    )
    await db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS egress_consent_forward_seq
        AFTER INSERT ON egress_consent
        BEGIN
            UPDATE audit_forward_sequences
               SET value = value + 1
             WHERE name = 'egress_consent';
            UPDATE egress_consent
               SET forward_seq = (
                       SELECT value FROM audit_forward_sequences
                        WHERE name = 'egress_consent'
                   )
             WHERE rowid = NEW.rowid;
        END
        """
    )
    # The forwarder polls past forward_seq every sweep; the index keeps
    # that an index seek instead of a full-table scan + sort.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_egress_consent_forward_seq"
        " ON egress_consent (forward_seq)"
    )


migration = Migration(
    id=37,
    name="0037_audit_forward_cursors",
    apply=apply,
)
