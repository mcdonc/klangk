"""Ordered, once-only schema migrations for the klangk SQLite database.

Why not Alembic / yoyo (#30)
----------------------------

- **Alembic** has no first-class async ``MigrationContext`` (upstream
  discussion sqlalchemy/alembic#1229); driving it from this codebase's
  aiosqlite connection means sync wrappers or a parallel engine. It also
  brings an ``env.py``/version-file/autogenerate machinery sized for
  multi-database ORM projects, while klangk owns one SQLite database
  whose entire schema is raw SQL in :mod:`klangk.model.schema`.
- **yoyo-migrations** is sync-only and CLI/file-layout oriented.

What fits instead is the Django model: an ordered list of async
migration functions applied at boot inside :func:`init_db`, each exactly
once, recorded in a ``schema_migrations`` table. ~80 lines, no new
dependencies, async-native.

Rules for contributors
----------------------

- The ``CREATE TABLE IF NOT EXISTS`` pile in :mod:`schema` is the
  *baseline*: historical tables (pre-#30) stay there and keep their
  ad-hoc repair blocks. Do not add new tables to it.
- **Every new or changed schema shape goes in ``MIGRATIONS`` below** as
  a new numbered migration. Never renumber, never reorder, never edit a
  migration that has shipped — append a new one instead.
- Migrations must be idempotent-safe under partial failure: SQLite
  autocommits DDL under Python's legacy transaction control, so a
  migration that raises after a ``CREATE`` leaves that object behind
  while the migration stays unrecorded (it re-runs next boot). Prefer
  ``IF NOT EXISTS`` guards and single-statement migrations so a re-run
  converges. (Same posture as Django: a failed migration is not
  recorded and is retried; operators inspect the DB if it keeps
  failing.)
- Ids must stay contiguous ``1..N`` — the runner refuses gaps so a
  cherry-picked branch cannot silently skip a migration on some
  deployment.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One schema migration: *id* (contiguous, 1-based), *name* (unique,
    used in logs/records), and *apply*, an async callable taking the raw
    aiosqlite connection (same contract ``init_db`` has)."""

    id: int
    name: str
    apply: Callable[[aiosqlite.Connection], Awaitable[None]]


async def _m0001_password_history(db: aiosqlite.Connection) -> None:
    """Password history for reuse prevention (#2582).

    One row per (user, password hash) at the time it was set; the users
    model consults the last ``KLANGKD_PASSWORD_HISTORY_COUNT`` entries
    before accepting a change. ``ON DELETE CASCADE`` keeps history from
    outliving its user.
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS password_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)  # noqa: S608
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_password_history_user
        ON password_history(user_id, id DESC)
    """)


MIGRATIONS: list[Migration] = [
    Migration(1, "0001_password_history", _m0001_password_history),
]


def _validate_migrations(
    migrations: list[Migration] | None = None,
) -> None:
    """Assert ids are contiguous 1..N and names unique (fail fast at
    import time rather than mid-boot on a deployed server)."""
    migrations = migrations if migrations is not None else MIGRATIONS
    ids = [m.id for m in migrations]
    if ids != list(range(1, len(migrations) + 1)):
        raise RuntimeError(
            f"MIGRATIONS ids must be contiguous 1..{len(migrations)},"
            f" got {ids}. Never renumber or reorder — append instead."
        )
    names = [m.name for m in migrations]
    if len(set(names)) != len(names):
        raise RuntimeError(f"Duplicate migration names: {names}")


_validate_migrations()


async def run_migrations(db: aiosqlite.Connection) -> list[str]:
    """Apply every pending migration in order; return what was applied.

    Owns the ``schema_migrations`` bookkeeping table. Each migration is
    committed together with its record row, so a failure mid-boot leaves
    prior migrations applied and recorded (Django semantics) and the
    failed one retried on the next ``init_db``.

    Must be called with no open transaction on *db* (``init_db``
    guarantees this: its preceding statements are DDL/SELECT only).
    """
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    cursor = await db.execute("SELECT id FROM schema_migrations")
    applied_ids = {row[0] for row in await cursor.fetchall()}

    applied_now: list[str] = []
    for migration in MIGRATIONS:
        if migration.id in applied_ids:
            continue
        logger.info("Applying schema migration %s", migration.name)
        await migration.apply(db)
        await db.execute(
            "INSERT INTO schema_migrations (id, name) VALUES (?, ?)",
            (migration.id, migration.name),
        )
        await db.commit()
        applied_now.append(migration.name)
    return applied_now
