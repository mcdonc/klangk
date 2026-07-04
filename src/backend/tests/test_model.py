"""Tests for model: users, workspaces, messages, port allocations."""

import asyncio
import uuid

import aiosqlite
import pytest
import sqlalchemy.exc

from klangk_backend import model


class TestMigration:
    async def test_migrate_old_schema(self, temp_data_dir):
        """Migrates a pre-OIDC database: password_hash NOT NULL, no
        provider/external_id columns."""
        db = await aiosqlite.connect(str(model.db.DB_PATH))
        model.db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            await db.execute("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified)"
                " VALUES ('u1', 'old@example.com', 'hash', 1)"
            )
            await db.commit()
        finally:
            await db.close()

        await model.init_db()

        # Old user survived migration
        user = await model.get_user_by_email("old@example.com")
        assert user is not None
        assert user["password_hash"] == "hash"
        assert user["provider"] == "local"
        assert user["external_id"] is None

        # Can create OIDC user (NULL password_hash)
        oidc_user = await model.create_user(
            "new@example.com",
            password_hash=None,
            verified=True,
            provider="kc",
            external_id="sub-1",
        )
        assert oidc_user["id"]

    async def test_migrate_workspaces_adds_auto_start(self, temp_data_dir):
        """Migrates a workspaces table missing the auto_start column."""
        db = await aiosqlite.connect(str(model.db.DB_PATH))
        model.db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
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
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified)"
                " VALUES ('u1', 'owner@example.com', 'hash', 1)"
            )
            # Workspaces table WITHOUT auto_start column
            await db.execute("""
                CREATE TABLE workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    container_id TEXT,
                    num_ports INTEGER NOT NULL DEFAULT 5,
                    image TEXT,
                    service_command TEXT,
                    mounts TEXT,
                    env TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(user_id, name)
                )
            """)
            await db.execute(
                "INSERT INTO workspaces (id, user_id, name)"
                " VALUES ('ws1', 'u1', 'old-ws')"
            )
            await db.commit()
        finally:
            await db.close()

        await model.init_db()

        # Verify column was added and old data survived
        async with model.transaction() as db:
            cursor = await db.execute("PRAGMA table_info(workspaces)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "auto_start" in cols

            cursor = await db.execute(
                "SELECT auto_start FROM workspaces WHERE id = 'ws1'"
            )
            row = await cursor.fetchone()
            assert row[0] == 0  # default value

    async def test_migrate_workspaces_renames_default_command(
        self, temp_data_dir
    ):
        """init_db renames the legacy default_command column to service_command
        (#1203), preserving existing data."""
        db = await aiosqlite.connect(str(model.db.DB_PATH))
        model.db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
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
            await db.execute(
                "INSERT INTO users (id, email, password_hash, verified)"
                " VALUES ('u1', 'owner@example.com', 'hash', 1)"
            )
            # Legacy workspaces table using the old default_command column.
            await db.execute("""
                CREATE TABLE workspaces (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    container_id TEXT,
                    num_ports INTEGER NOT NULL DEFAULT 5,
                    image TEXT,
                    default_command TEXT,
                    auto_start INTEGER NOT NULL DEFAULT 0,
                    setup_state TEXT NOT NULL DEFAULT 'complete',
                    health_check TEXT,
                    mounts TEXT,
                    env TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(user_id, name)
                )
            """)
            await db.execute(
                "INSERT INTO workspaces (id, user_id, name, default_command)"
                " VALUES ('ws1', 'u1', 'old-ws', 'echo hello')"
            )
            await db.commit()
        finally:
            await db.close()

        await model.init_db()

        # Column was renamed (old gone, new present) and data survived.
        async with model.transaction() as db:
            cursor = await db.execute("PRAGMA table_info(workspaces)")
            cols = {row[1] for row in await cursor.fetchall()}
            assert "service_command" in cols
            assert "default_command" not in cols

            cursor = await db.execute(
                "SELECT service_command FROM workspaces WHERE id = 'ws1'"
            )
            row = await cursor.fetchone()
            assert row[0] == "echo hello"


class TestUsers:
    async def test_create_user(self, db):
        user = await model.create_user("alice@example.com", "hash123")
        assert user["email"] == "alice@example.com"
        assert "id" in user
        assert user["handle"] == "alice"

    async def test_get_user_by_email(self, user):
        found = await model.get_user_by_email("testuser@example.com")
        assert found is not None
        assert found["id"] == user["id"]

    async def test_get_user_by_email_not_found(self, db):
        found = await model.get_user_by_email("nonexistent")
        assert found is None

    async def test_get_user_by_id(self, user):
        found = await model.get_user_by_id(user["id"])
        assert found is not None
        assert found["email"] == "testuser@example.com"

    async def test_get_user_by_id_not_found(self, db):
        found = await model.get_user_by_id("fake-id")
        assert found is None


class TestHandles:
    async def test_create_user_assigns_handle(self, db):
        user = await model.create_user("alice@example.com", "hash")
        assert user["handle"] == "alice"

    async def test_create_user_handle_conflict_appends_suffix(self, db):
        await model.create_user("alice@example.com", "hash")
        user2 = await model.create_user("alice@other.com", "hash")
        assert user2["handle"] == "alice-2"

    async def test_create_user_handle_from_special_email(self, db):
        user = await model.create_user("Alice+Dev@foo.com", "hash")
        assert user["handle"] == "alicedev"

    async def test_create_user_empty_local_part(self, db):
        user = await model.create_user("@foo.com", "hash")
        assert user["handle"] == "user"

    async def test_create_user_long_email(self, db):
        user = await model.create_user("a" * 100 + "@foo.com", "hash")
        assert len(user["handle"]) <= model.MAX_HANDLE_LEN

    async def test_get_user_handle(self, user):
        handle = await model.get_user_handle(user["id"])
        assert handle == user["handle"]

    async def test_get_user_handle_not_found(self, db):
        handle = await model.get_user_handle("fake-id")
        assert handle is None

    async def test_set_user_handle(self, user):
        await model.set_user_handle(user["id"], "newname")
        handle = await model.get_user_handle(user["id"])
        assert handle == "newname"

    async def test_set_user_handle_conflict(self, db):
        u1 = await model.create_user("a@a.com", "hash")
        await model.create_user("b@b.com", "hash")
        with pytest.raises(ValueError, match="already taken"):
            await model.set_user_handle(u1["id"], "b")

    async def test_set_user_handle_rejects_agent_handle(self, db):
        # A human must not be able to take the live agent's handle (#1160)
        # — independent of DB seeding, not just DB-uniqueness coincidence.
        import klangk_backend.model as us

        us.clear_agent_cache()
        user = await us.create_user("someone@example.com", "hash")
        with pytest.raises(
            ValueError, match="reserved for the workspace agent"
        ):
            await us.set_user_handle(user["id"], await us.agent_handle())
        # The rejection is the agent handle specifically (clanker), not a
        # generic conflict — a different handle still works.
        await us.set_user_handle(user["id"], "someone-else")

    async def test_set_user_handle_rejects_agent_handle_unseeded(self, db):
        # The fallback agent handle (clanker) is rejected even when the
        # agent row has NOT been seeded — the gap DB-uniqueness leaves.
        import klangk_backend.model as us

        us.clear_agent_cache()
        user = await us.create_user("someone@example.com", "hash")
        with pytest.raises(
            ValueError, match="reserved for the workspace agent"
        ):
            await us.set_user_handle(user["id"], "clanker")

    async def test_create_user_agent_email_gets_suffixed(self, db):
        # A derived handle colliding with the agent handle is suffixed,
        # not refused (registration derives, doesn't choose) — but the
        # user must never end up WITH the agent handle (#1160).
        import klangk_backend.model as us

        us.clear_agent_cache()
        user = await us.create_user("clanker@example.com", "hash")
        assert user["handle"] != await us.agent_handle()
        assert user["handle"] == "clanker-2"

    async def test_set_user_handle_invalid(self, user):
        with pytest.raises(ValueError, match="empty"):
            await model.set_user_handle(user["id"], "")
        with pytest.raises(ValueError, match="characters"):
            await model.set_user_handle(user["id"], "a" * 100)
        with pytest.raises(ValueError, match="dot"):
            await model.set_user_handle(user["id"], ".hidden")
        with pytest.raises(ValueError, match="reserved"):
            await model.set_user_handle(user["id"], "work")
        with pytest.raises(ValueError, match="lowercase"):
            await model.set_user_handle(user["id"], "Alice")

    async def test_get_user_by_handle(self, user):
        found = await model.get_user_by_handle(user["handle"])
        assert found is not None
        assert found["id"] == user["id"]
        assert found["handle"] == user["handle"]

    async def test_get_user_by_handle_not_found(self, db):
        found = await model.get_user_by_handle("nonexistent")
        assert found is None

    async def test_derive_handle(self):
        assert model.derive_handle("alice@example.com") == "alice"
        assert model.derive_handle("Alice+Dev@foo.com") == "alicedev"
        assert model.derive_handle("bob.smith@foo.com") == "bob.smith"
        assert model.derive_handle("@foo.com") == "user"
        assert model.derive_handle("admin") == "admin"

    async def test_generate_handle_derives_and_uniquifies(self, db):
        """generate_handle is the shared generator — derive + unique (#1256)."""
        # Fresh email → derives the local part, no suffix.
        async with model.transaction() as conn:
            assert (
                await model.generate_handle(conn, "alice@example.com")
                == "alice"
            )
        # After alice exists, the same email gets a -2 suffix.
        await model.create_user("alice@example.com", "hash", verified=True)
        async with model.transaction() as conn:
            assert (
                await model.generate_handle(conn, "alice@example.com")
                == "alice-2"
            )
            # Different local part still derives cleanly.
            assert (
                await model.generate_handle(conn, "bob.smith@foo.com")
                == "bob.smith"
            )
            # Garbage local part falls back to the "user" base.
            assert await model.generate_handle(conn, "@foo.com") == "user"

    async def test_insert_unverified_user_derives_handle_and_marks_unverified(
        self, db
    ):
        """insert_unverified_user inserts verified=0 + derived handle (#1256).

        It runs on the caller's transaction so an email-send failure can
        roll back the insert — verified here by checking that an exception
        on the same transaction leaves no row.
        """
        user_id = str(uuid.uuid4())
        # Committed happy path: insert, commit, then read back.
        async with model.transaction() as conn:
            handle = await model.insert_unverified_user(
                conn, user_id, "carol@example.com", "somehash"
            )
        assert handle == "carol"
        cursor = await model.fetchone(
            "SELECT email, handle, verified, password_hash FROM users"
            " WHERE id = ?",
            (user_id,),
        )
        assert cursor is not None
        assert cursor["email"] == "carol@example.com"
        assert cursor["handle"] == "carol"  # derived, not NULL
        assert cursor["verified"] == 0
        assert cursor["password_hash"] == "somehash"

        # Rollback path: an exception inside the transaction must leave
        # no row — this is the guarantee the register/invite routes rely
        # on when the verification email send fails.
        bad_id = str(uuid.uuid4())
        with pytest.raises(Exception):
            async with model.transaction() as conn:
                await model.insert_unverified_user(
                    conn, bad_id, "dave@example.com", "h"
                )
                raise RuntimeError("simulate email-send failure")
        assert await model.get_user_by_id(bad_id) is None

    async def test_validate_handle(self):
        assert model.validate_handle("alice") is None
        assert model.validate_handle("") is not None
        assert model.validate_handle("a" * 100) is not None
        assert model.validate_handle(".hidden") is not None
        assert model.validate_handle("work") is not None
        assert model.validate_handle("Alice") is not None

    async def test_handle_conflict_truncates_long_suffix(self, db):
        """When base handle is near max length, suffix is truncated."""
        long = "a" * model.MAX_HANDLE_LEN
        u1 = await model.create_user(long + "@a.com", "hash")
        assert u1["handle"] == long
        u2 = await model.create_user(long + "@b.com", "hash")
        assert len(u2["handle"]) <= model.MAX_HANDLE_LEN
        assert u2["handle"].endswith("-2")

    async def test_get_user_by_email_includes_handle(self, user):
        found = await model.get_user_by_email(user["email"])
        assert found["handle"] == user["handle"]

    async def test_get_user_by_id_includes_handle(self, user):
        found = await model.get_user_by_id(user["id"])
        assert found["handle"] == user["handle"]

    async def test_search_users_includes_handle(self, user):
        results = await model.search_users("testuser")
        assert len(results) > 0
        assert results[0]["handle"] == user["handle"]

    async def test_get_workspace_members_includes_handle(self, user):
        ws = await model.create_workspace(user["id"], "member-ws")
        other = await model.create_user(
            "other@test.com", "hash", verified=True
        )
        resource = f"/workspaces/{ws['id']}"
        await model.add_acl_entry(
            resource,
            0,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        members = await model.get_workspace_members(ws["id"])
        assert len(members) == 1
        assert members[0]["handle"] == other["handle"]


class TestWorkspaces:
    async def test_create_workspace(self, user):
        ws = await model.create_workspace(user["id"], "my-workspace")
        assert ws["name"] == "my-workspace"
        assert ws["user_id"] == user["id"]
        assert "id" in ws
        assert "created_at" in ws

    async def test_list_workspaces(self, user):
        await model.create_workspace(user["id"], "ws1")
        await model.create_workspace(user["id"], "ws2")
        result = await model.list_workspaces(user["id"])
        names = [ws["name"] for ws in result["items"]]
        assert "ws1" in names
        assert "ws2" in names
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_list_workspaces_pagination(self, user):
        for i in range(3):
            await model.create_workspace(user["id"], f"ws{i}")
        # Page size 2: first page has 2 items and signals more.
        page1 = await model.list_workspaces(user["id"], limit=2, offset=0)
        assert len(page1["items"]) == 2
        assert page1["has_more"] is True
        assert page1["next_offset"] == 2
        # Second page returns the remaining item, no more.
        page2 = await model.list_workspaces(
            user["id"], limit=2, offset=page1["next_offset"]
        )
        assert len(page2["items"]) == 1
        assert page2["has_more"] is False
        assert page2["next_offset"] is None
        # No overlap between pages.
        page1_ids = {ws["id"] for ws in page1["items"]}
        page2_ids = {ws["id"] for ws in page2["items"]}
        assert page1_ids.isdisjoint(page2_ids)

    async def test_list_workspaces_offset_beyond_end(self, user):
        await model.create_workspace(user["id"], "only")
        result = await model.list_workspaces(user["id"], offset=10)
        assert result["items"] == []
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_get_workspace(self, workspace, user):
        found = await model.get_workspace(workspace["id"], user["id"])
        assert found is not None
        assert found["name"] == "test-workspace"

    async def test_get_workspace_wrong_user(self, workspace):
        found = await model.get_workspace(workspace["id"], "wrong-user-id")
        assert found is None

    async def test_delete_workspace(self, workspace, user):
        deleted = await model.delete_workspace(workspace["id"], user["id"])
        assert deleted is True
        found = await model.get_workspace(workspace["id"], user["id"])
        assert found is None

    async def test_delete_workspace_not_found(self, user):
        deleted = await model.delete_workspace("fake-id", user["id"])
        assert deleted is False

    async def test_duplicate_workspace_name(self, user):
        await model.create_workspace(user["id"], "unique-name")
        with pytest.raises(Exception):
            await model.create_workspace(user["id"], "unique-name")

    async def test_create_workspace_with_auto_start(self, user):
        ws = await model.create_workspace(
            user["id"], "auto-ws", auto_start=True
        )
        assert ws["auto_start"] is True
        found = await model.get_workspace(ws["id"], user["id"])
        assert found["auto_start"] is True

    async def test_update_workspace_auto_start(self, user):
        ws = await model.create_workspace(user["id"], "no-auto")
        assert ws["auto_start"] is False
        updated = await model.update_workspace(
            ws["id"], user["id"], auto_start=True
        )
        assert updated is True
        found = await model.get_workspace(ws["id"], user["id"])
        assert found["auto_start"] is True

    async def test_list_auto_start_workspaces(self, user):
        await model.create_workspace(user["id"], "normal-ws")
        await model.create_workspace(user["id"], "auto-ws", auto_start=True)
        result = await model.list_auto_start_workspaces()
        assert len(result) == 1
        assert result[0]["name"] == "auto-ws"
        assert result[0]["auto_start"] is True


class TestWorkspaceSharing:
    async def _share(self, workspace_id, user_id):
        """Grant a user access via ACL entry."""
        resource = f"/workspaces/{workspace_id}"
        existing = await model.get_acl_entries(resource)
        max_pos = max((e["position"] for e in existing), default=-1)
        await model.add_acl_entry(
            resource,
            max_pos + 1,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=user_id,
        )

    async def test_share_workspace(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await self._share(workspace["id"], other["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert len(members) == 1
        assert members[0]["id"] == other["id"]
        assert members[0]["email"] == "other@example.com"

    async def test_get_workspace_without_user_id(self, workspace, user):
        """get_workspace without user_id returns any workspace."""
        found = await model.get_workspace(workspace["id"])
        assert found is not None
        assert found["name"] == "test-workspace"

    async def test_get_workspace_wrong_owner(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        found = await model.get_workspace(workspace["id"], other["id"])
        assert found is None

    async def test_unshare_workspace(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await self._share(workspace["id"], other["id"])
        # Remove ACL entries for other user
        resource = f"/workspaces/{workspace['id']}"
        entries = await model.get_acl_entries(resource)
        remaining = [
            e
            for e in entries
            if not (
                e["principal_type"] == model.PRINCIPAL_USER
                and e["user_id"] == other["id"]
            )
        ]
        for i, entry in enumerate(remaining):
            entry["position"] = i
        await model.replace_acl_entries(resource, remaining)
        members = await model.get_workspace_members(workspace["id"])
        assert len(members) == 0

    async def test_get_workspace_members_empty(self, workspace):
        members = await model.get_workspace_members(workspace["id"])
        assert members == []

    async def test_share_workspace_idempotent(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await self._share(workspace["id"], other["id"])
        await self._share(workspace["id"], other["id"])
        members = await model.get_workspace_members(workspace["id"])
        # Two ACEs but same user — get_workspace_members uses DISTINCT
        assert len(members) == 1

    async def test_get_workspace_members_ordered(self, workspace, user):
        u_b = await model.create_user("b@example.com", "hash")
        u_a = await model.create_user("a@example.com", "hash")
        await self._share(workspace["id"], u_b["id"])
        await self._share(workspace["id"], u_a["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert members[0]["email"] == "a@example.com"
        assert members[1]["email"] == "b@example.com"

    async def test_acl_cascade_on_user_delete(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await self._share(workspace["id"], other["id"])
        # Delete the user — CASCADE should remove ACL entries
        await model.delete_user(other["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert members == []

    async def test_list_shared_workspaces(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await self._share(workspace["id"], other["id"])
        result = await model.list_shared_workspaces(other["id"])
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == workspace["id"]
        assert result["items"][0]["name"] == "test-workspace"
        assert result["items"][0]["owner_email"] == user["email"]
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_list_shared_workspaces_empty(self, user):
        result = await model.list_shared_workspaces(user["id"])
        assert result["items"] == []
        assert result["has_more"] is False
        assert result["next_offset"] is None

    async def test_list_shared_workspaces_pagination(self, user):
        other = await model.create_user("sharer@example.com", "hash")
        for i in range(3):
            ws = await model.create_workspace(user["id"], f"shared{i}")
            await self._share(ws["id"], other["id"])
        page1 = await model.list_shared_workspaces(
            other["id"], limit=2, offset=0
        )
        assert len(page1["items"]) == 2
        assert page1["has_more"] is True
        assert page1["next_offset"] == 2
        page2 = await model.list_shared_workspaces(
            other["id"], limit=2, offset=page1["next_offset"]
        )
        assert len(page2["items"]) == 1
        assert page2["has_more"] is False
        assert page2["next_offset"] is None


class TestSearchUsers:
    async def test_search_by_prefix(self, user):
        await model.create_user("alice@example.com", "hash")
        await model.create_user("alice2@example.com", "hash")
        await model.create_user("bob@example.com", "hash")
        results = await model.search_users("alice")
        assert len(results) == 2
        assert all(r["email"].startswith("alice") for r in results)

    async def test_search_no_results(self, db):
        results = await model.search_users("zzzzz")
        assert results == []

    async def test_search_with_limit(self, user):
        for i in range(5):
            await model.create_user(f"match{i}@example.com", "hash")
        results = await model.search_users("match", limit=3)
        assert len(results) == 3


class TestPortAllocations:
    async def test_add_and_get_ports(self, workspace):
        await model.add_port_allocations(workspace["id"], [9000, 9001, 9002])
        ports = await model.get_workspace_ports(workspace["id"])
        assert ports == [9000, 9001, 9002]

    async def test_get_all_allocated_ports(self, workspace):
        await model.add_port_allocations(workspace["id"], [9000, 9001])
        all_ports = await model.get_all_allocated_ports()
        assert 9000 in all_ports
        assert 9001 in all_ports

    async def test_remove_ports(self, workspace):
        await model.add_port_allocations(workspace["id"], [9000, 9001, 9002])
        await model.remove_port_allocations(workspace["id"], [9001])
        ports = await model.get_workspace_ports(workspace["id"])
        assert ports == [9000, 9002]

    async def test_ports_cascade_on_workspace_delete(self, workspace, user):
        await model.add_port_allocations(workspace["id"], [9000, 9001])
        await model.delete_workspace(workspace["id"], user["id"])
        ports = await model.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_duplicate_port_rejected(self, workspace, user):
        await model.add_port_allocations(workspace["id"], [9000])
        # Create second workspace
        ws2 = await model.create_workspace(user["id"], "ws2")
        with pytest.raises(Exception):
            await model.add_port_allocations(ws2["id"], [9000])

    async def test_get_workspace_ports_empty(self, workspace):
        ports = await model.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_get_all_allocated_ports_empty(self, db):
        all_ports = await model.get_all_allocated_ports()
        assert all_ports == set()

    async def test_find_and_allocate_ports(self, workspace, monkeypatch):
        monkeypatch.setattr(model.ports, "port_in_use", lambda p: False)
        ports = await model.find_and_allocate_ports(workspace["id"], 3, 9000)
        assert ports == [9000, 9001, 9002]
        stored = await model.get_workspace_ports(workspace["id"])
        assert stored == [9000, 9001, 9002]

    async def test_find_and_allocate_skips_used(
        self, workspace, user, monkeypatch
    ):
        monkeypatch.setattr(model.ports, "port_in_use", lambda p: False)
        await model.add_port_allocations(workspace["id"], [9000, 9002])
        ws2 = await model.create_workspace(user["id"], "ws2")
        ports = await model.find_and_allocate_ports(ws2["id"], 3, 9000)
        assert ports == [9001, 9003, 9004]

    async def test_find_and_allocate_skips_os_bound_ports(
        self, workspace, monkeypatch
    ):
        monkeypatch.setattr(
            model.ports, "port_in_use", lambda p: p in {9001, 9003}
        )
        ports = await model.find_and_allocate_ports(workspace["id"], 3, 9000)
        assert ports == [9000, 9002, 9004]

    async def test_find_and_allocate_raises_when_exhausted(
        self, workspace, monkeypatch
    ):
        """Exhausting the port range fails fast instead of looping forever."""
        # Every port at/after start is treated as in-use; asking for any
        # ports from a start of MAX_PORT guarantees immediate exhaustion.
        monkeypatch.setattr(model.ports, "port_in_use", lambda p: True)
        with pytest.raises(ValueError):
            await model.find_and_allocate_ports(
                workspace["id"], 1, model.MAX_PORT
            )

    async def test_find_and_allocate_respects_max_port(
        self, workspace, user, monkeypatch
    ):
        """The scan never exceeds MAX_PORT and raises if it can't fulfil."""
        # Only the last two ports are free; requesting two succeeds, three raises.
        free = {model.MAX_PORT - 1, model.MAX_PORT}
        monkeypatch.setattr(
            model.ports, "port_in_use", lambda p: p not in free
        )
        ports = await model.find_and_allocate_ports(
            workspace["id"], 2, model.MAX_PORT - 1
        )
        assert ports == [model.MAX_PORT - 1, model.MAX_PORT]
        ws2 = await model.create_workspace(user["id"], "ws-max")
        with pytest.raises(ValueError):
            await model.find_and_allocate_ports(
                ws2["id"], 3, model.MAX_PORT - 1
            )


class TestPortInUse:
    def test_free_port(self):
        assert model.port_in_use(59123) is False

    def test_bound_port(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 59124))
            assert model.port_in_use(59124) is True


class TestServiceCommand:
    async def test_create_with_service_command(self, user):
        ws = await model.create_workspace(
            user["id"], "cmd-ws", service_command="pi"
        )
        assert ws["service_command"] == "pi"
        fetched = await model.get_workspace(ws["id"], user["id"])
        assert fetched["service_command"] == "pi"

    async def test_update_service_command(self, workspace, user):
        updated = await model.update_workspace(
            workspace["id"], user["id"], service_command="pi"
        )
        assert updated is True
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["service_command"] == "pi"

    async def test_clear_service_command(self, workspace, user):
        await model.update_workspace(
            workspace["id"], user["id"], service_command="pi"
        )
        await model.update_workspace(
            workspace["id"], user["id"], service_command=None
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["service_command"] is None

    async def test_update_nonexistent_workspace(self, user):
        updated = await model.update_workspace(
            "nonexistent", user["id"], service_command="pi"
        )
        assert updated is False

    async def test_update_multiple_fields(self, workspace, user):
        await model.update_workspace(
            workspace["id"],
            user["id"],
            name="renamed",
            service_command="pi",
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["name"] == "renamed"
        assert ws["service_command"] == "pi"

    async def test_create_with_mounts(self, user):
        mounts = ["/home/me/project:/work/project"]
        ws = await model.create_workspace(
            user["id"], "mount-ws", mounts=mounts
        )
        assert ws["mounts"] == mounts
        fetched = await model.get_workspace(ws["id"], user["id"])
        assert fetched["mounts"] == mounts

    async def test_update_mounts(self, workspace, user):
        mounts = ["/data:/mnt/data:ro"]
        await model.update_workspace(
            workspace["id"], user["id"], mounts=mounts
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["mounts"] == mounts

    async def test_list_includes_mounts(self, user):
        mounts = ["/tmp/test:/work/test"]
        await model.create_workspace(user["id"], "mount-list", mounts=mounts)
        result = await model.list_workspaces(user["id"])
        match = [w for w in result["items"] if w["name"] == "mount-list"]
        assert match[0]["mounts"] == mounts

    async def test_update_ignores_unknown_fields(self, workspace, user):
        result = await model.update_workspace(
            workspace["id"], user["id"], bogus="ignored"
        )
        assert result is False

    async def test_update_no_fields(self, workspace, user):
        result = await model.update_workspace(workspace["id"], user["id"])
        assert result is False

    async def test_list_includes_service_command(self, user):
        await model.create_workspace(
            user["id"], "cmd-ws", service_command="pi"
        )
        result = await model.list_workspaces(user["id"])
        match = [w for w in result["items"] if w["name"] == "cmd-ws"]
        assert len(match) == 1
        assert match[0]["service_command"] == "pi"


class TestContainerTracking:
    async def test_update_workspace_container(self, workspace, user):
        await model.update_workspace_container(
            workspace["id"], "container-123"
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] == "container-123"

    async def test_clear_workspace_container(self, workspace, user):
        await model.update_workspace_container(
            workspace["id"], "container-123"
        )
        await model.update_workspace_container(workspace["id"], None)
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] is None

    async def test_get_user_workspaces_with_containers(self, user):
        ws1 = await model.create_workspace(user["id"], "ws-c1")
        ws2 = await model.create_workspace(user["id"], "ws-c2")
        await model.create_workspace(user["id"], "ws-c3")  # no container
        await model.update_workspace_container(ws1["id"], "cid-1")
        await model.update_workspace_container(ws2["id"], "cid-2")
        result = await model.get_user_workspaces_with_containers(user["id"])
        ids = {r["id"] for r in result}
        assert ws1["id"] in ids
        assert ws2["id"] in ids
        assert len(result) == 2
        for r in result:
            assert r["container_id"] is not None

    async def test_get_user_workspaces_with_containers_empty(self, user):
        result = await model.get_user_workspaces_with_containers(user["id"])
        assert result == []


class TestTokenBlocklist:
    async def test_blocklist_and_check(self, db):
        await model.blocklist_token("jti-1", "2099-01-01T00:00:00Z")
        assert await model.is_token_blocklisted("jti-1") is True

    async def test_not_blocklisted(self, db):
        assert await model.is_token_blocklisted("jti-unknown") is False

    async def test_blocklist_duplicate_ignored(self, db):
        await model.blocklist_token("jti-2", "2099-01-01T00:00:00Z")
        # INSERT OR IGNORE should not raise
        await model.blocklist_token("jti-2", "2099-01-01T00:00:00Z")
        assert await model.is_token_blocklisted("jti-2") is True


class TestLoginAttempts:
    async def test_record_and_get_attempts(self, db):
        await model.record_failed_login("alice@example.com")
        info = await model.get_login_attempt_info("alice@example.com")
        assert info is not None
        assert info["attempt_count"] == 1
        assert info["locked_until"] is None

    async def test_record_multiple_attempts(self, db):
        for _ in range(3):
            await model.record_failed_login("alice@example.com")
        info = await model.get_login_attempt_info("alice@example.com")
        assert info["attempt_count"] == 3

    async def test_get_attempt_info_nonexistent(self, db):
        info = await model.get_login_attempt_info("nobody@example.com")
        assert info is None

    async def test_set_and_get_lockout(self, db):
        from datetime import datetime, timedelta, timezone

        await model.record_failed_login("alice@example.com")
        locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
        await model.set_login_lockout(
            "alice@example.com", locked_until.isoformat()
        )
        info = await model.get_login_attempt_info("alice@example.com")
        assert info["locked_until"] is not None

    async def test_clear_attempts(self, db):
        await model.record_failed_login("alice@example.com")
        await model.record_failed_login("alice@example.com")
        await model.clear_login_attempts("alice@example.com")
        info = await model.get_login_attempt_info("alice@example.com")
        assert info is None  # row deleted

    async def test_clear_attempts_nonexistent(self, db):
        # Should not raise
        await model.clear_login_attempts("nobody@example.com")

    async def test_record_resets_count(self, db):
        """reset=True starts a fresh count and clears stale lockout."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        stale_lock = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        async with model.transaction() as raw_db:
            await raw_db.execute(
                "INSERT INTO login_attempts"
                " (email, attempt_count, first_attempt_at, locked_until)"
                " VALUES (?, 5, ?, ?)",
                ("alice@example.com", old, stale_lock),
            )
        await model.record_failed_login("alice@example.com", reset=True)
        info = await model.get_login_attempt_info("alice@example.com")
        assert info["attempt_count"] == 1
        assert info["locked_until"] is None
        # first_attempt_at moved forward to ~now.
        first = datetime.fromisoformat(info["first_attempt_at"])
        assert (datetime.now(timezone.utc) - first).total_seconds() < 5

    async def test_record_reset_upserts_missing_row(self, db):
        """reset=True on a row that doesn't exist inserts count=1."""
        await model.record_failed_login("alice@example.com", reset=True)
        info = await model.get_login_attempt_info("alice@example.com")
        assert info is not None
        assert info["attempt_count"] == 1
        assert info["locked_until"] is None

    async def test_record_increments_within_window(self, db):
        """reset=False (default) increments the count."""
        await model.record_failed_login("alice@example.com")
        await model.record_failed_login("alice@example.com")
        info = await model.get_login_attempt_info("alice@example.com")
        assert info["attempt_count"] == 2


class TestChatMessagesMigration:
    async def test_migrate_adds_message_type_column(self, db):
        """init_db adds message_type column to existing tables that lack it."""
        async with model.transaction() as raw_db:
            # Drop and recreate without message_type to simulate old schema
            await raw_db.execute("DROP TABLE IF EXISTS chat_mentions")
            await raw_db.execute("DROP TABLE IF EXISTS chat_messages")
            await raw_db.execute("""
                CREATE TABLE chat_messages (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    user_email TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)

        # Re-run init_db — should add message_type via ALTER TABLE
        await model.init_db()

        async with model.transaction() as migrated_db:
            cursor = await migrated_db.execute(
                "PRAGMA table_info(chat_messages)"
            )
            cols = {row[1] for row in await cursor.fetchall()}
            assert "message_type" in cols


class TestChatMessages:
    async def test_add_chat_message(self, workspace, user):
        msg = await model.add_chat_message(
            workspace["id"], user["id"], "testuser@example.com", "hello"
        )
        assert msg["workspace_id"] == workspace["id"]
        assert msg["user_id"] == user["id"]
        assert msg["user_email"] == "testuser@example.com"
        assert msg["user_handle"] == "testuser"
        assert msg["message"] == "hello"
        assert msg["message_type"] == model.MSG_USER
        assert "id" in msg
        assert "created_at" in msg
        assert msg["mentions"] == []

    async def test_add_chat_message_with_type(self, workspace, user):
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            "testuser@example.com",
            "system event",
            message_type=model.MSG_SYSTEM,
        )
        assert msg["message_type"] == model.MSG_SYSTEM

        agent_msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            "agent@bot",
            "agent reply",
            message_type=model.MSG_AGENT,
        )
        assert agent_msg["message_type"] == model.MSG_AGENT

    async def test_get_chat_messages(self, workspace, user):
        await model.add_chat_message(
            workspace["id"], user["id"], "testuser@example.com", "first"
        )
        await model.add_chat_message(
            workspace["id"], user["id"], "testuser@example.com", "second"
        )
        msgs = await model.get_chat_messages(workspace["id"])
        assert len(msgs) == 2
        assert msgs[0]["message"] == "first"
        assert msgs[1]["message"] == "second"
        assert msgs[0]["user_handle"] == "testuser"
        assert msgs[1]["user_handle"] == "testuser"
        assert msgs[0]["message_type"] == model.MSG_USER
        assert msgs[1]["message_type"] == model.MSG_USER
        assert msgs[0]["mentions"] == []
        assert msgs[1]["mentions"] == []

    async def test_get_chat_messages_preserves_type(self, workspace, user):
        await model.add_chat_message(
            workspace["id"],
            "uid",
            "u@test.com",
            "joined",
            message_type=model.MSG_SYSTEM,
        )
        await model.add_chat_message(
            workspace["id"],
            "uid",
            "u@test.com",
            "hello",
        )
        msgs = await model.get_chat_messages(workspace["id"])
        assert len(msgs) == 2
        assert msgs[0]["message_type"] == model.MSG_SYSTEM
        assert msgs[1]["message_type"] == model.MSG_USER

    async def test_get_chat_messages_limit(self, workspace, user):
        for i in range(5):
            await model.add_chat_message(
                workspace["id"], "uid", "u@test.com", f"msg{i}"
            )
        msgs = await model.get_chat_messages(workspace["id"], limit=3)
        assert len(msgs) == 3
        assert msgs[0]["message"] == "msg2"
        assert msgs[1]["message"] == "msg3"
        assert msgs[2]["message"] == "msg4"

    async def test_chat_messages_cascade_delete(self, workspace, user):
        await model.add_chat_message(
            workspace["id"], "uid", "u@test.com", "bye"
        )
        await model.delete_workspace(workspace["id"], user["id"])
        msgs = await model.get_chat_messages(workspace["id"])
        assert msgs == []

    async def test_delete_chat_message(self, workspace, user):
        msg = await model.add_chat_message(
            workspace["id"], user["id"], "u@test.com", "to delete"
        )
        deleted = await model.delete_chat_message(msg["id"], user["id"])
        assert deleted
        msgs = await model.get_chat_messages(workspace["id"])
        assert msgs[0]["message"] == "<message deleted by author>"

    async def test_delete_chat_message_wrong_user(self, workspace, user):
        msg = await model.add_chat_message(
            workspace["id"], user["id"], "u@test.com", "mine"
        )
        deleted = await model.delete_chat_message(msg["id"], "other-uid")
        assert not deleted
        msgs = await model.get_chat_messages(workspace["id"])
        assert msgs[0]["message"] == "mine"


class TestChatMessagesPagination:
    async def test_get_messages_before(self, workspace, user):
        msgs = []
        for i in range(5):
            msgs.append(
                await model.add_chat_message(
                    workspace["id"], "uid", "u@test.com", f"msg{i}"
                )
            )
        # Load messages before the last one
        older = await model.get_chat_messages_before(
            workspace["id"], msgs[4]["id"], limit=50
        )
        assert len(older) == 4
        assert older[0]["message"] == "msg0"
        assert older[3]["message"] == "msg3"

    async def test_get_messages_before_with_limit(self, workspace, user):
        msgs = []
        for i in range(5):
            msgs.append(
                await model.add_chat_message(
                    workspace["id"], "uid", "u@test.com", f"msg{i}"
                )
            )
        older = await model.get_chat_messages_before(
            workspace["id"], msgs[4]["id"], limit=2
        )
        assert len(older) == 2
        assert older[0]["message"] == "msg2"
        assert older[1]["message"] == "msg3"

    async def test_get_messages_before_invalid_id(self, workspace, user):
        older = await model.get_chat_messages_before(
            workspace["id"], "nonexistent", limit=50
        )
        assert older == []

    async def test_get_messages_before_includes_mentions(
        self, workspace, user
    ):
        # Create a user and add ACL so mention resolution finds them
        target = await model.create_user(
            "mention-pag@test.com", "pass", verified=True
        )
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=target["id"],
        )
        await model.add_chat_message(
            workspace["id"], "uid", "u@test.com", f"hey @{target['handle']}"
        )
        anchor = await model.add_chat_message(
            workspace["id"], "uid", "u@test.com", "anchor"
        )
        older = await model.get_chat_messages_before(
            workspace["id"], anchor["id"]
        )
        assert len(older) == 1
        assert len(older[0]["mentions"]) > 0


class TestChatMentions:
    async def test_mention_workspace_owner(self, workspace, user):
        """@mentioning the workspace owner resolves to their user ID."""
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"hello @{user['handle']}",
        )
        assert msg["mentions"] == [user["id"]]

    async def test_mention_workspace_member(self, workspace, user):
        """@mentioning a workspace member (via ACL) resolves."""
        member = await model.create_user(
            "member@test.com", "hash", verified=True
        )
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"hey @{member['handle']} check this",
        )
        assert msg["mentions"] == [member["id"]]

    async def test_mention_non_member_ignored(self, workspace, user):
        """@mentioning someone not in the workspace produces no mentions."""
        await model.create_user("outsider@test.com", "hash", verified=True)
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            "hey @outsider",
        )
        assert msg["mentions"] == []

    async def test_mention_multiple_users(self, workspace, user):
        """Multiple @mentions in one message resolve correctly."""
        member = await model.create_user("m@test.com", "hash", verified=True)
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"@{user['handle']} and @{member['handle']}",
        )
        assert set(msg["mentions"]) == {user["id"], member["id"]}

    async def test_mention_deduplication(self, workspace, user):
        """Duplicate @mentions produce only one entry."""
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"@{user['handle']} @{user['handle']}",
        )
        assert msg["mentions"] == [user["id"]]

    async def test_mentions_in_history(self, workspace, user):
        """get_chat_messages includes mentions from the DB."""
        await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"hello @{user['handle']}",
        )
        msgs = await model.get_chat_messages(workspace["id"])
        assert len(msgs) == 1
        assert msgs[0]["mentions"] == [user["id"]]

    async def test_mentions_cascade_with_message(self, workspace, user):
        """Deleting a workspace cascades to chat_mentions."""
        await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"@{user['email']}",
        )
        await model.delete_workspace(workspace["id"], user["id"])
        msgs = await model.get_chat_messages(workspace["id"])
        assert msgs == []

    async def test_no_mention_pattern(self, workspace, user):
        """Messages without @ produce empty mentions."""
        msg = await model.add_chat_message(
            workspace["id"], user["id"], user["email"], "just plain text"
        )
        assert msg["mentions"] == []


class TestInvitations:
    async def test_create_and_get(self, db, admin_user):
        inv = await model.create_invitation("a@b.com", admin_user["id"])
        assert inv["email"] == "a@b.com"
        assert inv["status"] == "pending"

        fetched = await model.get_invitation(inv["id"])
        assert fetched["email"] == "a@b.com"
        assert fetched["status"] == "pending"

    async def test_get_nonexistent(self, db):
        assert await model.get_invitation("nonexistent") is None

    async def test_get_pending_by_email(self, db, admin_user):
        await model.create_invitation("p@b.com", admin_user["id"])
        pending = await model.get_pending_invitation_by_email("p@b.com")
        assert pending is not None
        assert pending["email"] == "p@b.com"

    async def test_get_pending_by_email_none(self, db):
        assert (
            await model.get_pending_invitation_by_email("no@one.com") is None
        )

    async def test_list(self, db, admin_user):
        await model.create_invitation("x@b.com", admin_user["id"])
        await model.create_invitation("y@b.com", admin_user["id"])
        result = await model.list_invitations()
        invs = result["invitations"]
        assert len(invs) >= 2
        emails = [i["email"] for i in invs]
        assert "x@b.com" in emails
        assert "y@b.com" in emails
        assert invs[0]["invited_by_email"] == "testadmin@example.com"
        # Paged envelope metadata + global pending count.
        assert result["page"] == 1
        assert result["page_size"] == 10
        assert result["total"] >= 2
        assert result["pending_count"] >= 2

    async def test_mark_accepted(self, db, admin_user):
        inv = await model.create_invitation("acc@b.com", admin_user["id"])
        assert await model.mark_invitation_accepted(inv["id"])
        fetched = await model.get_invitation(inv["id"])
        assert fetched["status"] == "accepted"
        assert fetched["accepted_at"] is not None
        # Can't accept again
        assert not await model.mark_invitation_accepted(inv["id"])

    async def test_revoke(self, db, admin_user):
        inv = await model.create_invitation("rev@b.com", admin_user["id"])
        assert await model.revoke_invitation(inv["id"])
        fetched = await model.get_invitation(inv["id"])
        assert fetched["status"] == "revoked"
        # Can't revoke again
        assert not await model.revoke_invitation(inv["id"])

    async def test_revoke_nonexistent(self, db):
        assert not await model.revoke_invitation("nonexistent")


class TestOIDCUsers:
    async def test_create_oidc_user(self, db):
        user = await model.create_user(
            "oidc@example.com",
            password_hash=None,
            verified=True,
            provider="keycloak",
            external_id="sub-123",
        )
        assert user["id"]
        assert user["email"] == "oidc@example.com"

    async def test_get_by_external_id(self, db):
        await model.create_user(
            "ext@example.com",
            password_hash=None,
            verified=True,
            provider="kc",
            external_id="ext-456",
        )
        found = await model.get_user_by_external_id("kc", "ext-456")
        assert found is not None
        assert found["email"] == "ext@example.com"
        assert found["provider"] == "kc"
        assert found["external_id"] == "ext-456"

    async def test_get_by_external_id_not_found(self, db):
        assert await model.get_user_by_external_id("kc", "nope") is None

    async def test_link_oidc_identity(self, db):
        user = await model.create_user(
            "link@example.com", "hash", verified=True
        )
        await model.link_oidc_identity(user["id"], "kc", "linked-sub")
        found = await model.get_user_by_external_id("kc", "linked-sub")
        assert found is not None
        assert found["id"] == user["id"]

    async def test_get_user_by_email_includes_oidc_fields(self, db):
        await model.create_user(
            "fields@example.com",
            password_hash=None,
            verified=True,
            provider="google",
            external_id="g-789",
        )
        user = await model.get_user_by_email("fields@example.com")
        assert user["provider"] == "google"
        assert user["external_id"] == "g-789"
        assert user["password_hash"] is None


class TestUpdatePasswordAgentGuard:
    async def test_update_password_rejects_agent_user(self, db):
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.update_password(model.AGENT_USER_ID, "hash")


class TestDeleteUserAgentGuard:
    async def test_delete_user_rejects_agent_user(self, db):
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.delete_user(model.AGENT_USER_ID)


class TestAddUserToGroupAgentGuard:
    async def test_add_user_to_group_rejects_agent(self, db):
        # Choke-point guard (#1135): every add_user_to_group caller
        # (role grants, group-member add, OIDC sync) is covered here.
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.add_user_to_group(model.AGENT_USER_ID, "g")


class TestAddAclEntryAgentGuard:
    async def test_add_acl_entry_rejects_agent(self, db):
        # Choke-point guard (#1135): direct PRINCIPAL_USER ACE grants
        # (e.g. add_workspace_member) are covered here.
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.add_acl_entry(
                "/workspaces/x",
                0,
                model.ACTION_ALLOW,
                "*",
                model.PRINCIPAL_USER,
                user_id=model.AGENT_USER_ID,
            )


class TestReplaceAclEntriesAgentGuard:
    async def test_replace_acl_entries_rejects_agent(self, db):
        # Choke-point guard (#1135): replace_acl_entries is the second
        # writer into acl_entries (a raw INSERT, fed request-body
        # user_id by the PUT-acl endpoints) — guarded like add_acl_entry.
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.replace_acl_entries(
                "/workspaces/x",
                [
                    {
                        "position": 0,
                        "action": model.ACTION_ALLOW,
                        "principal_type": model.PRINCIPAL_USER,
                        "permission": "*",
                        "user_id": model.AGENT_USER_ID,
                        "group_id": None,
                        "system_principal": None,
                    }
                ],
            )


class TestCreateWorkspaceWithAclAgentGuard:
    async def test_create_workspace_with_acl_rejects_agent(self, db):
        # Choke-point guard (#1135): the owner ACE is written by
        # _seed_workspace_acl via raw SQL (can't call the guarded
        # add_acl_entry), so the public entry point guards the creator.
        with pytest.raises(model.AgentPrincipalError, match="system agent"):
            await model.create_workspace_with_acl(model.AGENT_USER_ID, "ws")


class TestSchemaAgentBackstops:
    """Data-model belt-and-suspenders (#1135): the schema constraints that
    backstop the function-layer AgentPrincipalError guards. Each test writes
    raw SQL that bypasses the Python guards, proving the DB itself rejects
    making the agent a principal / mutating its identity / deleting it --
    the terminal backstop for the raw-SQL-writer bug class the re-audit
    found (replace_acl_entries, the seed path).
    """

    async def test_acl_entries_rejects_agent_user_principal(self, agent_user):
        # (A) CHECK on acl_entries: covers both writers (add_acl_entry and
        # replace_acl_entries).
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "INSERT INTO acl_entries"
                    " (resource, position, action, principal_type,"
                    "  permission, user_id)"
                    " VALUES ('/x', 0, 1, 1, '*', ?)",
                    (model.AGENT_USER_ID,),
                )

    async def test_user_groups_rejects_agent(self, agent_user):
        # (B) CHECK on user_groups: role grants, member adds, OIDC sync.
        group = await model.create_group("g")
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "INSERT INTO user_groups (user_id, group_id, source)"
                    " VALUES (?, ?, 'manual')",
                    (model.AGENT_USER_ID, group["id"]),
                )

    async def test_workspaces_rejects_agent_owner(self, agent_user):
        # (C) CHECK on workspaces.user_id (the owner column).
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "INSERT INTO workspaces (id, user_id, name)"
                    " VALUES ('ws', ?, 'n')",
                    (model.AGENT_USER_ID,),
                )

    async def test_users_rejects_agent_password(self, agent_user):
        # (D) CHECK: the agent must never carry a password.
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    ("x", model.AGENT_USER_ID),
                )

    async def test_agent_row_cannot_be_deleted(self, agent_user):
        # (E) BEFORE DELETE trigger: the agent row is undeletable.
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "DELETE FROM users WHERE id = ?",
                    (model.AGENT_USER_ID,),
                )

    async def test_agent_identity_columns_immutable(self, agent_user):
        # (F) BEFORE UPDATE trigger on provider/external_id: the agent must
        # stay provider='system' with no linked OIDC identity (the #1145
        # skeleton-key vector). link_oidc_identity sets exactly these.
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "UPDATE users SET provider = ? WHERE id = ?",
                    ("oidc", model.AGENT_USER_ID),
                )
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            async with model.transaction() as db:
                await db.execute(
                    "UPDATE users SET external_id = ? WHERE id = ?",
                    ("sub", model.AGENT_USER_ID),
                )

    async def test_agent_email_remains_mutable(self, agent_user):
        # F deliberately does NOT guard email: it is legitimately re-seeded
        # from env at boot (ON CONFLICT DO UPDATE SET email). Email policy
        # lives at the fn layer (#1145), not the schema. This test pins that
        # decision so a future "add an email trigger" change can't silently
        # break boot-time re-seeding.
        async with model.transaction() as db:
            await db.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                ("new@example.com", model.AGENT_USER_ID),
            )
        agent = await model.get_user_by_id(model.AGENT_USER_ID)
        assert agent["email"] == "new@example.com"


class TestHashFallbackHandle:
    def test_returns_hash_based_handle(self):
        from klangk_backend.model import hash_fallback_handle

        result = hash_fallback_handle("testbase")
        assert result.startswith("testbase-")
        assert len(result) <= model.MAX_HANDLE_LEN

    def test_truncates_long_base(self):
        from klangk_backend.model import hash_fallback_handle

        long_base = "a" * 100
        result = hash_fallback_handle(long_base)
        assert len(result) <= model.MAX_HANDLE_LEN
        # Should end with a hex suffix
        assert "-" in result


class TestUniqueHandleFallback:
    async def test_falls_back_to_hash_after_exhausting_suffixes(self, db):
        from unittest.mock import AsyncMock

        from klangk_backend.model import unique_handle

        # Mock a DB cursor that always finds a collision
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(1,))
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_cursor)

        result = await unique_handle(mock_db, "taken")
        # Should fall through to hash_fallback_handle
        assert "-" in result
        assert len(result) <= model.MAX_HANDLE_LEN
        # Should contain a hex suffix, not a numeric one
        parts = result.rsplit("-", 1)
        assert len(parts[1]) == 8  # sha256[:8]


class TestTransactionCancelNoLeak:
    """Regression: cancelling a task mid ``transaction()`` must not leak
    the underlying aiosqlite connection (#1250).

    ``engine.connect()`` opens the aiosqlite worker thread (and the real
    sqlite3 connection) before its await returns. Before the fix, a
    cancellation delivered during that window left the connection with no
    handle to close it: its worker thread outlived the event loop
    (``RuntimeError: Event loop is closed``) and the sqlite3 connection
    leaked (``ResourceWarning: unclosed database``).
    """

    @staticmethod
    def _track_aiosqlite(monkeypatch):
        open_ids = set()
        orig_init = aiosqlite.Connection.__init__
        orig_close = aiosqlite.Connection.close

        def init(self, *a, **kw):
            orig_init(self, *a, **kw)
            open_ids.add(id(self))

        def close(self, *a, **kw):
            open_ids.discard(id(self))
            return orig_close(self, *a, **kw)

        monkeypatch.setattr(aiosqlite.Connection, "__init__", init)
        monkeypatch.setattr(aiosqlite.Connection, "close", close)
        return open_ids

    async def test_cancel_during_acquire_closes_connection(
        self, temp_data_dir, monkeypatch
    ):
        await model.init_db()
        open_ids = self._track_aiosqlite(monkeypatch)

        async def bg():
            # fetchone -> transaction -> get_db -> engine.connect().
            await model.db.fetchone("SELECT 1")
            await asyncio.sleep(30)  # keep the task alive to cancel

        task = asyncio.create_task(bg())
        # Let the task enter the DB op (open the connection) before cancel.
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Every aiosqlite connection that was opened must have been closed,
        # even though the task was cancelled mid-acquisition.
        import gc

        gc.collect()
        assert not open_ids, (
            f"{len(open_ids)} aiosqlite connection(s) leaked after cancel"
        )

    async def test_normal_transaction_closes_connection(
        self, temp_data_dir, monkeypatch
    ):
        """Sanity: a transaction that runs to completion closes its conn."""
        await model.init_db()
        open_ids = self._track_aiosqlite(monkeypatch)

        async with model.db.transaction() as db:
            await db.execute("CREATE TABLE IF NOT EXISTS t (x)")

        import gc

        gc.collect()
        assert not open_ids
