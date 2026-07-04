"""Shared core: async engine, connection wrappers, and transaction helpers.

All DB access in the per-domain modules goes through :func:`transaction`
and :func:`fetchone` defined here.  The engine state (``engine``,
``DB_PATH``) also lives here so test fixtures that rebind it can target a
single, obvious location.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ..util import resolve_env_value

logger = logging.getLogger(__name__)

data_dir = Path(
    resolve_env_value("KLANGK_DATA_DIR", str(Path.home() / ".klangk" / "data"))
)
DB_PATH = data_dir / "klangk.db"

# ---------------------------------------------------------------------------
# SQLAlchemy async engine + compatibility wrappers
# ---------------------------------------------------------------------------

engine = None


class Row:
    """Row wrapper supporting both row["col"] and row[int] access."""

    __slots__ = ("_row",)

    def __init__(self, row):
        self._row = row

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        return self._row._mapping[key]

    def keys(self):
        return self._row._mapping.keys()


class CursorResult:
    """Wrap SQLAlchemy CursorResult to match the aiosqlite cursor API."""

    __slots__ = ("_result",)

    def __init__(self, result):
        self._result = result

    async def fetchone(self):
        row = self._result.fetchone()
        return None if row is None else Row(row)

    async def fetchall(self):
        return [Row(row) for row in self._result.fetchall()]

    @property
    def rowcount(self):
        return self._result.rowcount

    @property
    def lastrowid(self):
        return self._result.lastrowid


class Connection:
    """Wrap SQLAlchemy AsyncConnection to match the aiosqlite API.

    Callers keep using ``db.execute(sql, params)``, ``db.commit()``,
    ``db.rollback()``, and ``db.close()`` exactly as before.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=None):
        if params is not None:
            if isinstance(params, list):
                params = tuple(params)
            result = await self._conn.exec_driver_sql(sql, params)
        else:
            result = await self._conn.exec_driver_sql(sql)
        return CursorResult(result)

    async def commit(self):
        await self._conn.commit()

    async def rollback(self):
        await self._conn.rollback()

    async def close(self):
        await self._conn.close()


def make_engine(db_path: Path | str, **kwargs):
    """Create a new async engine with PRAGMA listeners.

    Uses NullPool so every ``transaction()`` gets a fresh connection and
    returns it immediately on close.  SQLite connections are cheap (a
    thread + file handle) and pooling them just creates artificial
    contention — under concurrent load a bounded QueuePool exhausts its
    slots and blocks the entire API with TimeoutError.
    """
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        **kwargs,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 15000")
        cursor.close()

    return engine


def ensure_engine():
    global engine
    if engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(DB_PATH)
    return engine


async def dispose_engine() -> None:
    """Dispose the current engine (shutdown / test teardown)."""
    global engine
    if engine is not None:
        await engine.dispose()
        engine = None


async def get_db() -> Connection:
    """Acquire a raw database connection from the pool.

    Caller is responsible for commit/rollback/close.

    The ``engine.connect()`` await is shielded so that a cancellation
    delivered mid-acquisition cannot orphan the underlying connection:
    aiosqlite opens its worker thread (and the real ``sqlite3.Connection``)
    before the await returns, so interrupting it leaves those resources
    with no handle to close them -- they then outlive the event loop and
    surface as ``RuntimeError: Event loop is closed`` / ``ResourceWarning:
    unclosed database`` (#1250). If we are cancelled, we drain the
    shielded connect and close whatever it produced before propagating.
    """
    engine = ensure_engine()
    task = asyncio.ensure_future(engine.connect())
    try:
        conn = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            conn = await task
            await conn.close()
        except Exception:  # pragma: no cover - defensive; close best-effort
            pass
        raise
    return Connection(conn)


@asynccontextmanager
async def transaction():
    """Context manager: auto-commits on clean exit, rolls back on error."""
    db = await get_db()
    try:
        yield db
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    finally:
        await db.close()


async def fetchone(query: str, params: tuple = ()) -> Row | None:
    """Run a single-row SELECT and return the row, or ``None``."""
    async with transaction() as db:
        cursor = await db.execute(query, params)
        return await cursor.fetchone()
