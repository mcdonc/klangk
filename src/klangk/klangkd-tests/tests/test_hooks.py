"""Tests for klangk.hooks: the workspace-created hook (#2762).

Loading mirrors the OIDC login hook (KLANGKD_OIDC_LOGIN_HOOK /
klangk.oidc.OIDC.load_login_hook); firing is log-and-continue — a
raising hook never fails the create.
"""

import logging
import types

import pytest

from klangk import hooks as hooks_mod
from klangk import model
from klangk.exceptions import ConfigurationError
from _helpers import make_settings


def _hooks(settings=None) -> hooks_mod.Hooks:
    """Build a fresh Hooks instance from explicit settings (no env)."""
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings or make_settings({}))
    )
    return hooks_mod.Hooks(app)


class TestParseHookValue:
    def test_path_only_defaults_func(self):
        path, func = hooks_mod.parse_hook_value("/etc/klangk/hook.py")
        assert path == "/etc/klangk/hook.py"
        assert func == "on_workspace_created"

    def test_path_with_func(self):
        path, func = hooks_mod.parse_hook_value("/etc/klangk/hook.py:mutate")
        assert path == "/etc/klangk/hook.py"
        assert func == "mutate"

    def test_colon_in_path(self):
        path, func = hooks_mod.parse_hook_value("/a/b:c/hook.py:mutate")
        assert path == "/a/b:c/hook.py"
        assert func == "mutate"


