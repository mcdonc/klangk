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
0015's (workspaces.classification_banner, #2768), and
0020's (the ``admin`` group renamed to ``admins``, #2934).
"""

import aiosqlite
import pytest

from klangk.model import migrations as migrations_mod
from klangk.model.acl import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)
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
            (18, "0018_egress_consent_permission"),
            (19, "0019_container_events"),
            (20, "0020_rename_admin_group"),
            (21, "0021_first_class_resource_acls"),
            (22, "0022_workspace_permission_renames"),
            (23, "0023_self_service_resources"),
            (24, "0024_join_workspace_permission"),
            (25, "0025_drop_dead_images_deny_row"),
            (26, "0026_volumes_admin_surface"),
            (27, "0027_retire_admin_marker"),
            (28, "0028_invitations_pending_unique"),
            (29, "0029_members_create_workspace"),
            (30, "0030_audit_hmac"),
            (31, "0031_password_age"),
            (32, "0032_must_change_password"),
            (33, "0033_user_sessions_last_seen"),
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
            # Migration 0031 added the password-age timestamp (#3177).
            info = await db.execute("PRAGMA table_info(users)")
            cols = {r[1] for r in await info.fetchall()}
            assert "password_set_at" in cols

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
                (18, "0018_egress_consent_permission"),
                (19, "0019_container_events"),
                (20, "0020_rename_admin_group"),
                (21, "0021_first_class_resource_acls"),
                (22, "0022_workspace_permission_renames"),
                (23, "0023_self_service_resources"),
                (24, "0024_join_workspace_permission"),
                (25, "0025_drop_dead_images_deny_row"),
                (26, "0026_volumes_admin_surface"),
                (27, "0027_retire_admin_marker"),
                (28, "0028_invitations_pending_unique"),
                (29, "0029_members_create_workspace"),
                (30, "0030_audit_hmac"),
                (31, "0031_password_age"),
                (32, "0032_must_change_password"),
                (33, "0033_user_sessions_last_seen"),
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


class TestM0018EgressConsentPermission:
    """m0018: backfill ``egress-consent`` onto existing role groups
    (#2883).

    Exercised against a hand-built post-m0010 schema (groups with the
    ``source`` marker, minimal ``acl_entries``) by calling ``apply``
    directly, mirroring the m0013 pattern.
    """

    WS_ID = "11111111-2222-3333-4444-555555555555"
    RESOURCE = f"/workspaces/{WS_ID}"

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0018-egress.db"))
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
        # A pre-#2883 seeded ACL: owner wildcard + one ACE per role group.
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

    async def _consent_aces(self, db) -> dict[str, int]:
        """group_id -> count of egress-consent Allow ACEs."""
        cursor = await db.execute(
            "SELECT group_id, COUNT(*) FROM acl_entries"
            " WHERE permission = 'egress-consent' AND action = 1"
            " GROUP BY group_id"
        )
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def test_backfill_grants_consent_roles_only(self, tmp_path):
        from klangk.model.migrations import m0018_egress_consent_permission

        db = await self._db(tmp_path)
        try:
            await m0018_egress_consent_permission.migration.apply(db)
            # Coders/collaborators gain it; owners are covered by the
            # wildcard; spectators (watch-only) never do.
            assert await self._consent_aces(db) == {
                "g-coders": 1,
                "g-collaborators": 1,
            }
            # Appended after the seeded entries, not interleaved.
            cursor = await db.execute(
                "SELECT position FROM acl_entries"
                " WHERE permission = 'egress-consent'"
                " AND group_id = 'g-coders'"
            )
            assert (await cursor.fetchone())[0] == 4
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_idempotent(self, tmp_path):
        from klangk.model.migrations import m0018_egress_consent_permission

        db = await self._db(tmp_path)
        try:
            await m0018_egress_consent_permission.migration.apply(db)
            await m0018_egress_consent_permission.migration.apply(db)
            assert await self._consent_aces(db) == {
                "g-coders": 1,
                "g-collaborators": 1,
            }
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_skips_manual_groups(self, tmp_path):
        """A manual group named like a role group is untouched — the
        backfill matches on the workspace-role source marker."""
        from klangk.model.migrations import m0018_egress_consent_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name, source) VALUES (?, ?, ?)",
                ("g-lookalike", f"coders-{self.WS_ID}-lookalike", "manual"),
            )
            await m0018_egress_consent_permission.migration.apply(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries WHERE group_id ="
                " 'g-lookalike'"
            )
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_backfill_empty_tables(self, tmp_path):
        """No groups: nothing raises, nothing is written."""
        from klangk.model.migrations import m0018_egress_consent_permission

        db = aiosqlite.connect(str(tmp_path / "m0018-egress-empty.db"))
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
            await m0018_egress_consent_permission.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
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
            migrations_mod.validate_migrations(
                [Migration(1, "a", None), Migration(3, "c", None)]  # noqa: ARG005
            )

    def test_rejects_duplicate_names(self):
        with pytest.raises(RuntimeError, match="Duplicate"):
            migrations_mod.validate_migrations(
                [Migration(1, "a", None), Migration(2, "a", None)]  # noqa: ARG005
            )

    def test_accepts_contiguous(self):
        migrations_mod.validate_migrations(
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

    async def test_backfill_skips_shadowed_allow_share(self, tmp_path):
        """An Allow share shadowed by an earlier same-principal Deny (or
        wildcard Deny) never took effect — backfilling it would grant a
        new power, not preserve one, so it is skipped (#2764 review)."""
        from klangk.model.migrations import m0017_change_acls_permission

        db = await self._db(tmp_path)
        try:
            # Deny * at position 4 shadows the Allow share at 5.
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 4, 0, 1, 'u-shadowed', NULL, '*')",
                (self.RESOURCE,),
            )
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 5, 1, 1, 'u-shadowed', NULL, 'share')",
                (self.RESOURCE,),
            )
            # A Deny share BELOW the Allow share shadows it too.
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 6, 0, 1, 'u-shadowed-2', NULL, 'share')",
                (self.RESOURCE,),
            )
            await db.execute(
                "INSERT INTO acl_entries (resource, position, action,"
                " principal_type, user_id, group_id, permission)"
                " VALUES (?, 7, 1, 1, 'u-shadowed-2', NULL, 'share')",
                (self.RESOURCE,),
            )
            await m0017_change_acls_permission.migration.apply(db)
            for uid in ("u-shadowed", "u-shadowed-2"):
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM acl_entries"
                    " WHERE user_id = ? AND permission = 'change-acls'",
                    (uid,),
                )
                assert (await cursor.fetchone())[0] == 0, uid
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


