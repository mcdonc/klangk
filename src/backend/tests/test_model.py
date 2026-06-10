"""Tests for model: users, workspaces, messages, port allocations."""

import aiosqlite
import pytest

from klangk_backend import model


class TestMigration:
    async def test_migrate_old_schema(self, temp_data_dir):
        """Migrates a pre-OIDC database: password_hash NOT NULL, no
        provider/external_id columns."""
        db = await aiosqlite.connect(str(model.DB_PATH))
        model.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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


class TestUsers:
    async def test_create_user(self, db):
        user = await model.create_user("alice@example.com", "hash123")
        assert user["email"] == "alice@example.com"
        assert "id" in user

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
        workspaces = await model.list_workspaces(user["id"])
        names = [ws["name"] for ws in workspaces]
        assert "ws1" in names
        assert "ws2" in names

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
        shared = await model.list_shared_workspaces(other["id"])
        assert len(shared) == 1
        assert shared[0]["id"] == workspace["id"]
        assert shared[0]["name"] == "test-workspace"
        assert shared[0]["owner_email"] == user["email"]

    async def test_list_shared_workspaces_empty(self, user):
        shared = await model.list_shared_workspaces(user["id"])
        assert shared == []


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
        monkeypatch.setattr(model, "_port_in_use", lambda p: False)
        ports = await model.find_and_allocate_ports(workspace["id"], 3, 9000)
        assert ports == [9000, 9001, 9002]
        stored = await model.get_workspace_ports(workspace["id"])
        assert stored == [9000, 9001, 9002]

    async def test_find_and_allocate_skips_used(
        self, workspace, user, monkeypatch
    ):
        monkeypatch.setattr(model, "_port_in_use", lambda p: False)
        await model.add_port_allocations(workspace["id"], [9000, 9002])
        ws2 = await model.create_workspace(user["id"], "ws2")
        ports = await model.find_and_allocate_ports(ws2["id"], 3, 9000)
        assert ports == [9001, 9003, 9004]

    async def test_find_and_allocate_skips_os_bound_ports(
        self, workspace, monkeypatch
    ):
        monkeypatch.setattr(model, "_port_in_use", lambda p: p in {9001, 9003})
        ports = await model.find_and_allocate_ports(workspace["id"], 3, 9000)
        assert ports == [9000, 9002, 9004]


class TestPortInUse:
    def test_free_port(self):
        assert model._port_in_use(59123) is False

    def test_bound_port(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 59124))
            assert model._port_in_use(59124) is True