class TestLoadWorkspaceCreatedHook:
    def test_no_hook_when_not_set(self):
        h = _hooks()
        h.load_workspace_created_hook()
        assert h.workspace_created_hook is None
        assert h.workspace_created_hook_is_async is False
        assert h.workspace_created_hook_source is None

    def test_hook_loaded_from_file(self, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def on_workspace_created(workspace, actor):\n"
            "    workspace['name'] = 'hooked'\n"
        )
        h = _hooks(
            make_settings({"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)})
        )
        h.load_workspace_created_hook()
        assert h.workspace_created_hook is not None
        assert h.workspace_created_hook_source == str(hook_file)
        ws = {"name": "orig"}
        h.workspace_created_hook(ws, {})
        assert ws["name"] == "hooked"

    def test_hook_loaded_with_custom_func_name(self, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def mutate(workspace, actor):\n"
            "    workspace['name'] = 'renamed'\n"
        )
        h = _hooks(
            make_settings(
                {"KLANGKD_WORKSPACE_CREATED_HOOK": f"{hook_file}:mutate"}
            )
        )
        h.load_workspace_created_hook()
        assert h.workspace_created_hook is not None
        ws = {}
        h.workspace_created_hook(ws, {})
        assert ws["name"] == "renamed"

    def test_async_hook_detected(self, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "async def on_workspace_created(workspace, actor):\n"
            "    return None\n"
        )
        h = _hooks(
            make_settings({"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)})
        )
        h.load_workspace_created_hook()
        assert h.workspace_created_hook_is_async is True

    def test_file_not_found(self):
        with pytest.raises(ConfigurationError, match="file not found"):
            _hooks(
                make_settings(
                    {"KLANGKD_WORKSPACE_CREATED_HOOK": "/nonexistent/hook.py"}
                )
            ).load_workspace_created_hook()

    def test_func_not_found(self, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text("x = 1\n")
        with pytest.raises(
            ConfigurationError, match="not found or not callable"
        ):
            _hooks(
                make_settings(
                    {"KLANGKD_WORKSPACE_CREATED_HOOK": f"{hook_file}:missing"}
                )
            ).load_workspace_created_hook()

    def test_not_callable(self, tmp_path):
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text("on_workspace_created = 42\n")
        with pytest.raises(
            ConfigurationError, match="not found or not callable"
        ):
            _hooks(
                make_settings(
                    {"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)}
                )
            ).load_workspace_created_hook()

    def test_reconfigure_reloads_hook(self, tmp_path):
        # SIGHUP path: reconfigure(app) swaps the app reference and
        # re-reads the (possibly changed) setting.
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def on_workspace_created(workspace, actor):\n    return None\n"
        )
        with_setting = make_settings(
            {"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)}
        )
        h = _hooks(with_setting)
        h.load_workspace_created_hook()
        assert h.workspace_created_hook is not None
        # New app whose settings dropped the var → hook unloaded.
        h.reconfigure(
            types.SimpleNamespace(
                state=types.SimpleNamespace(settings=make_settings({}))
            )
        )
        assert h.workspace_created_hook is None

    def test_failed_reload_keeps_previous_hook(self, tmp_path):
        # Assign-on-success (login-hook parity): a SIGHUP reload whose
        # file went missing raises, and the loaded hook stays active.
        hook_file = tmp_path / "myhook.py"
        hook_file.write_text(
            "def on_workspace_created(workspace, actor):\n    return None\n"
        )
        h = _hooks(
            make_settings({"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)})
        )
        h.load_workspace_created_hook()
        loaded = h.workspace_created_hook
        assert loaded is not None
        hook_file.unlink()
        with pytest.raises(ConfigurationError, match="file not found"):
            h.reconfigure(
                types.SimpleNamespace(
                    state=types.SimpleNamespace(
                        settings=make_settings(
                            {"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)}
                        )
                    )
                )
            )
        assert h.workspace_created_hook is loaded


class TestFireWorkspaceCreated:
    """Firing semantics against the real model (app_state fixture).

    The service-layer create path is exercised in test_api.py
    (TestWorkspaceCreatedHookFiring); these tests call
    fire_workspace_created directly on rows seeded via the model.
    """

    async def _seed(self, app_state, user, name="hooked-ws"):
        return (
            await app_state.state.model.workspaces.create_workspace_with_acl(
                user["id"], name
            )
        )

    async def _wire(self, app_state):
        hooks = hooks_mod.Hooks(app_state)
        app_state.state.hooks = hooks
        return hooks

    async def test_no_hook_returns_workspace(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out is ws

    async def test_sync_hook_attribute_mutation_persisted(
        self, app_state, user
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["egress_mode"] = "allow"
            workspace["env"] = {"HOOK": "ran"}

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-sync"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out["egress_mode"] == "allow"
        assert out["env"] == {"HOOK": "ran"}
        # Persisted: a fresh read sees the mutation.
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["egress_mode"] == "allow"
        assert row["env"] == {"HOOK": "ran"}

    async def test_async_hook_attribute_mutation_persisted(
        self, app_state, user
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            workspace["egress_mode"] = "static"

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-async"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out["egress_mode"] == "static"
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["egress_mode"] == "static"

    async def test_hook_sets_classification_banner(self, app_state, user):
        """#2768: the marking is a plain mutable attribute — a hook that
        sets it gets it persisted (normalized), like a per-level
        classification from the deployment's own logic."""
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["classification_banner"] = "  CUI  "

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-banner"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out["classification_banner"] == "CUI"
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["classification_banner"] == "CUI"

    async def test_actor_passed_through(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        seen = {}

        async def hook(workspace, actor):
            seen.update(actor)

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-actor"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        assert seen["id"] == user["id"]
        assert seen["email"] == user["email"]

    async def test_hook_rewrites_acl(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            # Drop the coders group's `files` grant, grant the actor
            # `view`, and prove read + write round-trip.
            entries = await workspace.acl_entries()
            kept = [
                e
                for e in entries
                if not (
                    e["principal"].startswith("coders-")
                    and e["permission"] == "files-view"
                )
            ]
            kept.append(
                {
                    "action": model.ACTION_ALLOW,
                    "principal_type": model.PRINCIPAL_USER,
                    "permission": "view",
                    "user_id": actor["id"],
                }
            )
            await workspace.rewrite_acl(kept)

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-acl"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)

        resource = f"/workspaces/{ws['id']}"
        entries = await app_state.state.model.acl.get_acl_entries_resolved(
            resource
        )
        # Owner wildcard survives.
        assert any(
            e["principal"] == user["email"] and e["permission"] == "*"
            for e in entries
        )
        assert any(
            e["principal"] == user["email"] and e["permission"] == "view"
            for e in entries
        ), "the appended user grant landed"
        coders = [e for e in entries if e["principal"].startswith("coders-")]
        assert coders, "coders role group still holds ACEs"
        assert all(e["permission"] != "files-view" for e in coders)
        collab_files = [
            e
            for e in entries
            if e["permission"] == "files-view"
            and e["principal"].startswith("collaborators-")
        ]
        assert collab_files, "collaborators group untouched by the filter"

    async def test_rewrite_acl_renumbers_positions(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            entries = await workspace.acl_entries()
            # Reverse the list — the order handed back is the new order.
            await workspace.rewrite_acl(list(reversed(entries)))

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-reorder"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws['id']}"
        )
        assert [e["position"] for e in entries] == list(range(len(entries)))

    async def test_raising_hook_logged_and_continued(
        self, app_state, user, caplog
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            workspace["egress_mode"] = "allow"
            raise RuntimeError("boom")

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-raise"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        # The create is not failed; the row is unchanged (the in-place
        # edit made before the raise is NOT persisted).
        assert out["egress_mode"] == "interactive"
        assert out is ws
        assert any(
            "workspace-created hook test-raise failed" in r.message
            for r in caplog.records
        )
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["egress_mode"] == "interactive"

    async def test_invalid_mutation_rejected(self, app_state, user, caplog):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["egress_mode"] = "bogus"

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-invalid"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        assert out["egress_mode"] == "interactive"
        assert any("invalid change" in r.message for r in caplog.records)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["egress_mode"] == "interactive"

    @pytest.mark.parametrize(
        "field, value",
        [
            ("setup_state", "bogus"),
            ("per_handle_home", "yes"),
            ("classification_banner", "A\nB"),
            ("classification_banner", "X" * 121),
        ],
    )
    async def test_invalid_enum_and_bool_mutations_rejected(
        self, app_state, user, field, value
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace[field] = value

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-invalid-value"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        # The row keeps its seeded value (not coerced).
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row[field] == ws[field]

    async def test_non_persistent_fields_ignored(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["num_ports"] = 99  # provisioned, not declarative

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-numports"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out is ws  # no persist path taken
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["num_ports"] == model.DEFAULT_PORTS_PER_WORKSPACE

    async def test_persist_failure_logged(self, app_state, user, caplog):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            # Valid per the pre-write validation, but the DB write is
            # made to fail below — the create must survive it.
            workspace["name"] = "renamed-by-hook"

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-persist-fail"
        ws = await self._seed(app_state, user)

        async def broken_update(*args, **kwargs):
            raise RuntimeError("db down")

        app_state.state.model.workspaces.update_workspace = broken_update
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        assert out is ws
        assert any(
            "persisting attribute changes" in r.message for r in caplog.records
        )

    async def test_nested_mutation_persisted(self, app_state, user):
        # Deep-copy handle: a nested edit is detected by the diff and
        # persisted (a shallow copy would alias the caller's dict and
        # see no change).
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "nested-ws", env={"A": "1"}
        )

        def hook(workspace, actor):
            workspace["env"]["B"] = "2"
            workspace["settings"] = dict(workspace["settings"] or {})

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-nested"
        out = await hooks.fire_workspace_created(ws, user)
        assert out["env"] == {"A": "1", "B": "2"}
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["env"] == {"A": "1", "B": "2"}

    async def test_nested_mutation_then_raise_leaves_row_untouched(
        self, app_state, user
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "nested-raise", settings={"idle_timeout": 60}
        )
        original_settings = {"idle_timeout": 60}

        async def hook(workspace, actor):
            workspace["settings"]["idle_timeout"] = 999
            workspace["egress_mode"] = "allow"
            raise RuntimeError("boom")

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-nested-raise"
        out = await hooks.fire_workspace_created(ws, user)
        # The caller's dict and the DB row both keep the seeded values.
        assert out is ws
        assert ws["settings"] == original_settings
        assert ws["egress_mode"] == "interactive"
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["settings"] == original_settings
        assert row["egress_mode"] == "interactive"

    async def test_deletion_clears_column(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        ws = await self._seed_env_workspace(app_state, user)

        def hook(workspace, actor):
            del workspace["env"]

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-del"
        out = await hooks.fire_workspace_created(ws, user)
        assert out["env"] is None
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["env"] is None

    async def _seed_env_workspace(self, app_state, user):
        return (
            await app_state.state.model.workspaces.create_workspace_with_acl(
                user["id"], "env-ws", env={"A": "1"}
            )
        )

    async def test_invalid_settings_rejected(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["settings"] = {"idle_timeout": -1}

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-bad-settings"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["settings"] is None

    async def test_nix_optin_rejected_while_disabled(self, app_state, user):
        # #2560: the hook mirrors POST /workspaces — a nix=true opt-in is
        # an invalid mutation while the feature is off (the default), so
        # it is dropped instead of persisted.
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["settings"] = {"nix": True}

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-nix-optin"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["settings"] is None

    async def test_settings_normalized_on_persist(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["settings"] = {"idle_timeout": "90"}

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-settings-norm"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["settings"] == {"idle_timeout": 90}

    async def test_disallowed_image_rejected(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["image"] = "definitely-not-an-image"

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-bad-image"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["image"] is None

    async def test_allowed_image_persisted(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        image = app_state.state.container_registry.image_name

        def hook(workspace, actor):
            workspace["image"] = image

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-good-image"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out["image"] == image
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["image"] == image

    async def test_invalid_mounts_rejected(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["mounts"] = ["nocolon"]

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-bad-mounts"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["mounts"] is None

    async def test_valid_named_volume_mount_persisted(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["mounts"] = ["myvol:/mnt"]

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-good-mounts"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        assert out["mounts"] == ["myvol:/mnt"]

    async def test_invalid_allowed_domains_rejected(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["allowed_domains"] = ["!!not-a-host!!"]

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-bad-domains"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["allowed_domains"] is None

    async def test_allowed_domains_normalized_on_persist(
        self, app_state, user
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["allowed_domains"] = ["a.com", "a.com"]

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-domains-norm"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["allowed_domains"] == ["a.com"]

    @pytest.mark.parametrize(
        "domains",
        [
            ["10.0.0.0/8"],  # CIDR in rejected_domains — refused
            ["!!not-a-host!!"],  # malformed host — refused
        ],
    )
    async def test_rejected_domains_variants_refused(
        self, app_state, user, domains
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace["rejected_domains"] = domains

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-rejected-domains"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row["rejected_domains"] is None

    async def test_created_at_carried_over(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            workspace["egress_mode"] = "static"

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-created-at"
        ws = await self._seed(app_state, user)
        out = await hooks.fire_workspace_created(ws, user)
        # The re-read row carries the insert-time created_at, so the
        # create response's shape is hook-invariant.
        assert out.get("created_at") == ws["created_at"]

    async def test_sync_hook_acl_helper_fails_loudly(
        self, app_state, user, caplog
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace.acl_entries()  # sync hook: must raise, not no-op

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-sync-acl"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        assert out is ws
        assert "requires an 'async def' workspace-created hook" in caplog.text

    async def test_rewrite_acl_coerces_int_strings(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            entries = await workspace.acl_entries()
            entries.append(
                {
                    "action": "1",  # ACTION_ALLOW as an int-string
                    "principal_type": "1",  # PRINCIPAL_USER as an int-string
                    "permission": "view",
                    "user_id": actor["id"],
                }
            )
            await workspace.rewrite_acl(entries)

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-coerce"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws['id']}"
        )
        appended = [
            e
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_USER
            and e["user_id"] == user["id"]
            and e["permission"] == "view"
        ]
        assert appended
        assert appended[0]["action"] == model.ACTION_ALLOW

    async def test_rewrite_acl_rejects_agent_principal(
        self, app_state, user, caplog
    ):
        from klangk.model import AGENT_USER_ID

        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            entries = await workspace.acl_entries()
            entries.append(
                {
                    "action": model.ACTION_ALLOW,
                    "principal_type": model.PRINCIPAL_USER,
                    "permission": "view",
                    "user_id": AGENT_USER_ID,
                }
            )
            await workspace.rewrite_acl(entries)

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-agent"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        # The create survives; the rewrite rolled back wholesale.
        assert out["id"] == ws["id"]
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws['id']}"
        )
        assert not any(e["user_id"] == AGENT_USER_ID for e in entries)

    async def test_rewrite_acl_rejects_cross_workspace_role_group(
        self, app_state, user, caplog
    ):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)
        other = await self._seed(app_state, user, name="other-ws")

        # Resolve another workspace's coders role group id directly.
        cursor = await app_state.state.db.fetchall(
            "SELECT id FROM groups WHERE name = ?",
            (f"coders-{other['id']}",),
        )
        foreign_group_id = cursor[0]["id"]

        async def hook(workspace, actor):
            entries = await workspace.acl_entries()
            entries.append(
                {
                    "action": model.ACTION_ALLOW,
                    "principal_type": model.PRINCIPAL_GROUP,
                    "permission": "view",
                    "group_id": foreign_group_id,
                }
            )
            await workspace.rewrite_acl(entries)

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-foreign-role"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        assert out["id"] == ws["id"]
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws['id']}"
        )
        assert not any(e["group_id"] == foreign_group_id for e in entries)

    async def test_rewrite_acl_bad_int_rejected(self, app_state, user):
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        async def hook(workspace, actor):
            await workspace.rewrite_acl(
                [
                    {
                        "action": "allow",  # not an int or int-string
                        "principal_type": model.PRINCIPAL_USER,
                        "permission": "*",
                        "user_id": actor["id"],
                    }
                ]
            )

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = True
        hooks.workspace_created_hook_source = "test-bad-int"
        ws = await self._seed(app_state, user)
        await hooks.fire_workspace_created(ws, user)
        # Hook failure: original seeded ACL intact.
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws['id']}"
        )
        assert any(e["permission"] == "*" for e in entries)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("allowed_domains", [123]),  # non-str entry -> AttributeError
            ("rejected_domains", [123]),  # non-str entry -> AttributeError
            ("allowed_domains", 5),  # non-iterable -> TypeError
            ("mounts", 5),  # non-iterable -> TypeError
        ],
    )
    async def test_mistyped_change_rejected_not_fatal(
        self, app_state, user, caplog, field, value
    ):
        """A mistyped (not merely invalid) value must not break the
        log-and-continue contract: the validator raising instead of
        returning an error string still leaves the workspace unchanged
        and the create un-failed."""
        await app_state.state.model.init_db()
        hooks = await self._wire(app_state)

        def hook(workspace, actor):
            workspace[field] = value

        hooks.workspace_created_hook = hook
        hooks.workspace_created_hook_is_async = False
        hooks.workspace_created_hook_source = "test-mistyped"
        ws = await self._seed(app_state, user)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            out = await hooks.fire_workspace_created(ws, user)
        assert out is ws
        assert any(
            "could not be validated" in r.message for r in caplog.records
        )
        row = await app_state.state.model.workspaces.get_workspace(ws["id"])
        assert row[field] is None


class TestHooksBranchGaps2834:
    """#2834 branch gate: hook-field diff and created_at carry-over
    outcomes."""

    def test_changed_fields_absent_everywhere_is_no_change(self):
        # A field deleted from the handle whose column was never set on
        # the row: nothing to clear, nothing to write.
        changed = hooks_mod.hook_field_changes(
            {"name": "x"},  # no mutable fields present
            {"name": "x"},
        )
        assert changed == {}

    async def test_fire_created_at_already_present_kept(self, app_state):
        # get_workspace returning created_at itself: the carry-over is a
        # no-op (the hook response keeps the refreshed value).
        from unittest.mock import AsyncMock

        hooks = _hooks()

        def _rename(handle, actor):
            handle["name"] = "renamed-ws"

        hooks.workspace_created_hook = _rename
        hooks.workspace_created_hook_source = "test-noop"
        hooks.workspace_created_hook_is_async = False
        workspace = {
            "id": "ws-1",
            "name": "w",
            "created_at": "t0",
            "user_id": "u1",
        }
        hooks.app.state.model = types.SimpleNamespace(
            workspaces=types.SimpleNamespace(
                get_workspace=AsyncMock(
                    return_value={
                        "id": "ws-1",
                        "name": "renamed-ws",
                        "created_at": "t1",
                        "user_id": "u1",
                    }
                ),
                update_workspace=AsyncMock(),
            )
        )
        # The hook made no attribute change, but the row was re-read and
        # ALREADY carries created_at: the carry-over is skipped and the
        # refreshed value wins.
        out = await hooks.fire_workspace_created(
            workspace, {"id": "u1", "email": "a@b"}
        )
        assert out["created_at"] == "t1"


class TestHookLoadBadModule2910:
    def test_unloadable_extension_raises(self, tmp_path):
        """A hook file that exists but yields no importable spec (no
        recognized extension) fails loudly at load."""
        import klangk.hooks as hooks_mod

        hook_file = tmp_path / "hook.txt"
        hook_file.write_text("def on_workspace_created(app, ws):\n")
        mgr = hooks_mod.Hooks(
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    settings=make_settings(
                        {"KLANGKD_WORKSPACE_CREATED_HOOK": str(hook_file)}
                    )
                )
            )
        )
        with pytest.raises(
            hooks_mod.ConfigurationError, match="could not load"
        ):
            mgr.load_workspace_created_hook()