class TestM0020RenameAdminGroup:
    """m0020: rename the seeded ``admin`` group to ``admins`` (#2934).

    The rename is in-place (same row id), so memberships and ACL
    principals survive; a pre-existing ``admins`` group is a collision
    the migration refuses rather than merges.
    """

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0020.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE groups ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
            " description TEXT, created_at TEXT NOT NULL"
            " DEFAULT (datetime('now')))"
        )
        await db.execute(
            "CREATE TABLE user_groups ("
            " user_id TEXT NOT NULL, group_id TEXT NOT NULL,"
            " source TEXT NOT NULL DEFAULT 'manual',"
            " PRIMARY KEY (user_id, group_id))"
        )
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT NOT NULL, position INTEGER NOT NULL,"
            " action INTEGER NOT NULL, principal_type INTEGER NOT NULL,"
            " user_id TEXT, group_id TEXT, system_principal INTEGER,"
            " permission TEXT NOT NULL)"
        )
        return db

    async def _seed_legacy_admin(self, db):
        await db.execute(
            "INSERT INTO groups (id, name) VALUES ('g1', 'admin')"
        )
        await db.execute(
            "INSERT INTO user_groups (user_id, group_id) VALUES ('u1', 'g1')"
        )
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, user_id,"
            "  group_id, system_principal, permission)"
            " VALUES ('/admin', 0, 1, 2, NULL, 'g1', NULL, '*')"
        )

    async def _names(self, db) -> dict:
        cursor = await db.execute("SELECT id, name FROM groups")
        return {r[0]: r[1] for r in await cursor.fetchall()}

    async def test_renames_keeping_id_and_foreign_keys(self, tmp_path):
        from klangk.model.migrations import m0020_rename_admin_group

        db = await self._db(tmp_path)
        try:
            await self._seed_legacy_admin(db)
            await m0020_rename_admin_group.migration.apply(db)

            # Same row id, new name.
            assert await self._names(db) == {"g1": "admins"}
            # Membership and ACL principal still point at the id.
            cursor = await db.execute(
                "SELECT group_id FROM user_groups WHERE user_id = 'u1'"
            )
            assert (await cursor.fetchone())[0] == "g1"
            cursor = await db.execute(
                "SELECT group_id FROM acl_entries WHERE resource = '/admin'"
            )
            assert (await cursor.fetchone())[0] == "g1"
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_admin_group_is_noop(self, tmp_path):
        """Fresh (pre-seed), already-migrated, and custom-renamed
        deployments all lack an ``admin`` row — nothing to do."""
        from klangk.model.migrations import m0020_rename_admin_group

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g9', 'custom')"
            )
            await m0020_rename_admin_group.migration.apply(db)
            assert await self._names(db) == {"g9": "custom"}
        finally:
            await db.__aexit__(None, None, None)

    async def test_collision_fails_fast(self, tmp_path):
        """A manually created ``admins`` group blocks the rename; the
        error tells the operator how to resolve it."""
        from klangk.model.migrations import m0020_rename_admin_group

        db = await self._db(tmp_path)
        try:
            await self._seed_legacy_admin(db)
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g2', 'admins')"
            )
            with pytest.raises(RuntimeError, match="'admins'"):
                await m0020_rename_admin_group.migration.apply(db)
            # Nothing was renamed.
            assert await self._names(db) == {"g1": "admin", "g2": "admins"}
        finally:
            await db.__aexit__(None, None, None)

    async def test_already_renamed_is_noop(self, tmp_path):
        from klangk.model.migrations import m0020_rename_admin_group

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g1', 'admins')"
            )
            await m0020_rename_admin_group.migration.apply(db)
            assert await self._names(db) == {"g1": "admins"}
        finally:
            await db.__aexit__(None, None, None)


class TestM0021FirstClassResourceAcls:
    """m0021 seeds Allow manage-* (admins) + Deny everyone on each
    first-class resource (#2944) — without the rows, admins lock out of
    users/groups/invitations/server/events/acl the moment the checks
    move off the /admin wildcard."""

    RESOURCE_PERMS = {
        "/users": "manage-users",
        "/groups": "manage-groups",
        "/invitations": "manage-invitations",
        "/server": "manage-server-schedule",
        "/events": "manage-events",
        "/acl": "manage-acls",
    }

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0021.db"))
        db = await db.__aenter__()
        # Minimal shape the migration touches (mirrors the m0013 test's
        # harness): groups + acl_entries only.
        await db.execute(
            "CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT)"
        )
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _admins(self, db) -> str:
        await db.execute(
            "INSERT INTO groups (id, name) VALUES ('g-admins', 'admins')"
        )
        await db.commit()
        return "g-admins"

    async def _entries(self, db, resource) -> list:
        cursor = await db.execute(
            "SELECT position, action, permission, group_id,"
            " system_principal FROM acl_entries WHERE resource = ?"
            " ORDER BY position",
            (resource,),
        )
        return list(await cursor.fetchall())

    async def test_upgraded_db_gets_all_six_pairs(self, tmp_path):
        from klangk.model.migrations import m0021_first_class_resource_acls

        db = await self._db(tmp_path)
        try:
            gid = await self._admins(db)
            # An "upgraded" DB has some ACL rows already (the old seeds).
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission) VALUES ('/', 0, 1, 2, ?, 'view')",
                (gid,),
            )
            await db.commit()
            await m0021_first_class_resource_acls.migration.apply(db)

            for resource, permission in self.RESOURCE_PERMS.items():
                assert await self._entries(db, resource) == [
                    (0, 1, permission, gid, None),
                    (1, 0, "*", None, 0),
                ], resource
        finally:
            await db.__aexit__(None, None, None)

    async def test_empty_table_is_a_noop(self, tmp_path):
        """Fresh DBs are the boot seed's job — inserting here would trip
        the seed's empty-table gate and lose the / and /workspaces rows."""
        from klangk.model.migrations import m0021_first_class_resource_acls

        db = await self._db(tmp_path)
        try:
            await self._admins(db)
            await m0021_first_class_resource_acls.migration.apply(db)
            for resource in self.RESOURCE_PERMS:
                assert await self._entries(db, resource) == []
        finally:
            await db.__aexit__(None, None, None)

    async def test_pre_staged_resource_is_untouched(self, tmp_path):
        """An operator who pre-staged grants on one resource keeps them
        verbatim — the migration never half-merges onto unknown rows."""
        from klangk.model.migrations import m0021_first_class_resource_acls

        db = await self._db(tmp_path)
        try:
            gid = await self._admins(db)
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission) VALUES ('/users', 5, 1, 2, ?,"
                " 'manage-users')",
                (gid,),
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/groups', 0, 1, 0, 1, 'custom')"
            )
            await db.commit()
            await m0021_first_class_resource_acls.migration.apply(db)

            # /users: exactly the operator's row.
            assert await self._entries(db, "/users") == [
                (5, 1, "manage-users", gid, None)
            ]
            # /groups: skipped too (any rows present -> skip).
            assert await self._entries(db, "/groups") == [
                (0, 1, "custom", None, 1)
            ]
            # Untouched resources still get the pair.
            assert await self._entries(db, "/acl") == [
                (0, 1, "manage-acls", gid, None),
                (1, 0, "*", None, 0),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_admins_group_still_denies_everyone(self, tmp_path):
        """A deployment with no 'admins' group (custom topology) gets the
        Deny rows only — the Allow rows have no principal to name, and
        default-deny applies either way."""
        from klangk.model.migrations import m0021_first_class_resource_acls

        db = await self._db(tmp_path)
        try:
            # No groups row at all; some existing ACL rows so the
            # migration is not in fresh-skip territory.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/', 0, 1, 0, 1, 'view')"
            )
            await db.commit()
            await m0021_first_class_resource_acls.migration.apply(db)

            for resource in self.RESOURCE_PERMS:
                assert await self._entries(db, resource) == [
                    (1, 0, "*", None, 0)
                ], resource
        finally:
            await db.__aexit__(None, None, None)

    async def test_legacy_groups_seed_is_replaced(self, tmp_path):
        """The blocker the #2945 review proved: every pre-#2943
        deployment carries the m0014-rewritten Allow create row on
        /groups. The migration must replace it with the manage-groups
        pair — leaving it locks admins out of group management."""
        from klangk.model.migrations import m0021_first_class_resource_acls

        db = await self._db(tmp_path)
        try:
            gid = await self._admins(db)
            # The m0014 output shape: single Allow create for the group.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission) VALUES ('/groups', 0, 1, 2, ?, 'create')",
                (gid,),
            )
            await db.commit()
            await m0021_first_class_resource_acls.migration.apply(db)

            assert await self._entries(db, "/groups") == [
                (0, 1, "manage-groups", gid, None),
                (1, 0, "*", None, 0),
            ]
        finally:
            await db.__aexit__(None, None, None)


