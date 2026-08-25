"""Ordered schema-migration runner tests (#30).

Covers the runner contract (fresh DB, replay no-op, failure semantics,
validation), migration 0001's shape (password_history + cascade),
0002's (users.last_login_at), 0003's (user_sessions + cascade,
#2585), 0004's (workstation columns on user_sessions, #2586),
0005's (users.disabled + users.last_activity_at, #2588), and
0009's (workspaces.per_handle_home backfill, #2719).
"""

import aiosqlite
import pytest

from klangk.model import migrations as migrations_mod
from klangk.model.migrations import Migration, run_migrations
from klangk.model.users import AGENT_USER_ID


async def _recorded(db) -> list[tuple[int, str]]:
    cursor = await db.execute(
        "SELECT id, name FROM schema_migrations ORDER BY id"
    )
    return [(r[0], r[1]) for r in await cursor.fetchall()]


async def run_migrations_with(db, ordered):
    """Run *ordered* migrations against *db* using the real runner logic.

    The production ``run_migrations`` reads the module-level MIGRATIONS;
    this helper temporarily swaps it so failure/fixture tests exercise
    the real code path against synthetic lists.
    """
    original = migrations_mod.MIGRATIONS
    migrations_mod.MIGRATIONS = ordered
    try:
        return await run_migrations(db)
    finally:
        migrations_mod.MIGRATIONS = original


