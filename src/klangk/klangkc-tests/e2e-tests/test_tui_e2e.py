"""E2E tests for the klangk TUI against a real backend.

Drives the real Textual TUI via in-process ``Pilot`` with a ``TuiState``
pointed at a real ``klangkd`` (TCP via proxy).  Asserts on rendered widget
state and real server/DB side-effects.

Run with: devenv shell -- test-cli-e2e -k TestTuiE2E
"""

import os
import sys
import time

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
from _e2e_server import start_server, stop_server, tracked_mkdtemp  # noqa: E402

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


async def _wait_for_worker(app, pilot, name, timeout=10.0):
    """Wait for a named worker to finish, with a timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        workers = [
            w for w in app.workers if w.name == name and not w.is_finished
        ]
        if not workers:
            break
        await pilot.pause()
    await pilot.pause()


async def _wait_for_screen(app, pilot, screen_type, timeout=30.0):
    """Wait for the app's active screen to be an instance of *screen_type*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if isinstance(app.screen, screen_type):
            break
        await pilot.pause()
    await pilot.pause()


async def _wait_for_workspace_loaded(app, pilot, timeout=30.0):
    """Wait until the detail screen's workspace data has loaded.

    ``WorkspaceDetailScreen.on_mount`` fetches the workspace from the API
    into ``screen._ws``; ``action_edit`` silently no-ops while ``_ws`` is
    still None, so a test that calls it right after the screen appears
    races the fetch under load (the edit screen is never pushed and
    ``_wait_for_screen`` times out). Poll until the data is there.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        screen = app.screen
        if (
            isinstance(screen, WorkspaceDetailScreen)
            and screen._ws is not None
        ):
            return
        await pilot.pause()
    await pilot.pause()


# ── server fixture ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def server():
    data_dir = tracked_mkdtemp("klangk-tui-e2e-")
    log_path = os.path.join(data_dir, "server.log")
    srv = start_server(
        uds=False,
        data_dir=data_dir,
        KLANGKD_JWT_SECRET="tui-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="tuiuser@example.com",
        KLANGKD_DEFAULT_PASSWORD="Tuipass1!",
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
        json={"identifier": "tuiuser@example.com", "password": "Tuipass1!"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


@pytest.fixture()
def tui_state(base_url, token, tmp_path, monkeypatch):
    """A TuiState pointed at the real server with a valid token."""
    # Write a temporary CLI config/state so TuiState can find the server.
    config_path = tmp_path / "klangk.yaml"
    state_path = tmp_path / "state.yaml"
    monkeypatch.setattr("klangk.cli.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("klangk.cli.config.STATE_PATH", state_path)
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
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def _api_delete_workspace(base_url, token, ws_id):
    """Delete a workspace via API (best-effort cleanup)."""
    httpx.delete(
        f"{base_url}/api/v1/workspaces/{ws_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def _api_get_workspace(base_url, token, ws_id):
    """Get a workspace by id via API (from the list endpoint)."""
    r = httpx.get(
        f"{base_url}/api/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    for ws in r.json():
        if ws["id"] == ws_id:
            return ws
    raise ValueError(f"Workspace {ws_id} not found")


def _api_wait_for_workspace_name(base_url, token, ws_id, name, timeout=15.0):
    """Poll the API until workspace ``ws_id``'s name becomes ``name``.

    The TUI edit form persists in a background worker (``save`` ->
    ``run_worker(do_save)``), so a rename lands asynchronously and reading
    the API immediately after ``save()`` races the worker's PUT (#2185).
    Poll until it lands instead of asserting on a single read.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _api_get_workspace(base_url, token, ws_id)
        if last["name"] == name:
            return last
        time.sleep(0.1)
    assert last is not None
    assert last["name"] == name, (
        f"workspace {ws_id} name never became {name!r} within {timeout}s "
        f"(last={last['name']!r})"
    )
    return last


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
        monkeypatch.setattr("klangk.cli.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config.STATE_PATH", state_path)
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
        monkeypatch.setattr("klangk.cli.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config.STATE_PATH", state_path)
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
            # Wait for the async login round-trip to land. Server-side
            # login ALWAYS pays a full PBKDF2-HMAC-SHA512 (600k iters —
            # dummy_verify_hash for unknown users, timing-leak defense),
            # which stretches well past 2s on a loaded CI runner (two
            # xdist workers + podman churn; #2740 flake family). Poll for
            # the message instead of a fixed sleep, bounded by the
            # client's own 15s HTTP budget.
            msg = ""
            deadline = asyncio.get_event_loop().time() + 15.0
            while asyncio.get_event_loop().time() < deadline:
                await pilot.pause()
                await asyncio.sleep(0.1)
                msg = str(app.screen.query_one("#message").render())
                if msg:
                    break
            # Should still be on login screen with an error message.
            assert isinstance(app.screen, LoginScreen)
            assert msg  # error message shown

    async def test_login_empty_fields(self, base_url, tmp_path, monkeypatch):
        """Login with empty fields shows validation message."""
        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config.STATE_PATH", state_path)
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
        monkeypatch.setattr("klangk.cli.config.CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config.STATE_PATH", state_path)

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
                # Wait for the initial list load to complete — two pauses
                # alone race the refresh-lists worker under CI load and the
                # list is still empty (same class as the #2539 flake).
                await _wait_for_worker(app, pilot, "refresh-lists")
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
            await _settle(app, pilot)
            assert isinstance(app.screen, MainScreen)
            # Wait for the initial list load to complete.
            await _wait_for_worker(app, pilot, "refresh-lists")
            # Verify workspace is in the list.
            lv = app.screen.query_one("#owned_list", ListView)
            names = [str(lab.render()) for lab in lv.query(Label)]
            assert any(ws_name in n for n in names)

            # Delete via API and refresh the list.
            _api_delete_workspace(base_url, token, ws_id)
            app.screen.refresh_lists()
            await _wait_for_worker(app, pilot, "refresh-lists")

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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
                # Wait for the workspace data to load — the body renders
                # empty until the on_mount fetch lands (same race class as
                # the #2539 flake).
                await _wait_for_workspace_loaded(app, pilot)
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
                await _wait_for_screen(app, pilot, CreateWorkspaceScreen)
                assert isinstance(app.screen, CreateWorkspaceScreen)

                # Fill in the name.
                app.screen.query_one("#name", Input).value = ws_name
                # Submit the form.
                app.screen.query_one("#create", Button).press()
                await pilot.pause()
                await pilot.pause()
                await pilot.pause()

                # Give the async create a moment to dispatch, then dismiss
                # any confirm dialog that may block navigation.
                await pilot.pause()
                await pilot.pause()
                if isinstance(app.screen, ConfirmScreen):
                    app.screen.dismiss(True)
                    await pilot.pause()

                # Workspace creation is async; poll the API until the workspace
                # appears rather than guessing a fixed wait window (CI runners
                # vary in load). The poll also captures the workspace id for
                # cleanup, replacing the separate follow-up GET.
                deadline = time.monotonic() + 30.0
                last_names: list[str] = []
                while True:
                    await pilot.pause()
                    r = httpx.get(
                        f"{base_url}/api/v1/workspaces",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30,
                    )
                    r.raise_for_status()
                    for w in r.json():
                        if w["name"] == ws_name:
                            ws_id = w["id"]
                            break
                    if ws_id is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            f"workspace {ws_name!r} not created within 30s; "
                            f"current workspaces: {last_names!r}"
                        )
                    last_names = [w["name"] for w in r.json()]
                    await asyncio.sleep(0.25)
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
                # Wait for the initial list load (see #2539 flake class).
                await _wait_for_worker(app, pilot, "refresh-lists")
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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
                assert isinstance(app.screen, WorkspaceDetailScreen)
                # Open edit form.  action_edit spawns async workers
                # (images + autostart fetch) before pushing the screen.
                # The edit action no-ops until the workspace data has
                # loaded; wait so the test doesn't race the fetch.
                await _wait_for_workspace_loaded(app, pilot)
                app.screen.action_edit()
                await _wait_for_screen(app, pilot, EditWorkspaceScreen)
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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
                # Open edit form.
                # The edit action no-ops until the workspace data has
                # loaded; wait so the test doesn't race the fetch.
                await _wait_for_workspace_loaded(app, pilot)
                app.screen.action_edit()
                await _wait_for_screen(app, pilot, EditWorkspaceScreen)
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Change the name.
                app.screen.query_one("#name", Input).value = new_name
                # Submit.
                app.screen.save()
                await _settle(app, pilot)
                await pilot.pause()

            # The edit form persists via a background worker (save ->
            # run_worker(do_save)), so a rename lands asynchronously; poll
            # until it shows up rather than racing the worker's PUT (#2185).
            ws = _api_wait_for_workspace_name(base_url, token, ws_id, new_name)
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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
                # The edit action no-ops until the workspace data has
                # loaded; wait so the test doesn't race the fetch.
                await _wait_for_workspace_loaded(app, pilot)
                app.screen.action_edit()
                await _wait_for_screen(app, pilot, EditWorkspaceScreen)
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
                await _wait_for_screen(app, pilot, WorkspaceDetailScreen)
                # The edit action no-ops until the workspace data has
                # loaded; wait so the test doesn't race the fetch.
                await _wait_for_workspace_loaded(app, pilot)
                app.screen.action_edit()
                await _wait_for_screen(app, pilot, EditWorkspaceScreen)
                assert isinstance(app.screen, EditWorkspaceScreen)
                # Clear name and submit.
                app.screen.query_one("#name", Input).value = ""
                app.screen.save()
                await pilot.pause()
                # Should still be on edit screen with error.
                assert isinstance(app.screen, EditWorkspaceScreen)
                msg = str(app.screen.query_one("#edit_msg", Static).render())
                assert "required" in msg.lower()
        finally:
            _api_delete_workspace(base_url, token, ws_id)