class TestLegacyShapesThroughProductionConnection:
    """The m0014/m0021 shape compares must tolerate the ``db.Row``
    wrapper (integer keys only — no slices) used on the production
    connection path, not just the raw-aiosqlite rows the per-migration
    harnesses use (#2980 review)."""

    async def _forget(self, app_state, migration_id: int) -> None:
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            await db.execute(
                "DELETE FROM schema_migrations WHERE id = ?", (migration_id,)
            )
            await db.commit()

    async def test_m0014_seeded_shape_reapplied_via_wrapper(
        self, temp_data_dir, app_state
    ):
        """m0014's seeded-shape check sees wrapper rows and consumes the
        pre-#2770 /groups seed (regression: row slicing crashed on the
        wrapper, boot-looping upgrades)."""
        await app_state.state.model.init_db()
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            await db.execute(
                "DELETE FROM acl_entries WHERE resource = '/groups'"
            )
            # The pre-m0014 seed: single system-authenticated Allow
            # create at position 0.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  group_id, system_principal, permission)"
                " VALUES ('/groups', 0, ?, ?, NULL, NULL, ?, 'create')",
                (ACTION_ALLOW, PRINCIPAL_SYSTEM, SYSTEM_AUTHENTICATED),
            )
            await db.commit()
        await self._forget(app_state, 14)

        # Through the production wrapper: the shape check must match,
        # and m0014 consumes the seeded row (no 'admin' group exists
        # post-m0020, so no replacement is inserted).
        await app_state.state.model.init_db()
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM acl_entries WHERE resource = '/groups'"
            )
            assert (await cursor.fetchone())[0] == 0

    async def test_m0021_legacy_groups_shape_replaced_via_wrapper(
        self, temp_data_dir, app_state
    ):
        """m0021's legacy-shape check sees wrapper rows and replaces the
        m0014 seed with the manage-groups pair (regression: row slicing
        crashed on the wrapper, boot-looping upgrades)."""
        await app_state.state.model.init_db()
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            cursor = await db.execute(
                "SELECT id FROM groups WHERE name = 'admins'"
            )
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    "INSERT INTO groups (id, name) VALUES ('g-admins',"
                    " 'admins')"
                )
                admins_id = "g-admins"
            else:
                admins_id = row[0]
            await db.execute(
                "DELETE FROM acl_entries WHERE resource = '/groups'"
            )
            # The m0014 output shape: single group-principal Allow
            # create at position 0.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  group_id, system_principal, permission)"
                " VALUES ('/groups', 0, ?, ?, NULL, ?, NULL, 'create')",
                (ACTION_ALLOW, PRINCIPAL_GROUP, admins_id),
            )
            await db.commit()
        await self._forget(app_state, 21)

        # Through the production wrapper: the legacy shape must be
        # recognized and replaced with the admins Allow + everyone Deny.
        await app_state.state.model.init_db()
        async with aiosqlite.connect(str(app_state.state.db.db_path)) as db:
            cursor = await db.execute(
                "SELECT position, action, principal_type, group_id,"
                " system_principal, permission FROM acl_entries"
                " WHERE resource = '/groups' ORDER BY position"
            )
            assert list(await cursor.fetchall()) == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    admins_id,
                    None,
                    "manage-groups",
                ),
                (1, ACTION_DENY, PRINCIPAL_SYSTEM, None, SYSTEM_EVERYONE, "*"),
            ]