class TestRunner:
    async def test_fresh_db_applies_and_records(
        self, temp_data_dir, app_state
    ):
        """init_db on a fresh DB applies every migration exactly once and
        records it; a second init_db is a no-op."""
        await app_state.state.model.init_db()
        expected = [
            (1, "0001_password_history"),
            (2, "0002_last_login_at"),
            (3, "0003_user_sessions"),
            (4, "0004_user_sessions_workstation"),
            (5, "0005_user_inactivity"),
            (6, "0006_host_schedules"),
            (7, "0007_server_schedules"),
            (8, "0008_agent_user_klangk"),
            (9, "0009_per_handle_home"),
        ]
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            assert await _recorded(db) == expected
            # Migration 0001 and 0003 created their tables (not the
            # baseline pile).
            for table in ("password_history", "user_sessions"):
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master"
                    f" WHERE type='table' AND name='{table}'"
                )
                assert await cursor.fetchone() is not None
            # Migration 0002 added its column to the baseline users table.
            info = await db.execute("PRAGMA table_info(users)")
            cols = {r[1] for r in await info.fetchall()}
            assert "last_login_at" in cols
            # Migration 0004 added the workstation columns (#2586).
            info = await db.execute("PRAGMA table_info(user_sessions)")
            cols = {r[1] for r in await info.fetchall()}
            assert {"source_ip", "user_agent"} <= cols
            # Migration 0005 added the inactivity columns (#2588).
            info = await db.execute("PRAGMA table_info(users)")
            cols = {r[1] for r in await info.fetchall()}
            assert {"disabled", "last_activity_at"} <= cols
            # Migration 0009 added the home-layout column (#2719).
            info = await db.execute("PRAGMA table_info(workspaces)")
            cols = {r[1] for r in await info.fetchall()}
            assert "per_handle_home" in cols

            # Migration 0008: no agent row exists on a fresh DB before
            # seeding (UPDATE is a no-op); recorded above.

            # Re-run: nothing new applied, still exactly five records.
            await app_state.state.model.init_db()
            assert await _recorded(db) == expected

    async def test_old_db_without_migrations_table(
        self, temp_data_dir, app_state
    ):
        """A pre-#30 database (users table only, no bookkeeping) gets the
        migration applied on the next init_db."""
        from _helpers import get_test_db

        db_path = get_test_db().db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    verified INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'local',
                    external_id TEXT,
                    handle TEXT UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            await db.commit()

        await app_state.state.model.init_db()
        async with aiosqlite.connect(str(db_path)) as db:
            assert await _recorded(db) == [
                (1, "0001_password_history"),
                (2, "0002_last_login_at"),
                (3, "0003_user_sessions"),
                (4, "0004_user_sessions_workstation"),
                (5, "0005_user_inactivity"),
                (6, "0006_host_schedules"),
                (7, "0007_server_schedules"),
                (8, "0008_agent_user_klangk"),
                (9, "0009_per_handle_home"),
            ]

    async def test_m0008_agent_identity_and_human_collision(
        self, temp_data_dir, app_state
    ):
        """m0008 rewrites a clanker-era agent row to the fixed identity and
        bumps a human holding 'klangk' to a unique alternative (#2718).

        Runs through the real runner: the pre-state is built, m0008's
        record row is deleted, and init_db re-runs it with BEGIN IMMEDIATE
        + schema_migrations interplay — the actual boot sequence."""

        await app_state.state.model.init_db()
        async with app_state.state.db.transaction() as db:
            # A human who claimed 'klangk' before it was reserved, and a
            # clanker-era agent row (init_db does not seed the agent —
            # Lifecycle.seed_agent_user does, so both rows are ours).
            await db.execute(
                "INSERT INTO users (id, email, handle) VALUES"
                " ('human-1', 'human@x.com', 'klangk')"
            )
            await db.execute(
                "INSERT INTO users (id, email, handle, verified, provider)"
                " VALUES (?, ?, ?, 1, 'system')",
                (AGENT_USER_ID, "clanker@example.com", "clanker"),
            )
            # Forget m0008 ran, so init_db re-applies it via the runner.
            await db.execute("DELETE FROM schema_migrations WHERE id = 8")
        app_state.state.model.users.clear_agent_cache()

        await app_state.state.model.init_db()

        async with app_state.state.db.transaction() as db:
            rows = {}
            cursor = await db.execute("SELECT id, handle FROM users")
            for row in await cursor.fetchall():
                rows[row[0]] = row[1]
            # m0008 re-applied and re-recorded.
            cursor = await db.execute(
                "SELECT name FROM schema_migrations WHERE id = 8"
            )
            rec = await cursor.fetchone()
            assert rec is not None and rec[0] == "0008_agent_user_klangk"
        assert rows["human-1"] == "klangk-2"
        assert rows[AGENT_USER_ID] == "klangk"

        # Idempotent: a re-run with the record present changes nothing
        # (the runner skips it; apply itself is also idempotent).
        await app_state.state.model.init_db()
        async with app_state.state.db.transaction() as db:
            cursor = await db.execute(
                "SELECT handle FROM users WHERE id = 'human-1'"
            )
            row = await cursor.fetchone()
            assert row is not None and row[0] == "klangk-2"

    async def test_pending_only(self, tmp_path):
        """Only unrecorded migrations run; recorded ones are skipped."""
        db_path = tmp_path / "runner.db"
        calls: list[str] = []

        async def _noop1(db):  # noqa: ARG001
            calls.append("m1")

        async def _noop2(db):  # noqa: ARG001
            calls.append("m2")

        async with aiosqlite.connect(str(db_path)) as db:
            # Bootstrap bookkeeping + a recorded 0001 via the runner
            # itself (empty list: table only), then hand-insert the
            # pre-existing record.
            await run_migrations_with(db, [])
            await db.execute(
                "INSERT INTO schema_migrations (id, name)"
                " VALUES (1, '0001_one')"
            )
            await db.commit()

            applied = await run_migrations_with(
                db,
                [
                    Migration(1, "0001_one", _noop1),
                    Migration(2, "0002_two", _noop2),
                ],
            )

            assert applied == ["0002_two"]
            assert calls == ["m2"]
            assert await _recorded(db) == [
                (1, "0001_one"),
                (2, "0002_two"),
            ]


