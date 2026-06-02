"""Tests for model: users, workspaces, messages, port allocations."""

import pytest

from klangk_backend import model


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
    async def test_share_workspace(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await model.share_workspace(workspace["id"], other["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert len(members) == 1
        assert members[0]["id"] == other["id"]
        assert members[0]["email"] == "other@example.com"

    async def test_get_workspace_shared_user(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        # Before sharing, other cannot access
        found = await model.get_workspace(workspace["id"], other["id"])
        assert found is None
        # After sharing, other can access
        await model.share_workspace(workspace["id"], other["id"])
        found = await model.get_workspace(workspace["id"], other["id"])
        assert found is not None
        assert found["name"] == "test-workspace"

    async def test_get_workspace_still_none_for_unshared(
        self, workspace, user
    ):
        other = await model.create_user("other@example.com", "hash")
        found = await model.get_workspace(workspace["id"], other["id"])
        assert found is None

    async def test_unshare_workspace(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await model.share_workspace(workspace["id"], other["id"])
        await model.unshare_workspace(workspace["id"], other["id"])
        found = await model.get_workspace(workspace["id"], other["id"])
        assert found is None
        members = await model.get_workspace_members(workspace["id"])
        assert len(members) == 0

    async def test_get_workspace_members_empty(self, workspace):
        members = await model.get_workspace_members(workspace["id"])
        assert members == []

    async def test_share_workspace_idempotent(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await model.share_workspace(workspace["id"], other["id"])
        await model.share_workspace(workspace["id"], other["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert len(members) == 1

    async def test_get_workspace_members_ordered(self, workspace, user):
        u_b = await model.create_user("b@example.com", "hash")
        u_a = await model.create_user("a@example.com", "hash")
        await model.share_workspace(workspace["id"], u_b["id"])
        await model.share_workspace(workspace["id"], u_a["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert members[0]["email"] == "a@example.com"
        assert members[1]["email"] == "b@example.com"

    async def test_workspace_access_cascade_on_delete(self, workspace, user):
        other = await model.create_user("other@example.com", "hash")
        await model.share_workspace(workspace["id"], other["id"])
        await model.delete_workspace(workspace["id"], user["id"])
        members = await model.get_workspace_members(workspace["id"])
        assert members == []


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
