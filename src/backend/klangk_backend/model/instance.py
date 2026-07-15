"""Instance identity: a persistent, auto-generated installation UUID.

The instance ID serves two purposes:

1. **Container-label / PID-file isolation** — so multiple klangk servers
   on one host don't collide.
2. **Provenance stamping** — workspace exports embed this ID so imports
   can distinguish domestic archives from foreign ones (#1146).

The ID is a single line of text in ``<data_dir>/instance-id``. It lives in
``data_dir`` (next to ``klangk.db``), **not** in a runtime/state dir, because
it *identifies the data*: its lifetime is tied to the data, not to a process
run. The ``runtime_dir()`` that holds the PID file (``XDG_RUNTIME_DIR`` /
``/run/user/<uid>``) is wiped on reboot — the instance ID must survive that,
so it follows the data (#1553).

The file is the **single source of truth**. The instance ID is never stored
in the DB; there is no DB read and no migration path. On first boot a UUID-4
is generated and written atomically; subsequent boots read it back. The
``klangk-instance-id`` console script (see ``_instance_id.py``) reads the
same file, so external callers never open the SQLite DB.
"""

import os
import uuid
from pathlib import Path

from .db import get_current_db

#: Filename of the instance-ID file within ``data_dir``.
INSTANCE_ID_FILENAME = "instance-id"

_cache: str | None = None


def instance_id_path() -> Path:
    """Return ``<data_dir>/instance-id`` for the active DB's data dir.

    Resolves ``data_dir`` from the current DB's settings (which, in an
    external process, derives from ``KLANGK_DATA_DIR`` / ``KLANGK_STATE_DIR``
    env vars). Does **not** open the SQLite DB — only the path is computed.
    """
    return get_current_db().data_dir / INSTANCE_ID_FILENAME


def get_instance_id() -> str:
    """Return the cached instance ID.

    Raises ``RuntimeError`` if called before ``resolve_instance_id``.
    """
    if _cache is None:
        raise RuntimeError(
            "instance ID not yet resolved; call resolve_instance_id first"
        )
    return _cache


def resolve_instance_id() -> str:
    """Read the instance ID from ``<data_dir>/instance-id``, creating it if absent.

    Used by the server at startup (top of the lifespan, before seed/admin
    setup). If the file exists its (stripped) contents are used; otherwise a
    UUID-4 is generated and written atomically — ``instance-id.tmp`` then
    ``os.replace`` — since the file is the only copy and a torn write would
    otherwise be fatal. An empty/garbage file is regenerated the same way.

    The resolved value is cached in-memory for the process lifetime.
    """
    global _cache

    path = instance_id_path()
    resolved: str | None = None
    if path.exists():
        resolved = path.read_text().strip() or None

    if resolved is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        resolved = str(uuid.uuid4())
        tmp = path.parent / f"{path.name}.tmp"
        tmp.write_text(resolved)
        os.replace(tmp, path)

    _cache = resolved
    return resolved
