"""Shared core: async engine, connection wrappers, and transaction helpers.

All DB access in the per-domain modules goes through :func:`transaction`
and :func:`fetchone` defined here.  The engine state also lives here so
test fixtures that rebind it can target a single, obvious location.

#1452: the import-time ``data_dir`` / ``DB_PATH`` globals (frozen from a
global env read at import) are gone. All DB state/derivation lives on
:class:`DB`, trivially constructed from settings.

#1520: the module-level ``_db`` singleton (#1492's bridge) is gone. The
active :class:`DB` is now carried by a :class:`~contextvars.ContextVar`
built at startup (:func:`set_current_db`) and read by the module-level
``transaction`` / ``fetchone`` / ``get_db`` delegates via
:func:`get_current_db`. This keeps the ~50 ``model/`` call sites working
without threading a ``DB`` through every signature (that larger pass is
out of scope — see #1452 scope fence) while giving each task/request its
own context-owned instance instead of one process-wide module global.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ..settings import KlangkSettings

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
        raw = settings.data_dir
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
# Context-owned DB instance + delegation functions
#
# The active :class:`DB` is bound at startup (:func:`set_current_db`) into a
# :class:`~contextvars.ContextVar` and read by the module-level
# ``transaction`` / ``fetchone`` / ``get_db`` delegates via
# :func:`get_current_db`. Each task/request sees the instance bound in its
# own context (a child of the lifespan's context), not a process-wide module
# global — so concurrent requests are isolated and the active ``DB`` is always
# the one bound by the app serving this context (#1520). The delegates keep
# their signatures so the ~50 ``model/`` call sites that do
# ``from .db import transaction`` keep working without threading a ``DB``
# through every signature (that larger pass is explicitly out of scope — see
# #1452 scope fence).
# ---------------------------------------------------------------------------

_current_db: ContextVar[DB] = ContextVar("klangk_db")


def set_current_db(db: DB) -> Token[DB]:
    """Bind ``db`` as the active DB for the current context.

    Called once at lifespan startup (before any DB access) with
    ``app.state.db``. Returns the reset :class:`Token` — callers that bind
    for a bounded scope (tests, request dependencies) pass it to
    :func:`reset_current_db` on teardown.
    """
    return _current_db.set(db)


def reset_current_db(token: Token[DB]) -> None:
    """Reset the active-DB ContextVar to its pre-bind state (teardown)."""
    _current_db.reset(token)


def get_current_db() -> DB:
    """Return the DB bound in the current context.

    If nothing is bound (standalone import-and-query / ops REPL path, before
    the lifespan has run), lazily constructs one from the live environment and
    binds it for the current context. This preserves the pre-#1520 REPL
    ergonomics while making the lazily-built instance context-owned rather
    than a module singleton.
    """
    try:
        return _current_db.get()
    except LookupError:
        db = DB(KlangkSettings(os.environ))
        _current_db.set(db)
        return db


async def get_db() -> Connection:
    """Module-level delegate to the current DB's :meth:`DB.get_db`."""
    return await get_current_db().get_db()


@asynccontextmanager
async def transaction():
    """Module-level delegate to the current DB's :meth:`DB.transaction`."""
    async with get_current_db().transaction() as db:
        yield db


async def fetchone(query: str, params: tuple = ()) -> Row | None:
    """Module-level delegate to the current DB's :meth:`DB.fetchone`."""
    return await get_current_db().fetchone(query, params)
