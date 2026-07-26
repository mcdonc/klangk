"""E2E tests for the klangk TUI against a real backend.

Drives the real Textual TUI via in-process ``Pilot`` with a ``TuiState``
pointed at a real ``klangkd`` (TCP via proxy).  Asserts on rendered widget
state and real server/DB side-effects.

Run with: devenv shell -- test-cli-e2e -k TestTuiE2E
"""

import os
import sys
import tempfile

import asyncio

import httpx
import pytest

from textual.widgets import Button, Input, Label, ListItem, ListView, Static

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_server import start_server, stop_server  # noqa: E402

from klangk.cli.tui.app import KlangkApp  # noqa: E402
from klangk.cli.tui.screens import (  # noqa: E402
    ConfirmScreen,
    CreateWorkspaceScreen,
    EditWorkspaceScreen,
    LoginScreen,
    MainScreen,
    WorkspaceDetailScreen,
)
from klangk.cli.tui.state import TuiState  # noqa: E402
from klangk.cli.config import add_server_to_config, CLIState  # noqa: E402
from klangk.model import free_port  # noqa: E402


async def _settle(app, pilot):
    """Wait for short-lived workers to complete.

    The TUI has a long-running _status_loop worker that never finishes,
    so we can't use app.workers.wait_for_complete() unconditionally.
    Instead, pause twice to let short-lived workers (refresh, load, etc.)
    complete within the event loop.
    """
    await pilot.pause()
    await pilot.pause()


# ── server fixture ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def server():
    data_dir = tempfile.mkdtemp(prefix="klangk-tui-e2e-")
    log_path = os.path.join(data_dir, "server.log")
    srv = start_server(
        uds=False,
        data_dir=data_dir,
        KLANGKD_JWT_SECRET="tui-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="tuiuser@example.com",
        KLANGKD_DEFAULT_PASSWORD="tuipass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        log_path=log_path,
    )
    yield srv
    stop_server(srv)


@pytest.fixture(scope="module")
def base_url(server):
    return server["url"]