class TestDefaultCommand:
    async def test_create_with_default_command(self, user):
        ws = await model.create_workspace(
            user["id"], "cmd-ws", default_command="pi"
        )
        assert ws["default_command"] == "pi"
        fetched = await model.get_workspace(ws["id"], user["id"])
        assert fetched["default_command"] == "pi"

    async def test_update_default_command(self, workspace, user):
        updated = await model.update_workspace(
            workspace["id"], user["id"], default_command="pi"
        )
        assert updated is True
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["default_command"] == "pi"

    async def test_clear_default_command(self, workspace, user):
        await model.update_workspace(
            workspace["id"], user["id"], default_command="pi"
        )
        await model.update_workspace(
            workspace["id"], user["id"], default_command=None
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["default_command"] is None

    async def test_update_nonexistent_workspace(self, user):
        updated = await model.update_workspace(
            "nonexistent", user["id"], default_command="pi"
        )
        assert updated is False

    async def test_update_multiple_fields(self, workspace, user):
        await model.update_workspace(
            workspace["id"],
            user["id"],
            name="renamed",
            default_command="pi",
        )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["name"] == "renamed"
        assert ws["default_command"] == "pi"

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
        wss = await model.list_workspaces(user["id"])
        match = [w for w in wss if w["name"] == "mount-list"]
        assert match[0]["mounts"] == mounts

    async def test_update_ignores_unknown_fields(self, workspace, user):
        result = await model.update_workspace(
            workspace["id"], user["id"], bogus="ignored"
        )
        assert result is False

    async def test_update_no_fields(self, workspace, user):
        result = await model.update_workspace(workspace["id"], user["id"])
        assert result is False

    async def test_list_includes_default_command(self, user):
        await model.create_workspace(
            user["id"], "cmd-ws", default_command="pi"
        )
        wss = await model.list_workspaces(user["id"])
        match = [w for w in wss if w["name"] == "cmd-ws"]
        assert len(match) == 1
        assert match[0]["default_command"] == "pi"


class TestWriteDefaultCommand:
    def test_write_and_clear(self, tmp_path, monkeypatch):
        from klangk_backend import workspaces

        monkeypatch.setattr(workspaces, "WORKSPACES_ROOT", tmp_path)
        workspaces.write_default_command("u1", "ws1", "pi")
        cmd_file = tmp_path / "u1" / "config" / "ws1" / "default-command"
        assert cmd_file.read_text() == "pi"

        workspaces.write_default_command("u1", "ws1", None)
        assert not cmd_file.exists()

    def test_clear_nonexistent(self, tmp_path, monkeypatch):
        from klangk_backend import workspaces

        monkeypatch.setattr(workspaces, "WORKSPACES_ROOT", tmp_path)
        # Should not raise even if file doesn't exist
        workspaces.write_default_command("u1", "ws1", None)


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


class TestChatMessagesMigration:
    async def test_migrate_adds_message_type_column(self, db):
        """init_db adds message_type column to existing tables that lack it."""
        raw_db = await model.get_db()
        try:
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
            await raw_db.commit()
        finally:
            await raw_db.close()

        # Re-run init_db — should add message_type via ALTER TABLE
        await model.init_db()

        migrated_db = await model.get_db()
        try:
            cursor = await migrated_db.execute(
                "PRAGMA table_info(chat_messages)"
            )
            cols = {row[1] for row in await cursor.fetchall()}
            assert "message_type" in cols
        finally:
            await migrated_db.close()


class TestChatMessages:
    async def test_add_chat_message(self, workspace, user):
        msg = await model.add_chat_message(
            workspace["id"], user["id"], "testuser@example.com", "hello"
        )
        assert msg["workspace_id"] == workspace["id"]
        assert msg["user_id"] == user["id"]
        assert msg["user_email"] == "testuser@example.com"
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
            workspace["id"], "uid-a", "a@test.com", "first"
        )
        await model.add_chat_message(
            workspace["id"], "uid-b", "b@test.com", "second"
        )
        msgs = await model.get_chat_messages(workspace["id"])
        assert len(msgs) == 2
        assert msgs[0]["message"] == "first"
        assert msgs[1]["message"] == "second"
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


class TestChatMentions:
    async def test_mention_workspace_owner(self, workspace, user):
        """@mentioning the workspace owner resolves to their user ID."""
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"hello @{user['email']}",
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
            "hey @member@test.com check this",
        )
        assert msg["mentions"] == [member["id"]]

    async def test_mention_non_member_ignored(self, workspace, user):
        """@mentioning someone not in the workspace produces no mentions."""
        await model.create_user("outsider@test.com", "hash", verified=True)
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            "hey @outsider@test.com",
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
            f"@{user['email']} and @m@test.com",
        )
        assert set(msg["mentions"]) == {user["id"], member["id"]}

    async def test_mention_deduplication(self, workspace, user):
        """Duplicate @mentions produce only one entry."""
        msg = await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"@{user['email']} @{user['email']}",
        )
        assert msg["mentions"] == [user["id"]]

    async def test_mentions_in_history(self, workspace, user):
        """get_chat_messages includes mentions from the DB."""
        await model.add_chat_message(
            workspace["id"],
            user["id"],
            user["email"],
            f"hello @{user['email']}",
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
        invs = await model.list_invitations()
        assert len(invs) >= 2
        emails = [i["email"] for i in invs]
        assert "x@b.com" in emails
        assert "y@b.com" in emails
        assert invs[0]["invited_by_email"] == "testadmin@example.com"

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