class TestM0022WorkspacePermissionRenames:
    """m0022 renames the stored workspace-sphere ACEs to the specific
    #2946 names — without it, every existing workspace's grants
    (including the role groups) stop matching the new checks."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0022.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE groups (id TEXT PRIMARY KEY, name TEXT, source TEXT)"
        )
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _seed_legacy(self, db):
        """An old-shape deployment: collection create + a workspace
        carrying every renamed name plus the untouched ones, with
        coders/collaborators/spectators role groups in place."""
        for gid, name in (
            ("g-coders", "coders-ws-1"),
            ("g-collab", "collaborators-ws-1"),
            ("g-spec", "spectators-ws-1"),
        ):
            await db.execute(
                "INSERT INTO groups (id, name, source)"
                " VALUES (?, ?, 'workspace-role')",
                (gid, name),
            )
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, group_id,"
            "  permission) VALUES ('/workspaces', 0, 1, 2, 'g-a', 'create')"
        )
        # per-workspace row: one ACE per legacy permission
        renames = [
            "create",
            "edit",
            "delete",
            "monitor",
            "export",
            "share",
            "change-acls",
            "admin",
            "files",
            "view",
            "terminal",
            "files-download",
            "files-write",
        ]
        for i, perm in enumerate(renames):
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-1', ?, 1, 1, 'u-1', ?)",
                (i, perm),
            )
        # Role groups: coders/collaborators carry the old seeded grant
        # list shape (incl. terminal + monitor); spectators the short
        # one.
        role_rows = (
            [
                ("g-coders", 100 + i, p)
                for i, p in enumerate(
                    [
                        "view",
                        "monitor",
                        "terminal",
                        "egress-consent",
                        "code-in-isolation",
                        "exec-and-sync",
                        "spectate-on-shared-terminals",
                        "files",
                        "files-download",
                        "files-write",
                    ]
                )
            ]
            + [
                ("g-collab", 200 + i, p)
                for i, p in enumerate(
                    [
                        "view",
                        "monitor",
                        "terminal",
                        "files",
                        "files-download",
                        "files-write",
                    ]
                )
            ]
            + [
                ("g-spec", 300 + i, p)
                for i, p in enumerate(
                    [
                        "view",
                        "monitor",
                        "terminal",
                        "spectate-on-shared-terminals",
                    ]
                )
            ]
        )
        for gid, pos, perm in role_rows:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-1', ?, 1, 2, ?, ?)",
                (pos, gid, perm),
            )
        await db.commit()

    async def _perms(self, db, resource, user_only=False) -> list:
        sql = "SELECT permission FROM acl_entries WHERE resource = ?"
        if user_only:
            sql += " AND user_id = 'u-1'"
        cursor = await db.execute(sql + " ORDER BY position", (resource,))
        return [r[0] for r in await cursor.fetchall()]

    async def test_renames_by_scope(self, tmp_path):
        from klangk.model.migrations import m0022_workspace_permission_renames

        db = await self._db(tmp_path)
        try:
            await self._seed_legacy(db)
            await m0022_workspace_permission_renames.migration.apply(db)

            assert await self._perms(db, "/workspaces") == ["create-workspace"]
            assert await self._perms(
                db, "/workspaces/ws-1", user_only=True
            ) == [
                # renamed
                "duplicate-workspace",
                "edit-workspace",
                "delete-workspace",
                "monitor-workspace",
                "export-workspace",
                "share-workspace",
                "share-advanced",
                "transfer-workspace",
                "files-view",
                # untouched
                "view",
                "terminal",
                "files-download",
                "files-write",
            ]

            # Lifecycle trio granted to coders + collaborators role
            # groups only.
            async def group_perms(gid):
                cur = await db.execute(
                    "SELECT permission FROM acl_entries"
                    " WHERE resource = '/workspaces/ws-1' AND group_id = ?"
                    " ORDER BY position",
                    (gid,),
                )
                return [r[0] for r in await cur.fetchall()]

            trio = {"start-workspace", "stop-workspace", "restart-workspace"}
            for gid in ("g-coders", "g-collab"):
                assert trio <= set(await group_perms(gid)), gid
            assert not (trio & set(await group_perms("g-spec")))
            # Idempotent: a re-run inserts nothing.
            before = await group_perms("g-coders")
            await m0022_workspace_permission_renames.migration.apply(db)
            assert await group_perms("g-coders") == before
            perms = await self._perms(db, "/workspaces/ws-1", user_only=True)
            assert perms[:1] == ["duplicate-workspace"]
        finally:
            await db.__aexit__(None, None, None)

    async def test_unmapped_names_left_alone(self, tmp_path):
        """A legacy ``admin`` row on /workspaces itself has no mapping
        (the transfer gate is per-workspace) and must survive."""
        from klangk.model.migrations import m0022_workspace_permission_renames

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission) VALUES ('/workspaces', 0, 1, 2, 'g-a',"
                " 'admin')"
            )
            await db.commit()
            await m0022_workspace_permission_renames.migration.apply(db)
            assert await self._perms(db, "/workspaces") == ["admin"]
        finally:
            await db.__aexit__(None, None, None)


class TestM0023SelfServiceResources:
    """m0023 seeds the #2946 self-service pairs (Allow Authenticated +
    Deny Everyone on /volumes, /images, /llm-proxy) and the /users
    search-users row on existing deployments."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0023.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _rows(self, db, resource) -> list:
        cursor = await db.execute(
            "SELECT position, action, permission, system_principal"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return list(await cursor.fetchall())

    async def test_upgraded_db_gets_the_pairs(self, tmp_path):
        from klangk.model import ACTION_ALLOW, ACTION_DENY
        from klangk.model.migrations import m0023_self_service_resources

        db = await self._db(tmp_path)
        try:
            # Existing deployment: some rows elsewhere (skips the
            # fresh-DB gate) plus a /users pair from #2944 seeds.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/users', 0, 1, 2, 'g-a', 'manage-users')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/users', 1, 0, 3, 0, '*')"
            )
            await db.commit()
            await m0023_self_service_resources.migration.apply(db)

            for resource, permission in (
                ("/volumes", "manage-volumes"),
                ("/images", "view-images"),
            ):
                assert await self._rows(db, resource) == [
                    (0, ACTION_ALLOW, permission, 1),
                    (1, ACTION_DENY, "*", 0),
                ]
            # /users: search-users inserted at 1, Deny shifted to 2.
            assert await self._rows(db, "/users") == [
                (0, ACTION_ALLOW, "manage-users", None),
                (1, ACTION_ALLOW, "search-users", 1),
                (2, ACTION_DENY, "*", 0),
            ]
            # Idempotent: re-run changes nothing.
            await m0023_self_service_resources.migration.apply(db)
            assert await self._rows(db, "/users") == [
                (0, ACTION_ALLOW, "manage-users", None),
                (1, ACTION_ALLOW, "search-users", 1),
                (2, ACTION_DENY, "*", 0),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_three_consecutive_users_rows(self, tmp_path):
        """Regression (#2956 review): a single ``position + 1`` UPDATE
        violates UNIQUE(resource, position) once /users holds a run of
        >= 2 consecutive positions — the admin ACL browser's output
        shape after an operator adds one custom rule. The two-step
        offset shift must handle it."""
        from klangk.model import ACTION_ALLOW, ACTION_DENY
        from klangk.model.migrations import m0023_self_service_resources

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/users', 0, 1, 2, 'g-a', 'manage-users')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  permission)"
                " VALUES ('/users', 1, 1, 1, 'u-9', 'manage-users')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/users', 2, 0, 3, 0, '*')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/', 0, 1, 3, 1, 'view')"
            )
            await db.commit()
            await m0023_self_service_resources.migration.apply(db)
            assert await self._rows(db, "/users") == [
                (0, ACTION_ALLOW, "manage-users", None),
                (1, ACTION_ALLOW, "search-users", 1),
                (2, ACTION_ALLOW, "manage-users", None),
                (3, ACTION_DENY, "*", 0),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        """An empty acl_entries table belongs to the boot seeds."""
        from klangk.model.migrations import m0023_self_service_resources

        db = await self._db(tmp_path)
        try:
            await m0023_self_service_resources.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_operator_staged_resource_skipped(self, tmp_path):
        """A resource someone pre-populated is left untouched."""
        from klangk.model.migrations import m0023_self_service_resources

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/volumes', 0, 1, 2, 'g-a', 'custom'),"
                "        ('/', 0, 1, 3, NULL, 'view')"
            )
            await db.commit()
            await m0023_self_service_resources.migration.apply(db)
            assert await self._rows(db, "/volumes") == [(0, 1, "custom", None)]
        finally:
            await db.__aexit__(None, None, None)


