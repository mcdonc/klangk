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


class TestSuggestHandle:
    def test_email_local_part(self):
        assert ws_mod.suggest_handle("alice@example.com") == "alice"

    def test_sanitizes_special_chars(self):
        assert ws_mod.suggest_handle("Alice+Dev@foo.com") == "alicedev"

    def test_preserves_dots_dashes_underscores(self):
        assert ws_mod.suggest_handle("bob.smith@foo.com") == "bob.smith"
        assert ws_mod.suggest_handle("bob-smith@foo.com") == "bob-smith"
        assert ws_mod.suggest_handle("bob_smith@foo.com") == "bob_smith"

    def test_empty_local_part_fallback(self):
        assert ws_mod.suggest_handle("@foo.com") == "user"

    def test_no_at_sign(self):
        assert ws_mod.suggest_handle("admin") == "admin"

    def test_truncates_long_handle(self):
        result = ws_mod.suggest_handle("a" * 100 + "@foo.com")
        assert len(result) <= ws_mod._MAX_HANDLE_LEN


class TestSetUserHandle:
    async def test_creates_handle(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws")
        result = ws_mod.set_user_handle(
            user["id"], ws["id"], "user-uuid-1", "alice"
        )
        assert result == "/home/alice"
        home = ws_mod.home_path(user["id"], ws["id"])
        symlink = home / "alice"
        assert symlink.is_symlink()
        assert os.readlink(symlink) == ".users/user-uuid-1"
        assert (home / ".users" / "user-uuid-1").is_dir()

    async def test_idempotent(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws2")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "bob")
        result = ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "bob")
        assert result == "/home/bob"

    async def test_conflict_raises(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws3")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "alice")
        with pytest.raises(ValueError, match="already taken"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-2", "alice")

    async def test_rename_handle(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws4")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "alice")
        result = ws_mod.set_user_handle(
            user["id"], ws["id"], "uid-1", "alicia"
        )
        assert result == "/home/alicia"
        home = ws_mod.home_path(user["id"], ws["id"])
        assert not (home / "alice").exists()
        assert (home / "alicia").is_symlink()

    async def test_reserved_name_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws5")
        with pytest.raises(ValueError, match="reserved"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "work")

    async def test_dot_prefix_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws6")
        with pytest.raises(ValueError, match="dot"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", ".hidden")

    async def test_invalid_chars_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws7")
        with pytest.raises(ValueError, match="lowercase"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "Alice")
        with pytest.raises(ValueError, match="lowercase"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "a b")

    async def test_empty_handle_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws8")
        with pytest.raises(ValueError, match="empty"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "")

    async def test_too_long_handle_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws9")
        with pytest.raises(ValueError, match="characters"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "a" * 100)

    async def test_file_conflict_rejected(self, user):
        ws = await ws_mod.create_workspace(user["id"], "handle-ws10")
        home = ws_mod.home_path(user["id"], ws["id"])
        (home / "taken").mkdir()
        with pytest.raises(ValueError, match="conflicts"):
            ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "taken")


class TestGetUserHandle:
    async def test_returns_handle(self, user):
        ws = await ws_mod.create_workspace(user["id"], "get-handle")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "alice")
        handle = ws_mod.get_user_handle(user["id"], ws["id"], "uid-1")
        assert handle == "alice"

    async def test_returns_none_when_absent(self, user):
        ws = await ws_mod.create_workspace(user["id"], "get-handle2")
        handle = ws_mod.get_user_handle(user["id"], ws["id"], "uid-1")
        assert handle is None

    async def test_returns_none_for_nonexistent_workspace(self, user):
        handle = ws_mod.get_user_handle(user["id"], "fake-ws", "uid-1")
        assert handle is None


class TestSuggestAlternative:
    async def test_suggests_suffixed_name(self, user):
        ws = await ws_mod.create_workspace(user["id"], "alt-ws")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "alice")
        alt = ws_mod.suggest_alternative(user["id"], ws["id"], "alice")
        assert alt == "alice-2"

    async def test_skips_taken_suffixes(self, user):
        ws = await ws_mod.create_workspace(user["id"], "alt-ws2")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-1", "alice")
        ws_mod.set_user_handle(user["id"], ws["id"], "uid-2", "alice-2")
        alt = ws_mod.suggest_alternative(user["id"], ws["id"], "alice")
        assert alt == "alice-3"

    async def test_truncates_long_handle(self, user):
        ws = await ws_mod.create_workspace(user["id"], "alt-ws3")
        long_handle = "a" * ws_mod._MAX_HANDLE_LEN
        # Create a symlink with the long handle to force suffix
        ws_mod.set_user_handle(
            user["id"],
            ws["id"],
            "uid-1",
            long_handle[: ws_mod._MAX_HANDLE_LEN],
        )
        alt = ws_mod.suggest_alternative(
            user["id"], ws["id"], long_handle[: ws_mod._MAX_HANDLE_LEN]
        )
        assert len(alt) <= ws_mod._MAX_HANDLE_LEN
        assert alt.endswith("-2")
