"""Ordered schema-migration runner tests (#30).

Covers the runner contract (fresh DB, replay no-op, failure semantics,
validation), migration 0001's shape (password_history + cascade),
0002's (users.last_login_at), 0003's (user_sessions + cascade,
#2585), 0004's (workstation columns on user_sessions, #2586),
0005's (users.disabled + users.last_activity_at, #2588),
0009's (workspaces.per_handle_home backfill, #2719),
0010's (groups.source marker + name-pattern backfill, #2750),
0011's (`files-download` mirror of Allow `files` ACEs, #2705),
0012's (`files-write` mirror of Allow `files-download` ACEs), and
0013's (exec-and-sync role-group backfill, #2706/#2712), 0014's
(/groups create ACE tightened to the admin group, #2770), and
0015's (workspaces.classification_banner, #2768).
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
            (10, "0010_groups_source"),
            (11, "0011_files_download"),
            (12, "0012_files_write"),
            (13, "0013_exec_and_sync_permission"),
            (14, "0014_groups_create_admin"),
            (15, "0015_classification_banner"),
            (16, "0016_monitor_permission"),
            (17, "0017_change_acls_permission"),
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
            # Migration 0010 added the groups source marker (#2750).
            info = await db.execute("PRAGMA table_info(groups)")
            cols = {r[1] for r in await info.fetchall()}
            assert "source" in cols

            # Migration 0008: no agent row exists on a fresh DB before
            # seeding (UPDATE is a no-op); recorded above.

            # Re-run: nothing new applied, still exactly ten records.
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
                (10, "0010_groups_source"),
                (11, "0011_files_download"),
                (12, "0012_files_write"),
                (13, "0013_exec_and_sync_permission"),
                (14, "0014_groups_create_admin"),
                (15, "0015_classification_banner"),
                (16, "0016_monitor_permission"),
                (17, "0017_change_acls_permission"),
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


class TestM0010GroupsSource:
    """m0010: groups.source marker + name-pattern backfill (#2750).

    The backfill is exercised against a hand-built pre-migration
    schema (legacy ``groups`` without the column, minimal
    ``workspaces``) by calling ``apply`` directly, the same pattern the
    failure-semantics tests use for synthetic migrations.
    """

    WS_ID = "11111111-2222-3333-4444-555555555555"

    async def _legacy_db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0010.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE groups ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
            " description TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        await db.execute("CREATE TABLE workspaces (id TEXT, name TEXT)")
        await db.execute(
            "INSERT INTO workspaces (id, name) VALUES (?, ?)",
            (self.WS_ID, "legacy-ws"),
        )
        return db

    async def _sources(self, db) -> dict[str, tuple[str, str]]:
        cursor = await db.execute(
            "SELECT name, source, description FROM groups"
        )
        return {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}

    async def test_backfill_classifies_and_normalizes(self, tmp_path):
        """Pattern-matching rows for an existing workspace become
        workspace-role with normalized descriptions; everything else
        stays manual."""
        from klangk.model.migrations import m0010_groups_source

        db = await self._legacy_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name, description) VALUES"
                " ('g1', ?, ?)",
                (
                    f"owners-{self.WS_ID}",
                    f"owners-{self.WS_ID} for workspace legacy-ws",
                ),
            )
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g2', 'human')"
            )
            # Role-shaped name but the suffix is not a UUID.
            await db.execute(
                "INSERT INTO groups (id, name) VALUES"
                " ('g3', 'coders-not-a-uuid')"
            )
            # Collision: role-shaped + valid UUID, but no such workspace.
            await db.execute(
                "INSERT INTO groups (id, name) VALUES"
                " ('g4', 'spectators-99999999-9999-9999-9999-999999999999')"
            )
            await m0010_groups_source.migration.apply(db)

            rows = await self._sources(db)
            source, description = rows[f"owners-{self.WS_ID}"]
            assert source == "workspace-role"
            assert description == (
                "Workspace role group: owners of workspace legacy-ws"
            )
            assert rows["human"][0] == "manual"
            assert rows["coders-not-a-uuid"][0] == "manual"
            assert (
                rows["spectators-99999999-9999-9999-9999-999999999999"][0]
                == "manual"
            )
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_empty_groups_table(self, tmp_path):
        """No rows to classify: the ALTER still lands, nothing raises."""
        from klangk.model.migrations import m0010_groups_source

        db = await self._legacy_db(tmp_path)
        try:
            await m0010_groups_source.migration.apply(db)
            assert await self._sources(db) == {}
            info = await db.execute("PRAGMA table_info(groups)")
            cols = {r[1] for r in await info.fetchall()}
            assert "source" in cols
        finally:
            await db.__aexit__(None, None, None)


class TestM0011FilesDownload:
    """Migration 0011 mirrors Allow `files` ACEs as Allow `files-download`
    at the adjacent position (#2705)."""

    async def _legacy_db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0011.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT NOT NULL, position INTEGER NOT NULL,"
            " action INTEGER NOT NULL, principal_type INTEGER NOT NULL,"
            " user_id TEXT, group_id TEXT, system_principal INTEGER,"
            " permission TEXT NOT NULL,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _rows(self, db, resource: str) -> list[tuple]:
        cursor = await db.execute(
            "SELECT position, action, principal_type, user_id, group_id,"
            " system_principal, permission FROM acl_entries"
            " WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return await cursor.fetchall()

    async def _insert(
        self,
        db,
        resource,
        position,
        action,
        permission,
        *,
        principal_type=1,
        user_id=None,
        group_id=None,
        system_principal=None,
    ):
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource,
                position,
                action,
                principal_type,
                user_id,
                group_id,
                system_principal,
                permission,
            ),
        )

    async def test_mirrors_allow_files_adjacent(self, tmp_path):
        """Each Allow `files` ACE gains an adjacent Allow `files-download`
        twin; `*`, Deny `files`, and other permissions are untouched."""
        from klangk.model.migrations import m0011_files_download

        db = await self._legacy_db(tmp_path)
        try:
            res = "/workspaces/ws-1"
            # position 0: Allow * for the owner — already covers download.
            await self._insert(db, res, 0, 1, "*", user_id="u-owner")
            # position 1: Allow files for a member — must be mirrored.
            await self._insert(db, res, 1, 1, "files", user_id="u-member")
            # position 2: Deny files for a group — must NOT be mirrored.
            await self._insert(
                db, res, 2, 0, "files", principal_type=2, group_id="g-1"
            )
            # position 3: Allow view — untouched.
            await self._insert(db, res, 3, 1, "view", user_id="u-member")
            await m0011_files_download.migration.apply(db)

            rows = await self._rows(db, res)
            assert rows == [
                (0, 1, 1, "u-owner", None, None, "*"),
                (1, 1, 1, "u-member", None, None, "files"),
                (2, 1, 1, "u-member", None, None, "files-download"),
                (3, 0, 2, None, "g-1", None, "files"),
                (4, 1, 1, "u-member", None, None, "view"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_mirror_precedes_later_deny_wildcard(self, tmp_path):
        """The mirror takes the position right after its source, so a
        later Deny `*` keeps the same answer for `files-download` as the
        source entry gave for `files` (appending at the end would be
        shadowed by the deny)."""
        from klangk.model.migrations import m0011_files_download

        db = await self._legacy_db(tmp_path)
        try:
            res = "/workspaces/ws-2"
            await self._insert(db, res, 0, 1, "files", user_id="u-member")
            await self._insert(
                db, res, 1, 0, "*", principal_type=0, system_principal=0
            )
            await m0011_files_download.migration.apply(db)

            rows = await self._rows(db, res)
            assert rows == [
                (0, 1, 1, "u-member", None, None, "files"),
                (1, 1, 1, "u-member", None, None, "files-download"),
                (2, 0, 0, None, None, 0, "*"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_files_entries_noop(self, tmp_path):
        """Resources without Allow `files` entries are untouched."""
        from klangk.model.migrations import m0011_files_download

        db = await self._legacy_db(tmp_path)
        try:
            await self._insert(
                db, "/", 0, 0, "*", principal_type=0, system_principal=0
            )
            await self._insert(
                db, "/", 1, 1, "view", principal_type=0, system_principal=1
            )
            await m0011_files_download.migration.apply(db)
            assert await self._rows(db, "/") == [
                (0, 0, 0, None, None, 0, "*"),
                (1, 1, 0, None, None, 1, "view"),
            ]
        finally:
            await db.__aexit__(None, None, None)


class TestM0012FilesUpload:
    """Migration 0012 mirrors Allow `files-download` ACEs as Allow
    `files-write` at the adjacent position (upload tracks download)."""

    async def _legacy_db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0012.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT NOT NULL, position INTEGER NOT NULL,"
            " action INTEGER NOT NULL, principal_type INTEGER NOT NULL,"
            " user_id TEXT, group_id TEXT, system_principal INTEGER,"
            " permission TEXT NOT NULL,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _rows(self, db, resource: str) -> list[tuple]:
        cursor = await db.execute(
            "SELECT position, action, principal_type, user_id, group_id,"
            " system_principal, permission FROM acl_entries"
            " WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return await cursor.fetchall()

    async def _insert(
        self,
        db,
        resource,
        position,
        action,
        permission,
        *,
        principal_type=1,
        user_id=None,
        group_id=None,
        system_principal=None,
    ):
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource,
                position,
                action,
                principal_type,
                user_id,
                group_id,
                system_principal,
                permission,
            ),
        )

    async def test_mirrors_allow_files_download_adjacent(self, tmp_path):
        """Each Allow `files-download` ACE gains an adjacent Allow
        `files-write` twin; `files`, Deny `files-download`, and `*` are
        untouched — in particular a plain `files` grant does NOT gain
        upload (the source is `files-download`, not `files`)."""
        from klangk.model.migrations import m0012_files_write

        db = await self._legacy_db(tmp_path)
        try:
            res = "/workspaces/ws-1"
            await self._insert(db, res, 0, 1, "*", user_id="u-owner")
            await self._insert(db, res, 1, 1, "files", user_id="u-member")
            # 0011's mirror of the entry above — the source 0012 copies.
            await self._insert(
                db, res, 2, 1, "files-download", user_id="u-member"
            )
            # Deny files-download must not be mirrored.
            await self._insert(
                db,
                res,
                3,
                0,
                "files-download",
                principal_type=2,
                group_id="g-1",
            )
            await m0012_files_write.migration.apply(db)

            rows = await self._rows(db, res)
            assert rows == [
                (0, 1, 1, "u-owner", None, None, "*"),
                (1, 1, 1, "u-member", None, None, "files"),
                (2, 1, 1, "u-member", None, None, "files-download"),
                (3, 1, 1, "u-member", None, None, "files-write"),
                (4, 0, 2, None, "g-1", None, "files-download"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_files_download_entries_noop(self, tmp_path):
        """A pre-0011 database shape (only `files`/`*` grants) is
        untouched — 0012 never invents grants from `files`."""
        from klangk.model.migrations import m0012_files_write

        db = await self._legacy_db(tmp_path)
        try:
            await self._insert(db, "/", 0, 1, "files", user_id="u-member")
            await m0012_files_write.migration.apply(db)
            assert await self._rows(db, "/") == [
                (0, 1, 1, "u-member", None, None, "files"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_0011_then_0012_grants_both_channels(self, tmp_path):
        """The realistic upgrade path: on a legacy DB, 0011 materializes
        `files-download` mirrors, then 0012 copies them — every `files`
        holder ends up with both transfer permissions."""
        from klangk.model.migrations import (
            m0011_files_download,
            m0012_files_write,
        )

        db = await self._legacy_db(tmp_path)
        try:
            res = "/workspaces/ws-2"
            await self._insert(db, res, 0, 1, "*", user_id="u-owner")
            await self._insert(db, res, 1, 1, "files", user_id="u-member")
            await m0011_files_download.migration.apply(db)
            await m0012_files_write.migration.apply(db)

            rows = await self._rows(db, res)
            assert rows == [
                (0, 1, 1, "u-owner", None, None, "*"),
                (1, 1, 1, "u-member", None, None, "files"),
                (2, 1, 1, "u-member", None, None, "files-download"),
                (3, 1, 1, "u-member", None, None, "files-write"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_post_0011_mirror_deletion_withholds_upload(self, tmp_path):
        """The motivating case for mirroring `files-download` (not
        `files`): an operator who deleted one member's `files-download`
        mirror after 0011 withholds upload from that member only, while a
        member whose mirror is intact gains it."""
        from klangk.model.migrations import (
            m0011_files_download,
            m0012_files_write,
        )

        db = await self._legacy_db(tmp_path)
        try:
            res = "/workspaces/ws-3"
            await self._insert(db, res, 0, 1, "files", user_id="u-kept")
            await self._insert(db, res, 1, 1, "files", user_id="u-trimmed")
            await m0011_files_download.migration.apply(db)
            # Operator deletes u-trimmed's mirror post-0011.
            await db.execute(
                "DELETE FROM acl_entries"
                " WHERE resource = ? AND user_id = ?"
                " AND permission = 'files-download'",
                (res, "u-trimmed"),
            )
            await m0012_files_write.migration.apply(db)

            perms = {
                (user_id, perm)
                for _, _, _, user_id, _, _, perm in await self._rows(db, res)
            }
            assert ("u-kept", "files-download") in perms
            assert ("u-kept", "files-write") in perms
            assert ("u-trimmed", "files-download") not in perms
            assert ("u-trimmed", "files-write") not in perms
        finally:
            await db.__aexit__(None, None, None)


class TestM0013ExecAndSyncPermission:
    """m0013: backfill ``exec-and-sync`` onto existing role groups (#2706/#2712).

    Exercised against a hand-built post-m0010 schema (groups with the
    ``source`` marker, minimal ``acl_entries``) by calling ``apply``
    directly, mirroring the m0010 pattern.
    """

    WS_ID = "11111111-2222-3333-4444-555555555555"
    RESOURCE = f"/workspaces/{WS_ID}"

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0013.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT, source TEXT)"
        )
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT)"
        )
        for role in ("owners", "coders", "collaborators", "spectators"):
            await db.execute(
                "INSERT INTO groups (id, name, source) VALUES (?, ?, ?)",
                (f"g-{role}", f"{role}-{self.WS_ID}", "workspace-role"),
            )
        await db.execute(
            "INSERT INTO groups (id, name, source) VALUES (?, ?, ?)",
            ("g-human", "human", "manual"),
        )
        # A pre-#2706 seeded ACL: owner wildcard + one ACE per role group.
        for resource, pos, gid, perm in (
            (self.RESOURCE, 0, "g-owners", "*"),
            (self.RESOURCE, 1, "g-coders", "terminal"),
            (self.RESOURCE, 2, "g-collaborators", "terminal"),
            (self.RESOURCE, 3, "g-spectators", "terminal"),
        ):
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, group_id, permission)"
                " VALUES (?, ?, 1, 2, ?, ?)",
                (resource, pos, gid, perm),
            )
        return db

    async def _exec_aces(self, db) -> dict[str, int]:
        """group_id -> count of exec-and-sync Allow ACEs."""
        cursor = await db.execute(
            "SELECT group_id, COUNT(*) FROM acl_entries"
            " WHERE permission = 'exec-and-sync' AND action = 1"
            " GROUP BY group_id"
        )
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def test_backfill_grants_exec_and_sync_roles_only(self, tmp_path):
        from klangk.model.migrations import m0013_exec_and_sync_permission

        db = await self._db(tmp_path)
        try:
            await m0013_exec_and_sync_permission.migration.apply(db)
            assert await self._exec_aces(db) == {
                "g-coders": 1,
                "g-collaborators": 1,
            }
            # Appended after the seeded entries, not interleaved.
            cursor = await db.execute(
                "SELECT position FROM acl_entries"
                " WHERE permission = 'exec-and-sync' AND group_id = 'g-coders'"
            )
            assert (await cursor.fetchone())[0] == 4
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_idempotent(self, tmp_path):
        from klangk.model.migrations import m0013_exec_and_sync_permission

        db = await self._db(tmp_path)
        try:
            await m0013_exec_and_sync_permission.migration.apply(db)
            await m0013_exec_and_sync_permission.migration.apply(db)
            assert await self._exec_aces(db) == {
                "g-coders": 1,
                "g-collaborators": 1,
            }
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_skips_manual_groups(self, tmp_path):
        """A manual group named like a role group is untouched — the
        backfill matches on the workspace-role source marker."""
        from klangk.model.migrations import m0013_exec_and_sync_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name, source) VALUES (?, ?, ?)",
                ("g-lookalike", f"coders-{self.WS_ID}-lookalike", "manual"),
            )
            await m0013_exec_and_sync_permission.migration.apply(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries WHERE group_id ="
                " 'g-lookalike'"
            )
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_empty_groups_table(self, tmp_path):
        """No groups: nothing raises, nothing is written."""
        from klangk.model.migrations import m0013_exec_and_sync_permission

        db = aiosqlite.connect(str(tmp_path / "m0013-empty.db"))
        db = await db.__aenter__()
        try:
            await db.execute(
                "CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT,"
                " source TEXT)"
            )
            await db.execute(
                "CREATE TABLE acl_entries ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource TEXT, position INTEGER, action INTEGER,"
                " principal_type INTEGER, user_id TEXT, group_id TEXT,"
                " system_principal INTEGER, permission TEXT)"
            )
            await m0013_exec_and_sync_permission.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)


class TestM0014GroupsCreateAdmin:
    """Migration 0014 tightens the seeded Allow-create→authenticated ACE
    on /groups to the admin group (#2770)."""

    async def _legacy_db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0013.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT NOT NULL, position INTEGER NOT NULL,"
            " action INTEGER NOT NULL, principal_type INTEGER NOT NULL,"
            " user_id TEXT, group_id TEXT, system_principal INTEGER,"
            " permission TEXT NOT NULL,"
            " UNIQUE(resource, position))"
        )
        await db.execute(
            "CREATE TABLE groups ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
            " description TEXT,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        return db

    async def _rows(self, db) -> list[tuple]:
        cursor = await db.execute(
            "SELECT position, action, principal_type, user_id, group_id,"
            " system_principal, permission FROM acl_entries"
            " WHERE resource = '/groups' ORDER BY position"
        )
        return await cursor.fetchall()

    async def _seed_open_ace(self, db):
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES ('/groups', 0, 1, 0, NULL, NULL, 1, 'create')"
        )

    async def test_rewrites_seeded_ace_to_admin_group(self, tmp_path):
        """The exact seeded shape is replaced with an Allow-create ACE
        for the admin group."""
        from klangk.model.migrations import m0014_groups_create_admin

        db = await self._legacy_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-admin', 'admin')"
            )
            await self._seed_open_ace(db)
            await m0014_groups_create_admin.migration.apply(db)

            assert await self._rows(db) == [
                (0, 1, 2, None, "g-admin", None, "create")
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_groups_entries_noop(self, tmp_path):
        """Fresh / pre-seed deployments (nothing on /groups) are
        untouched — the new lifecycle seed covers them."""
        from klangk.model.migrations import m0014_groups_create_admin

        db = await self._legacy_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-admin', 'admin')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  group_id, system_principal, permission)"
                " VALUES ('/admin', 0, 1, 2, NULL, 'g-admin', NULL, '*')"
            )
            await m0014_groups_create_admin.migration.apply(db)

            assert await self._rows(db) == []
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries WHERE resource = '/admin'"
            )
            assert (await cursor.fetchone())[0] == 1
        finally:
            await db.__aexit__(None, None, None)

    async def test_customized_resource_untouched(self, tmp_path):
        """Any other /groups shape means an operator customized it —
        extra entries, a different principal, a different permission —
        and the migration must not clobber the operator's choice."""
        from klangk.model.migrations import m0014_groups_create_admin

        db = await self._legacy_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-admin', 'admin')"
            )
            # Seeded shape + an operator-added loosening for members.
            await self._seed_open_ace(db)
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-m', 'members')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  group_id, system_principal, permission)"
                " VALUES ('/groups', 1, 1, 2, NULL, 'g-m', NULL, 'create')"
            )
            await m0014_groups_create_admin.migration.apply(db)

            rows = await self._rows(db)
            assert len(rows) == 2
            assert (0, 1, 0, None, None, 1, "create") in rows
            assert (1, 1, 2, None, "g-m", None, "create") in rows
        finally:
            await db.__aexit__(None, None, None)

    async def test_seeded_shape_without_admin_group_deletes_only(
        self, tmp_path
    ):
        """No 'admin' group row (ensure_admin_group recreates it at
        boot): the seeded ACE is removed with no replacement."""
        from klangk.model.migrations import m0014_groups_create_admin

        db = await self._legacy_db(tmp_path)
        try:
            await self._seed_open_ace(db)
            await m0014_groups_create_admin.migration.apply(db)

            assert await self._rows(db) == []
        finally:
            await db.__aexit__(None, None, None)


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


