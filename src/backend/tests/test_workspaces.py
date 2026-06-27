"""Tests for workspaces: workspace lifecycle, directory management, port allocation."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import workspaces as ws_mod, container


class TestCreateWorkspace:
    async def test_creates_workspace_and_dirs(self, user):
        ws = await ws_mod.create_workspace(user["id"], "my-ws")
        assert ws["name"] == "my-ws"
        assert ws["user_id"] == user["id"]
        assert "id" in ws

        data_path = ws_mod.workspace_path(user["id"], ws["id"])
        assert data_path.exists()
        assert data_path.is_dir()

        home_dir = ws_mod.home_path(user["id"], ws["id"])
        assert home_dir.exists()
        assert home_dir.is_dir()

        users_dir = home_dir / ".users"
        assert users_dir.exists()
        assert users_dir.is_dir()

    async def test_allocates_ports(self, user):
        ws = await ws_mod.create_workspace(user["id"], "ported")
        ports = await container.registry.get_workspace_ports(ws["id"])
        assert len(ports) == ws["num_ports"]
        assert all(p >= container.PORT_RANGE_START for p in ports)

    async def test_duplicate_name_fails(self, user):
        await ws_mod.create_workspace(user["id"], "unique")
        with pytest.raises(Exception):
            await ws_mod.create_workspace(user["id"], "unique")

    async def test_allocate_ports_failure_cleans_up(self, user):
        """If allocate_ports raises, DB record and directories are removed."""
        with patch.object(
            container.registry,
            "allocate_ports",
            new_callable=AsyncMock,
            side_effect=RuntimeError("port exhaustion"),
        ):
            with pytest.raises(RuntimeError, match="port exhaustion"):
                await ws_mod.create_workspace(user["id"], "boom")

        # DB record should have been cleaned up
        result = await ws_mod.list_workspaces(user["id"])
        assert all(ws["name"] != "boom" for ws in result)

        # Name should be reusable (proves full cleanup)
        ws = await ws_mod.create_workspace(user["id"], "boom")
        assert ws["name"] == "boom"


class TestListWorkspaces:
    async def test_list_empty(self, user):
        result = await ws_mod.list_workspaces(user["id"])
        assert result == []

    async def test_list_multiple(self, user):
        await ws_mod.create_workspace(user["id"], "ws-a")
        await ws_mod.create_workspace(user["id"], "ws-b")
        result = await ws_mod.list_workspaces(user["id"])
        names = [ws["name"] for ws in result]
        assert "ws-a" in names
        assert "ws-b" in names
        assert len(result) == 2


class TestGetWorkspace:
    async def test_get_existing(self, user):
        ws = await ws_mod.create_workspace(user["id"], "findme")
        found = await ws_mod.get_workspace(ws["id"], user["id"])
        assert found is not None
        assert found["name"] == "findme"

    async def test_get_nonexistent(self, user):
        found = await ws_mod.get_workspace("fake-id", user["id"])
        assert found is None

    async def test_get_wrong_user(self, user):
        ws = await ws_mod.create_workspace(user["id"], "mine")
        found = await ws_mod.get_workspace(ws["id"], "other-user")
        assert found is None


class TestDeleteWorkspace:
    async def test_delete_removes_db_and_dirs(self, user):
        ws = await ws_mod.create_workspace(user["id"], "doomed")
        data_path = ws_mod.workspace_path(user["id"], ws["id"])
        home_dir = ws_mod.home_path(user["id"], ws["id"])
        (data_path / "file.txt").write_text("hello")
        (home_dir / ".bashrc").write_text("# custom")

        deleted = await ws_mod.delete_workspace(ws["id"], user["id"])
        assert deleted is True
        assert await ws_mod.get_workspace(ws["id"], user["id"]) is None
        assert not data_path.exists()
        assert not home_dir.exists()

    async def test_delete_nonexistent(self, user):
        deleted = await ws_mod.delete_workspace("fake-id", user["id"])
        assert deleted is False

    async def test_delete_cascades_ports(self, user):
        ws = await ws_mod.create_workspace(user["id"], "ported")
        ports_before = await container.registry.get_workspace_ports(ws["id"])
        assert len(ports_before) > 0

        await ws_mod.delete_workspace(ws["id"], user["id"])
        ports_after = await container.registry.get_workspace_ports(ws["id"])
        assert ports_after == []

    async def test_delete_missing_dirs_ok(self, user):
        ws = await ws_mod.create_workspace(user["id"], "no-dirs")
        home_dir = ws_mod.home_path(user["id"], ws["id"])
        import shutil

        shutil.rmtree(home_dir)

        deleted = await ws_mod.delete_workspace(ws["id"], user["id"])
        assert deleted is True


class TestHostPaths:
    def test_workspace_host_path_creates_dir(self, user, temp_data_dir):
        path = ws_mod.get_workspace_host_path(user["id"], "ws-1")
        assert path.exists()
        assert path.is_dir()

    def test_home_host_path_creates_dir(self, user, temp_data_dir):
        path = ws_mod.get_home_host_path(user["id"], "ws-1")
        assert path.exists()
        assert path.is_dir()

    def test_workspace_host_path_idempotent(self, user, temp_data_dir):
        path1 = ws_mod.get_workspace_host_path(user["id"], "ws-1")
        path2 = ws_mod.get_workspace_host_path(user["id"], "ws-1")
        assert path1 == path2

    def test_paths_are_under_data_dir(self, user, temp_data_dir):
        path = ws_mod.get_workspace_host_path(user["id"], "ws-1")
        assert str(path).startswith(str(temp_data_dir))

        home = ws_mod.get_home_host_path(user["id"], "ws-1")
        assert str(home).startswith(str(temp_data_dir))


class TestEnsureHomeSymlink:
    async def test_creates_symlink(self, user):
        ws = await ws_mod.create_workspace(user["id"], "symlink-ws")
        home = ws_mod.home_path(user["id"], ws["id"])
        result, created = ws_mod.ensure_home_symlink(home, "alice", "uid-1")
        assert result == "/home/alice"
        assert created is True
        symlink = home / "alice"
        assert symlink.is_symlink()
        assert os.readlink(symlink) == ".users/uid-1"
        assert (home / ".users" / "uid-1").is_dir()

    async def test_idempotent(self, user):
        ws = await ws_mod.create_workspace(user["id"], "symlink-ws2")
        home = ws_mod.home_path(user["id"], ws["id"])
        ws_mod.ensure_home_symlink(home, "bob", "uid-1")
        result, created = ws_mod.ensure_home_symlink(home, "bob", "uid-1")
        assert result == "/home/bob"
        assert created is False

    async def test_rename_removes_old_symlink(self, user):
        ws = await ws_mod.create_workspace(user["id"], "symlink-ws3")
        home = ws_mod.home_path(user["id"], ws["id"])
        ws_mod.ensure_home_symlink(home, "alice", "uid-1")
        result, created = ws_mod.ensure_home_symlink(home, "alicia", "uid-1")
        assert result == "/home/alicia"
        assert created is False
        assert not (home / "alice").exists()
        assert (home / "alicia").is_symlink()

    async def test_replaces_stale_symlink_from_import(self, user):
        """Imported workspace has a symlink for a different user ID.

        The old user's files should be adopted into the new user dir.
        """
        ws = await ws_mod.create_workspace(user["id"], "symlink-ws4")
        home = ws_mod.home_path(user["id"], ws["id"])
        # Simulate imported workspace: symlink for old user ID with files.
        (home / ".users").mkdir(parents=True, exist_ok=True)
        old_dir = home / ".users" / "old-uid"
        old_dir.mkdir()
        (old_dir / ".bashrc").write_text("# old bashrc")
        (old_dir / ".profile").write_text("# old profile")
        (home / "admin").symlink_to(".users/old-uid")
        # New user connects — different user ID, same handle.
        result, created = ws_mod.ensure_home_symlink(home, "admin", "new-uid")
        assert result == "/home/admin"
        assert created is False  # content adopted, no skel needed
        assert (home / "admin").is_symlink()
        assert os.readlink(home / "admin") == ".users/new-uid"
        # Files were moved from old-uid to new-uid.
        new_dir = home / ".users" / "new-uid"
        assert (new_dir / ".bashrc").read_text() == "# old bashrc"
        assert (new_dir / ".profile").read_text() == "# old profile"


class TestPopulateHomeSkel:
    async def test_execs_setup_home(self):
        """populate_home_skel runs podman exec with klangk-setup-home script."""
        with patch(
            "klangk_backend.workspaces.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_exec:
            await ws_mod.populate_home_skel("cid-123", "uid-456")
        mock_exec.assert_awaited_once_with(
            "cid-123",
            ["/opt/klangk/bin/klangk-setup-home", "/home/.users/uid-456"],
            user="klangk",
            timeout=10,
        )

    async def test_logs_warning_on_failure(self):
        """populate_home_skel logs but does not raise on failure."""
        with patch(
            "klangk_backend.workspaces.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=OSError("podman not found"),
        ):
            # Should not raise
            await ws_mod.populate_home_skel("cid-123", "uid-456")