class TestM0024JoinWorkspacePermission:
    """m0024 grants ``join-workspace`` alongside every stored ``terminal``
    row (#2975: the connect gate moves off ``terminal``, which becomes the
    Terminal-tab visibility signal). The copy must preserve the ACL's
    first-match-wins answers — Allow AND Deny, on any resource the
    ancestor walk consults — not just replicate the Allow grants."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0024.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _rows(self, db, resource, principal_sql=""):
        cursor = await db.execute(
            "SELECT permission, action FROM acl_entries"
            f" WHERE resource = ?{principal_sql} ORDER BY position",
            (resource,),
        )
        return [
            f"{r[0]}:{'allow' if r[1] == 1 else 'deny'}"
            for r in await cursor.fetchall()
        ]

    async def test_copies_terminal_aces_in_place(self, tmp_path):
        """User and group principals holding Allow ``terminal`` each gain
        a ``join-workspace`` sibling directly AFTER the source row (later
        rows shift up); the terminal rows stay (copy, not rename)."""
        from klangk.model.migrations import m0024_join_workspace_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-1', 0, 1, 1, 'u-1', 'view'),"
                "        ('/workspaces/ws-1', 1, 1, 1, 'u-1', 'terminal'),"
                "        ('/workspaces/ws-1', 3, 1, 1, 'u-9', 'other')"
            )
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-1', 2, 1, 2, 'g-spec', 'terminal')"
            )
            await db.commit()
            await m0024_join_workspace_permission.migration.apply(db)

            # Sibling lands right after its source; rows above shift up
            # (the 'other' row moved from 3 to 4 for the u-1 sibling,
            # then to 5 for the g-spec sibling).
            assert await self._rows(
                db, "/workspaces/ws-1", " AND user_id = 'u-1'"
            ) == ["view:allow", "terminal:allow", "join-workspace:allow"]
            assert await self._rows(
                db, "/workspaces/ws-1", " AND user_id = 'u-9'"
            ) == ["other:allow"]
            assert await self._rows(
                db, "/workspaces/ws-1", " AND group_id = 'g-spec'"
            ) == ["terminal:allow", "join-workspace:allow"]
            cursor = await db.execute(
                "SELECT position FROM acl_entries"
                " WHERE resource = '/workspaces/ws-1' AND user_id = 'u-9'"
            )
            assert (await cursor.fetchone())[0] == 5
            # Idempotent: a re-run inserts nothing.
            await m0024_join_workspace_permission.migration.apply(db)
            assert await self._rows(
                db, "/workspaces/ws-1", " AND user_id = 'u-1'"
            ) == ["view:allow", "terminal:allow", "join-workspace:allow"]
        finally:
            await db.__aexit__(None, None, None)

    async def test_deny_rows_copied_answer_preserved(self, tmp_path):
        """A Deny ``terminal`` above an Allow used to block the connect
        gate (first-match-wins); the copied Deny sibling must sit before
        the copied Allow sibling so the join-workspace query answers the
        same way the terminal query did (#2891-class review finding)."""
        from klangk.acl import check_permission_inmemory
        from klangk.model.migrations import m0024_join_workspace_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-2', 0, 0, 1, 'u-x', 'terminal'),"
                "        ('/workspaces/ws-2', 1, 1, 1, 'u-x', 'terminal')"
            )
            await db.commit()

            async def entries_by_resource():
                cur = await db.execute(
                    "SELECT resource, position, action, principal_type,"
                    " user_id, group_id, system_principal, permission"
                    " FROM acl_entries ORDER BY resource, position"
                )
                out = {}
                for row in await cur.fetchall():
                    out.setdefault(row[0], []).append(
                        {
                            "position": row[1],
                            "action": row[2],
                            "principal_type": row[3],
                            "user_id": row[4],
                            "group_id": row[5],
                            "system_principal": row[6],
                            "permission": row[7],
                        }
                    )
                return out

            principals = {
                "user_id": "u-x",
                "group_ids": [],
                "authenticated": True,
            }
            entries = await entries_by_resource()
            assert not check_permission_inmemory(
                "/workspaces/ws-2", principals, "terminal", entries
            )
            await m0024_join_workspace_permission.migration.apply(db)
            entries = await entries_by_resource()
            assert not check_permission_inmemory(
                "/workspaces/ws-2", principals, "join-workspace", entries
            ), "Deny-above-Allow order must be preserved by the copy"
            # Row order: deny-term, deny-join, allow-term, allow-join.
            assert await self._rows(
                db, "/workspaces/ws-2", " AND user_id = 'u-x'"
            ) == [
                "terminal:deny",
                "join-workspace:deny",
                "terminal:allow",
                "join-workspace:allow",
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_ancestor_resource_terminal_gets_sibling(self, tmp_path):
        """An Allow ``terminal`` on the collection ``/workspaces``
        answered the old gate through the ancestor walk; the copy must
        not be scoped to the per-workspace GLOB or those deployments
        lock out on upgrade."""
        from klangk.model.migrations import m0024_join_workspace_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/workspaces', 0, 1, 0, 1, 'terminal'),"
                "        ('/', 1, 1, 0, 1, 'terminal')"
            )
            await db.commit()
            await m0024_join_workspace_permission.migration.apply(db)
            assert await self._rows(db, "/workspaces") == [
                "terminal:allow",
                "join-workspace:allow",
            ]
            assert await self._rows(db, "/") == [
                "terminal:allow",
                "join-workspace:allow",
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_existing_join_rows_left_alone(self, tmp_path):
        """A principal who already holds ``join-workspace`` on the
        resource (a pre-migrated or hand-built shape) gets no second
        sibling, and their row order is otherwise untouched."""
        from klangk.model.migrations import m0024_join_workspace_permission

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, user_id,"
                "  permission)"
                " VALUES ('/workspaces/ws-3', 0, 1, 1, 'u-done',"
                "         'join-workspace'),"
                "        ('/workspaces/ws-3', 1, 1, 1, 'u-done', 'terminal')"
            )
            await db.commit()
            await m0024_join_workspace_permission.migration.apply(db)
            assert await self._rows(
                db, "/workspaces/ws-3", " AND user_id = 'u-done'"
            ) == ["join-workspace:allow", "terminal:allow"]
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        """An empty acl_entries table belongs to the boot seeds, which
        already grant join-workspace alongside terminal."""
        from klangk.model.migrations import m0024_join_workspace_permission

        db = await self._db(tmp_path)
        try:
            await m0024_join_workspace_permission.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)


