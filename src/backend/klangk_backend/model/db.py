"""Shared core: async engine, connection wrappers, and transaction helpers.

All DB access in the per-domain modules goes through :func:`transaction`
and :func:`fetchone` defined here.  The engine state also lives here so
test fixtures that rebind it can target a single, obvious location.

#1452: the import-time ``data_dir`` / ``DB_PATH`` globals (frozen from a
global env read at import) are gone. All DB state/derivation lives on
:class:`DB`, trivially constructed from settings. A module-level
``_db`` instance is set at startup (and rebound per-test by the conftest
fixture) so the existing ``transaction``/``fetchone``/``get_db`` module
functions continue to work without threading a ``DB`` through every
``model/`` call site — that larger pass is explicitly out of scope for
this slice (see #1452 scope fence).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ..settings import KlangkSettings, get_settings

logger = logging.getLogger(__name__)


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


class DB:
    """Settings-derived database: owns the engine cache and connection helpers.

    Constructed once at startup (``app.state.db = DB(settings)``) and
    trivially standalone for the import-and-query / ops REPL path
    (``DB(KlangkSettings(os.environ))``). The data dir and DB path are
    computed from settings at construction — no import-time global, no
    frozen-at-import hazard (#1452).
    """

    def __init__(self, settings: KlangkSettings):
        self.settings = settings
        raw = settings.data_dir or str(Path.home() / ".klangk" / "data")
        self.data_dir = Path(raw)
        self.db_path = self.data_dir / "klangk.db"
        self.engine = None

    def make_engine(self):
        """Create a new async engine with PRAGMA listeners.

        Uses NullPool so every ``transaction()`` gets a fresh connection and
        returns it immediately on close.  SQLite connections are cheap (a
        thread + file handle) and pooling them just creates artificial
        contention — under concurrent load a bounded QueuePool exhausts its
        slots and blocks the entire API with TimeoutError.
        """
        url = f"sqlite+aiosqlite:///{self.db_path}"
        engine = create_async_engine(
            url,
            poolclass=NullPool,
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 15000")
            cursor.close()

        return engine

    def ensure_engine(self):
        if self.engine is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.engine = self.make_engine()
        return self.engine

    async def dispose_engine(self) -> None:
        """Dispose the current engine (shutdown / test teardown)."""
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    async def get_db(self) -> Connection:
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
        engine = self.ensure_engine()
        task = asyncio.ensure_future(engine.connect())
        try:
            conn = await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                conn = await task
                await conn.close()
            except (
                Exception
            ):  # pragma: no cover - defensive; close best-effort
                pass
            raise
        return Connection(conn)

    @asynccontextmanager
    async def transaction(self):
        """Context manager: auto-commits on clean exit, rolls back on error."""
        db = await self.get_db()
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def fetchone(self, query: str, params: tuple = ()) -> Row | None:
        """Run a single-row SELECT and return the row, or ``None``."""
        async with self.transaction() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchone()


# ---------------------------------------------------------------------------
# Module-level instance + delegation functions
#
# A single ``_db`` is set at startup (and rebound per-test). The module-level
# ``transaction`` / ``fetchone`` / ``get_db`` delegate to it so the ~50
# ``model/`` call sites that do ``from .db import transaction`` keep working
# without threading a ``DB`` through every signature — that larger pass is
# explicitly out of scope for #1452 (scope fence: it implies folding all of
# model/ into DB, which is a separate decision about the data-access shape).
# ---------------------------------------------------------------------------

_db: DB | None = None


def set_db(db: DB) -> None:
    """Set the module-level DB instance (startup / test fixture)."""
    global _db
    _db = db


def get_default_db() -> DB:
    """Return the module-level DB, constructing one lazily if needed.

    Lazily constructs from :func:`get_settings` so the import-and-query /
    ops REPL path works without an explicit ``set_db`` call — matching the
    pre-refactor behavior where ``ensure_engine`` read the global env.
    """
    global _db
    if _db is None:
        _db = DB(get_settings())
    return _db


async def get_db() -> Connection:
    """Module-level delegate to the default DB's :meth:`DB.get_db`."""
    return await get_default_db().get_db()


@asynccontextmanager
async def transaction():
    """Module-level delegate to the default DB's :meth:`DB.transaction`."""
    async with get_default_db().transaction() as db:
        yield db


async def fetchone(query: str, params: tuple = ()) -> Row | None:
    """Module-level delegate to the default DB's :meth:`DB.fetchone`."""
    return await get_default_db().fetchone(query, params)


async def dispose_engine() -> None:
    """Module-level delegate to the default DB's :meth:`DB.dispose_engine`."""
    await get_default_db().dispose_engine()
