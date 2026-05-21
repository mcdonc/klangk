"""Tests for workspace_manager: workspace lifecycle, directory management, port allocation."""

import pytest

from bark_backend import workspace_manager, container_manager


class TestCreateWorkspace:
    async def test_creates_workspace_and_dirs(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "my-ws")
        assert ws["name"] == "my-ws"
        assert ws["user_id"] == user["id"]
        assert "id" in ws

        data_path = workspace_manager._workspace_path(user["id"], ws["id"])
        assert data_path.exists()
        assert data_path.is_dir()

        sessions_path = workspace_manager._sessions_path(user["id"], ws["id"])
        assert sessions_path.exists()
        assert sessions_path.is_dir()

    async def test_allocates_ports(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "ported")
        ports = await container_manager.get_workspace_ports(ws["id"])
        assert len(ports) == ws["num_ports"]
        assert all(p >= container_manager.PORT_RANGE_START for p in ports)

    async def test_duplicate_name_fails(self, user):
        await workspace_manager.create_workspace(user["id"], "unique")
        with pytest.raises(Exception):
            await workspace_manager.create_workspace(user["id"], "unique")


class TestListWorkspaces:
    async def test_list_empty(self, user):
        result = await workspace_manager.list_workspaces(user["id"])
        assert result == []

    async def test_list_multiple(self, user):
        await workspace_manager.create_workspace(user["id"], "ws-a")
        await workspace_manager.create_workspace(user["id"], "ws-b")
        result = await workspace_manager.list_workspaces(user["id"])
        names = [ws["name"] for ws in result]
        assert "ws-a" in names
        assert "ws-b" in names
        assert len(result) == 2


class TestGetWorkspace:
    async def test_get_existing(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "findme")
        found = await workspace_manager.get_workspace(ws["id"], user["id"])
        assert found is not None
        assert found["name"] == "findme"

    async def test_get_nonexistent(self, user):
        found = await workspace_manager.get_workspace("fake-id", user["id"])
        assert found is None

    async def test_get_wrong_user(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "mine")
        found = await workspace_manager.get_workspace(ws["id"], "other-user")
        assert found is None


class TestDeleteWorkspace:
    async def test_delete_removes_db_and_dirs(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "doomed")
        data_path = workspace_manager._workspace_path(user["id"], ws["id"])
        sessions_path = workspace_manager._sessions_path(user["id"], ws["id"])
        (data_path / "file.txt").write_text("hello")

        deleted = await workspace_manager.delete_workspace(
            ws["id"], user["id"]
        )
        assert deleted is True
        assert (
            await workspace_manager.get_workspace(ws["id"], user["id"]) is None
        )
        assert not data_path.exists()
        assert not sessions_path.exists()

    async def test_delete_nonexistent(self, user):
        deleted = await workspace_manager.delete_workspace(
            "fake-id", user["id"]
        )
        assert deleted is False

    async def test_delete_cascades_ports(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "ported")
        ports_before = await container_manager.get_workspace_ports(ws["id"])
        assert len(ports_before) > 0

        await workspace_manager.delete_workspace(ws["id"], user["id"])
        ports_after = await container_manager.get_workspace_ports(ws["id"])
        assert ports_after == []

    async def test_delete_missing_dirs_ok(self, user):
        ws = await workspace_manager.create_workspace(user["id"], "no-dirs")
        data_path = workspace_manager._workspace_path(user["id"], ws["id"])
        sessions_path = workspace_manager._sessions_path(user["id"], ws["id"])
        data_path.rmdir()
        sessions_path.rmdir()

        deleted = await workspace_manager.delete_workspace(
            ws["id"], user["id"]
        )
        assert deleted is True


class TestHostPaths:
    def test_workspace_host_path_creates_dir(self, user, temp_data_dir):
        path = workspace_manager.get_workspace_host_path(user["id"], "ws-1")
        assert path.exists()
        assert path.is_dir()

    def test_sessions_host_path_creates_dir(self, user, temp_data_dir):
        path = workspace_manager.get_sessions_host_path(user["id"], "ws-1")
        assert path.exists()
        assert path.is_dir()

    def test_workspace_host_path_idempotent(self, user, temp_data_dir):
        path1 = workspace_manager.get_workspace_host_path(user["id"], "ws-1")
        path2 = workspace_manager.get_workspace_host_path(user["id"], "ws-1")
        assert path1 == path2

    def test_paths_are_under_data_dir(self, user, temp_data_dir):
        path = workspace_manager.get_workspace_host_path(user["id"], "ws-1")
        assert str(path).startswith(str(temp_data_dir))

        sessions = workspace_manager.get_sessions_host_path(user["id"], "ws-1")
        assert str(sessions).startswith(str(temp_data_dir))