class TestM0025DropDeadImagesDenyRow:
    """m0025 deletes the retired seed-shape Deny Everyone ``*`` row on
    /images (#2994). The row gates no route (no /images route checks a
    permission other than view-images; unauthenticated requests die at
    the JWT middleware) — but it did mask the root / Allow view
    inheritance in /my-permissions, which dropping it exposes."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0025.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _rows(self, db, resource) -> list:
        cursor = await db.execute(
            "SELECT position, action, permission, system_principal"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return list(await cursor.fetchall())

    async def test_drops_only_the_dead_deny(self, tmp_path):
        from klangk.model import ACTION_ALLOW
        from klangk.model.migrations import m0025_drop_dead_images_deny_row

        db = await self._db(tmp_path)
        try:
            # The #2946 seed shape + an operator row at position 2.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type,"
                "  system_principal, permission)"
                " VALUES ('/images', 0, 1, 0, 1, 'view-images'),"
                "        ('/images', 1, 0, 0, 0, '*'),"
                "        ('/images', 2, 1, 2, NULL, 'custom')"
            )
            await db.commit()
            await m0025_drop_dead_images_deny_row.migration.apply(db)
            assert await self._rows(db, "/images") == [
                (0, ACTION_ALLOW, "view-images", 1),
                (2, ACTION_ALLOW, "custom", None),
            ]
            # Idempotent: re-run changes nothing.
            await m0025_drop_dead_images_deny_row.migration.apply(db)
            assert await self._rows(db, "/images") == [
                (0, ACTION_ALLOW, "view-images", 1),
                (2, ACTION_ALLOW, "custom", None),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_custom_deny_shapes_untouched(self, tmp_path):
        """A Deny row that is not the seed's Everyone ``*`` at position 1
        (e.g. a group-scoped deny, a ``view-images`` deny, or the same
        ``*`` deny moved to another position) is operator intent and
        stays."""
        from klangk.model import ACTION_DENY
        from klangk.model.migrations import m0025_drop_dead_images_deny_row

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  system_principal, permission)"
                " VALUES ('/images', 0, 0, 2, 'g-a', NULL, '*'),"
                "        ('/images', 1, 0, 0, NULL, 0, 'view-images'),"
                "        ('/images', 2, 0, 0, NULL, 0, '*')"
            )
            await db.commit()
            await m0025_drop_dead_images_deny_row.migration.apply(db)
            assert await self._rows(db, "/images") == [
                (0, ACTION_DENY, "*", None),
                (1, ACTION_DENY, "view-images", 0),
                (2, ACTION_DENY, "*", 0),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        """An empty acl_entries table belongs to the boot seeds."""
        from klangk.model.migrations import m0025_drop_dead_images_deny_row

        db = await self._db(tmp_path)
        try:
            await m0025_drop_dead_images_deny_row.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)


class TestM0026VolumesAdminSurface:
    """m0026 rewrites /volumes from the #2946 self-service pair (Allow
    manage-volumes Authenticated + Deny * Everyone) to the #2993 admin
    pair (Allow view-volumes + Allow manage-volumes for the admins
    group at positions 0/1)."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0026.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        await db.execute(
            "CREATE TABLE groups ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE,"
            " description TEXT, source TEXT, created_at TEXT)"
        )
        return db

    async def _seed_old_pair(self, db) -> None:
        """The #2946 rows a deployed database holds on /volumes."""
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type,"
            "  system_principal, permission)"
            " VALUES ('/volumes', 0, 1, 0, 1, 'manage-volumes'),"
            "        ('/volumes', 1, 0, 0, 0, '*'),"
            "        ('/', 0, 1, 0, 1, 'view')"
        )

    async def _rows(self, db, resource) -> list:
        cursor = await db.execute(
            "SELECT position, action, principal_type, group_id,"
            " system_principal, permission"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return list(await cursor.fetchall())

    async def test_upgraded_db_gets_the_admin_pair(self, tmp_path):
        from klangk.model import ACTION_ALLOW, PRINCIPAL_GROUP
        from klangk.model.migrations import m0026_volumes_admin_surface

        db = await self._db(tmp_path)
        try:
            await self._seed_old_pair(db)
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-a', 'admins')"
            )
            await db.commit()
            await m0026_volumes_admin_surface.migration.apply(db)

            assert await self._rows(db, "/volumes") == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "view-volumes",
                ),
                (
                    1,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "manage-volumes",
                ),
            ]
            # Idempotent: re-run changes nothing.
            await m0026_volumes_admin_surface.migration.apply(db)
            assert len(await self._rows(db, "/volumes")) == 2
        finally:
            await db.__aexit__(None, None, None)

    async def test_operator_rows_survive_below_the_admin_rows(self, tmp_path):
        """A scoped self-service grant an operator staged on top of the
        #2946 pair survives, shifted below the inserted admin rows (the
        old Allow-Authenticated row gave admins access; the rewrite
        must not let a later Deny shadow their replacement rows)."""
        from klangk.model import ACTION_ALLOW, PRINCIPAL_GROUP
        from klangk.model.migrations import m0026_volumes_admin_surface

        db = await self._db(tmp_path)
        try:
            await self._seed_old_pair(db)
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/volumes', 2, 1, 2, 'g-team', 'manage-volumes')"
            )
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-a', 'admins')"
            )
            await db.commit()
            await m0026_volumes_admin_surface.migration.apply(db)

            assert await self._rows(db, "/volumes") == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "view-volumes",
                ),
                (
                    1,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "manage-volumes",
                ),
                (
                    4,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-team",
                    None,
                    "manage-volumes",
                ),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_operator_row_at_position_zero_settles_below(self, tmp_path):
        """Regression (review B1): an operator who replaced the seed with
        their own row at position 0 must see it settle to position 2 —
        a strict > in the settle step stranded the row parked at
        exactly the offset, permanently."""
        from klangk.model import ACTION_ALLOW, PRINCIPAL_GROUP
        from klangk.model.migrations import m0026_volumes_admin_surface

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/volumes', 0, 1, 2, 'g-op', 'manage-volumes'),"
                "        ('/', 0, 1, 0, NULL, 'view')"
            )
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-a', 'admins')"
            )
            await db.commit()
            await m0026_volumes_admin_surface.migration.apply(db)

            assert await self._rows(db, "/volumes") == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "view-volumes",
                ),
                (
                    1,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "manage-volumes",
                ),
                (
                    2,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-op",
                    None,
                    "manage-volumes",
                ),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        from klangk.model.migrations import m0026_volumes_admin_surface

        db = await self._db(tmp_path)
        try:
            await m0026_volumes_admin_surface.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_admins_group_still_drops_the_old_rows(self, tmp_path):
        """Without an admins group there is no grantee — the #2946 rows
        still go (the m0021 posture), and nothing is inserted."""
        from klangk.model.migrations import m0026_volumes_admin_surface

        db = await self._db(tmp_path)
        try:
            await self._seed_old_pair(db)
            await db.commit()
            await m0026_volumes_admin_surface.migration.apply(db)
            assert await self._rows(db, "/volumes") == []
        finally:
            await db.__aexit__(None, None, None)


