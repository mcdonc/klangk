"""Tests for user_store: users, workspaces, messages, port allocations."""

import pytest

from bark_backend import user_store


class TestUsers:
    async def test_create_user(self, db):
        user = await user_store.create_user("alice@example.com", "hash123")
        assert user["email"] == "alice@example.com"
        assert "id" in user

    async def test_get_user_by_email(self, user):
        found = await user_store.get_user_by_email("testuser@example.com")
        assert found is not None
        assert found["id"] == user["id"]

    async def test_get_user_by_email_not_found(self, db):
        found = await user_store.get_user_by_email("nonexistent")
        assert found is None

    async def test_get_user_by_id(self, user):
        found = await user_store.get_user_by_id(user["id"])
        assert found is not None
        assert found["email"] == "testuser@example.com"

    async def test_get_user_by_id_not_found(self, db):
        found = await user_store.get_user_by_id("fake-id")
        assert found is None


class TestWorkspaces:
    async def test_create_workspace(self, user):
        ws = await user_store.create_workspace(user["id"], "my-workspace")
        assert ws["name"] == "my-workspace"
        assert ws["user_id"] == user["id"]
        assert "id" in ws

    async def test_list_workspaces(self, user):
        await user_store.create_workspace(user["id"], "ws1")
        await user_store.create_workspace(user["id"], "ws2")
        workspaces = await user_store.list_workspaces(user["id"])
        names = [ws["name"] for ws in workspaces]
        assert "ws1" in names
        assert "ws2" in names

    async def test_get_workspace(self, workspace, user):
        found = await user_store.get_workspace(workspace["id"], user["id"])
        assert found is not None
        assert found["name"] == "test-workspace"

    async def test_get_workspace_wrong_user(self, workspace):
        found = await user_store.get_workspace(
            workspace["id"], "wrong-user-id"
        )
        assert found is None

    async def test_delete_workspace(self, workspace, user):
        deleted = await user_store.delete_workspace(
            workspace["id"], user["id"]
        )
        assert deleted is True
        found = await user_store.get_workspace(workspace["id"], user["id"])
        assert found is None

    async def test_delete_workspace_not_found(self, user):
        deleted = await user_store.delete_workspace("fake-id", user["id"])
        assert deleted is False

    async def test_duplicate_workspace_name(self, user):
        await user_store.create_workspace(user["id"], "unique-name")
        with pytest.raises(Exception):
            await user_store.create_workspace(user["id"], "unique-name")


class TestMessages:
    async def test_save_and_get_messages(self, workspace):
        await user_store.save_message(workspace["id"], "user", "hello")
        await user_store.save_message(workspace["id"], "assistant", "hi there")
        messages = await user_store.get_messages(workspace["id"])
        assert len(messages) == 2
        assert messages[0]["entry_type"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["entry_type"] == "assistant"

    async def test_messages_cascade_on_workspace_delete(self, workspace, user):
        await user_store.save_message(workspace["id"], "user", "test")
        await user_store.delete_workspace(workspace["id"], user["id"])
        messages = await user_store.get_messages(workspace["id"])
        assert len(messages) == 0


class TestPortAllocations:
    async def test_add_and_get_ports(self, workspace):
        await user_store.add_port_allocations(
            workspace["id"], [9000, 9001, 9002]
        )
        ports = await user_store.get_workspace_ports(workspace["id"])
        assert ports == [9000, 9001, 9002]

    async def test_get_all_allocated_ports(self, workspace):
        await user_store.add_port_allocations(workspace["id"], [9000, 9001])
        all_ports = await user_store.get_all_allocated_ports()
        assert 9000 in all_ports
        assert 9001 in all_ports

    async def test_remove_ports(self, workspace):
        await user_store.add_port_allocations(
            workspace["id"], [9000, 9001, 9002]
        )
        await user_store.remove_port_allocations(workspace["id"], [9001])
        ports = await user_store.get_workspace_ports(workspace["id"])
        assert ports == [9000, 9002]

    async def test_ports_cascade_on_workspace_delete(self, workspace, user):
        await user_store.add_port_allocations(workspace["id"], [9000, 9001])
        await user_store.delete_workspace(workspace["id"], user["id"])
        ports = await user_store.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_duplicate_port_rejected(self, workspace, user):
        await user_store.add_port_allocations(workspace["id"], [9000])
        # Create second workspace
        ws2 = await user_store.create_workspace(user["id"], "ws2")
        with pytest.raises(Exception):
            await user_store.add_port_allocations(ws2["id"], [9000])

    async def test_get_workspace_ports_empty(self, workspace):
        ports = await user_store.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_get_all_allocated_ports_empty(self, db):
        all_ports = await user_store.get_all_allocated_ports()
        assert all_ports == set()

    async def test_find_and_allocate_ports(self, workspace):
        ports = await user_store.find_and_allocate_ports(
            workspace["id"], 3, 9000
        )
        assert ports == [9000, 9001, 9002]
        stored = await user_store.get_workspace_ports(workspace["id"])
        assert stored == [9000, 9001, 9002]

    async def test_find_and_allocate_skips_used(self, workspace, user):
        await user_store.add_port_allocations(workspace["id"], [9000, 9002])
        ws2 = await user_store.create_workspace(user["id"], "ws2")
        ports = await user_store.find_and_allocate_ports(ws2["id"], 3, 9000)
        assert ports == [9001, 9003, 9004]


class TestContainerTracking:
    async def test_update_workspace_container(self, workspace, user):
        await user_store.update_workspace_container(
            workspace["id"], "container-123"
        )
        ws = await user_store.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] == "container-123"

    async def test_clear_workspace_container(self, workspace, user):
        await user_store.update_workspace_container(
            workspace["id"], "container-123"
        )
        await user_store.update_workspace_container(workspace["id"], None)
        ws = await user_store.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] is None

    async def test_get_user_workspaces_with_containers(self, user):
        ws1 = await user_store.create_workspace(user["id"], "ws-c1")
        ws2 = await user_store.create_workspace(user["id"], "ws-c2")
        await user_store.create_workspace(user["id"], "ws-c3")  # no container
        await user_store.update_workspace_container(ws1["id"], "cid-1")
        await user_store.update_workspace_container(ws2["id"], "cid-2")
        result = await user_store.get_user_workspaces_with_containers(
            user["id"]
        )
        ids = {r["id"] for r in result}
        assert ws1["id"] in ids
        assert ws2["id"] in ids
        assert len(result) == 2
        for r in result:
            assert r["container_id"] is not None

    async def test_get_user_workspaces_with_containers_empty(self, user):
        result = await user_store.get_user_workspaces_with_containers(
            user["id"]
        )
        assert result == []