class TestFailureSemantics:
    async def test_failed_migration_unrecorded_prior_committed(
        self, tmp_path, monkeypatch
    ):
        """A raising migration is not recorded (retried next boot) while
        prior migrations stay applied and recorded."""
        db_path = tmp_path / "fail.db"
        # Bootstrap the bookkeeping table first (as run_migrations does).
        async with aiosqlite.connect(str(db_path)) as db:
            await run_migrations_with(db, [])

        async def _ok(db):
            await db.execute(
                "CREATE TABLE IF NOT EXISTS marker_ok (x INTEGER)"
            )

        async def _bad(db):  # noqa: ARG001
            raise RuntimeError("boom")

        ordered = [
            Migration(1, "0001_ok", _ok),
            Migration(2, "0002_bad", _bad),
        ]

        async with aiosqlite.connect(str(db_path)) as db:
            # Suppress the runner's logging of the failure? No — the
            # exception propagates; that IS the contract.
            with pytest.raises(RuntimeError, match="boom"):
                await run_migrations_with(db, ordered)
            assert await _recorded(db) == [(1, "0001_ok")]
            cursor = await db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='marker_ok'"
            )
            assert await cursor.fetchone() is not None

            # Next boot with the migration fixed (appended as a working
            # one) converges without re-running 0001.
            async def _fixed(db):  # noqa: ARG001
                pass

            await run_migrations_with(
                db,
                [
                    Migration(1, "0001_ok", _ok),
                    Migration(2, "0002_bad", _fixed),
                ],
            )
            assert await _recorded(db) == [
                (1, "0001_ok"),
                (2, "0002_bad"),
            ]

    async def test_crash_between_ddl_and_record_rolls_back(self, tmp_path):
        """The central partial-failure claim: a migration whose DDL
        succeeded but that failed before its record row (a CREATE
        followed by a raise) must leave NO durable trace — the BEGIN
        IMMEDIATE rollback undoes the DDL, so the retry can never hit
        "duplicate column name" / "table already exists".

        Without the explicit transaction this test fails on stock
        sqlite3 legacy autocommit (the DDL would survive) — exactly the
        boot-loop bug the adversarial review caught.
        """
        db_path = tmp_path / "crash.db"

        async def _crasher(db):
            await db.execute("CREATE TABLE crashed_marker (x INTEGER)")
            raise RuntimeError("simulated crash after DDL")

        async def _fixed(db):  # noqa: ARG001
            pass

        async with aiosqlite.connect(str(db_path)) as db:
            with pytest.raises(RuntimeError, match="simulated crash"):
                await run_migrations_with(
                    db, [Migration(1, "0001_crash", _crasher)]
                )
            # Nothing durable: no record row AND no table.
            assert await _recorded(db) == []
            cursor = await db.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='table' AND name='crashed_marker'"
            )
            assert await cursor.fetchone() is None

            # Retry with the fixed migration: applies cleanly, exactly
            # once — no duplicate-object error.
            applied = await run_migrations_with(
                db, [Migration(1, "0001_crash", _fixed)]
            )
            assert applied == ["0001_crash"]
            assert await _recorded(db) == [(1, "0001_crash")]


class TestRenameDetection:
    async def test_renamed_shipped_migration_raises(self, tmp_path):
        """A recorded id whose name changed must fail loudly — a silent
        rename forks history (the record says one thing, the code
        another)."""
        db_path = tmp_path / "rename.db"

        async def _noop(db):  # noqa: ARG001
            pass

        async with aiosqlite.connect(str(db_path)) as db:
            await run_migrations_with(
                db, [Migration(1, "0001_original", _noop)]
            )
            with pytest.raises(RuntimeError, match="frozen once shipped"):
                await run_migrations_with(
                    db, [Migration(1, "0001_renamed", _noop)]
                )
            # Record is untouched by the failure.
            assert await _recorded(db) == [(1, "0001_original")]