class TestM0027RetireAdminMarker:
    """m0027 deletes every stored ``/admin`` row (#2995): instance-admin
    status derives from admins-group membership, and no check consults
    the marker tree, so the rows are inert."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0027.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        return db

    async def _count(self, db, resource):
        cursor = await db.execute(
            "SELECT COUNT(*) FROM acl_entries WHERE resource = ?",
            (resource,),
        )
        return (await cursor.fetchone())[0]

    async def test_deletes_admin_subtree_keeps_others(self, tmp_path):
        """The seeded marker pair, the pre-#2944 ``/admin/*`` delegation
        rows, and any operator-staged /admin rows all go; every other
        resource's rows are untouched."""
        from klangk.model.migrations import m0027_retire_admin_marker

        db = await self._db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/admin', 0, 1, 2, 'g-admin', '*'),"
                "        ('/admin', 1, 0, 0, NULL, '*'),"
                "        ('/admin/users', 0, 1, 2, 'g-admin', '*'),"
                "        ('/users', 0, 1, 2, 'g-admin', 'manage-users')"
            )
            await db.commit()
            await m0027_retire_admin_marker.migration.apply(db)

            assert await self._count(db, "/admin") == 0
            assert await self._count(db, "/admin/users") == 0
            assert await self._count(db, "/users") == 1
            # Idempotent: a re-run deletes nothing new and raises not.
            await m0027_retire_admin_marker.migration.apply(db)
            assert await self._count(db, "/users") == 1
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        """An empty acl_entries table (fresh install) is untouched — the
        boot seed owns it and no longer plants /admin rows."""
        from klangk.model.migrations import m0027_retire_admin_marker

        db = await self._db(tmp_path)
        try:
            await m0027_retire_admin_marker.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)


