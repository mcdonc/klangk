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

    async def test_invalid_setup_state_rejected(self, user):
        """Invalid setup_state raises ValueError (#1033)."""
        from klangk_backend import model

        # Service layer (goes through create_workspace_with_acl).
        with pytest.raises(ValueError, match="Invalid setup_state"):
            await ws_mod.create_workspace(
                user["id"], "bad-state", setup_state="bogus"
            )
        # Row-only model primitive validates the same way.
        with pytest.raises(ValueError, match="Invalid setup_state"):
            await model.create_workspace(
                user["id"], "bad-row", setup_state="bogus"
            )

    async def test_setup_state_defaults_to_complete(self, user):
        """Workspaces without a setup command default to 'complete'."""
        ws = await ws_mod.create_workspace(user["id"], "default-state")
        assert ws["setup_state"] == "complete"

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
        assert all(ws["name"] != "boom" for ws in result["items"])

        # Name should be reusable (proves full cleanup)
        ws = await ws_mod.create_workspace(user["id"], "boom")
        assert ws["name"] == "boom"


async def test_update_workspace_invalid_setup_state_rejected(user):
    """update_workspace rejects an invalid setup_state (#1033)."""
    from klangk_backend.model import update_workspace

    ws = await ws_mod.create_workspace(user["id"], "upd-state")
    with pytest.raises(ValueError, match="Invalid setup_state"):
        await update_workspace(ws["id"], ws["user_id"], setup_state="bogus")


async def test_update_workspace_sets_setup_state(user):
    """update_workspace can transition setup_state (#1033)."""
    from klangk_backend.model import get_workspace, update_workspace

    ws = await ws_mod.create_workspace(
        user["id"], "upd-ok", setup_state="pending"
    )
    assert ws["setup_state"] == "pending"
    await update_workspace(ws["id"], ws["user_id"], setup_state="complete")
    refreshed = await get_workspace(ws["id"])
    assert refreshed["setup_state"] == "complete"


async def test_create_workspace_with_acl_seeds_owner_and_role_groups(user):
    """create_workspace_with_acl seeds the owner ACE + 4 role groups (#128)."""
    from klangk_backend import model

    ws = await model.create_workspace_with_acl(user["id"], "seeded")
    resource = f"/workspaces/{ws['id']}"

    # Owner ACE at position 0 grants the creator everything.
    entries = await model.get_acl_entries(resource)
    owner_aces = [
        e
        for e in entries
        if e["principal_type"] == model.PRINCIPAL_USER
        and e["user_id"] == user["id"]
    ]
    assert len(owner_aces) == 1
    assert owner_aces[0]["position"] == 0
    assert owner_aces[0]["permission"] == "*"

    # All four role groups exist and the creator is in owners.
    for suffix in ["owners", "coders", "collaborators", "spectators"]:
        group = await model.get_group_by_name(f"{suffix}-{ws['id']}")
        assert group is not None, f"expected {suffix} group"
    owner_group = await model.get_group_by_name(f"owners-{ws['id']}")
    assert owner_group["id"] in await model.get_user_group_ids(user["id"])

    # Position counter is global across all groups (no collisions).
    positions = sorted(e["position"] for e in entries)
    assert positions == list(range(len(entries)))
    # 1 owner ACE + 1 + 5 + 7 + 2 group ACEs.
    assert len(entries) == 1 + 1 + 5 + 7 + 2


async def test_create_workspace_with_acl_rollback_on_seeding_failure(user):
    """If ACL seeding fails, the row and any partial ACEs/groups are rolled
    back — nothing is orphaned (#128)."""
    from klangk_backend import model
    from klangk_backend.model import workspaces as model_ws

    captured: dict = {}

    async def _boom(db, ws, user_id):
        captured["id"] = ws["id"]
        raise RuntimeError("seeding boom")

    with patch.object(
        model_ws,
        "_seed_workspace_acl",
        new_callable=AsyncMock,
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError, match="seeding boom"):
            await model.create_workspace_with_acl(user["id"], "orphan-test")

    ws_id = captured["id"]
    resource = f"/workspaces/{ws_id}"

    # No workspace row, no ACL entries, no role groups left behind.
    assert await model.get_workspace(ws_id) is None
    assert await model.get_acl_entries(resource) == []
    for suffix in ["owners", "coders", "collaborators", "spectators"]:
        assert await model.get_group_by_name(f"{suffix}-{ws_id}") is None

    # Name is reusable — proves full cleanup of the row.
    ws = await model.create_workspace_with_acl(user["id"], "orphan-test")
    assert ws["name"] == "orphan-test"