class TestPerHandleHome:
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


class TestClassificationBanner:
    async def test_upgrade_adds_null_column(self, temp_data_dir, app_state):
        """A database last booted before #2768 (workspaces without
        classification_banner) gets the column from migration 0015, and
        every pre-existing row reads back NULL — inherit the deploy
        default, resolved at display time.
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
            "SELECT classification_banner FROM workspaces WHERE id = 'ws-old'"
        )
        assert rows[0][0] is None
        # Idempotent: re-running init_db skips the recorded migration.
        await app_state.state.model.init_db()
        rows = await app_state.state.db.fetchall(
            "SELECT classification_banner FROM workspaces WHERE id = 'ws-old'"
        )
        assert rows[0][0] is None


class TestM0016MonitorPermission:
    """m0016: backfill ``monitor`` onto existing terminal holders (#2783).

    Health/status reception moved from the ``terminal`` permission to
    the dedicated ``monitor`` permission; this migration preserves
    status delivery for every principal that already had ``terminal``
    (role groups and direct user shares alike).
    """

    WS_ID = "11111111-2222-3333-4444-555555555555"
    RESOURCE = f"/workspaces/{WS_ID}"

    async def _db(self, tmp_path):
        import aiosqlite

        db = aiosqlite.connect(str(tmp_path / "m0016.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT)"
        )
        # A pre-#2783 workspace ACL: owner wildcard, role-group terminal
        # ACEs, and a direct member share (user-principal terminal).
        for resource, pos, principal_type, user_id, group_id, perm in (
            (self.RESOURCE, 0, 1, "u-owner", None, "*"),
            (self.RESOURCE, 1, 2, None, "g-coders", "terminal"),
            (self.RESOURCE, 2, 2, None, "g-spectators", "terminal"),
            (self.RESOURCE, 3, 1, "u-member", None, "terminal"),
            (self.RESOURCE, 4, 1, "u-member", None, "view"),
        ):
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, ?, 1, ?, ?, ?, ?)",
                (resource, pos, principal_type, user_id, group_id, perm),
            )
        return db

    async def _monitor_aces(self, db) -> dict[tuple, int]:
        cursor = await db.execute(
            "SELECT user_id, group_id, COUNT(*) FROM acl_entries"
            " WHERE permission = 'monitor' AND action = 1"
            " GROUP BY user_id, group_id"
        )
        return {(row[0], row[1]): row[2] for row in await cursor.fetchall()}

    async def test_backfill_covers_groups_and_direct_users(self, tmp_path):
        from klangk.model.migrations import m0016_monitor_permission

        db = await self._db(tmp_path)
        try:
            await m0016_monitor_permission.migration.apply(db)
            # Role groups and the direct member share are covered; the
            # wildcard owner needs nothing.
            assert await self._monitor_aces(db) == {
                (None, "g-coders"): 1,
                (None, "g-spectators"): 1,
                ("u-member", None): 1,
            }
            # Appended after the seeded entries, not interleaved.
            cursor = await db.execute(
                "SELECT position FROM acl_entries"
                " WHERE permission = 'monitor' AND group_id = 'g-coders'"
            )
            assert (await cursor.fetchone())[0] == 5
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_idempotent(self, tmp_path):
        from klangk.model.migrations import m0016_monitor_permission

        db = await self._db(tmp_path)
        try:
            await m0016_monitor_permission.migration.apply(db)
            await m0016_monitor_permission.migration.apply(db)
            assert await self._monitor_aces(db) == {
                (None, "g-coders"): 1,
                (None, "g-spectators"): 1,
                ("u-member", None): 1,
            }
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_skips_deny_terminal(self, tmp_path):
        """Only ``Allow`` terminal ACEs earn the backfill — a Deny row is\n        not a grant."""
        from klangk.model.migrations import m0016_monitor_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 5, 0, 1, 'u-denied', NULL, 'terminal')",
                (self.RESOURCE,),
            )
            await m0016_monitor_permission.migration.apply(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries"
                " WHERE user_id = 'u-denied' AND permission = 'monitor'"
            )
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_empty_table(self, tmp_path):
        """No ACEs: nothing raises, nothing is written."""
        import aiosqlite

        from klangk.model.migrations import m0016_monitor_permission

        db = aiosqlite.connect(str(tmp_path / "m0016-empty.db"))
        db = await db.__aenter__()
        try:
            await db.execute(
                "CREATE TABLE acl_entries ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource TEXT, position INTEGER, action INTEGER,"
                " principal_type INTEGER, user_id TEXT, group_id TEXT,"
                " system_principal INTEGER, permission TEXT)"
            )
            await m0016_monitor_permission.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)


class TestM0017ChangeAclsPermission:
    """m0017: backfill ``change-acls`` onto existing ``share`` holders (#2764).

    Raw ACL editing moved from ``share`` to the dedicated
    ``change-acls`` permission; this migration preserves the ability for
    every principal that already had ``share`` (role groups and direct
    user shares alike).
    """

    WS_ID = "11111111-2222-3333-4444-555555555555"
    RESOURCE = f"/workspaces/{WS_ID}"

    async def _db(self, tmp_path):
        import aiosqlite

        db = aiosqlite.connect(str(tmp_path / "m0017.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT)"
        )
        # A pre-#2764 workspace ACL: owner wildcard, a share-holding
        # role group, and a direct member share (user-principal share).
        for resource, pos, principal_type, user_id, group_id, perm in (
            (self.RESOURCE, 0, 1, "u-owner", None, "*"),
            (self.RESOURCE, 1, 2, None, "g-editors", "share"),
            (self.RESOURCE, 2, 1, "u-member", None, "share"),
            (self.RESOURCE, 3, 1, "u-member", None, "view"),
        ):
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, ?, 1, ?, ?, ?, ?)",
                (resource, pos, principal_type, user_id, group_id, perm),
            )
        return db

    async def _change_acls_aces(self, db) -> dict[tuple, int]:
        cursor = await db.execute(
            "SELECT user_id, group_id, COUNT(*) FROM acl_entries"
            " WHERE permission = 'change-acls' AND action = 1"
            " GROUP BY user_id, group_id"
        )
        return {(row[0], row[1]): row[2] for row in await cursor.fetchall()}

    async def test_backfill_covers_groups_and_direct_users(self, tmp_path):
        from klangk.model.migrations import m0017_change_acls_permission

        db = await self._db(tmp_path)
        try:
            await m0017_change_acls_permission.migration.apply(db)
            # The share-holding group and the direct member share are
            # covered; the wildcard owner needs nothing.
            assert await self._change_acls_aces(db) == {
                (None, "g-editors"): 1,
                ("u-member", None): 1,
            }
            # Appended after the seeded entries, not interleaved.
            cursor = await db.execute(
                "SELECT position FROM acl_entries"
                " WHERE permission = 'change-acls' AND group_id = 'g-editors'"
            )
            assert (await cursor.fetchone())[0] == 4
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_idempotent(self, tmp_path):
        from klangk.model.migrations import m0017_change_acls_permission

        db = await self._db(tmp_path)
        try:
            await m0017_change_acls_permission.migration.apply(db)
            await m0017_change_acls_permission.migration.apply(db)
            assert await self._change_acls_aces(db) == {
                (None, "g-editors"): 1,
                ("u-member", None): 1,
            }
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_skips_deny_share(self, tmp_path):
        """Only ``Allow`` share ACEs earn the backfill — a Deny row is
        not a grant."""
        from klangk.model.migrations import m0017_change_acls_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 4, 0, 1, 'u-denied', NULL, 'share')",
                (self.RESOURCE,),
            )
            await m0017_change_acls_permission.migration.apply(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries"
                " WHERE user_id = 'u-denied' AND permission = 'change-acls'"
            )
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_empty_table(self, tmp_path):
        """No ACEs: nothing raises, nothing is written."""
        import aiosqlite

        from klangk.model.migrations import m0017_change_acls_permission

        db = aiosqlite.connect(str(tmp_path / "m0017-empty.db"))
        db = await db.__aenter__()
        try:
            await db.execute(
                "CREATE TABLE acl_entries ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource TEXT, position INTEGER, action INTEGER,"
                " principal_type INTEGER, user_id TEXT, group_id TEXT,"
                " system_principal INTEGER, permission TEXT)"
            )
            await m0017_change_acls_permission.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)
