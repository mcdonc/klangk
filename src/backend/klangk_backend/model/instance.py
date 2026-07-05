"""Instance identity: a persistent, auto-generated installation UUID.

The instance ID serves two purposes:

1. **Container-label / PID-file isolation** — so multiple klangk servers
   on one host don't collide.
2. **Provenance stamping** — workspace exports embed this ID so imports
   can distinguish domestic archives from foreign ones (#1146).

On first boot a UUID-4 is generated and persisted to the
``instance_metadata`` table.  Subsequent boots read the stored value.
"""

import uuid

from .db import get_db

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
