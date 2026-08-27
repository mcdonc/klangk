"""Tests for workspaces: workspace lifecycle, directory management, port allocation."""

import logging
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk import container
from klangk import model
from klangk import workspaces as ws_mod


class TestCreateWorkspace:
    async def test_creates_workspace_and_dirs(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "my-ws"
        )
        assert ws["name"] == "my-ws"
        assert ws["user_id"] == user["id"]
        assert "id" in ws

        data_path = app_state.state.workspaces.home_path(ws["id"])
        assert data_path.exists()
        assert data_path.is_dir()

        home_dir = app_state.state.workspaces.home_path(ws["id"])
        assert home_dir.exists()
        assert home_dir.is_dir()

        users_dir = home_dir / ".users"
        assert users_dir.exists()
        assert users_dir.is_dir()

    async def test_default_workspace_flows_interactive_to_start(
        self, user, app_state, monkeypatch
    ):
        # #2325: a workspace created via the model picks up the deploy default
        # egress_mode (interactive) and start_workspace forwards it to the
        # container registry. This pins the wiring the container tests (which
        # call start_container directly with an explicit egress_mode) can't
        # catch -- a regression that dropped the kwarg at workspaces.py's
        # start_workspace, or flipped its .get() default back to "static",
        # would silently start the workspace unrestricted with every other
        # test still green.
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "ws-interactive"
        )
        # The model default is interactive (EGRESS_MODE_DEFAULT).
        assert ws["egress_mode"] == "interactive"
        registry = app_state.state.container_registry
        fake = AsyncMock(return_value=("cid", "created"))
        monkeypatch.setattr(registry, "start_container", fake)
        await app_state.state.workspaces.start_workspace(ws)
        fake.assert_awaited_once()
        assert fake.call_args.args[0].egress_mode == "interactive"

    async def test_allocates_ports(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "ported"
        )
        registry = app_state.state.container_registry
        ports = await registry.get_workspace_ports(ws["id"])
        assert len(ports) == ws["num_ports"]
        assert all(
            p >= app_state.state.container_registry.port_range_start
            for p in ports
        )

    async def test_duplicate_name_fails(self, user, app_state):
        await app_state.state.workspaces.create_workspace(user["id"], "unique")
        with pytest.raises(Exception):
            await app_state.state.workspaces.create_workspace(
                user["id"], "unique"
            )

    async def test_existing_workspace_ids_delegates_to_model(
        self, user, app_state
    ):
        # The service wrapper delegates to the model; the live id-set is the
        # basis for the orphan sidecar-token sweep (container.py, #2309).
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "sweep-me"
        )
        ids = await app_state.state.workspaces.existing_workspace_ids()
        assert ws["id"] in ids
        await app_state.state.workspaces.delete_workspace(ws["id"], user["id"])
        assert ws["id"] not in (
            await app_state.state.workspaces.existing_workspace_ids()
        )

    async def test_invalid_setup_state_rejected(self, user, app_state):
        """Invalid setup_state raises ValueError (#1033)."""
        # Service layer (goes through create_workspace_with_acl).
        with pytest.raises(ValueError, match="Invalid setup_state"):
            await app_state.state.workspaces.create_workspace(
                user["id"],
                "bad-state",
                setup_state="bogus",
            )
        # Row-only model primitive validates the same way.
        with pytest.raises(ValueError, match="Invalid setup_state"):
            await app_state.state.model.workspaces.create_workspace(
                user["id"], "bad-row", setup_state="bogus"
            )

    async def test_setup_state_defaults_to_complete(self, user, app_state):
        """Workspaces without a setup command default to 'complete'."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "default-state"
        )
        assert ws["setup_state"] == "complete"

    async def test_allocate_ports_failure_cleans_up(self, user, app_state):
        """If allocate_ports raises, DB record and directories are removed."""
        registry = app_state.state.container_registry
        with patch.object(
            registry,
            "allocate_ports",
            new_callable=AsyncMock,
            side_effect=RuntimeError("port exhaustion"),
        ):
            with pytest.raises(RuntimeError, match="port exhaustion"):
                await app_state.state.workspaces.create_workspace(
                    user["id"], "boom"
                )

        # DB record should have been cleaned up
        result = await app_state.state.workspaces.list_workspaces(user["id"])
        assert all(ws["name"] != "boom" for ws in result["items"])

        # Name should be reusable (proves full cleanup)
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "boom"
        )
        assert ws["name"] == "boom"


async def test_update_workspace_invalid_setup_state_rejected(user, app_state):
    """update_workspace rejects an invalid setup_state (#1033)."""
    ws = await app_state.state.workspaces.create_workspace(
        user["id"], "upd-state"
    )
    with pytest.raises(ValueError, match="Invalid setup_state"):
        await app_state.state.model.workspaces.update_workspace(
            ws["id"], ws["user_id"], setup_state="bogus"
        )


async def test_update_workspace_sets_setup_state(user, app_state):
    """update_workspace can transition setup_state (#1033)."""
    ws = await app_state.state.workspaces.create_workspace(
        user["id"], "upd-ok", setup_state="pending"
    )
    assert ws["setup_state"] == "pending"
    await app_state.state.model.workspaces.update_workspace(
        ws["id"], ws["user_id"], setup_state="complete"
    )
    refreshed = await app_state.state.model.workspaces.get_workspace(ws["id"])
    assert refreshed["setup_state"] == "complete"


async def test_create_workspace_with_acl_seeds_owner_and_role_groups(
    user, app_state
):
    """create_workspace_with_acl seeds the owner ACE + 4 role groups (#128)."""
    from klangk import model

    ws = await app_state.state.model.workspaces.create_workspace_with_acl(
        user["id"], "seeded"
    )
    resource = f"/workspaces/{ws['id']}"

    # Owner ACE at position 0 grants the creator everything.
    entries = await app_state.state.model.acl.get_acl_entries(resource)
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
        group = await app_state.state.model.users.get_group_by_name(
            f"{suffix}-{ws['id']}"
        )
        assert group is not None, f"expected {suffix} group"
    owner_group = await app_state.state.model.users.get_group_by_name(
        f"owners-{ws['id']}"
    )
    assert owner_group[
        "id"
    ] in await app_state.state.model.users.get_user_group_ids(user["id"])

    # Position counter is global across all groups (no collisions).
    positions = sorted(e["position"] for e in entries)
    assert positions == list(range(len(entries)))
    # 1 owner ACE + 1 + 7 + 9 + 2 group ACEs (coders/collaborators carry
    # `files-download`/`files-write` alongside `files` (#2705) and
    # `exec-and-sync` (#2706/#2712)).
    assert len(entries) == 1 + 1 + 7 + 9 + 2
    # Coder/collaborator grants include both transfer permissions and
    # the exec-channel permission.
    for suffix in ["coders", "collaborators"]:
        group = await app_state.state.model.users.get_group_by_name(
            f"{suffix}-{ws['id']}"
        )
        perms = {
            e["permission"]
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_GROUP
            and e["group_id"] == group["id"]
        }
        assert {
            "files",
            "files-download",
            "files-write",
            "exec-and-sync",
        } <= perms


async def test_create_workspace_with_acl_rollback_on_seeding_failure(
    user, app_state
):
    """If ACL seeding fails, the row and any partial ACEs/groups are rolled
    back — nothing is orphaned (#128)."""
    from klangk.model import workspaces as model_ws

    captured: dict = {}

    async def _boom(db, ws, user_id):
        captured["id"] = ws["id"]
        raise RuntimeError("seeding boom")

    with patch.object(
        model_ws.WorkspacesModel,
        "_seed_workspace_acl",
        new_callable=AsyncMock,
        side_effect=_boom,
    ):
        with pytest.raises(RuntimeError, match="seeding boom"):
            await app_state.state.model.workspaces.create_workspace_with_acl(
                user["id"], "orphan-test"
            )

    ws_id = captured["id"]
    resource = f"/workspaces/{ws_id}"

    # No workspace row, no ACL entries, no role groups left behind.
    assert await app_state.state.model.workspaces.get_workspace(ws_id) is None
    assert await app_state.state.model.acl.get_acl_entries(resource) == []
    for suffix in ["owners", "coders", "collaborators", "spectators"]:
        assert (
            await app_state.state.model.users.get_group_by_name(
                f"{suffix}-{ws_id}"
            )
            is None
        )

    # Name is reusable — proves full cleanup of the row.
    ws = await app_state.state.model.workspaces.create_workspace_with_acl(
        user["id"], "orphan-test"
    )
    assert ws["name"] == "orphan-test"


class TestListWorkspaces:
    async def test_list_empty(self, user, app_state):
        result = await app_state.state.workspaces.list_workspaces(user["id"])
        assert result == {
            "items": [],
            "has_more": False,
            "next_offset": None,
        }

    async def test_list_multiple(self, user, app_state):
        await app_state.state.workspaces.create_workspace(user["id"], "ws-a")
        await app_state.state.workspaces.create_workspace(user["id"], "ws-b")
        result = await app_state.state.workspaces.list_workspaces(user["id"])
        names = [ws["name"] for ws in result["items"]]
        assert "ws-a" in names
        assert "ws-b" in names
        assert len(result["items"]) == 2


class TestGetWorkspace:
    async def test_get_existing(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "findme"
        )
        found = await app_state.state.workspaces.get_workspace(
            ws["id"], user["id"]
        )
        assert found is not None
        assert found["name"] == "findme"

    async def test_get_nonexistent(self, user, app_state):
        found = await app_state.state.workspaces.get_workspace(
            "fake-id", user["id"]
        )
        assert found is None

    async def test_get_wrong_user(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "mine"
        )
        found = await app_state.state.workspaces.get_workspace(
            ws["id"], "other-user"
        )
        assert found is None


class TestDeleteWorkspace:
    async def test_delete_removes_db_and_dirs(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "doomed"
        )
        home_dir = app_state.state.workspaces.home_path(ws["id"])
        (home_dir / "klangk" / "file.txt").parent.mkdir(parents=True)
        (home_dir / "klangk" / "file.txt").write_text("hello")
        (home_dir / ".bashrc").write_text("# custom")

        deleted = await app_state.state.workspaces.delete_workspace(
            ws["id"], user["id"]
        )
        assert deleted is True
        assert (
            await app_state.state.workspaces.get_workspace(
                ws["id"], user["id"]
            )
            is None
        )
        assert not home_dir.exists()

    async def test_delete_nonexistent(self, user, app_state):
        deleted = await app_state.state.workspaces.delete_workspace(
            "fake-id", user["id"]
        )
        assert deleted is False

    async def test_delete_cascades_ports(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "ported"
        )
        registry = app_state.state.container_registry
        ports_before = await registry.get_workspace_ports(ws["id"])
        assert len(ports_before) > 0

        await app_state.state.workspaces.delete_workspace(ws["id"], user["id"])
        ports_after = await registry.get_workspace_ports(ws["id"])
        assert ports_after == []

    async def test_delete_missing_dirs_ok(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "no-dirs"
        )
        home_dir = app_state.state.workspaces.home_path(ws["id"])
        import shutil

        shutil.rmtree(home_dir)

        deleted = await app_state.state.workspaces.delete_workspace(
            ws["id"], user["id"]
        )
        assert deleted is True


class TestHostPaths:
    def test_home_host_path_creates_dir(self, user, temp_data_dir, app_state):
        path = app_state.state.workspaces.get_home_host_path("ws-1")
        assert path.exists()
        assert path.is_dir()

    def test_home_host_path_idempotent(self, user, temp_data_dir, app_state):
        path1 = app_state.state.workspaces.get_home_host_path("ws-1")
        path2 = app_state.state.workspaces.get_home_host_path("ws-1")
        assert path1 == path2

    def test_paths_are_under_data_dir(self, user, temp_data_dir, app_state):
        home = app_state.state.workspaces.get_home_host_path("ws-1")
        assert str(home).startswith(str(temp_data_dir))


class TestEnsureHomeSymlink:
    async def test_creates_symlink(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "symlink-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        result, created = await app_state.state.workspaces.ensure_home_symlink(
            home, "alice", "uid-1"
        )
        assert result == "/home/alice"
        assert created is True
        symlink = home / "alice"
        assert symlink.is_symlink()
        assert os.readlink(symlink) == ".users/uid-1"
        assert (home / ".users" / "uid-1").is_dir()

    async def test_heals_unlistable_users_dirs(self, user, app_state, caplog):
        """#2766/#2769: .users and the per-handle home have the same
        umask-tainted-mkdir hazard as the volume root — the per-handle
        home resolves through them, so an unlistable .users/<uid> breaks
        the Files tab one level down. Healed loudly on every connect."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "symlink-mode-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        users = home / ".users"
        users.mkdir(parents=True, exist_ok=True)
        (users / "uid-1").mkdir(exist_ok=True)
        os.chmod(users, 0o0711)
        os.chmod(users / "uid-1", 0o0711)

        with caplog.at_level(logging.WARNING, logger="klangk.workspaces"):
            await app_state.state.workspaces.ensure_home_symlink(
                home, "carol", "uid-1"
            )

        for healed in (users, users / "uid-1"):
            mode = stat.S_IMODE(os.stat(healed).st_mode)
            assert mode & 0o0555 == 0o0555
            assert mode & 0o700 == 0o700
        assert len([r for r in caplog.records if "#2766" in r.message]) == 2

    async def test_idempotent(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "symlink-ws2"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        await app_state.state.workspaces.ensure_home_symlink(
            home, "bob", "uid-1"
        )
        result, created = await app_state.state.workspaces.ensure_home_symlink(
            home, "bob", "uid-1"
        )
        assert result == "/home/bob"
        assert created is False

    async def test_rename_removes_old_symlink(self, user, app_state):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "symlink-ws3"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        await app_state.state.workspaces.ensure_home_symlink(
            home, "alice", "uid-1"
        )
        result, created = await app_state.state.workspaces.ensure_home_symlink(
            home, "alicia", "uid-1"
        )
        assert result == "/home/alicia"
        assert created is False
        assert not (home / "alice").exists()
        assert (home / "alicia").is_symlink()

    async def test_replaces_stale_symlink_from_import(self, user, app_state):
        """Imported workspace has a symlink for a different user ID.

        The old user's files should be adopted into the new user dir.
        """
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "symlink-ws4"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        # Simulate imported workspace: symlink for old user ID with files.
        (home / ".users").mkdir(parents=True, exist_ok=True)
        old_dir = home / ".users" / "old-uid"
        old_dir.mkdir()
        (old_dir / ".bashrc").write_text("# old bashrc")
        (old_dir / ".profile").write_text("# old profile")
        (home / "admin").symlink_to(".users/old-uid")
        # New user connects — different user ID, same handle.
        result, created = await app_state.state.workspaces.ensure_home_symlink(
            home, "admin", "new-uid"
        )
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
        mock_pod = MagicMock()
        with patch.object(
            mock_pod,
            "exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_exec:
            await ws_mod.populate_home_skel("cid-123", "uid-456", mock_pod)
        mock_exec.assert_awaited_once_with(
            "cid-123",
            ["/opt/klangk/bin/klangk-setup-home", "/home/.users/uid-456"],
            user="klangk",
            timeout=10,
        )

    async def test_logs_warning_on_failure(self):
        """populate_home_skel logs but does not raise on failure."""
        mock_pod = MagicMock()
        with patch.object(
            mock_pod,
            "exec_container",
            new_callable=AsyncMock,
            side_effect=OSError("podman not found"),
        ):
            # Should not raise
            await ws_mod.populate_home_skel("cid-123", "uid-456", mock_pod)


class TestAutoStartWorkspaces:
    async def test_returns_zero_when_env_not_set(self, user, app_state):
        # allow_autostart unset -> skip entirely
        result = await app_state.state.workspaces.auto_start_workspaces()
        assert result == 0

    async def test_starts_auto_start_workspaces(self, user, app_state):
        registry = app_state.state.container_registry
        ws1 = await app_state.state.workspaces.create_workspace(
            user["id"], "auto-ws1", auto_start=True
        )
        ws2 = await app_state.state.workspaces.create_workspace(
            user["id"], "auto-ws2", auto_start=True
        )
        await app_state.state.workspaces.create_workspace(
            user["id"], "normal-ws"
        )

        # Pre-populate states so idle_timeout can be set.
        from klangk.container import ContainerState

        registry.states[ws1["id"]] = ContainerState(
            ws1["id"], "cid-1", registry
        )
        registry.states[ws2["id"]] = ContainerState(
            ws2["id"], "cid-2", registry
        )
        try:
            with patch.object(
                app_state.state.settings, "allow_autostart", "1"
            ):
                with patch.object(
                    registry,
                    "start_container",
                    new_callable=AsyncMock,
                    return_value=("cid-abc", "started"),
                ) as mock_start:
                    with patch(
                        "klangk.workspaces.asyncio.sleep",
                        new_callable=AsyncMock,
                    ) as mock_sleep:
                        result = await app_state.state.workspaces.auto_start_workspaces()
            assert result == 2
            assert mock_start.await_count == 2
            mock_sleep.assert_awaited_once()
            assert registry.states[ws1["id"]].idle_timeout == 0
            assert registry.states[ws2["id"]].idle_timeout == 0
        finally:
            registry.states.pop(ws1["id"], None)
            registry.states.pop(ws2["id"], None)

    async def test_handles_start_failure_gracefully(self, user, app_state):
        registry = app_state.state.container_registry
        await app_state.state.workspaces.create_workspace(
            user["id"], "fail-ws", auto_start=True
        )
        with patch.object(app_state.state.settings, "allow_autostart", "1"):
            with patch.object(
                registry,
                "start_container",
                new_callable=AsyncMock,
                side_effect=RuntimeError("container failed"),
            ):
                result = (
                    await app_state.state.workspaces.auto_start_workspaces()
                )
        assert result == 0


class TestStartWorkspace:
    """Tests for start_workspace: the thin dict-unpacking wrapper.

    The service-command firing and agent-home provisioning moved to
    the create choke point inside start_container (see
    ContainerRegistry._bringup, #1244), and idle_timeout pinning moved to auto_start_workspaces
    (boot path only). So start_workspace itself only unpacks the
    workspace dict and delegates to registry.start_container.
    """

    async def test_unpacks_dict_and_starts_container(self, user, app_state):
        registry = app_state.state.container_registry
        ws = await app_state.state.workspaces.create_workspace(
            user["id"],
            "start-ws",
            auto_start=True,
            service_command="openclaw gateway",
        )
        try:
            with patch.object(
                registry,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-x", "created"),
            ) as mock_start:
                cid, status = await app_state.state.workspaces.start_workspace(
                    ws
                )
            assert cid == "cid-x"
            assert status == "created"
            mock_start.assert_awaited_once()
            # The service_command is threaded through to start_container
            # so the create choke point (bringup) can fire it.
            assert mock_start.call_args.args[0].service_command == (
                "openclaw gateway"
            )
        finally:
            registry.states.pop(ws["id"], None)

    async def test_does_not_pin_idle_timeout(self, user, app_state):
        """Only the boot path (auto_start_workspaces) pins idle_timeout."""
        registry = app_state.state.container_registry
        ws = await app_state.state.workspaces.create_workspace(
            user["id"],
            "start-ws-no-idle",
            auto_start=True,
        )
        from klangk.container import ContainerState

        # Registry default idle timeout is non-zero; start_workspace
        # must not clobber it.
        default_timeout = ContainerState(
            ws["id"], "cid-y", registry
        ).idle_timeout
        registry.states[ws["id"]] = ContainerState(ws["id"], "cid-y", registry)
        try:
            with patch.object(
                registry,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-y", "created"),
            ):
                await app_state.state.workspaces.start_workspace(ws)
            assert registry.states[ws["id"]].idle_timeout == default_timeout
        finally:
            registry.states.pop(ws["id"], None)

    async def test_idle_timeout_override_is_applied(self, user, app_state):
        """A settings.idle_timeout override pins the container state (#864).

        The bag is applied inside ``ContainerRegistry.start_container`` --
        the single start choke point every path funnels through (POST
        /start AND a WebSocket connect; the WS path used to miss it, found
        by the idle fuzz harness #2514). Only an explicit override is
        materialized onto the container state; when no override is present
        the state stays None so ``get_idle_timeout()`` lazily reads the
        live deploy default (reload-safe). See
        ``test_no_idle_override_stays_lazy`` for that path.
        """
        registry = app_state.state.container_registry
        from klangk.container import ContainerState

        state = ContainerState("ws-apply", "cid-z", registry)
        registry.states["ws-apply"] = state
        try:
            with patch.object(
                registry,
                "_start_container_inner",
                new_callable=AsyncMock,
                return_value=("cid-z", "created"),
            ):
                await registry.start_container(
                    container.ContainerStartSpec(
                        "ws-apply",
                        "/home",
                        workspace_settings={"idle_timeout": 600},
                    )
                )
            # The override is materialized onto the live state.
            assert state.idle_timeout == 600
        finally:
            registry.states.pop("ws-apply", None)

    async def test_no_idle_override_stays_lazy(self, user, app_state):
        """No idle_timeout in the bag -> state stays None (lazy fallback)."""
        registry = app_state.state.container_registry
        from klangk.container import ContainerState

        state = ContainerState("ws-lazy", "cid-w", registry)
        registry.states["ws-lazy"] = state
        try:
            with patch.object(
                registry,
                "_start_container_inner",
                new_callable=AsyncMock,
                return_value=("cid-w", "created"),
            ):
                await registry.start_container(
                    container.ContainerStartSpec(
                        "ws-lazy",
                        "/home",
                        workspace_settings={"cpu_limit": 2.0},  # not idle
                    )
                )
            assert state.idle_timeout is None
        finally:
            registry.states.pop("ws-lazy", None)


async def test_idle_timeout_zero_pins_alive(user, app_state):
    """``settings.idle_timeout: 0`` means "never idle out" (#864).

    The schema accepts 0 for idle_timeout specifically (pids / bridge stay
    strictly positive); the apply path is membership-based
    (``"idle_timeout" in bag``), so 0 is materialized onto the container
    state — and the idle reaper's ``timeout > 0`` guard then skips it,
    pinning the workspace alive forever.
    """
    registry = app_state.state.container_registry
    from klangk.container import ContainerState

    state = ContainerState("ws-zero", "cid-0", registry)
    registry.states["ws-zero"] = state
    try:
        with patch.object(
            registry,
            "_start_container_inner",
            new_callable=AsyncMock,
            return_value=("cid-0", "created"),
        ):
            await registry.start_container(
                container.ContainerStartSpec(
                    "ws-zero",
                    "/home",
                    workspace_settings={"idle_timeout": 0},
                )
            )
        # 0 is materialized (not treated as "absent" by a truthiness check).
        assert state.idle_timeout == 0
    finally:
        registry.states.pop("ws-zero", None)


class TestEnsureSharedHome:
    """The shared home ``/home/klangk``: a plain real directory on the
    home mount (#2717), no ``.users/{uid}`` symlink indirection, ensured
    + populated under both layouts before the first login shell."""

    async def test_creates_plain_dir_and_populates_skel_once(
        self, user, app_state
    ):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as skel_mock:
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
            # Freshly created → exactly-one skel copy into the shared
            # home path.
            skel_mock.assert_awaited_once_with(
                "cid", model.AGENT_USER_ID, home="/home/klangk"
            )

            shared_dir = home / "klangk"
            assert shared_dir.is_dir()
            assert not shared_dir.is_symlink()

            # Idempotent: once the home has content (the skel copy), a
            # later create never re-populates (customizations must not
            # be clobbered).
            (shared_dir / ".profile").write_text("# populated\n")
            skel_mock.reset_mock()
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
            skel_mock.assert_not_awaited()

    async def test_failed_populate_retries_on_next_create(
        self, user, app_state
    ):
        """The skel exec failure is swallowed, so a failed populate leaves
        the home EMPTY — and emptiness is what gates the retry: the next
        create re-populates instead of leaving the workspace profile-less
        forever (the #2717 acceptance criterion is not best-effort)."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-retry-ws"
        )

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as skel_mock:
            # First create: populate "fails" (mock does nothing) — the
            # directory stays empty.
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
            assert skel_mock.await_count == 1
            # Second create (container recreate): still empty → retried.
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid-2"
            )
            assert skel_mock.await_count == 2
            assert skel_mock.await_args.kwargs["home"] == "/home/klangk"

    async def test_populate_skel_home_override(self):
        """populate_home_skel(home=...) threads the override through; the
        default path stays /home/.users/{user_id}."""
        import types as types_mod
        from unittest.mock import AsyncMock, patch

        from klangk.workspaces import Workspaces

        podman = object()
        ws = Workspaces(
            types_mod.SimpleNamespace(
                state=types_mod.SimpleNamespace(podman=podman)
            )
        )
        with patch(
            "klangk.workspaces.populate_home_skel", new_callable=AsyncMock
        ) as mock_skel:
            await ws.populate_home_skel("cid", "uid-9")
            mock_skel.assert_awaited_once_with("cid", "uid-9", podman)

            mock_skel.reset_mock()
            await ws.populate_home_skel("cid", "uid-9", home="/home/klangk")
            mock_skel.assert_awaited_once_with(
                "cid", "uid-9", podman, home="/home/klangk"
            )

    async def test_adopts_legacy_chat_era_symlink(self, user, app_state):
        """A ``klangk`` symlink left by the chat-era provisioning (pointing
        at a non-empty ``.users/{AGENT_USER_ID}``) is adopted as-is — no
        removal, no restructuring of existing volumes, and no skel
        re-population."""
        from klangk.model import AGENT_USER_ID

        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-legacy-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        users = home / ".users" / AGENT_USER_ID
        users.mkdir(parents=True, exist_ok=True)
        (users / ".profile").write_text("# legacy\n")  # non-empty target
        legacy = home / "klangk"
        legacy.symlink_to(f".users/{AGENT_USER_ID}")

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as skel_mock:
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
        # A resolving symlink with content: leave it be, no skel.
        skel_mock.assert_not_awaited()
        assert legacy.is_symlink()
        assert os.readlink(legacy) == f".users/{AGENT_USER_ID}"

    async def test_empty_adopted_symlink_target_gets_skel(
        self, user, app_state
    ):
        """A legacy symlink onto an EMPTY target (the chat-era agent dir
        never had content) is adopted but still populated through the
        symlink — the service session's login shell needs a .profile
        there."""
        from klangk.model import AGENT_USER_ID

        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-empty-legacy-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        (home / ".users" / AGENT_USER_ID).mkdir(parents=True, exist_ok=True)
        legacy = home / "klangk"
        legacy.symlink_to(f".users/{AGENT_USER_ID}")

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as skel_mock:
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
        skel_mock.assert_awaited_once_with(
            "cid", model.AGENT_USER_ID, home="/home/klangk"
        )
        assert legacy.is_symlink()  # still adopted, not replaced

    async def test_dangling_legacy_symlink_replaced_by_real_dir(
        self, user, app_state
    ):
        """A DANGLING ``klangk`` symlink (chat-era target deleted) must not
        crash create: ``mkdir(exist_ok=True)`` re-raises FileExistsError
        on a dangling symlink, which would fail ``_bringup`` after the
        container is already running (and it never runs again for that
        container). The broken link is removed and a real directory
        materialized instead (#2717)."""
        from klangk.model import AGENT_USER_ID

        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-dangling-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        dangling = home / "klangk"
        dangling.symlink_to(f".users/{AGENT_USER_ID}")  # target absent

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as skel_mock:
            # Must not raise.
            await app_state.state.workspaces.ensure_shared_home(
                ws["id"], "cid"
            )
        assert not dangling.is_symlink()
        assert dangling.is_dir()
        skel_mock.assert_awaited_once_with(
            "cid", model.AGENT_USER_ID, home="/home/klangk"
        )

    async def test_heals_unlistable_volume_root_mode(
        self, user, app_state, caplog
    ):
        """#2766: a volume root without group/world r-x (umask-tainted
        mkdir, or an inherited volume mode) is traversable but not
        listable by the container user — the Files tab showed /home as
        empty. The start choke point ORs r-x back in; owner bits keep."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-mode-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "klangk").mkdir(exist_ok=True)
        os.chmod(home, 0o0711)
        os.chmod(home / "klangk", 0o0711)

        with patch(
            "klangk.workspaces.Workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ):
            with caplog.at_level(logging.WARNING, logger="klangk.workspaces"):
                await app_state.state.workspaces.ensure_shared_home(
                    ws["id"], "cid"
                )

        for healed in (home, home / "klangk"):
            mode = stat.S_IMODE(os.stat(healed).st_mode)
            assert mode & 0o0555 == 0o0555
            assert mode & 0o700 == 0o700
        # The heal is loud (#2766): a restrictive root is evidence of
        # the creation bug recurring and must leave a trace.
        healed_logs = [r for r in caplog.records if "#2766" in r.message]
        assert len(healed_logs) == 2
        assert all("NOT listable" in r.message for r in healed_logs)
        assert any(str(home) in r.message for r in healed_logs)
        assert any("0711" in r.message for r in healed_logs)
        assert any("0755" in r.message for r in healed_logs)

    async def test_unchmoddable_volume_root_does_not_crash(
        self, user, app_state, caplog
    ):
        """A root the daemon can't chmod (foreign uid) is skipped with a
        warning — container start must not fail; the listing error
        surfaces via the files API instead (#2766)."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-home-unfixable-ws"
        )
        home = app_state.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o0711)

        with (
            patch(
                "klangk.workspaces.os.chmod",
                side_effect=PermissionError(1, "Operation not permitted"),
            ),
            patch(
                "klangk.workspaces.Workspaces.populate_home_skel",
                new_callable=AsyncMock,
            ) as skel_mock,
        ):
            # Must not raise.
            with caplog.at_level(logging.WARNING, logger="klangk.workspaces"):
                await app_state.state.workspaces.ensure_shared_home(
                    ws["id"], "cid"
                )
        skel_mock.assert_awaited_once_with(
            "cid", model.AGENT_USER_ID, home="/home/klangk"
        )
        # The failed heal is equally loud (#2766).
        assert any(
            "cannot make" in r.message and str(home) in r.message
            for r in caplog.records
        )