class TestM0028InvitationsPendingUnique:
    """m0028 collapses duplicate pending invitations (the pre-#3101 race
    residue) and backfills the one-pending-per-email partial index."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0028.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE invitations ("
            " id TEXT PRIMARY KEY, email TEXT NOT NULL,"
            " invited_by TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " accepted_at TEXT)"
        )
        return db

    async def _insert(
        self, db, id, email, status, created_at, invited_by="inviter"
    ):
        await db.execute(
            "INSERT INTO invitations (id, email, invited_by, status,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (id, email, invited_by, status, created_at),
        )

    async def _statuses(self, db, email) -> list[tuple]:
        cursor = await db.execute(
            "SELECT id, status FROM invitations WHERE email = ?"
            " ORDER BY created_at, id",
            (email,),
        )
        return list(await cursor.fetchall())

    async def test_dedupes_and_creates_index(self, tmp_path):
        from klangk.model.migrations import m0028_invitations_pending_unique

        db = await self._db(tmp_path)
        try:
            # Race residue: two pendings for one email (oldest first).
            await self._insert(db, "old", "a@b.com", "pending", "2026-01-01")
            await self._insert(db, "new", "a@b.com", "pending", "2026-01-02")
            # History for the same email must survive untouched.
            await self._insert(db, "acc", "a@b.com", "accepted", "2025-12-01")
            await self._insert(db, "rev", "a@b.com", "revoked", "2025-12-02")
            # Another email's pending is unaffected.
            await self._insert(db, "b1", "b@b.com", "pending", "2026-01-03")
            await db.commit()

            await m0028_invitations_pending_unique.migration.apply(db)

            assert await self._statuses(db, "a@b.com") == [
                ("acc", "accepted"),
                ("rev", "revoked"),
                ("old", "pending"),
                ("new", "revoked"),
            ]
            assert await self._statuses(db, "b@b.com") == [("b1", "pending")]

            # The index now forbids a second pending for one email.
            with pytest.raises(Exception) as exc_info:
                await self._insert(db, "again", "a@b.com", "pending", "x")
            assert "UNIQUE" in str(exc_info.value)

            # Idempotent: a re-run changes nothing and raises not.
            await m0028_invitations_pending_unique.migration.apply(db)
            assert await self._statuses(db, "a@b.com") == [
                ("acc", "accepted"),
                ("rev", "revoked"),
                ("old", "pending"),
                ("new", "revoked"),
            ]
        finally:
            await db.__aexit__(None, None, None)

    async def test_no_duplicates_is_noop(self, tmp_path):
        from klangk.model.migrations import m0028_invitations_pending_unique

        db = await self._db(tmp_path)
        try:
            await self._insert(db, "solo", "c@b.com", "pending", "2026-01-01")
            await db.commit()
            await m0028_invitations_pending_unique.migration.apply(db)
            assert await self._statuses(db, "c@b.com") == [("solo", "pending")]
            # An empty pending set is fine too.
            await db.execute(
                "UPDATE invitations SET status = 'accepted' WHERE id = 'solo'"
            )
            await db.commit()
            await m0028_invitations_pending_unique.migration.apply(db)
            assert await self._statuses(db, "c@b.com") == [
                ("solo", "accepted")
            ]
        finally:
            await db.__aexit__(None, None, None)


class TestM0029MembersCreateWorkspace:
    """m0029 appends the #3137 members create-workspace grant on
    existing deployments (Allow group:members create-workspace at the
    end of /workspaces), creating the group when missing."""

    async def _db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0029.db"))
        db = await db.__aenter__()
        await db.execute(
            "CREATE TABLE acl_entries ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " resource TEXT, position INTEGER, action INTEGER,"
            " principal_type INTEGER, user_id TEXT, group_id TEXT,"
            " system_principal INTEGER, permission TEXT,"
            " UNIQUE(resource, position))"
        )
        await db.execute(
            "CREATE TABLE groups ("
            " id TEXT PRIMARY KEY, name TEXT UNIQUE,"
            " description TEXT, source TEXT, created_at TEXT)"
        )
        return db

    async def _rows(self, db, resource) -> list[tuple]:
        cursor = await db.execute(
            "SELECT position, action, principal_type, group_id,"
            " system_principal, permission"
            " FROM acl_entries WHERE resource = ? ORDER BY position",
            (resource,),
        )
        return list(await cursor.fetchall())

    async def _seed_stock(self, db, admins_id="g-a", members_id="g-m"):
        """The stock #2569 shape: Allow create-workspace admins @0 on
        /workspaces plus the root pair. ``members_id=None`` skips the
        members group row (pre-#2569 database)."""
        await db.execute(
            "INSERT INTO acl_entries"
            " (resource, position, action, principal_type, group_id,"
            "  permission)"
            " VALUES ('/workspaces', 0, 1, 2, ?, 'create-workspace'),"
            "        ('/', 0, 1, 0, NULL, 'view'),"
            "        ('/', 1, 0, 0, NULL, '*')",
            (admins_id,),
        )
        await db.execute(
            "INSERT INTO groups (id, name) VALUES (?, 'admins')",
            (admins_id,),
        )
        if members_id is not None:
            await db.execute(
                "INSERT INTO groups (id, name) VALUES (?, 'members')",
                (members_id,),
            )

    async def test_upgraded_db_gets_the_members_grant(self, tmp_path):
        from klangk.model.migrations import m0029_members_create_workspace

        db = await self._db(tmp_path)
        try:
            await self._seed_stock(db)
            await db.commit()
            await m0029_members_create_workspace.migration.apply(db)

            # The grant lands at position 1 — the fresh-seed layout.
            assert await self._rows(db, "/workspaces") == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "create-workspace",
                ),
                (
                    1,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-m",
                    None,
                    "create-workspace",
                ),
            ]
            # Idempotent: a re-run inserts nothing.
            await m0029_members_create_workspace.migration.apply(db)
            assert len(await self._rows(db, "/workspaces")) == 2
        finally:
            await db.__aexit__(None, None, None)

    async def test_fresh_db_is_noop(self, tmp_path):
        """An empty acl_entries table belongs to the boot seeds."""
        from klangk.model.migrations import m0029_members_create_workspace

        db = await self._db(tmp_path)
        try:
            await m0029_members_create_workspace.migration.apply(db)
            cursor = await db.execute("SELECT COUNT(*) FROM acl_entries")
            assert (await cursor.fetchone())[0] == 0
            cursor = await db.execute("SELECT COUNT(*) FROM groups")
            assert (await cursor.fetchone())[0] == 0
        finally:
            await db.__aexit__(None, None, None)

    async def test_creates_missing_members_group(self, tmp_path):
        """A pre-#2569 database with no members row gets the group."""
        from klangk.model.migrations import m0029_members_create_workspace

        db = await self._db(tmp_path)
        try:
            await self._seed_stock(db, members_id=None)
            await db.commit()
            await m0029_members_create_workspace.migration.apply(db)

            cursor = await db.execute(
                "SELECT description FROM groups WHERE name = 'members'"
            )
            assert await cursor.fetchone() == ("All regular users",)
            rows = await self._rows(db, "/workspaces")
            assert len(rows) == 2
            assert rows[1][5] == "create-workspace"
            # The new row points at the group that was created.
            cursor = await db.execute(
                "SELECT 1 FROM acl_entries e JOIN groups g"
                " ON e.group_id = g.id WHERE g.name = 'members'"
                " AND e.permission = 'create-workspace'"
            )
            assert await cursor.fetchone() is not None
        finally:
            await db.__aexit__(None, None, None)

    async def test_operator_grant_not_duplicated(self, tmp_path):
        """An operator who already granted members create-workspace
        (or a re-run after a partial apply) inserts nothing."""
        from klangk.model.migrations import m0029_members_create_workspace

        db = await self._db(tmp_path)
        try:
            await self._seed_stock(db)
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/workspaces', 1, 1, 2, 'g-m',"
                "         'create-workspace')"
            )
            await db.commit()
            await m0029_members_create_workspace.migration.apply(db)
            assert len(await self._rows(db, "/workspaces")) == 2
        finally:
            await db.__aexit__(None, None, None)

    async def test_operator_deny_stays_ahead(self, tmp_path):
        """A staged Deny keeps first-match-wins priority: the grant
        appends after it, never between it and the admins row."""
        from klangk.model.migrations import m0029_members_create_workspace

        db = await self._db(tmp_path)
        try:
            await self._seed_stock(db)
            # The documented old-posture recipe: Deny create-workspace
            # for members (action 0), plus a scoped Allow for another
            # group at position 2.
            await db.execute(
                "INSERT INTO acl_entries"
                " (resource, position, action, principal_type, group_id,"
                "  permission)"
                " VALUES ('/workspaces', 1, 0, 2, 'g-m',"
                "         'create-workspace'),"
                "        ('/workspaces', 2, 1, 2, 'g-x',"
                "         'create-workspace')",
            )
            await db.execute(
                "INSERT INTO groups (id, name) VALUES ('g-x', 'devs')"
            )
            await db.commit()
            await m0029_members_create_workspace.migration.apply(db)

            assert await self._rows(db, "/workspaces") == [
                (
                    0,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-a",
                    None,
                    "create-workspace",
                ),
                (
                    1,
                    ACTION_DENY,
                    PRINCIPAL_GROUP,
                    "g-m",
                    None,
                    "create-workspace",
                ),
                (
                    2,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-x",
                    None,
                    "create-workspace",
                ),
                (
                    3,
                    ACTION_ALLOW,
                    PRINCIPAL_GROUP,
                    "g-m",
                    None,
                    "create-workspace",
                ),
            ]
        finally:
            await db.__aexit__(None, None, None)


class TestM0033UserSessionsLastSeen:
    """m0033 adds the per-session last_seen_at column (#3151) and
    backfills it from created_at so arming the feature judges existing
    rows by age-since-issuance instead of hitting a NULL."""

    async def _old_shape_db(self, tmp_path):
        """A pre-#3151 user_sessions table (no last_seen_at column)."""
        db = aiosqlite.connect(str(tmp_path / "m0033.db"))
        await db.__aenter__()
        await db.execute("""
            CREATE TABLE user_sessions (
                jti TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "INSERT INTO user_sessions (jti, user_id, created_at,"
            " expires_at) VALUES ('jti-a', 'u1', '2026-01-01 10:00:00',"
            " '2099-01-01T00:00:00+00:00')"
        )
        await db.commit()
        return db

    async def test_adds_column_and_backfills(self, tmp_path):
        db = await self._old_shape_db(tmp_path)
        try:
            from klangk.model.migrations import m0033_user_sessions_last_seen

            await m0033_user_sessions_last_seen.migration.apply(db)
            info = await db.execute("PRAGMA table_info(user_sessions)")
            cols = {r[1] for r in await info.fetchall()}
            assert "last_seen_at" in cols
            cursor = await db.execute(
                "SELECT last_seen_at FROM user_sessions WHERE jti = 'jti-a'"
            )
            # Backfilled from created_at (space-separated SQLite form —
            # parseable by datetime.fromisoformat, like the ISO form).
            assert await cursor.fetchone() == ("2026-01-01 10:00:00",)
        finally:
            await db.__aexit__(None, None, None)
