"""Instance identity: a persistent, auto-generated installation UUID.

The instance ID serves two purposes:

1. **Container-label / PID-file isolation** — so multiple klangk servers
   on one host don't collide.
2. **Provenance stamping** — workspace exports embed this ID so imports
   can distinguish domestic archives from foreign ones (#1146).

On first boot a UUID-4 is generated and persisted to the
``instance_metadata`` table.  Subsequent boots read the stored value.
The ``klangk-instance-id`` console script (see ``_instance_id.py``)
exposes this to non-Python callers (shell scripts, TypeScript tests).
"""

import sqlite3
import uuid

from .db import DB_PATH, get_db

_cache: str | None = None


def get_instance_id() -> str:
    """Return the cached instance ID.

    Raises ``RuntimeError`` if called before ``resolve_instance_id``.
    """
    if _cache is None:
        raise RuntimeError(
            "instance ID not yet resolved; call resolve_instance_id first"
        )
    return _cache


async def resolve_instance_id() -> str:
    """Read or generate the instance ID, persist it, and cache it.

    Used by the server at startup (inside the async event loop).
    Reads from the ``instance_metadata`` table.  If no row exists,
    generates a UUID-4 and inserts it.
    """
    global _cache

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT value FROM instance_metadata WHERE key = 'instance_id'"
        )
        row = await cursor.fetchone()

        if row is not None:
            resolved = row["value"]
        else:
            resolved = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO instance_metadata (key, value)"
                " VALUES ('instance_id', ?)",
                (resolved,),
            )
            await db.commit()

        _cache = resolved
        return resolved
    finally:
        await db.close()


def resolve_instance_id_sync() -> str:
    """Synchronous variant for use outside the async event loop.

    Opens the SQLite database directly (no SQLAlchemy engine), reads or
    creates the instance ID, and returns it.  Used by the
    ``klangk-instance-id`` console script.
    """
    global _cache

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS instance_metadata"
            " (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT value FROM instance_metadata WHERE key = 'instance_id'"
        ).fetchone()

        if row is not None:
            resolved = row[0]
        else:
            resolved = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO instance_metadata (key, value)"
                " VALUES ('instance_id', ?)",
                (resolved,),
            )
            conn.commit()

        _cache = resolved
        return resolved
    finally:
        conn.close()