class TestListWorkspaces:
    async def test_list_empty(self, user):
        result = await ws_mod.list_workspaces(user["id"])
        assert result == {
            "items": [],
            "has_more": False,
            "next_offset": None,
        }

    async def test_list_multiple(self, user):
        await ws_mod.create_workspace(user["id"], "ws-a")
        await ws_mod.create_workspace(user["id"], "ws-b")
        result = await ws_mod.list_workspaces(user["id"])
        names = [ws["name"] for ws in result["items"]]
        assert "ws-a" in names
        assert "ws-b" in names
        assert len(result["items"]) == 2


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


class TestAutoStartWorkspaces:
    async def test_returns_zero_when_env_not_set(self, user):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KLANGK_ALLOW_AUTOSTART", None)
            result = await ws_mod.auto_start_workspaces()
        assert result == 0

    async def test_starts_auto_start_workspaces(self, user):
        ws1 = await ws_mod.create_workspace(
            user["id"], "auto-ws1", auto_start=True
        )
        ws2 = await ws_mod.create_workspace(
            user["id"], "auto-ws2", auto_start=True
        )
        await ws_mod.create_workspace(user["id"], "normal-ws")

        # Pre-populate states so idle_timeout can be set.
        from klangk_backend.container import ContainerState

        container.registry.states[ws1["id"]] = ContainerState(
            ws1["id"], "cid-1"
        )
        container.registry.states[ws2["id"]] = ContainerState(
            ws2["id"], "cid-2"
        )
        try:
            with patch.dict(os.environ, {"KLANGK_ALLOW_AUTOSTART": "1"}):
                with patch.object(
                    container.registry,
                    "start_container",
                    new_callable=AsyncMock,
                    return_value=("cid-abc", "started"),
                ) as mock_start:
                    with patch(
                        "klangk_backend.workspaces.asyncio.sleep",
                        new_callable=AsyncMock,
                    ) as mock_sleep:
                        result = await ws_mod.auto_start_workspaces()
            assert result == 2
            assert mock_start.await_count == 2
            mock_sleep.assert_awaited_once()
            assert container.registry.states[ws1["id"]].idle_timeout == 0
            assert container.registry.states[ws2["id"]].idle_timeout == 0
        finally:
            container.registry.states.pop(ws1["id"], None)
            container.registry.states.pop(ws2["id"], None)

    async def test_handles_start_failure_gracefully(self, user):
        await ws_mod.create_workspace(user["id"], "fail-ws", auto_start=True)
        with patch.dict(os.environ, {"KLANGK_ALLOW_AUTOSTART": "1"}):
            with patch.object(
                container.registry,
                "start_container",
                new_callable=AsyncMock,
                side_effect=RuntimeError("container failed"),
            ):
                result = await ws_mod.auto_start_workspaces()
        assert result == 0


class TestStartWorkspace:
    """Tests for start_workspace: the thin dict-unpacking wrapper.

    The service-command firing and agent-home provisioning moved to
    the create choke point inside start_container (see bringup.bringup,
    #1244), and idle_timeout pinning moved to auto_start_workspaces
    (boot path only). So start_workspace itself only unpacks the
    workspace dict and delegates to registry.start_container.
    """

    async def test_unpacks_dict_and_starts_container(self, user):
        ws = await ws_mod.create_workspace(
            user["id"],
            "start-ws",
            auto_start=True,
            service_command="openclaw gateway",
        )
        try:
            with patch.object(
                container.registry,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-x", "created"),
            ) as mock_start:
                cid, status = await ws_mod.start_workspace(ws)
            assert cid == "cid-x"
            assert status == "created"
            mock_start.assert_awaited_once()
            # The service_command is threaded through to start_container
            # so the create choke point (bringup) can fire it.
            assert mock_start.call_args.kwargs["service_command"] == (
                "openclaw gateway"
            )
        finally:
            container.registry.states.pop(ws["id"], None)

    async def test_does_not_pin_idle_timeout(self, user):
        """Only the boot path (auto_start_workspaces) pins idle_timeout."""
        ws = await ws_mod.create_workspace(
            user["id"], "start-ws-no-idle", auto_start=True
        )
        from klangk_backend.container import ContainerState

        # Registry default idle timeout is non-zero; start_workspace
        # must not clobber it.
        default_timeout = ContainerState(ws["id"], "cid-y").idle_timeout
        container.registry.states[ws["id"]] = ContainerState(ws["id"], "cid-y")
        try:
            with patch.object(
                container.registry,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-y", "created"),
            ):
                await ws_mod.start_workspace(ws)
            assert (
                container.registry.states[ws["id"]].idle_timeout
                == default_timeout
            )
        finally:
            container.registry.states.pop(ws["id"], None)
