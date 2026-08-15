"""CRUD for the ``process_launch`` table (#2520).

Each row is one process-launch event captured inside a workspace
container: pid/ppid, uid, comm/argv, timestamp, and best-effort principal
attribution (``agent`` / ``user:<handle>`` / ``unknown``) with the method
used to derive it plus the pane-input hint. Rows are audit data, keyed by
(workspace, pid, event_kind, started_at) — a pid may legitimately appear
many times across its lifetime (birth + execs).
"""

import time
import uuid

# Event kinds (subset of the watcher contract that produce rows).
KIND_BIRTH = "birth"
KIND_EXEC = "exec"
KINDS = frozenset({KIND_BIRTH, KIND_EXEC})

# Attribution methods.
METHOD_ANCHOR = "anchor"
METHOD_FALLBACK = "fallback"
METHODS = frozenset({METHOD_ANCHOR, METHOD_FALLBACK})

# Canonical column list for SELECTs + _row_to_dict, so the read shape
# cannot drift from the schema (mirrors egress_consent's _EC_COLUMNS).
_PL_COLUMNS = (
    "id, workspace_id, pid, ppid, uid, comm, argv, started_at,"
    " principal, attribution_method, pane_hint, event_kind"
)

_CREATE_SQL = """
    INSERT INTO process_launch
        (id, workspace_id, pid, ppid, uid, comm, argv, started_at,
         principal, attribution_method, pane_hint, event_kind)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class ProcessLaunchModel:
    """CRUD for the ``process_launch`` table."""

    def __init__(self, app):
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    async def record_launch(
        self,
        *,
        workspace_id: str,
        pid: int,
        ppid: int | None,
        uid: int | None,
        comm: str | None,
        argv: str | None,
        started_at: float,
        principal: str,
        attribution_method: str,
        pane_hint: str | None = None,
        event_kind: str = KIND_BIRTH,
    ) -> dict:
        """Insert one launch row. Caller pre-validates kind/method."""
        row_id = str(uuid.uuid4())
        kind = event_kind if event_kind in KINDS else KIND_BIRTH
        async with self.app.state.db.transaction() as db:
            await db.execute(
                _CREATE_SQL,
                (
                    row_id,
                    workspace_id,
                    pid,
                    ppid,
                    uid,
                    comm,
                    argv,
                    started_at,
                    principal,
                    attribution_method,
                    pane_hint,
                    kind,
                ),
            )
        return {
            "id": row_id,
            "workspace_id": workspace_id,
            "pid": pid,
            "ppid": ppid,
            "uid": uid,
            "comm": comm,
            "argv": argv,
            "started_at": started_at,
            "principal": principal,
            "attribution_method": attribution_method,
            "pane_hint": pane_hint,
            "event_kind": kind,
        }

    async def list_launches(
        self,
        workspace_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Newest-first launch rows for a workspace."""
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                f"SELECT {_PL_COLUMNS} FROM process_launch"  # noqa: S608
                " WHERE workspace_id = ?"
                " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (workspace_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def count_launches(self, workspace_id: str) -> int:
        async with self.app.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM process_launch WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def prune(self, *, keep_rows: int, keep_seconds: float) -> int:
        """Retention: delete rows older than either bound (#2303 lesson).

        ``keep_rows`` caps the table globally (newest kept);
        ``keep_seconds`` drops rows older than the wall-clock cutoff.
        Returns the number of rows deleted. Both bounds apply; pass a huge
        value to disable one.
        """
        now = time.time()
        async with self.app.state.db.transaction() as db:
            deleted = 0
            if keep_seconds and keep_seconds > 0:
                cutoff = now - keep_seconds
                cursor = await db.execute(
                    "DELETE FROM process_launch WHERE started_at < ?",
                    (cutoff,),
                )
                deleted += cursor.rowcount if cursor.rowcount else 0
            # Global row cap: delete everything below the newest N.
            if keep_rows and keep_rows > 0:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM process_launch"
                )
                row = await cursor.fetchone()
                total = int(row[0]) if row else 0
                if total > keep_rows:
                    cursor = await db.execute(
                        "DELETE FROM process_launch WHERE id NOT IN ("
                        " SELECT id FROM process_launch"
                        " ORDER BY started_at DESC LIMIT ?)",
                        (keep_rows,),
                    )
                    deleted += cursor.rowcount if cursor.rowcount else 0
        return deleted

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "workspace_id": row[1],
            "pid": row[2],
            "ppid": row[3],
            "uid": row[4],
            "comm": row[5],
            "argv": row[6],
            "started_at": row[7],
            "principal": row[8],
            "attribution_method": row[9],
            "pane_hint": row[10],
            "event_kind": row[11],
        }
