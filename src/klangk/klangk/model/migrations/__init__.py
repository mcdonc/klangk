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
- **Every new or changed schema shape is one module in this package**
  (``m<NNNN>_<slug>.py``, e.g. ``m0001_password_history.py``)
  exposing ``migration = Migration(N, name, apply)``, imported and
  appended to ``MIGRATIONS`` below in id order. Never renumber, never
  reorder, never edit a migration that has shipped — append a new one
  instead.
- Each migration runs inside one explicit ``BEGIN IMMEDIATE``
  transaction committed together with its ``schema_migrations`` record
  row. SQLite DDL is transactional *only* under an explicit BEGIN —
  under Python's default legacy transaction control DDL autocommits the
  moment it executes, which would leave a half-applied migration
  durable-but-unrecorded on failure (a failed ``ALTER TABLE ADD
  COLUMN`` would then boot-loop forever on ``duplicate column name``).
  The runner opens the transaction; migrations must NOT issue their own
  BEGIN/COMMIT and must not rely on autocommit visibility.
- A migration that raises is rolled back and retried on the next boot
  (same posture as Django: failed migrations are not recorded;
  operators inspect the DB if one keeps failing).
- Ids must stay contiguous ``1..N`` — the runner refuses gaps so a
  cherry-picked branch cannot silently skip a migration on some
  deployment. Names are frozen once shipped: the runner raises if a
  recorded id's name changes (a rename would silently fork history).