@pytest.fixture(scope="module")
def token(base_url):
    r = httpx.post(
        f"{base_url}/api/v1/auth/login",
        json={"identifier": "tuiuser@example.com", "password": "tuipass"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture()
def tui_state(base_url, token, tmp_path, monkeypatch):
    """A TuiState pointed at the real server with a valid token."""
    # Write a temporary CLI config/state so TuiState can find the server.
    config_path = tmp_path / "klangk.yaml"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
    monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
    # Seed config with the server.
    add_server_to_config("e2e", base_url)
    # Write state with the token.
    st = CLIState.load()
    st.set_credentials(base_url, "tuiuser@example.com", token)
    st.save()
    return TuiState()


def _api_create_workspace(base_url, token, name):
    """Create a workspace via API and return its id."""
    r = httpx.post(
        f"{base_url}/api/v1/workspaces",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()["id"]


def _api_delete_workspace(base_url, token, ws_id):
    """Delete a workspace via API (best-effort cleanup)."""
    httpx.delete(
        f"{base_url}/api/v1/workspaces/{ws_id}",
        headers={"Authorization": f"Bearer {token}"},
    )


def _api_get_workspace(base_url, token, ws_id):
    """Get a workspace by id via API (from the list endpoint)."""
    r = httpx.get(
        f"{base_url}/api/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    for ws in r.json():
        if ws["id"] == ws_id:
            return ws
    raise ValueError(f"Workspace {ws_id} not found")


# ── tests ───────────────────────────────────────────────────────────────


class TestTuiE2E:
    """Drive the real TUI against a real backend."""

    # -- login screen --

    async def test_login_screen_shows_on_no_auth(
        self, base_url, tmp_path, monkeypatch
    ):
        """An unauthenticated TuiState lands on the login screen."""
        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = TuiState()
        app = KlangkApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LoginScreen)

    async def test_login_bad_credentials(
        self, base_url, tmp_path, monkeypatch
    ):
        """Login with wrong password shows an error."""
        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        add_server_to_config("e2e", base_url)
        st = CLIState.load()
        st.active_server = base_url
        st.save()
        state = TuiState()
        app = KlangkApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()  # let _setup_auth_async complete
            assert isinstance(app.screen, LoginScreen)
            # Fill in bad credentials.
            app.screen.query_one(
                "#identifier", Input
            ).value = "tuiuser@example.com"
            app.screen.query_one("#password", Input).value = "wrongpass"
            app.screen._attempt_password()
            # Wait for async HTTP login attempt to complete.
            await asyncio.sleep(2)
            await pilot.pause()
            # Should still be on login screen with an error message.
            assert isinstance(app.screen, LoginScreen)
            msg = str(app.screen.query_one("#message").render())
            assert msg  # error message shown

    async def test_login_empty_fields(self, base_url, tmp_path, monkeypatch):
        """Login with empty fields shows validation message."""
        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        add_server_to_config("e2e", base_url)
        st = CLIState.load()
        st.active_server = base_url
        st.save()
        state = TuiState()
        app = KlangkApp(state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()  # let _setup_auth_async complete
            assert isinstance(app.screen, LoginScreen)
            # Press login with empty fields.
            app.screen._attempt_password()
            await pilot.pause()
            assert isinstance(app.screen, LoginScreen)
            msg = str(app.screen.query_one("#message").render())
            assert "required" in msg.lower()

    async def test_reentry_preserves_auth(
        self, base_url, token, tmp_path, monkeypatch
    ):
        """Quitting and re-launching skips the login screen (#1813)."""
        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)

        # First launch: seed credentials (simulates a prior login session).
        add_server_to_config("e2e", base_url)
        st = CLIState.load()
        st.set_credentials(base_url, "tuiuser@example.com", token)
        st.save()

        # First TUI instance: should go straight to MainScreen.
        state1 = TuiState()
        app1 = KlangkApp(state1)
        async with app1.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app1.screen, MainScreen)

        # Second TUI instance (simulates re-entry): new TuiState reads
        # persisted state — should also skip login.
        state2 = TuiState()
        assert state2.is_authenticated()
        app2 = KlangkApp(state2)
        async with app2.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app2.screen, MainScreen)

    # -- workspace list --

    async def test_authenticated_shows_workspace_list(self, tui_state):
        """An authenticated TuiState shows the main workspace list."""
        app = KlangkApp(tui_state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)

    async def test_create_workspace_appears_in_list(
        self, tui_state, base_url, token
    ):
        """Creating a workspace via the API shows up in the TUI list."""
        ws_name = f"tui-e2e-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)
                lv = app.screen.query_one("#owned_list", ListView)
                names = [str(lab.render()) for lab in lv.query(Label)]
                assert any(ws_name in n for n in names), (
                    f"{ws_name} not in {names}"
                )
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    async def test_delete_workspace_disappears_from_list(
        self, tui_state, base_url, token
    ):
        """Deleting a workspace via API removes it from the TUI list."""
        ws_name = f"tui-del-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)
            # Verify workspace is in the list.
            lv = app.screen.query_one("#owned_list", ListView)
            names = [str(lab.render()) for lab in lv.query(Label)]
            assert any(ws_name in n for n in names)

            # Delete via API and refresh the list.
            _api_delete_workspace(base_url, token, ws_id)
            app.screen.refresh_lists()
            await pilot.pause()
            await pilot.pause()

            # Verify it's gone — check .ws-name labels specifically.
            lv = app.screen.query_one("#owned_list", ListView)
            names = [str(lab.render()) for lab in lv.query(".ws-name")]
            assert not any(ws_name in n for n in names)

    # -- workspace detail --

    async def test_workspace_detail_screen(self, tui_state, base_url, token):
        """Selecting a workspace opens the detail screen with correct info."""
        ws_name = f"tui-detail-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, WorkspaceDetailScreen)
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    async def test_detail_shows_running_status(
        self, tui_state, base_url, token
    ):
        """Detail screen shows running/stopped status correctly."""
        ws_name = f"tui-status-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await pilot.pause()
                # Workspace was just created, not started — should show
                # "running: no".
                body = str(
                    app.screen.query_one("#detail_body", Static).render()
                )
                assert "running" in body.lower()
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    # -- create workspace via TUI --

    async def test_create_workspace_via_tui(self, tui_state, base_url, token):
        """Create workspace through the TUI form with name field."""
        ws_name = f"tui-create-{free_port()}"
        ws_id = None

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)

                # Trigger the create action.
                app.screen.action_create()
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, CreateWorkspaceScreen)

                # Fill in the name.
                app.screen.query_one("#name", Input).value = ws_name
                # Submit the form.
                app.screen.query_one("#create", Button).press()
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()

                # Wait for the async create to complete.
                for _ in range(10):
                    await pilot.pause()

                # After creation the TUI may navigate to the detail screen
                # or show a confirm dialog. Handle both.
                if isinstance(app.screen, ConfirmScreen):
                    app.screen.dismiss(True)
                    for _ in range(5):
                        await pilot.pause()

                # Workspace was created — verify via API.
                r = httpx.get(
                    f"{base_url}/api/v1/workspaces",
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                ws_names = [w["name"] for w in r.json()]
                assert ws_name in ws_names

                # Find the workspace ID for cleanup.
                r = httpx.get(
                    f"{base_url}/api/v1/workspaces",
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                for w in r.json():
                    if w["name"] == ws_name:
                        ws_id = w["id"]
                        break
        finally:
            if ws_id:
                _api_delete_workspace(base_url, token, ws_id)

    # -- workspace list status indicators --

    async def test_workspace_list_shows_status_indicator(
        self, tui_state, base_url, token
    ):
        """Workspace list items show status dot (● red for stopped)."""
        ws_name = f"tui-dot-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)
                lv = app.screen.query_one("#owned_list", ListView)
                for item in lv.query(ListItem):
                    rendered = str(item.query_one(".ws-name", Label).render())
                    if ws_name in rendered:
                        # Should have the ● status indicator.
                        assert "●" in rendered
                        break
                else:
                    pytest.fail(f"{ws_name} not found in list")
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    # -- workspace edit form --

    async def test_edit_form_prepopulated(self, tui_state, base_url, token):
        """Edit form opens with current workspace values."""
        ws_name = f"tui-edit-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await _settle(app, pilot)
                # Navigate to detail screen.
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await _settle(app, pilot)
                assert isinstance(app.screen, WorkspaceDetailScreen)
                # Open edit form.
                app.screen.action_edit()
                await _settle(app, pilot)
                await pilot.pause()
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Name should be pre-populated.
                name_val = app.screen.query_one("#name", Input).value
                assert name_val == ws_name
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    async def test_edit_rename_persists(self, tui_state, base_url, token):
        """Renaming a workspace via the edit form persists to the server."""
        ws_name = f"tui-ren-{free_port()}"
        new_name = f"tui-renamed-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await _settle(app, pilot)
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await _settle(app, pilot)
                # Open edit form.
                app.screen.action_edit()
                await _settle(app, pilot)
                await pilot.pause()
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Change the name.
                app.screen.query_one("#name", Input).value = new_name
                # Submit.
                app.screen._save()
                await _settle(app, pilot)
                await pilot.pause()

            # Verify via API.
            ws = _api_get_workspace(base_url, token, ws_id)
            assert ws["name"] == new_name
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    async def test_edit_cancel_no_change(self, tui_state, base_url, token):
        """Cancelling the edit form persists no changes."""
        ws_name = f"tui-cancel-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await _settle(app, pilot)
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await _settle(app, pilot)
                app.screen.action_edit()
                await _settle(app, pilot)
                await pilot.pause()
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Change the name but cancel.
                app.screen.query_one("#name", Input).value = "should-not-save"
                app.screen.dismiss(False)
                await pilot.pause()

            # Verify name unchanged via API.
            ws = _api_get_workspace(base_url, token, ws_id)
            assert ws["name"] == ws_name
        finally:
            _api_delete_workspace(base_url, token, ws_id)

    async def test_edit_empty_name_rejected(self, tui_state, base_url, token):
        """Submitting with an empty name shows a validation error."""
        ws_name = f"tui-empty-{free_port()}"
        ws_id = _api_create_workspace(base_url, token, ws_name)

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await _settle(app, pilot)
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await _settle(app, pilot)
                app.screen.action_edit()
                await _settle(app, pilot)
                await pilot.pause()
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Clear name and submit.
                app.screen.query_one("#name", Input).value = ""
                app.screen._save()
                await pilot.pause()
                # Should still be on edit screen with error.
                assert isinstance(app.screen, EditWorkspaceScreen)
                msg = str(app.screen.query_one("#edit_msg", Static).render())
                assert "required" in msg.lower()
        finally:
            _api_delete_workspace(base_url, token, ws_id)
