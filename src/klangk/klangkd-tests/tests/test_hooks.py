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
        path, func = hooks_mod._parse_hook_value("/etc/klangk/hook.py")
        assert path == "/etc/klangk/hook.py"
        assert func == "on_workspace_created"

    def test_path_with_func(self):
        path, func = hooks_mod._parse_hook_value("/etc/klangk/hook.py:mutate")
        assert path == "/etc/klangk/hook.py"
        assert func == "mutate"

    def test_colon_in_path(self):
        path, func = hooks_mod._parse_hook_value("/a/b:c/hook.py:mutate")
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


class TestFireWorkspaceCreated:
    """Firing semantics against the real model (app_state fixture).

    The service-layer create path is exercised in test_api.py
    (TestWorkspaceCreatedHookFiring); these tests call
    fire_workspace_created directly on rows seeded via the model.
    """

    async def _seed(self, app_state, user):
        return (
            await app_state.state.model.workspaces.create_workspace_with_acl(
                user["id"], "hooked-ws"
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
                    and e["permission"] == "files"
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
        assert all(e["permission"] != "files" for e in coders)
        collab_files = [
            e
            for e in entries
            if e["permission"] == "files"
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