"""

import logging

from klangk.model.migrations import m0001_password_history
from klangk.model.migrations import m0002_last_login_at
from klangk.model.migrations import m0003_user_sessions
from klangk.model.migrations import m0004_user_sessions_workstation
from klangk.model.migrations import m0005_user_inactivity
from klangk.model.migrations import m0006_host_schedules
from klangk.model.migrations import m0007_server_schedules
from klangk.model.migrations import m0008_agent_user_klangk
from klangk.model.migrations import m0009_per_handle_home
from klangk.model.migrations import m0010_groups_source
from klangk.model.migrations import m0011_files_download
from klangk.model.migrations import m0012_files_write
from klangk.model.migrations import m0013_exec_and_sync_permission
from klangk.model.migrations import m0014_groups_create_admin
from klangk.model.migrations import m0015_classification_banner
from klangk.model.migrations import m0016_monitor_permission
from klangk.model.migrations import m0017_change_acls_permission
from klangk.model.migrations import m0018_egress_consent_permission
from klangk.model.migrations import m0019_container_events
from klangk.model.migrations import m0020_rename_admin_group
from klangk.model.migrations import m0021_first_class_resource_acls
from klangk.model.migrations import m0022_workspace_permission_renames
from klangk.model.migrations import m0023_self_service_resources
from klangk.model.migrations import m0024_join_workspace_permission
from klangk.model.migrations import m0025_drop_dead_images_deny_row
from klangk.model.migrations import m0026_volumes_admin_surface
from klangk.model.migrations import m0027_retire_admin_marker
from klangk.model.migrations import m0028_invitations_pending_unique
from klangk.model.migrations import m0029_members_create_workspace
from klangk.model.migrations import m0030_audit_hmac
from klangk.model.migrations import m0031_password_age
from klangk.model.migrations import m0032_must_change_password
from klangk.model.migrations import m0033_user_sessions_last_seen
from klangk.model.migrations import m0034_audit_events
from klangk.model.migrations.base import Migration

__all__ = ["MIGRATIONS", "Migration", "run_migrations"]

logger = logging.getLogger(__name__)


MIGRATIONS: list[Migration] = [
    m0001_password_history.migration,
    m0002_last_login_at.migration,
    m0003_user_sessions.migration,
    m0004_user_sessions_workstation.migration,
    m0005_user_inactivity.migration,
    m0006_host_schedules.migration,
    m0007_server_schedules.migration,
    m0008_agent_user_klangk.migration,
    m0009_per_handle_home.migration,
    m0010_groups_source.migration,
    m0011_files_download.migration,
    m0012_files_write.migration,
    m0013_exec_and_sync_permission.migration,
    m0014_groups_create_admin.migration,
    m0015_classification_banner.migration,
    m0016_monitor_permission.migration,
    m0017_change_acls_permission.migration,
    m0018_egress_consent_permission.migration,
    m0019_container_events.migration,
    m0020_rename_admin_group.migration,
    m0021_first_class_resource_acls.migration,
    m0022_workspace_permission_renames.migration,
    m0023_self_service_resources.migration,
    m0024_join_workspace_permission.migration,
    m0025_drop_dead_images_deny_row.migration,
    m0026_volumes_admin_surface.migration,
    m0027_retire_admin_marker.migration,
    m0028_invitations_pending_unique.migration,
    m0029_members_create_workspace.migration,
    m0030_audit_hmac.migration,
    m0031_password_age.migration,
    m0032_must_change_password.migration,
    m0033_user_sessions_last_seen.migration,
    m0034_audit_events.migration,
]


def _assert_contiguous_ids(migrations: list[Migration]) -> None:
    """Migration ids must be 1..N in order."""
    ids = [m.id for m in migrations]
    if ids != list(range(1, len(migrations) + 1)):
        raise RuntimeError(
            f"MIGRATIONS ids must be contiguous 1..{len(migrations)},"
            f" got {ids}. Never renumber or reorder — append instead."
        )


def _assert_unique_names(migrations: list[Migration]) -> None:
    """Migration names must be unique."""
    names = [m.name for m in migrations]
    if len(set(names)) != len(names):
        raise RuntimeError(f"Duplicate migration names: {names}")


def validate_migrations(
    migrations: list[Migration] | None = None,
) -> None:
    """Assert ids are contiguous 1..N and names unique (fail fast at
    import time rather than mid-boot on a deployed server)."""
    migrations = migrations if migrations is not None else MIGRATIONS
    _assert_contiguous_ids(migrations)
    _assert_unique_names(migrations)


validate_migrations()


async def run_migrations(db) -> list[str]:
    """Apply every pending migration in order; return what was applied.

    Owns the ``schema_migrations`` bookkeeping table. Each migration
    runs inside an explicit ``BEGIN IMMEDIATE`` transaction committed
    together with its record row — SQLite DDL is transactional only
    under an explicit BEGIN (legacy autocommit would leave failed
    migrations half-applied), so apply+record is genuinely atomic: a
    failure rolls both back and the migration is retried on the next
    ``init_db``.

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
    cursor = await db.execute("SELECT id, name FROM schema_migrations")
    applied = {row[0]: row[1] for row in await cursor.fetchall()}

    applied_now: list[str] = []
    for migration in MIGRATIONS:
        _assert_recorded_name_matches(migration, applied)
        if migration.id in applied:
            continue
        logger.info("Applying schema migration %s", migration.name)
        await _apply_migration(db, migration)
        applied_now.append(migration.name)
    return applied_now


def _assert_recorded_name_matches(
    migration: Migration, applied: dict[int, str]
) -> None:
    """A recorded id must still carry its shipped name."""
    recorded = applied.get(migration.id)
    if recorded is None or recorded == migration.name:
        return
    raise RuntimeError(
        f"Migration {migration.id} is recorded as"
        f" {recorded!r} but the code says"
        f" {migration.name!r}. Migration names are frozen"
        " once shipped (a rename forks history); restore"
        " the recorded name or append a new migration."
    )


async def _apply_migration(db, migration: Migration) -> None:
    """Apply one migration + its record row atomically.

    Closes any implicit transaction left open by earlier init_db
    statements (sqlite3 legacy control implicitly begins before DML;
    an open transaction would make BEGIN IMMEDIATE fail with
    "cannot start a transaction within a transaction"), then runs
    apply+record in one explicit ``BEGIN IMMEDIATE`` transaction —
    SQLite DDL is transactional only under an explicit BEGIN (legacy
    autocommit would leave failed migrations half-applied), so a
    failure rolls both back and the migration is retried on the next
    ``init_db``.
    """
    await db.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        await migration.apply(db)
        await db.execute(
            "INSERT INTO schema_migrations (id, name) VALUES (?, ?)",
            (migration.id, migration.name),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
