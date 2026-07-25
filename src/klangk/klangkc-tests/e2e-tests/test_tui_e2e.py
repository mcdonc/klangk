"""E2E tests for the klangk TUI against a real backend.

Drives the real Textual TUI via in-process ``Pilot`` with a ``TuiState``
pointed at a real ``klangkd`` (TCP via proxy).  Asserts on rendered widget
state and real server/DB side-effects.

Run with: devenv shell -- test-cli-e2e -k TestTuiE2E
"""

import os
import sys
import tempfile

import httpx
import pytest

from textual.widgets import Label, ListView

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_server import start_server, stop_server  # noqa: E402

from klangk.cli.tui.app import KlangkApp  # noqa: E402
from klangk.cli.tui.screens import (  # noqa: E402
    LoginScreen,
    MainScreen,
    WorkspaceDetailScreen,
)
from klangk.cli.tui.state import TuiState  # noqa: E402
from klangk.cli.config import add_server_to_config, CLIState  # noqa: E402
from klangk.model import free_port  # noqa: E402

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


# ── tests ───────────────────────────────────────────────────────────────


class TestTuiE2E:
    """Drive the real TUI against a real backend."""

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
        # Create via API.
        ws_name = f"tui-e2e-{free_port()}"
        r = httpx.post(
            f"{base_url}/api/v1/workspaces",
            json={"name": ws_name},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        ws_id = r.json()["id"]

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)
                # The workspace should appear in the owned list.
                lv = app.screen.query_one("#owned_list", ListView)
                names = [str(lab.render()) for lab in lv.query(Label)]
                assert any(ws_name in n for n in names), (
                    f"{ws_name} not in {names}"
                )
        finally:
            # Clean up.
            httpx.delete(
                f"{base_url}/api/v1/workspaces/{ws_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

    async def test_workspace_detail_screen(self, tui_state, base_url, token):
        """Selecting a workspace opens the detail screen with correct info."""
        ws_name = f"tui-detail-{free_port()}"
        r = httpx.post(
            f"{base_url}/api/v1/workspaces",
            json={"name": ws_name},
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        ws_id = r.json()["id"]

        app = KlangkApp(tui_state)
        try:
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.pause()
                # Push detail screen.
                app.push_screen(WorkspaceDetailScreen(ws_name))
                await pilot.pause()
                await pilot.pause()
                assert isinstance(app.screen, WorkspaceDetailScreen)
                title = str(app.screen.query_one("#detail_title").render())
                assert ws_name in title
        finally:
            httpx.delete(
                f"{base_url}/api/v1/workspaces/{ws_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