class TestTokenBlocklist:
    async def test_blocklist_and_check(self, db):
        await user_store.blocklist_token("jti-1", "2099-01-01T00:00:00Z")
        assert await user_store.is_token_blocklisted("jti-1") is True

    async def test_not_blocklisted(self, db):
        assert await user_store.is_token_blocklisted("jti-unknown") is False

    async def test_blocklist_duplicate_ignored(self, db):
        await user_store.blocklist_token("jti-2", "2099-01-01T00:00:00Z")
        # INSERT OR IGNORE should not raise
        await user_store.blocklist_token("jti-2", "2099-01-01T00:00:00Z")
        assert await user_store.is_token_blocklisted("jti-2") is True


class TestDeleteWorkspaceMessages:
    async def test_delete_messages(self, workspace):
        await user_store.save_message(workspace["id"], "user", "msg1")
        await user_store.save_message(workspace["id"], "assistant", "msg2")
        await user_store.delete_workspace_messages(workspace["id"])
        messages = await user_store.get_messages(workspace["id"])
        assert messages == []

    async def test_delete_messages_empty(self, workspace):
        # Should not raise when no messages exist
        await user_store.delete_workspace_messages(workspace["id"])
        messages = await user_store.get_messages(workspace["id"])
        assert messages == []


class TestMessageDetails:
    async def test_save_message_with_tool_data(self, workspace):
        msg_id = await user_store.save_message(
            workspace["id"],
            "tool",
            "tool_call",
            tool_args='{"cmd": "ls"}',
            tool_output="file.txt",
            is_complete=True,
        )
        assert msg_id > 0
        messages = await user_store.get_messages(workspace["id"])
        msg = messages[0]
        assert msg["tool_args"] == '{"cmd": "ls"}'
        assert msg["tool_output"] == "file.txt"
        assert msg["is_complete"] is True
        assert msg["is_queued"] is False

    async def test_save_queued_message(self, workspace):
        await user_store.save_message(
            workspace["id"], "user", "queued msg", is_queued=True
        )
        messages = await user_store.get_messages(workspace["id"])
        assert messages[0]["is_queued"] is True

    async def test_messages_ordered_by_id(self, workspace):
        await user_store.save_message(workspace["id"], "user", "first")
        await user_store.save_message(workspace["id"], "assistant", "second")
        await user_store.save_message(workspace["id"], "user", "third")
        messages = await user_store.get_messages(workspace["id"])
        contents = [m["content"] for m in messages]
        assert contents == ["first", "second", "third"]

    async def test_messages_have_created_at(self, workspace):
        await user_store.save_message(workspace["id"], "user", "hello")
        messages = await user_store.get_messages(workspace["id"])
        assert messages[0]["created_at"] is not None