class TestValidation:
    def test_rejects_gap(self):
        with pytest.raises(RuntimeError, match="contiguous"):
            migrations_mod._validate_migrations(
                [Migration(1, "a", None), Migration(3, "c", None)]  # noqa: ARG005
            )

    def test_rejects_duplicate_names(self):
        with pytest.raises(RuntimeError, match="Duplicate"):
            migrations_mod._validate_migrations(
                [Migration(1, "a", None), Migration(2, "a", None)]  # noqa: ARG005
            )

    def test_accepts_contiguous(self):
        migrations_mod._validate_migrations(
            [Migration(1, "a", None), Migration(2, "b", None)]  # noqa: ARG005
        )


class TestPerUserHome:
    async def test_upgrade_adds_column_and_backfills_true(
        self, temp_data_dir, app_state
    ):
        """A database last booted before #2719 (workspaces table without
        per_handle_home) gets the column from migration 0009, and every
        pre-existing row reads back as 1: every pre-feature workspace
        was per-handle by construction (#2719).
        """
        from _helpers import get_test_db

        db_path = get_test_db().db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    container_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "INSERT INTO workspaces (id, user_id, name)"
                " VALUES ('ws-old', 'user-x', 'legacy')"
            )
            await db.commit()

        await app_state.state.model.init_db()
        rows = await app_state.state.db.fetchall(
            "SELECT per_handle_home FROM workspaces WHERE id = 'ws-old'"
        )
        assert rows[0][0] == 1
        # Idempotent: re-running init_db skips the recorded migration and
        # leaves the row untouched.
        await app_state.state.model.init_db()
        rows = await app_state.state.db.fetchall(
            "SELECT per_handle_home FROM workspaces WHERE id = 'ws-old'"
        )
        assert rows[0][0] == 1


class TestUserSessionsWorkstation:
    async def test_upgrade_from_0003_schema_adds_columns(
        self, temp_data_dir, app_state
    ):
        """A database last booted on #2585's schema (user_sessions without
        workstation columns) gets them added by migration 0004, and the
        pre-existing rows read back with an unknown (NULL) workstation.
        """
        from _helpers import get_test_db

        db_path = get_test_db().db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE user_sessions (
                    jti TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    expires_at TEXT NOT NULL
                )
            """)
            await db.execute(
                "INSERT INTO user_sessions (jti, user_id, expires_at)"
                " VALUES ('jti-old', ?, '2099-01-01T00:00:00+00:00')",
                ("user-x",),
            )
            await db.commit()

        await app_state.state.model.init_db()
        rows = await app_state.state.db.fetchall(
            "SELECT jti, source_ip, user_agent FROM user_sessions"
        )
        assert [(r[0], r[1], r[2]) for r in rows] == [("jti-old", None, None)]


class TestPasswordHistory:
    async def test_cascade_on_user_delete(self, temp_data_dir, app_state):
        """History rows die with their user (ON DELETE CASCADE)."""
        await app_state.state.model.init_db()
        users = app_state.state.model.users
        user = await users.create_user(
            "hist@example.com", "hash", verified=True
        )
        async with app_state.state.db.transaction() as db:
            await db.execute(
                "INSERT INTO password_history (user_id, password_hash)"
                " VALUES (?, ?)",
                (user["id"], "old-hash"),
            )
        await users.delete_user(user["id"])
        row = await app_state.state.db.fetchone(
            "SELECT COUNT(*) FROM password_history WHERE user_id = ?",
            (user["id"],),
        )
        assert row[0] == 0

    async def test_user_sessions_cascade_on_user_delete(
        self, temp_data_dir, app_state
    ):
        """user_sessions rows die with their user (ON DELETE CASCADE, #2585)."""
        await app_state.state.model.init_db()
        users = app_state.state.model.users
        user = await users.create_user(
            "sess@example.com", "hash", verified=True
        )
        await app_state.state.model.sessions.record_session(
            user["id"], "jti-cascade", "2099-01-01T00:00:00+00:00"
        )
        await users.delete_user(user["id"])
        row = await app_state.state.db.fetchone(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?",
            (user["id"],),
        )
        assert row[0] == 0
