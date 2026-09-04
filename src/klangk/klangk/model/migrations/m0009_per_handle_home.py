"""Migration 0009: workspaces.per_handle_home (#2719, #2169 chunk 1).

Whether the workspace uses the per-handle home layout (each connecting
user gets a private ``/home/.users/{id}`` directory with a
``/home/{handle}`` symlink) — ``1`` — or a shared klangk home —
``0``. Set at create time (an omitted body field stores the
deploy-wide ``KLANGKD_PER_HANDLE_HOME`` flag's value) and editable
afterwards via ``PUT /workspaces/{id}``; a flip applies to the layout
realized on the next connect/start, never to a live session. Since
#3135 the deploy flag is a ceiling, not just a default: while it is
off, a stored ``1`` is inert (every start/connect resolves to the
shared home — ``workspace_settings.resolve_per_handle_home``), so
this column's value is the stored intent, not the live layout.

``ADD COLUMN ... NOT NULL DEFAULT 1`` backfills every existing row
with ``1`` in the same statement (SQLite populates pre-existing rows
with the column default): every pre-feature workspace was per-handle by
construction, so the historical value is unambiguously true. The
deploy-wide default for NEW workspaces comes from
``KLANGKD_PER_HANDLE_HOME`` (settings), not this column default — the
column default just encodes "per-handle" for direct row inserts that
omit the field.
"""

from klangk.model.migrations.base import Migration


async def apply(db) -> None:
    await db.execute(
        "ALTER TABLE workspaces"
        " ADD COLUMN per_handle_home INTEGER NOT NULL DEFAULT 1"
    )


migration = Migration(9, "0009_per_handle_home", apply)
