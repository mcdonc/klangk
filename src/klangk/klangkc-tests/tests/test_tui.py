"""Tests for the klangk TUI foundation (issue #1746).

Covers the textual app shell, login/server-switch flows, the live state
bridge, the WebSocket status listener, the bare-``klangk`` launch wiring,
and the ``add_server_to_config`` helper — under the 100% coverage gate.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from rich.text import Text
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from klangk.cli import config as cfgmod
from klangk.cli import tui as tui_pkg
from klangk.cli.client import AuthError, Workspace, WorkspaceNotFoundError
from klangk.cli.tui import screens as scr
from klangk.cli.tui.screens import main as scr_main
from klangk.cli.tui.screens import workspace_detail as scr_detail
from klangk.cli.tui.screens import account as scr_account
from klangk.cli.tui import state as tui_state_mod
from klangk.cli.tui import ws as ws_mod
from klangk.cli.tui.app import KlangkApp, run_tui
from klangk.cli.tui.widgets import StatusBar
from klangk.cli.config import (
    AliasConflictError,
    CLIConfig,
    CLIState,
    ServerEntry,
    add_server_to_config,
    remove_server_from_config,
    update_server_in_config,
)
from klangk.cli.tui.screens import (
    AccountScreen,
    AddServerScreen,
    ConfirmScreen,
    CreateWorkspaceScreen,
    DuplicateScreen,
    EditServerScreen,
    EditWorkspaceScreen,
    InputScreen,
    LoginScreen,
    MainScreen,
    ServerSwitchScreen,
    TransferScreen,
    WorkspaceDetailScreen,
)
from klangk.cli.tui.state import LoginError, TuiState
from klangk.cli.tui.ws import listen_for_status


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redirect_xdg(monkeypatch, tmp_path):
    """Point CLI config/state files at tmp_path (never the user's real ones)."""
    cpath = tmp_path / "klangk.yaml"
    spath = tmp_path / "klangk-state.yaml"
    monkeypatch.setattr(cfgmod, "_CONFIG_PATH", cpath)
    monkeypatch.setattr(cfgmod, "_STATE_PATH", spath)
    # Prevent the local klangkd UDS socket from being detected.
    monkeypatch.setattr(
        tui_state_mod,
        "default_server_uds_path",
        lambda: str(tmp_path / "nonexistent.sock"),
    )
    return cpath, spath


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeOptionSelected:
    """Stand-in for OptionList.OptionSelected carrying an option id."""

    def __init__(self, option_id):
        self.option = type("Opt", (), {"id": option_id})()


class FakeSelected:
    """Stand-in for ListView.Selected carrying an item name."""

    def __init__(self, name):
        self.item = type("Item", (), {"name": name})()


def _lv_texts(list_view):
    """Rendered text of each Label in a ListView (content assertions)."""
    return [str(lab.render()) for lab in list_view.query(Label)]


class FakeBtnPress:
    """Stand-in for Button.Pressed carrying a button id."""

    def __init__(self, button_id):
        self.button = type("B", (), {"id": button_id})()


def _st(**methods):
    """A TuiState with the given methods overridden (for Pilot tests)."""
    st = TuiState()
    for k, v in methods.items():
        setattr(st, k, v)
    return st


def _authed_state(**extra):
    base = dict(
        is_authenticated=lambda: True,
        current_url=lambda: "https://x.example",
        email=lambda: "me@x.example",
        token=lambda: "tok",
        known_servers=lambda: [],
        list_owned_workspaces=lambda: [],
        list_shared_workspaces=lambda: [],
        list_terminals=_async_empty,
        close_terminal=_async_empty,
        restart_workspace=lambda n: None,
    )
    base.update(extra)
    return _st(**base)


def _ws(owned=None, shared=None, **extra):
    """Authed state whose workspace lists return the given workspaces."""
    base = dict(
        is_authenticated=lambda: True,
        current_url=lambda: "https://x.example",
        email=lambda: "me@x.example",
        token=lambda: "tok",
        known_servers=lambda: [],
        list_owned_workspaces=lambda: owned or [],
        list_shared_workspaces=lambda: shared or [],
        list_terminals=_async_empty,
        close_terminal=_async_empty,
        restart_workspace=lambda n: None,
    )
    base.update(extra)
    return _st(**base)


def _wsobj(name, **k):
    return Workspace(id="id-" + name, name=name, created_at="x", **k)


def _detail_value(body: str, label: str) -> str | None:
    """Return the value-column text for ``label``'s row in the workspace
    detail table, or None if no such row.

    The detail body renders as a two-column table (#1910): a label at the
    start of a line, then a column of padding (>=2 spaces), then the value.
    A label is matched only when it's followed by that padding gap, so
    ``health`` doesn't match the ``health note`` / ``health check`` rows.
    Multi-line value cells (mounts / environment / allowed domains) are
    rejoined with newlines."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith(label):
            continue
        rest = line[len(label) :]
        # "health" must not match "health note" (rest starts with a word,
        # not the column gap). The gap is always >=2 spaces.
        if rest.strip() and not rest[:2].isspace():
            continue
        parts = [rest.strip()] if rest.strip() else []
        for cont in lines[i + 1 :]:
            if not cont[:1].isspace():
                break
            if cont.strip():
                parts.append(cont.strip())
        return "\n".join(parts)
    return None


async def _async_empty(*a, **k):
    """Async stub for TuiState terminal methods (returns no terminals)."""
    return []


# Capture the real refresh loop before any test stubs it, so direct-call
# coverage tests can still exercise it despite the autouse stub below.
_real_run_token_refresh_loop = scr_main.run_token_refresh_loop


@pytest.fixture(autouse=True)
def _stub_tui_bg_workers(monkeypatch):
    """Stub MainScreen's on-mount bg workers for every TUI test.

    #1882's on_mount spawns a status-WS worker and a proactive token-refresh
    worker (a 60s-sleep loop). The refresh loop never completes during a
    test, wedging ``wait_for_complete()`` for any test that mounts a
    MainScreen without stubbing it. Tests that need the real refresh loop
    call ``_real_run_token_refresh_loop`` directly.
    """

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "run_token_refresh_loop", _noop)
    monkeypatch.setattr(scr_main, "listen_for_status", _noop)


# ---------------------------------------------------------------------------
# config.add_server_to_config
# ---------------------------------------------------------------------------


def test_add_server_creates_file(redirect_xdg):
    cpath, _ = redirect_xdg
    assert not cpath.exists()
    add_server_to_config("prod", "https://prod.example", user="me@x")
    loaded = CLIConfig.load()
    assert loaded.servers["prod"].url == "https://prod.example"
    assert loaded.servers["prod"].user == "me@x"


def test_add_server_merges_existing(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    loaded = CLIConfig.load()
    assert set(loaded.servers) == {"a", "b"}
    assert loaded.servers["a"].url == "https://a.example"


def test_add_server_rejects_duplicate_alias(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    with pytest.raises(AliasConflictError, match="'a' already exists"):
        add_server_to_config("a", "https://a2.example")
    # Original entry is preserved.
    loaded = CLIConfig.load()
    assert loaded.servers["a"].url == "https://a.example"


def test_remove_server_from_config(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    assert remove_server_from_config("a") is True
    assert set(CLIConfig.load().servers) == {"b"}
    # removing an absent alias is a no-op (False)
    assert remove_server_from_config("zzz") is False


def test_update_server_in_config(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    assert update_server_in_config("a", "a", "https://a2.example") is True
    loaded = CLIConfig.load()
    assert loaded.servers["a"].url == "https://a2.example"


def test_update_server_rename(redirect_xdg):
    add_server_to_config("old", "https://old.example")
    assert update_server_in_config("old", "new", "https://new.example") is True
    loaded = CLIConfig.load()
    assert "old" not in loaded.servers
    assert loaded.servers["new"].url == "https://new.example"


def test_update_server_not_found(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    assert (
        update_server_in_config("missing", "m", "https://m.example") is False
    )


def test_update_server_alias_collision(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    with pytest.raises(AliasConflictError):
        update_server_in_config("a", "b", "https://a.example")
    # Both entries untouched.
    loaded = CLIConfig.load()
    assert loaded.servers["a"].url == "https://a.example"
    assert loaded.servers["b"].url == "https://b.example"


def test_update_server_preserves_fields(redirect_xdg):
    add_server_to_config("a", "https://a.example", user="me@x")
    assert update_server_in_config("a", "a", "https://a2.example") is True
    loaded = CLIConfig.load()
    assert loaded.servers["a"].url == "https://a2.example"
    assert loaded.servers["a"].user == "me@x"


def test_update_server_sets_user(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    assert (
        update_server_in_config("a", "a", "https://a.example", user="new@x")
        is True
    )
    loaded = CLIConfig.load()
    assert loaded.servers["a"].user == "new@x"


def test_update_server_no_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "_CONFIG_PATH", tmp_path / "nope.yaml")
    assert update_server_in_config("a", "a", "https://x.example") is False


def test_update_server_bare_string_entry(redirect_xdg):
    """A hand-edited YAML with a bare-string entry is promoted to a dict."""
    cpath, _ = redirect_xdg
    cpath.write_text("servers:\n  legacy: https://old.example\n")
    assert (
        update_server_in_config("legacy", "legacy", "https://new.example")
        is True
    )
    loaded = CLIConfig.load()
    assert loaded.servers["legacy"].url == "https://new.example"


def test_remove_server_no_config_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cfgmod, "_CONFIG_PATH", tmp_path / "nope.yaml")
    assert remove_server_from_config("a") is False


# ---------------------------------------------------------------------------
# TuiState
# ---------------------------------------------------------------------------


def test_current_url_override_wins(redirect_xdg):
    st = CLIState()
    st.active_server = "https://active.example"
    st.save()
    assert TuiState().current_url() == "https://active.example"
    assert TuiState("https://override").current_url() == "https://override"


def test_current_url_none_when_unconfigured(redirect_xdg):
    t = TuiState()
    assert t.current_url() is None
    assert t.token() is None
    assert t.email() is None
    assert t.is_authenticated() is False


def test_reentry_auth_persists(redirect_xdg):
    """Credentials survive TuiState re-creation (#1813)."""
    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.save()

    # First TuiState instance — authenticated.
    t1 = TuiState()
    assert t1.is_authenticated()
    assert t1.current_url() == "https://srv.example"
    assert t1.token() == "tok123"

    # New TuiState reads persisted state — should also be authenticated.
    t2 = TuiState()
    assert t2.is_authenticated()
    assert t2.current_url() == "https://srv.example"
    assert t2.token() == "tok123"


def test_known_servers_roundtrip(redirect_xdg):
    add_server_to_config("alpha", "https://a.example")
    add_server_to_config("beta", "https://b.example")
    servers = TuiState().known_servers()
    assert {s.alias for s in servers} == {"alpha", "beta"}
    assert all(isinstance(s.url, str) for s in servers)


def test_token_email_client_from_state(redirect_xdg):
    st = CLIState()
    st.set_credentials("https://x.example", "me@x", "tok")
    st.save()
    t = TuiState()
    assert t.current_url() == "https://x.example"
    assert t.token() == "tok"
    assert t.email() == "me@x"
    assert t.is_authenticated() is True
    c = t.client()
    assert c.server_url == "https://x.example"
    assert c.token == "tok"


def test_auth_mode_variants(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod, "fetch_config", lambda url: tui_state_mod._UNREACHABLE
    )
    assert t.auth_mode() == "unreachable"

    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: None)
    assert t.auth_mode() == "password"

    monkeypatch.setattr(
        tui_state_mod, "fetch_config", lambda url: {"auth_modes": "oidc"}
    )
    assert t.auth_mode() == "oidc"

    # No server configured -> safe default.
    assert TuiState().auth_mode() == "password"


def test_validate_server_for_switch_unreachable(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod, "fetch_config", lambda url: tui_state_mod._UNREACHABLE
    )
    assert t.validate_server_for_switch("https://x.example") == "unreachable"


def test_validate_server_for_switch_not_klangk(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: None)
    assert t.validate_server_for_switch("https://x.example") == "unreachable"


def test_validate_server_for_switch_none_auth(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"auth_modes": "none"},
    )
    assert t.validate_server_for_switch("https://x.example") == "ok"


def test_validate_server_for_switch_no_token(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"auth_modes": "password"},
    )
    assert t.validate_server_for_switch("https://x.example") == "auth_required"


def test_validate_server_for_switch_token_valid(monkeypatch, redirect_xdg):
    add_server_to_config("x", "https://x.example")
    st = CLIState()
    st.set_credentials("https://x.example", "me@test", "tok123")
    st.save()
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"auth_modes": "password"},
    )
    monkeypatch.setattr(
        tui_state_mod,
        "http_request",
        lambda *a, **kw: FakeResp(200, {"email": "me@test"}),
    )
    assert t.validate_server_for_switch("https://x.example") == "ok"


def test_validate_server_for_switch_token_expired(monkeypatch, redirect_xdg):
    add_server_to_config("x", "https://x.example")
    st = CLIState()
    st.set_credentials("https://x.example", "me@test", "old-tok")
    st.save()
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"auth_modes": "password"},
    )
    monkeypatch.setattr(
        tui_state_mod,
        "http_request",
        lambda *a, **kw: FakeResp(401, {}),
    )
    assert t.validate_server_for_switch("https://x.example") == "auth_required"


def test_validate_server_for_switch_http_error(monkeypatch, redirect_xdg):
    add_server_to_config("x", "https://x.example")
    st = CLIState()
    st.set_credentials("https://x.example", "me@test", "tok123")
    st.save()
    t = TuiState("https://x.example")
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"auth_modes": "password"},
    )

    def raise_error(*a, **kw):
        raise httpx.ConnectError("fail")

    monkeypatch.setattr(tui_state_mod, "http_request", raise_error)
    assert t.validate_server_for_switch("https://x.example") == "unreachable"


def test_oidc_providers(monkeypatch, redirect_xdg):
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"oidc_providers": [{"id": "google"}]},
    )
    assert TuiState("https://x.example").oidc_providers() == [{"id": "google"}]
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: None)
    assert TuiState("https://x.example").oidc_providers() == []
    assert TuiState().oidc_providers() == []


def test_allow_autostart(monkeypatch, redirect_xdg):
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"allow_autostart": True},
    )
    assert TuiState("https://x.example").allow_autostart() is True
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"allow_autostart": False},
    )
    assert TuiState("https://x.example").allow_autostart() is False
    # missing field / non-dict / no server -> safe default False
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: {})
    assert TuiState("https://x.example").allow_autostart() is False
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: None)
    assert TuiState("https://x.example").allow_autostart() is False
    assert TuiState().allow_autostart() is False


async def test_create_terminal_delegates(monkeypatch, redirect_xdg):

    created = {}

    async def fake_create(name, window_name):
        created.update(name=name, window=window_name)
        return [
            {"index": 0, "name": "main"},
            {"index": 1, "name": window_name},
        ]

    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.create_terminal = fake_create
    t = TuiState("https://x.example")
    monkeypatch.setattr(t, "client", lambda: fake_client)
    result = await t.create_terminal("ws1", "term-1")
    assert created == {"name": "ws1", "window": "term-1"}
    assert len(result) == 2


def test_login_password_success(monkeypatch, redirect_xdg):
    captured = {}

    def fake_http(url, method, path, **kwargs):
        captured["sent"] = kwargs["json"]
        return FakeResp(200, {"access_token": "abc"})

    monkeypatch.setattr(tui_state_mod, "http_request", fake_http)
    email = TuiState("https://x.example").login_password("me@x", "pw")
    assert email == "me@x"
    assert captured["sent"] == {"identifier": "me@x", "password": "pw"}
    assert TuiState().token() == "abc"


def test_login_password_failures(monkeypatch, redirect_xdg):
    t = TuiState("https://x.example")

    with pytest.raises(LoginError):
        TuiState().login_password("a", "b")

    def boom(url, method, path, **kwargs):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(tui_state_mod, "http_request", boom)
    with pytest.raises(LoginError):
        t.login_password("a", "b")

    monkeypatch.setattr(
        tui_state_mod,
        "http_request",
        lambda *a, **k: FakeResp(401, {"detail": "bad creds"}),
    )
    with pytest.raises(LoginError, match="bad creds"):
        t.login_password("a", "b")

    class BadJson(FakeResp):
        def json(self):
            raise ValueError("no json")

    monkeypatch.setattr(
        tui_state_mod, "http_request", lambda *a, **k: BadJson(500)
    )
    with pytest.raises(LoginError, match="HTTP 500"):
        t.login_password("a", "b")

    monkeypatch.setattr(
        tui_state_mod, "http_request", lambda *a, **k: FakeResp(200, {})
    )
    with pytest.raises(LoginError, match="no access token"):
        t.login_password("a", "b")


def test_login_none(monkeypatch, redirect_xdg):
    # no server (empty state) -> LoginError
    with pytest.raises(LoginError):
        TuiState().login_none()

    monkeypatch.setattr(
        tui_state_mod, "local_login", lambda url: ("local", "tok")
    )
    assert TuiState("https://x.example").login_none() == "local"
    assert TuiState("https://x.example").token() == "tok"

    def die(url):
        raise SystemExit(1)

    monkeypatch.setattr(tui_state_mod, "local_login", die)
    with pytest.raises(LoginError):
        TuiState("https://x.example").login_none()


def test_oidc_login(monkeypatch, redirect_xdg):
    # no server (empty state) -> LoginError
    with pytest.raises(LoginError):
        TuiState().oidc_login("google")

    seen = {}

    def fake_oidc(url, provider_id, state):
        seen["args"] = (url, provider_id)
        state.set_credentials(url, "oidc@x", "otok")
        state.save()

    monkeypatch.setattr(tui_state_mod, "_oidc_browser_login", fake_oidc)
    TuiState("https://x.example").oidc_login("google")
    assert seen["args"] == ("https://x.example", "google")

    def die(*a):
        raise SystemExit(1)

    monkeypatch.setattr(tui_state_mod, "_oidc_browser_login", die)
    with pytest.raises(LoginError):
        TuiState("https://x.example").oidc_login("google")


def test_logout_switch_add(redirect_xdg):
    st = CLIState()
    st.set_credentials("https://x.example", "me@x", "tok")
    st.save()
    t = TuiState()
    assert t.is_authenticated()

    t.logout()
    assert TuiState().token() is None

    # logout with no server is a no-op
    TuiState().logout()

    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    TuiState().switch_server("https://b.example")
    assert TuiState().current_url() == "https://b.example"

    TuiState().add_server("c", "https://c.example", user="u")
    assert TuiState().current_url() == "https://c.example"
    loaded = CLIConfig.load()
    assert loaded.servers["c"].url == "https://c.example"
    assert loaded.servers["c"].user == "u"


def test_delete_server(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    TuiState().switch_server("https://a.example")  # make 'a' active
    assert TuiState().state().active_server == "https://a.example"

    # delete by url -> alias gone, active pointer cleared
    assert TuiState().delete_server("https://a.example") is True
    assert set(CLIConfig.load().servers) == {"b"}
    assert TuiState().state().active_server is None

    # not found
    assert TuiState().delete_server("https://nope.example") is False


def test_update_server(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    TuiState().switch_server("https://a.example")  # make 'a' active
    assert TuiState().state().active_server == "https://a.example"

    # update URL -> alias stays, active pointer updated
    assert TuiState().update_server("a", "a", "https://a2.example") is True
    loaded = CLIConfig.load()
    assert loaded.servers["a"].url == "https://a2.example"
    assert TuiState().state().active_server == "https://a2.example"

    # rename alias -> old gone, new present, active pointer preserved
    assert (
        TuiState().update_server("a", "renamed", "https://a2.example") is True
    )
    loaded = CLIConfig.load()
    assert "a" not in loaded.servers
    assert loaded.servers["renamed"].url == "https://a2.example"
    assert TuiState().state().active_server == "https://a2.example"

    # not found
    assert (
        TuiState().update_server("missing", "m", "https://m.example") is False
    )


# ---------------------------------------------------------------------------
# ws.listen_for_status
# ---------------------------------------------------------------------------


class FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class FakeCM:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *a):
        return False


async def test_listen_for_status_filters_and_forwards(monkeypatch):
    collected = []
    frames = [
        '{"type": "workspaces_changed"}',
        "not-json",
        "123",  # valid JSON but not a dict
        '{"type": "service_health"}',
    ]
    monkeypatch.setattr(
        ws_mod, "ws_connect", lambda *a, **k: FakeCM(FakeWS(frames))
    )
    await listen_for_status("/sock", "tok", on_event=collected.append)
    assert collected == [
        {"type": "workspaces_changed"},
        {"type": "service_health"},
    ]


# ---------------------------------------------------------------------------
# Pilot tests: app + screens
# ---------------------------------------------------------------------------


async def test_app_opens_login_when_unauthenticated():
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    async with app.run_test():
        assert isinstance(app.screen, LoginScreen)


async def test_app_uses_ansi_light_theme():
    """#1904: the TUI defaults to Textual's built-in ansi-light theme
    (terminal-palette-aware) instead of the hard-coded klangk palette. The
    klangk theme stays registered so it remains selectable."""
    from klangk.cli.tui.app import KLANGK_THEME

    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    # The default is set in __init__, before run_test mounts the app.
    assert app.theme == "ansi-light"
    async with app.run_test() as pilot:
        await pilot.pause()
        # Still ansi-light after mount (no on_mount override flips it).
        assert app.theme == "ansi-light"
        # The klangk theme stays registered — switching to it must not raise
        # (Textual raises ThemeError for an unknown theme name).
        app.theme = "klangk"
        await pilot.pause()
        assert app.theme == "klangk"
        assert KLANGK_THEME.name == "klangk"


async def test_app_none_mode_auto_logs_in():
    flag = {"ok": False}

    def fake_none():
        flag["ok"] = True
        st.is_authenticated = lambda: True
        return "local"

    st = _st(
        auth_mode=lambda: "none",
        is_authenticated=lambda: False,
        login_none=fake_none,
        current_url=lambda: "/sock",
        email=lambda: "local",
        token=lambda: "tok",
        known_servers=lambda: [],
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()  # let the deferred no-auth login run
        assert isinstance(app.screen, MainScreen)
    assert flag["ok"] is True


async def test_app_none_mode_failure_falls_back_to_login():
    def boom():
        raise LoginError("nope")

    st = _st(
        auth_mode=lambda: "none",
        is_authenticated=lambda: False,
        login_none=boom,
        current_url=lambda: "/sock",
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()  # deferred no-auth attempt runs + fails
        assert isinstance(app.screen, LoginScreen)
        assert "No-auth login failed" in str(
            app.screen.query_one("#message").render()
        )


async def test_main_screen_renders_status(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        bar = screen.query_one("#status")
        assert "https://x.example" in str(bar.render())
        assert "me@x.example" in str(bar.render())


async def test_main_screen_status_event_updates_live_extra(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        screen = app.screen
        screen._on_status_event({"type": "service_health"})
        await pilot.pause()
        assert app.live_extra == "live: service_health"
        assert "live: service_health" in str(
            screen.query_one("#status").render()
        )


async def test_refresh_status_no_widget(monkeypatch):
    """_refresh_status is a no-op when #status is not mounted yet."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test():
        screen = app.screen
        # Simulate the #status widget being absent (e.g. before mount).
        orig = screen.query_one
        from textual.dom import NoMatches as _NM

        def raise_no_status(sel, *a):
            if sel == "#status":
                raise _NM("no #status")
            return orig(sel, *a)

        screen.query_one = raise_no_status
        # Should not raise — the except NoMatches guard handles it.
        screen._refresh_status()
        screen.query_one = orig


async def test_status_loop_no_token_returns_early(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state(token=lambda: None))
    async with app.run_test():
        await app.screen._status_loop()  # no token -> early return


async def test_status_loop_handles_disconnect(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("ws died")

    monkeypatch.setattr(scr_main, "listen_for_status", boom)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await app.screen._status_loop()
        await pilot.pause()
        assert "status: disconnected" in app.live_extra


async def test_login_password_flow_success(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def fake_login(identifier, password):
        st.is_authenticated = lambda: True
        st.email = lambda: identifier
        st.token = lambda: "tok"
        return identifier

    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        known_servers=lambda: [],
        login_password=fake_login,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        login.query_one("#identifier", Input).value = "me@x"
        login.query_one("#password", Input).value = "pw"
        login._attempt_password()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_login_password_flow_empty_and_fail():
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    async with app.run_test() as _pilot:
        login = app.screen
        login._attempt_password()  # empty fields
        await app.workers.wait_for_complete()
        assert "required" in str(login.query_one("#message").render())

        st.login_password = lambda a, b: (_ for _ in ()).throw(
            LoginError("bad creds")
        )
        login.query_one("#identifier", Input).value = "me@x"
        login.query_one("#password", Input).value = "pw"
        login._attempt_password()
        await app.workers.wait_for_complete()
        assert "bad creds" in str(login.query_one("#message").render())
        assert isinstance(app.screen, LoginScreen)


async def test_login_input_submitted_triggers_password(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def fake_login(identifier, password):
        st.is_authenticated = lambda: True
        st.email = lambda: identifier
        st.token = lambda: "tok"
        return identifier

    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        known_servers=lambda: [],
        login_password=fake_login,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        ident = login.query_one("#identifier", Input)
        ident.value = "me@x"
        login.query_one("#password", Input).value = "pw"
        login.on_input_submitted(Input.Submitted(ident, ident.value))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_login_oidc_flow(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    # success
    def fake_oidc(provider_id):
        st.is_authenticated = lambda: True
        st.token = lambda: "otok"
        st.email = lambda: "oidc@x"

    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "oidc",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        oidc_providers=lambda: [{"id": "google"}],
        oidc_login=fake_oidc,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.screen._attempt_oidc()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    # no providers -> message
    st2 = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "oidc",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        oidc_providers=lambda: [],
    )
    app2 = KlangkApp(st2)
    async with app2.run_test() as pilot:
        app2.screen._attempt_oidc()
        await app2.workers.wait_for_complete()
        assert "SSO provider" in str(
            app2.screen.query_one("#message").render()
        )

    # failure -> message
    st3 = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "oidc",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        oidc_providers=lambda: [{"id": "google"}],
        oidc_login=lambda pid: (_ for _ in ()).throw(LoginError("nope")),
    )
    app3 = KlangkApp(st3)
    async with app3.run_test() as pilot:
        app3.screen._attempt_oidc()
        await app3.workers.wait_for_complete()
        assert "SSO failed" in str(app3.screen.query_one("#message").render())


async def test_login_unreachable_mode():
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "unreachable",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    async with app.run_test():
        assert "Cannot reach" in str(app.screen.query_one("#notice").render())


async def test_login_oidc_button_visibility_by_auth_mode(monkeypatch):
    """#1864: the "Log in via browser" button renders only when the server
    offers OIDC (auth mode ``oidc`` or ``both``); otherwise it's hidden
    entirely (``display``, not just ``disabled``)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    async def oidc_display(mode, **extra):
        st = _st(
            is_authenticated=lambda: False,
            auth_mode=lambda: mode,
            current_url=lambda: "https://x.example",
            email=lambda: None,
            token=lambda: None,
            **extra,
        )
        app = KlangkApp(st)
        async with app.run_test() as pilot:
            await pilot.pause()  # let the deferred no-auth attempt schedule
            await app.workers.wait_for_complete()
            await pilot.pause()
            return app.screen.query_one("#oidc", Button).display

    assert await oidc_display("oidc") is True
    assert await oidc_display("both") is True
    assert await oidc_display("password") is False
    assert await oidc_display("unreachable") is False
    # none: force the deferred no-auth attempt to fail so the screen stays on
    # LoginScreen and the button can be read.
    assert (
        await oidc_display(
            "none",
            login_none=lambda: (_ for _ in ()).throw(LoginError("nope")),
        )
        is False
    )

    # No server selected at all -> hidden too.
    st = _st(
        is_authenticated=lambda: False,
        current_url=lambda: None,
        known_servers=lambda: [],
        email=lambda: None,
        token=lambda: None,
    )
    app = KlangkApp(st)
    async with app.run_test():
        assert app.screen.query_one("#oidc", Button).display is False


async def test_logout_returns_to_login(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    called = {"out": False}

    def fake_logout():
        called["out"] = True
        st.is_authenticated = lambda: False
        st.token = lambda: None

    st = _authed_state(logout=fake_logout)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, MainScreen)
        app.do_logout()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
    assert called["out"] is True


async def test_server_switch_selects_and_returns():
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
            tui_state_mod.ServerInfo("b", "https://b.example"),
        ],
        current_url=lambda: "https://a.example",
    )
    switched = {}
    st.switch_server = lambda url: switched.setdefault("url", url)
    st.validate_server_for_switch = lambda url: "ok"
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)
        app.screen.on_list_view_selected(FakeSelected("https://b.example"))
        await app.workers.wait_for_complete()
        assert switched["url"] == "https://b.example"
        assert isinstance(app.screen, MainScreen)


async def test_server_switch_empty_name_noop():
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
        ],
    )
    switched = {}
    st.switch_server = lambda url: switched.setdefault("url", url)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.screen.on_list_view_selected(FakeSelected(""))
        await app.workers.wait_for_complete()
        assert switched == {}


async def test_server_switch_no_servers_hint():
    app = KlangkApp(_authed_state(known_servers=lambda: []))
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        assert "No servers" in str(
            app.screen.query_one("#switch_msg").render()
        )


async def test_add_server_succeeds_returns_to_main():
    st = _authed_state()
    added = {}
    st.add_server = lambda alias, url, user=None: added.setdefault(
        "a", (alias, url)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        add_screen = app.screen
        add_screen.query_one("#alias", Input).value = "prod"
        add_screen.query_one("#url", Input).value = "https://p.example"
        add_screen._add()
        await app.workers.wait_for_complete()
        assert added["a"] == ("prod", "https://p.example")
        assert isinstance(app.screen, MainScreen)


async def test_add_server_empty_fields_error():
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        app.screen._add()
        await app.workers.wait_for_complete()
        assert "required" in str(app.screen.query_one("#add_msg").render())


async def test_add_server_input_submit():
    st = _authed_state()
    added = {}
    st.add_server = lambda alias, url, user=None: added.setdefault(
        "a", (alias, url)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        s = app.screen
        alias_input = s.query_one("#alias", Input)
        alias_input.value = "staging"
        url_input = s.query_one("#url", Input)
        url_input.value = "https://s.example"
        s.on_input_submitted(Input.Submitted(url_input, url_input.value))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert added["a"] == ("staging", "https://s.example")


# --- server switch validation (#1842) ---


async def test_server_switch_unreachable(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
            tui_state_mod.ServerInfo("b", "https://b.example"),
        ],
        current_url=lambda: "https://a.example",
    )
    switched = {}
    st.switch_server = lambda url: switched.setdefault("url", url)
    st.validate_server_for_switch = lambda url: "unreachable"
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.wait_for_scheduled_animations()
        app.screen.on_list_view_selected(FakeSelected("https://b.example"))
        await app.workers.wait_for_complete()
        # switch_server should NOT have been called
        assert switched == {}
        # should still be on ServerSwitchScreen with error message
        assert isinstance(app.screen, ServerSwitchScreen)
        msg = str(app.screen.query_one("#switch_msg").render())
        assert "Cannot reach" in msg


async def test_server_switch_auth_required(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
            tui_state_mod.ServerInfo("b", "https://b.example"),
        ],
        current_url=lambda: "https://a.example",
    )
    switched = {}
    st.switch_server = lambda url: switched.setdefault("url", url)
    st.validate_server_for_switch = lambda url: "auth_required"
    app = KlangkApp(st)
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.wait_for_scheduled_animations()
        app.screen.on_list_view_selected(FakeSelected("https://b.example"))
        await app.workers.wait_for_complete()
        await pilot.wait_for_scheduled_animations()
        # switch_server IS called (server changed)
        assert switched["url"] == "https://b.example"
        # should land on LoginScreen
        assert isinstance(app.screen, scr.LoginScreen)


# --- edit server (#1762) ---


async def test_edit_server_saves(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        current_url=lambda: "https://prod.example",
    )
    updated = {}
    st.update_server = lambda old, new, url, user=None: (
        updated.__setitem__("u", (old, new, url)) or True
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        # Highlight the first server and edit it.
        lv = app.screen.query_one("#server_options", ListView)
        lv.index = 0
        await pilot.pause()
        app.screen.action_edit_server()
        await pilot.pause()
        assert isinstance(app.screen, EditServerScreen)
        app.screen.query_one("#alias", Input).value = "production"
        # Keep URL unchanged — no server_changed() call.
        app.screen._save()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert updated["u"] == (
            "prod",
            "production",
            "https://prod.example",
        )
        # Dismissed back to ServerSwitchScreen (URL unchanged).
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_server_switch_hints_inline_not_in_footer(monkeypatch):
    """#1872: server-scoped keys (e/d) are hinted inline on the servers list
    header and hidden from the Footer."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        current_url=lambda: "https://prod.example",
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()

        # (a) Server action hints render on the list header, with the literal
        # [e] / [d] keycaps intact (not eaten as Rich markup).
        hints = str(app.screen.query_one("#server_hints").render())
        assert "[e]" in hints
        assert "[d]" in hints
        assert "edit" in hints
        assert "delete" in hints

        bindings = {b.key: b for b in app.screen.BINDINGS}

        # (b) Server keys still exist (so the keys work) but are hidden.
        assert bindings["e"].show is False
        assert bindings["d"].show is False

        # (c) The screen-level Back key remains visible.
        assert bindings["escape"].show is True


async def test_edit_server_empty_fields(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        app.screen.query_one("#alias", Input).value = ""
        app.screen._save()
        await app.workers.wait_for_complete()
        assert "required" in str(
            app.screen.query_one("#edit_srv_msg").render()
        )


async def test_edit_server_invalid_url(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        app.screen.query_one("#url", Input).value = "not-a-url"
        app.screen._save()
        await app.workers.wait_for_complete()
        assert "http(s)://" in str(
            app.screen.query_one("#edit_srv_msg").render()
        )


async def test_edit_server_cancel(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
        ],
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        app.screen.action_cancel()
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_edit_server_not_found(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state()
    st.update_server = lambda *a, **k: False
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(
            EditServerScreen(alias="gone", url="https://g.example")
        )
        await pilot.pause()
        app.screen._save()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert (
            "not found"
            in str(app.screen.query_one("#edit_srv_msg").render()).lower()
        )


async def test_edit_server_alias_conflict(monkeypatch):
    """Renaming to an existing alias shows an error."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state()

    def conflict(*a, **k):
        from klangk.cli.config import AliasConflictError

        raise AliasConflictError("Alias 'b' already exists.")

    st.update_server = conflict
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        app.screen.query_one("#alias", Input).value = "b"
        app.screen._save()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert (
            "already exists"
            in str(app.screen.query_one("#edit_srv_msg").render()).lower()
        )


async def test_edit_server_url_change_triggers_server_changed(monkeypatch):
    """Changing the URL of a server calls server_changed()."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
        ],
    )
    st.update_server = lambda *a, **k: True
    changed = []
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.server_changed = lambda: changed.append(True)
        # Go through action_edit_server so the real _on_edit callback is wired.
        lv = app.screen.query_one("#server_options", ListView)
        lv.index = 0
        await pilot.pause()
        app.screen.action_edit_server()
        await pilot.pause()
        assert isinstance(app.screen, EditServerScreen)
        app.screen.query_one("#url", Input).value = "https://new.example"
        app.screen._save()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert changed == [True]


async def test_edit_server_no_highlight(monkeypatch):
    """action_edit_server is a no-op when no server is highlighted."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state(known_servers=lambda: []))
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.screen.action_edit_server()
        await pilot.pause()
        # Still on switch screen — no crash, no edit screen.
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_edit_server_via_input_submit(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state()
    updated = {}
    st.update_server = lambda old, new, url, user=None: (
        updated.__setitem__("u", (old, new, url)) or True
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        url_input = app.screen.query_one("#url", Input)
        url_input.value = "https://new.example"
        app.screen.on_input_submitted(
            Input.Submitted(url_input, url_input.value)
        )
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert updated["u"] == ("a", "a", "https://new.example")


async def test_edit_server_cancel_button(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
        ],
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        cancel_btn = app.screen.query_one("#cancel", Button)
        app.screen.on_button_pressed(Button.Pressed(cancel_btn))
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_edit_server_no_alias_on_item(monkeypatch):
    """action_edit_server is a no-op when the item has no server_alias."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
        ],
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        lv = app.screen.query_one("#server_options", ListView)
        lv.index = 0
        await pilot.pause()
        # Remove the server_alias attribute to hit the early return.
        child = lv.highlighted_child
        if hasattr(child, "server_alias"):
            del child.server_alias
        app.screen.action_edit_server()
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_edit_server_via_button(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state()
    updated = {}
    st.update_server = lambda old, new, url, user=None: (
        updated.__setitem__("u", (old, new, url)) or True
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(EditServerScreen(alias="a", url="https://a.example"))
        await pilot.pause()
        s = app.screen
        save_btn = s.query_one("#save", Button)
        s.on_button_pressed(Button.Pressed(save_btn))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "u" in updated


# --- workspace list / detail / actions (#1747) ---


def test_tui_state_workspace_methods(monkeypatch, redirect_xdg):
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.list_workspaces.return_value = [_wsobj("a")]
    fake.list_shared_workspaces.return_value = [_wsobj("b")]
    fake.resolve_workspace.return_value = _wsobj("a")
    fake.duplicate_workspace.return_value = {"id": "3", "name": "c"}
    st = TuiState("https://x.example")
    monkeypatch.setattr(st, "client", lambda: fake)
    assert st.list_owned_workspaces()[0].name == "a"
    assert st.list_shared_workspaces()[0].name == "b"
    assert st.find_workspace("a").name == "a"
    st.restart_workspace("a")
    st.stop_workspace("a")
    st.start_workspace("a")
    st.delete_workspace("a")
    assert st.duplicate_workspace("a", "c") == {"id": "3", "name": "c"}
    fake.restart_workspace.assert_called_once_with("a")
    fake.stop_workspace.assert_called_once_with("a")
    fake.start_workspace.assert_called_once_with("a")
    fake.delete_workspace.assert_called_once_with("a")
    fake.duplicate_workspace.assert_called_once_with("a", "c")

    fake.create_workspace.return_value = _wsobj("new")
    fake.list_images.return_value = {"default": "base", "allowed": ["base"]}
    created = st.create_workspace("new", image="base", mounts=["/h:/c"])
    assert created.name == "new"
    assert st.list_images() == {"default": "base", "allowed": ["base"]}
    fake.create_workspace.assert_called_once_with(
        "new",
        image="base",
        service_command=None,
        auto_start=False,
        mounts=["/h:/c"],
        env=None,
        health_check=None,
        allowed_domains=None,
    )
    fake.list_images.assert_called_once_with()

    # #1778: update_workspace forwards to the client.
    st.update_workspace("id-x", name="renamed", allowed_domains=["a.com"])
    fake.update_workspace.assert_called_once_with(
        "id-x", name="renamed", allowed_domains=["a.com"]
    )


def test_tui_state_terminal_methods(monkeypatch, redirect_xdg):
    from unittest.mock import AsyncMock, MagicMock

    import asyncio

    fake = MagicMock()
    fake.list_terminals = AsyncMock(return_value=[{"index": 0, "name": "m"}])
    fake.close_terminal = AsyncMock(return_value=[])
    st = TuiState("https://x.example")
    monkeypatch.setattr(st, "client", lambda: fake)
    assert asyncio.run(st.list_terminals("a")) == [{"index": 0, "name": "m"}]
    assert asyncio.run(st.close_terminal("a", 0)) == []
    fake.list_terminals.assert_called_once_with("a")
    fake.close_terminal.assert_called_once_with("a", 0)


async def test_main_screen_lists_and_status(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(
        _ws(
            owned=[
                _wsobj("alpha", running=True, health="healthy"),
                _wsobj("beta"),
            ],
            shared=[_wsobj("gamma", owner_email="o@x")],
        )
    )
    async with app.run_test():
        m = app.screen
        assert len(m.query_one("#owned_list", ListView).query(ListItem)) == 2
        assert len(m.query_one("#shared_list", ListView).query(ListItem)) == 1
        status = str(m.query_one("#status").render())
        assert "https://x.example" in status
        assert "me@x.example" in status


# ---------------------------------------------------------------------------
# MainScreen per-workspace actions (act on the highlighted row, #1878)
# ---------------------------------------------------------------------------


async def _settle(app):
    """Deterministically drain pending workers.

    ``pilot.pause()`` is a one-tick, nondeterministic wait. The proper
    primitive is ``app.workers.wait_for_complete()`` — but
    ``MainScreen.refresh_lists`` registers its worker ``exclusive=True``, so
    a refresh spawned mid-test (e.g. after an action) cancels a still-pending
    one, and ``wait_for_complete()`` raises ``WorkerCancelled`` on the
    cancelled worker. That cancellation is *expected* (it's how exclusive
    workers avoid overlapping fetches), so retry until the pool drains.
    """
    from textual.worker import WorkerCancelled, WorkerFailed

    while app.workers:
        try:
            await app.workers.wait_for_complete()
        except (WorkerCancelled, WorkerFailed):
            continue
        break


async def _highlight_first(pilot, app, *, pane="#owned_list"):
    """Populate the MainScreen, highlight row 0, return the screen."""
    await _settle(app)
    m = app.screen
    lv = m.query_one(pane, ListView)
    lv.focus()
    lv.index = 0
    await pilot.pause()  # let the Highlighted event propagate
    return m


async def test_main_screen_action_hints_toggle_stop_start(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(
        _ws(
            owned=[
                _wsobj("alpha", running=True),
                _wsobj("beta", running=False),
            ]
        )
    )
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        hints = str(m.query_one(".ws_hints", Static).render())
        assert "restart" in hints and "s stop" in hints
        assert "open" in hints and "dup" in hints
        assert "del" in hints and "edit" in hints
        # Highlight the stopped row -> label flips to 'start'.
        lv = m.query_one("#owned_list", ListView)
        lv.index = 1
        await pilot.pause()
        m._refresh_action_hints()
        assert "s start" in str(m.query_one(".ws_hints", Static).render())


async def test_main_screen_action_requires_highlight(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)
        lv.index = None  # nothing highlighted
        await pilot.pause()
        # Every per-workspace action guards on a highlighted row.
        for action in (
            "action_restart",
            "action_stop",
            "action_duplicate",
            "action_delete",
            "action_edit",
        ):
            getattr(m, action)()
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
        assert "Select a workspace" in (app.live_extra or "")


async def test_main_screen_action_stop_cancel(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    stopped = {}
    st = _ws(owned=[_wsobj("alpha", running=True)])
    st.stop_workspace = lambda n: stopped.__setitem__("s", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_stop()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)  # cancel
        await pilot.pause()
        assert "s" not in stopped


async def test_main_screen_action_delete_cancel(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    deleted = {}
    st = _ws(owned=[_wsobj("alpha")])
    st.delete_workspace = lambda n: deleted.__setitem__("d", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)  # cancel
        await pilot.pause()
        assert "d" not in deleted


async def test_main_screen_action_duplicate_cancel(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    duped = {}
    st = _ws(owned=[_wsobj("alpha")])
    st.duplicate_workspace = lambda n, nn: duped.__setitem__("d", (n, nn))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_duplicate()
        await pilot.pause()
        assert isinstance(app.screen, DuplicateScreen)
        app.screen.on_button_pressed(FakeBtnPress("cancel"))  # cancel
        await pilot.pause()
        assert "d" not in duped


# --- import / export with progress (#1758) ---


def test_tui_state_export_import(monkeypatch, redirect_xdg):
    """TuiState.export/import forward to the client, resolving name->id."""
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.resolve_workspace.return_value = _wsobj("a")
    fake.import_workspace.return_value = _wsobj("imp")
    st = TuiState("https://x.example")
    monkeypatch.setattr(st, "client", lambda: fake)

    # export resolves the name to an id, then downloads to the path.
    st.export_workspace("a", Path("a.tar.gz"))
    fake.resolve_workspace.assert_called_once_with("a")
    fake.export_workspace.assert_called_once_with(
        "id-a", Path("a.tar.gz"), on_progress=None
    )

    # import forwards the archive + optional name + progress callback.
    assert st.import_workspace(Path("imp.tar.gz")).name == "imp"
    fake.import_workspace.assert_called_once_with(
        Path("imp.tar.gz"), name=None, on_progress=None
    )

    # on_progress is threaded straight through to the client.
    def cb(d, t):
        return None

    st.export_workspace("a", Path("a.tar.gz"), on_progress=cb)
    fake.export_workspace.assert_called_with(
        "id-a", Path("a.tar.gz"), on_progress=cb
    )


def test_fmt_transfer_known_unknown_and_units():
    """Byte formatter covers B/KB/MB/GB and the unknown-total branch."""
    from klangk.cli.tui.screens._base import _fmt_transfer, _human_bytes

    assert _human_bytes(0) == "0.0 B"
    assert _human_bytes(512).endswith("B")
    assert _human_bytes(1536) == "1.5 KB"
    assert _human_bytes(2 * 1024 * 1024) == "2.0 MB"
    assert _human_bytes(3 * 1024**3) == "3.0 GB"
    assert _fmt_transfer(1536, 4096) == "1.5 KB / 4.0 KB"
    assert _fmt_transfer(9999, None) == "9.8 KB (size unknown)"


async def test_input_screen_ok_cancel_and_enter(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        cap = {}
        app.push_screen(
            InputScreen("Path:", default="x.tar.gz", ok_label="Go"),
            lambda r: cap.__setitem__("r", r),
        )
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        # OK with the default value commits it.
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        assert cap["r"] == "x.tar.gz"

        # Cancel dismisses with None.
        app.push_screen(
            InputScreen("Path:"), lambda r: cap.__setitem__("c", r)
        )
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        assert cap["c"] is None

        # Empty value on OK -> None.
        app.push_screen(
            InputScreen("Path:"), lambda r: cap.__setitem__("e", r)
        )
        await pilot.pause()
        app.screen.query_one("#inp_value", Input).value = "   "
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        assert cap["e"] is None

        # Enter submits the focused input.
        app.push_screen(
            InputScreen("Path:", default="z"),
            lambda r: cap.__setitem__("s", r),
        )
        await pilot.pause()
        inp = app.screen.query_one("#inp_value", Input)
        app.screen.on_input_submitted(Input.Submitted(input=inp, value="z"))
        await pilot.pause()
        assert cap["s"] == "z"


async def test_transfer_screen_success_error_and_progress(monkeypatch):
    """TransferScreen drives the bar from the worker thread and reports
    success/failure via its dismiss value (#1758)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))

    async with app.run_test() as pilot:
        cap = {}

        # Success: on_progress fires from the thread with both a known and
        # an unknown total (covers both _update branches), then dismisses.
        def ok_call(on_progress):
            on_progress(50, 200)
            on_progress(80, None)
            return None

        app.push_screen(
            TransferScreen("Working", ok_call, "done!"),
            lambda r: cap.__setitem__("ok", r),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert cap["ok"] == (True, "done!")

        # Failure: make_call raises -> dismisses with (False, str(exc)).
        def bad_call(on_progress):
            raise RuntimeError("boom")

        app.push_screen(
            TransferScreen("Working", bad_call, "done!"),
            lambda r: cap.__setitem__("bad", r),
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert cap["bad"] == (False, "boom")


async def test_detail_export_flow_with_progress(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    exported = {}

    def fake_export(name, out, on_progress=None):
        if on_progress:
            on_progress(10, 100)
            on_progress(100, 100)
        exported["args"] = (name, str(out))
        return None

    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.export_workspace = fake_export
    app = KlangkApp(st)
    # Spy on app.notify so we can assert the completion toast (#1758):
    # a resolved absolute filesystem path is toasted on success.
    notified = []
    orig_notify = app.notify

    def spy_notify(message="", *a, **k):
        notified.append(message)
        return orig_notify(message, *a, **k)

    app.notify = spy_notify
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        # 'x' opens the export prompt prefilled with <name>.tar.gz.
        d.action_export()
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        app.screen.on_button_pressed(FakeBtnPress("ok"))  # accept default
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        # Transfer ran (mock completes instantly) -> back on detail. The
        # export is written to the *resolved absolute* path (the relative
        # default is resolved against the TUI's CWD).
        resolved = str(Path("alpha.tar.gz").resolve())
        assert exported["args"] == ("alpha", resolved)
        # A completion toast fired carrying the full filesystem path.
        assert any(resolved in m for m in notified), notified


async def test_detail_export_failure_shows_inline_error(monkeypatch):
    """#1758: on export failure the error text is shown inline on the
    detail screen (not toasted) — the error path of _on_export_done."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)

    def fake_export(name, out, on_progress=None):
        raise RuntimeError("disk full")

    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.export_workspace = fake_export
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        d.action_export()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("ok"))  # accept default
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        # Failure -> error text rendered inline on the detail screen.
        assert "disk full" in str(d.query_one("#detail_msg", Static).render())


async def test_detail_export_cancel_aborts(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        d.action_export()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        # No transfer pushed; still on the detail screen.
        assert not isinstance(app.screen, TransferScreen)


async def test_main_import_missing_file_flashes(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _ws(owned=[_wsobj("alpha")])
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_import()
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        app.screen.query_one(
            "#inp_value", Input
        ).value = "/no/such/nope.tar.gz"
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        # Missing file -> no transfer, error flashed on the status bar.
        assert not isinstance(app.screen, TransferScreen)
        status = m.query_one("#status", StatusBar)
        assert "not found" in str(status.render())


async def test_main_import_flow_with_progress(monkeypatch, tmp_path):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    archive = tmp_path / "imp.tar.gz"
    archive.write_bytes(b"PK\x03\x04payload")
    imported = {}

    def fake_import(path, name=None, on_progress=None):
        if on_progress:
            on_progress(8, 16)
            on_progress(16, 16)
        imported["path"] = str(path)
        return _wsobj("imp")

    st = _ws(owned=[_wsobj("alpha")])
    st.import_workspace = fake_import
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        refreshed = {}
        m.refresh_lists = lambda: refreshed.__setitem__("r", True)
        m.action_import()
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        app.screen.query_one("#inp_value", Input).value = str(archive)
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        assert imported["path"] == str(archive)
        assert refreshed.get("r") is True  # list refreshed on success
        status = m.query_one("#status", StatusBar)
        assert "Imported" in str(status.render())


async def test_main_import_cancel_aborts(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _ws(owned=[_wsobj("alpha")])
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_import()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        assert not isinstance(app.screen, TransferScreen)


async def test_main_screen_action_edit_load_fallbacks(monkeypatch):
    """_do_edit tolerates find_workspace/list_images/allow_autostart errors."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha")
    # Generic load error -> flashed, no screen pushed.
    st = _ws(owned=[a])
    st.find_workspace = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Could not load workspace" in (app.live_extra or "")

    # find_workspace ok but list_images / allow_autostart raise -> the edit
    # screen still opens with empty/default fallbacks.
    st2 = _ws(owned=[a])
    st2.find_workspace = lambda n: a
    st2.list_images = lambda: (_ for _ in ()).throw(RuntimeError("img"))
    st2.allow_autostart = lambda: (_ for _ in ()).throw(RuntimeError("auto"))
    app2 = KlangkApp(st2)
    async with app2.run_test() as pilot:
        m = await _highlight_first(pilot, app2)
        m.action_edit()
        await app2.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app2.screen, EditWorkspaceScreen)


async def test_main_screen_on_edited_refreshes(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        called = {}
        m.refresh_lists = lambda: called.__setitem__("r", True)
        m._on_edited(True)  # truthy result -> refresh
        assert called.get("r") is True
        m._on_edited(None)  # falsy -> no refresh
        assert called.get("r") is True  # unchanged


async def test_main_screen_update_running_refreshes_hints(monkeypatch):
    """_update_running reaches the hint-refresh tail for a known workspace."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        # Highlight alpha, then flip its running state via a status event.
        m._update_running("id-alpha", False)
        await pilot.pause()
        assert "s start" in str(m.query_one(".ws_hints", Static).render())


async def test_main_screen_highlighted_ws_falls_back_to_name(monkeypatch):
    """_highlighted_ws resolves by name when the row has no workspace_id."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    # A workspace with an empty id is never entered into _ws_by_id, so the
    # name fallback path is exercised.
    no_id = Workspace(id="", name="gamma", created_at="x")
    app = KlangkApp(_ws(owned=[no_id]))
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        assert m._highlighted_ws() is no_id


async def test_main_screen_action_restart_success_cancel_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    restarted = {}
    st = _ws(owned=[a])
    st.restart_workspace = lambda n: restarted.__setitem__("r", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        # cancel -> not restarted
        m.action_restart()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert "r" not in restarted
        # confirm -> restarted
        m.action_restart()
        await pilot.pause()
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert restarted.get("r") == "alpha"
        assert "Restart requested" in (app.live_extra or "")
        # The success path refreshed the list and cleared the highlight;
        # re-select the row before re-triggering.
        m.query_one("#owned_list", ListView).index = 0
        await pilot.pause()
        # error
        st.restart_workspace = lambda n: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        m.action_restart()
        await pilot.pause()
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Restart failed" in (app.live_extra or "")


async def test_main_screen_action_stop_running_success_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    stopped = {}
    st = _ws(owned=[a])
    st.stop_workspace = lambda n: stopped.__setitem__("s", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_stop()  # running -> confirm
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert stopped.get("s") == "alpha"
        m.query_one("#owned_list", ListView).index = 0
        await pilot.pause()
        # error
        st.stop_workspace = lambda n: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        m.action_stop()
        await pilot.pause()
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Stop failed" in (app.live_extra or "")


async def test_main_screen_action_start_stopped(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=False)
    started = []
    st = _ws(owned=[a])
    st.start_workspace = lambda n: started.append(n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_stop()  # stopped -> start, no confirm
        await _settle(app)
        assert started == ["alpha"]
        assert not isinstance(app.screen, ConfirmScreen)


async def test_main_screen_action_start_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=False)
    st = _ws(owned=[a])
    st.start_workspace = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_stop()
        await _settle(app)
        assert "Start failed" in (app.live_extra or "")


async def test_main_screen_action_delete_success_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    deleted = {}
    st = _ws(owned=[_wsobj("alpha")])
    st.delete_workspace = lambda n: deleted.__setitem__("d", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert deleted.get("d") == "alpha"
        m.query_one("#owned_list", ListView).index = 0
        await pilot.pause()
        # error
        st.delete_workspace = lambda n: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        m.action_delete()
        await pilot.pause()
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Delete failed" in (app.live_extra or "")


async def test_main_screen_action_duplicate_success_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    duped = {}
    st = _ws(owned=[_wsobj("alpha")])
    st.duplicate_workspace = lambda n, nn: duped.__setitem__("d", (n, nn))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_duplicate()
        await pilot.pause()
        assert isinstance(app.screen, DuplicateScreen)
        app.screen.on_button_pressed(FakeBtnPress("ok"))  # prefilled copy
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert duped.get("d") == ("alpha", "alpha-copy")
        m.query_one("#owned_list", ListView).index = 0
        await pilot.pause()
        # error
        st.duplicate_workspace = lambda n, nn: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        m.action_duplicate()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Duplicate failed" in (app.live_extra or "")


async def test_main_screen_action_edit_pushes_screen(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.list_images = lambda: {"default": "base", "allowed": ["base"]}
    st.allow_autostart = lambda: True
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EditWorkspaceScreen)


async def test_main_screen_action_edit_not_found(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    from klangk.cli.client import WorkspaceNotFoundError

    st = _ws(owned=[_wsobj("alpha")])
    st.find_workspace = lambda n: (_ for _ in ()).throw(
        WorkspaceNotFoundError("nope")
    )
    st.list_images = lambda: {}
    st.allow_autostart = lambda: False
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "not found" in (app.live_extra or "")


async def test_main_screen_c_key_switches_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)


async def test_main_screen_u_duplicate_d_delete_bindings(monkeypatch):
    """#1888: 'u' triggers Duplicate and 'd' triggers Delete (not the old
    'd'/'x'). Locks in the keybinding wiring, not just the action methods."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        # 'u' -> DuplicateScreen
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, DuplicateScreen)
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        # 'd' -> Delete confirm
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        # The inline hint bar advertises the new keys.
        hints = str(m.query_one(".ws_hints", Static).render())
        assert "[u dup]" in hints and "[d del]" in hints


async def test_main_screen_action_targets_active_tab(monkeypatch):
    """#1879 review: per-workspace actions act on the highlighted row of the
    ACTIVE tab. TabbedContent toggles display on the TabPane (the list's
    parent), not on the list, so naively checking lv.display silently targets
    the Owned list even on the Shared tab — destructive actions included."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    restarted = {}
    app = KlangkApp(
        _ws(
            owned=[_wsobj("alpha", running=True)],
            shared=[_wsobj("beta", running=True)],
            restart_workspace=lambda n: restarted.__setitem__("r", n),
        )
    )
    async with app.run_test() as pilot:
        await _settle(app)
        m = app.screen
        # Switch to the Shared tab and highlight beta.
        m.query_one("#ws_tabs").active = "shared_pane"
        await pilot.pause()
        shared = m.query_one("#shared_list", ListView)
        shared.focus()
        shared.index = 0
        await pilot.pause()
        assert m._active_list().id == "shared_list"
        assert m._highlighted_item().name == "beta"
        # The confirm dialog must name beta, and the request must target beta.
        m.action_restart()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)
        await pilot.pause()  # let the dismiss callback spawn the worker
        await _settle(app)
        assert restarted.get("r") == "beta"


async def test_main_screen_hints_refresh_on_tab_switch(monkeypatch):
    """#1879 review: the hint bar re-renders on tab switch even when the new
    pane has no highlight yet (no Highlighted event would fire)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha", running=True)], shared=[]))
    async with app.run_test() as pilot:
        await _settle(app)
        m = app.screen
        # Highlight alpha on the Owned tab -> hint says 'stop'.
        owned = m.query_one("#owned_list", ListView)
        owned.focus()
        owned.index = 0
        await pilot.pause()
        m._refresh_action_hints()
        assert "s stop" in str(m.query_one(".ws_hints", Static).render())
        # Switch to the empty Shared tab -> no highlight, label defaults to
        # 'start', not the stale 'stop' from the hidden Owned row.
        m.query_one("#ws_tabs").active = "shared_pane"
        await pilot.pause()
        assert "s start" in str(m.query_one(".ws_hints", Static).render())


async def test_main_screen_shows_created_date(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = Workspace(
        id="id-alpha",
        name="alpha",
        created_at="2025-06-15T10:30:00",
        running=True,
    )
    app = KlangkApp(_ws(owned=[ws]))
    async with app.run_test():
        m = app.screen
        items = m.query_one("#owned_list", ListView).query(ListItem)
        date_label = items[0].query_one(".ws-date")
        assert "2025-06-15" in str(date_label.render())


async def test_main_screen_shows_short_id_on_row(monkeypatch):
    """#1899: each list row shows the first 8 chars of the workspace id,
    a prefix of the full id on the detail screen, so a row can be matched
    to its workspace."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = Workspace(
        id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        name="alpha",
        created_at="2025-06-15T10:30:00",
        running=True,
    )
    app = KlangkApp(_ws(owned=[ws], shared=[]))
    async with app.run_test():
        m = app.screen
        items = m.query_one("#owned_list", ListView).query(ListItem)
        id_label = items[0].query_one(".ws-id")
        rendered = str(id_label.render())
        assert "3fa85f64" in rendered  # first 8 chars of the full id
        assert "3fa85f64-5717-4562-b3fc-2c963f66afa6" not in rendered
        # The full id is NOT on the row (only its 8-char prefix is).


async def test_main_screen_shared_list_shows_short_id(monkeypatch):
    """#1899: the shared-workspaces list also shows the short id — no
    regression from the row-population path that the Owned list uses."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = Workspace(
        id="abcdef12-3456-7890-abcd-ef1234567890",
        name="shared-ws",
        created_at="2025-06-15T10:30:00",
        running=False,
    )
    app = KlangkApp(_ws(owned=[], shared=[ws]))
    async with app.run_test():
        m = app.screen
        items = m.query_one("#shared_list", ListView).query(ListItem)
        id_label = items[0].query_one(".ws-id")
        assert "abcdef12" in str(id_label.render())


async def test_main_screen_row_id_and_date_are_separate_columns(monkeypatch):
    """#1907: the workspace id and date render as separate, fixed-width
    columns (not two auto-width labels stuck together), with clear left
    spacing on the date column so the date never runs flush against the id."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = Workspace(
        id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        name="alpha",
        created_at="2025-06-15T10:30:00",
        running=True,
    )
    app = KlangkApp(_ws(owned=[ws], shared=[]))
    async with app.run_test():
        m = app.screen
        items = m.query_one("#owned_list", ListView).query(ListItem)
        id_label = items[0].query_one(".ws-id")
        date_label = items[0].query_one(".ws-date")
        # Fixed (cell) widths, not auto: ids/dates line up across rows of
        # varying name length (.ws-name absorbs the slack at width: 1fr).
        assert id_label.styles.width.is_cells
        assert date_label.styles.width.is_cells
        assert not id_label.styles.width.is_auto
        assert not date_label.styles.width.is_auto
        # Clear space between the id and date columns.
        assert date_label.styles.padding.left >= 2


async def test_update_running_unknown_workspace(monkeypatch):
    """_update_running returns early for unknown workspace_id (line 692)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha", running=True)]))
    async with app.run_test():
        m = app.screen
        # Call with an ID that isn't in _ws_by_id — should not raise.
        m._update_running("nonexistent-id", False)
        # Alpha is still shown as running (no change).
        items = m.query_one("#owned_list", ListView).query(ListItem)
        assert len(items) == 1


async def test_update_running_no_label(monkeypatch):
    """_update_running handles NoMatches when Label is missing (lines 700-701)."""
    from textual.dom import NoMatches as _NM

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test():
        m = app.screen
        items = m.query_one("#owned_list", ListView).query(ListItem)
        # Patch query_one on the matching item so it raises NoMatches.
        orig = items[0].query_one

        def raise_no_matches(sel, *a):
            if sel == ".ws-name":
                raise _NM("no label")
            return orig(sel, *a)

        items[0].query_one = raise_no_matches
        # Should not raise — the except NoMatches: pass handles it.
        m._update_running(a.id, False)


async def test_main_screen_list_error_shows_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def boom():
        raise RuntimeError("net")

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test():
        m = app.screen
        assert len(m.query_one("#owned_list", ListView).query(ListItem)) == 1
        assert len(m.query_one("#shared_list", ListView).query(ListItem)) == 1


async def test_focus_visible_list_on_mount(monkeypatch):
    """Focus lands on the first workspace row, not the tab strip (#1792)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha"), _wsobj("beta")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert isinstance(app.focused, ListView)
        assert app.focused.index == 0


async def test_focus_visible_list_empty(monkeypatch):
    """When the workspace list is empty, focus degrades gracefully (#1792)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[], shared=[]))
    async with app.run_test() as pilot:
        await pilot.pause()
        # No crash — focus stays elsewhere (not on a list with no items).


async def test_shared_tab_stays_when_empty(monkeypatch):
    """Switching to 'Shared to me' stays there even when the list is empty (#1843)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")], shared=[]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        tc = app.screen.query_one("#ws_tabs")

        # Switch to the "Shared to me" tab by finding its pane ID.
        panes = list(tc.query("TabPane"))
        shared_pane_id = panes[1].id
        tc.active = shared_pane_id
        await pilot.pause()
        assert tc.active == shared_pane_id

        # A subsequent refresh must not drag focus back to "Owned by me".
        app.screen.refresh_lists()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert tc.active == shared_pane_id


async def test_filter_narrows_workspace_list(monkeypatch):
    """Typing in the filter input narrows the visible workspace list (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(
        _ws(owned=[_wsobj("alpha"), _wsobj("beta"), _wsobj("alphabet")])
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)
        assert len(lv.query(ListItem)) == 3

        # Type "alph" into the filter.
        inp = m.query_one("#filter_input", Input)
        inp.value = "alph"
        await pilot.pause()
        items = lv.query(ListItem)
        assert len(items) == 2
        names = {i.name for i in items}
        assert names == {"alpha", "alphabet"}


def test_main_screen_matches_name_or_id():
    """_matches narrows by name or id (and the 8-char id prefix) (#1911)."""
    ws = Workspace(
        id="3fa85f64-1b2c-4d5e-8f9a-0123456789ab",
        name="alpha",
        created_at="x",
    )
    # name substring
    assert MainScreen._matches(ws, "alph")
    # full id
    assert MainScreen._matches(ws, "3fa85f64-1b2c-4d5e-8f9a-0123456789ab")
    # 8-char row prefix (a substring of the id)
    assert MainScreen._matches(ws, "3fa85f64")
    # case-insensitive: queries are lowercased before _matches is called
    assert MainScreen._matches(ws, "alpha")
    # neither name nor id
    assert not MainScreen._matches(ws, "zzz")
    # missing attrs don't crash; empty id never matches on its own
    bare = type("W", (), {"name": "x", "id": ""})()
    assert MainScreen._matches(bare, "x")
    assert not MainScreen._matches(bare, "y")


async def test_filter_matches_workspace_id(monkeypatch):
    """Typing a workspace's id (or prefix) into the filter matches it (#1911)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = Workspace(
        id="3fa85f64-1b2c-4d5e-8f9a-0123456789ab", name="alpha", created_at="x"
    )
    b = Workspace(
        id="11111111-2222-3333-4444-555555555555", name="beta", created_at="x"
    )
    app = KlangkApp(_ws(owned=[a, b]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)
        assert len(lv.query(ListItem)) == 2

        # Type the 8-char prefix of alpha's id -> only alpha matches.
        inp = m.query_one("#filter_input", Input)
        inp.value = "3fa85f64"
        await pilot.pause()
        items = lv.query(ListItem)
        assert len(items) == 1
        assert items[0].name == "alpha"

        # Full id also narrows to alpha.
        inp.value = "3fa85f64-1b2c-4d5e-8f9a-0123456789ab"
        await pilot.pause()
        assert len(lv.query(ListItem)) == 1

        # Clearing the filter shows both again (empty filter = all).
        inp.value = ""
        await pilot.pause()
        assert len(lv.query(ListItem)) == 2

        # Name matching still works unchanged.
        inp.value = "bet"
        await pilot.pause()
        items = lv.query(ListItem)
        assert len(items) == 1
        assert items[0].name == "beta"


async def test_filter_no_matches_shows_placeholder(monkeypatch):
    """A filter with no matches shows '(no matches)' placeholder (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        inp = m.query_one("#filter_input", Input)
        inp.value = "zzz"
        await pilot.pause()
        items = m.query_one("#owned_list", ListView).query(ListItem)
        assert len(items) == 1
        assert "no matches" in str(items[0].query_one(Label).render()).lower()


async def test_filter_escape_clears_then_returns(monkeypatch):
    """Escape in filter clears text first, then returns focus to list (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha"), _wsobj("beta")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        inp = m.query_one("#filter_input", Input)
        inp.focus()
        inp.value = "alph"
        await pilot.pause()

        # First Escape clears the text.
        await pilot.press("escape")
        await pilot.pause()
        assert inp.value == ""
        # List is restored (both items visible again).
        assert len(m.query_one("#owned_list", ListView).query(ListItem)) == 2

        # Second Escape returns focus to the workspace list.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.focused, ListView)


async def test_cycle_sort(monkeypatch):
    """Pressing 'o' cycles sort: created↓ → created↑ → name↑ → name↓ (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = Workspace(id="id-a", name="alpha", created_at="2025-01-01T00:00:00")
    b = Workspace(id="id-b", name="beta", created_at="2025-06-01T00:00:00")
    app = KlangkApp(_ws(owned=[a, b]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)

        def names():
            return [i.name for i in lv.query(ListItem)]

        # Default: created desc (newest first).
        assert names() == ["beta", "alpha"]

        # 1st press: created asc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["alpha", "beta"]
        assert "created" in str(m.query_one("#sort_btn", Button).label)
        assert "▲" in str(m.query_one("#sort_btn", Button).label)

        # 2nd press: name asc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["alpha", "beta"]
        assert "name" in str(m.query_one("#sort_btn", Button).label)

        # 3rd press: name desc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["beta", "alpha"]

        # 4th press: back to created desc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["beta", "alpha"]
        assert "created" in str(m.query_one("#sort_btn", Button).label)
        assert "▼" in str(m.query_one("#sort_btn", Button).label)


async def test_filter_preserved_on_refresh(monkeypatch):
    """A live refresh re-applies the active filter (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha"), _wsobj("beta")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        inp = m.query_one("#filter_input", Input)
        inp.value = "alph"
        await pilot.pause()
        lv = m.query_one("#owned_list", ListView)
        assert len(lv.query(ListItem)) == 1

        # Simulate a workspaces_changed refresh.
        m.refresh_lists()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(lv.query(ListItem)) == 1
        assert lv.query(ListItem)[0].name == "alpha"


async def test_focus_filter_action(monkeypatch):
    """The '/' keybinding focuses the filter input (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        m.action_focus_filter()
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "filter_input"


async def test_filter_bar_renders_input_without_impacting_list(monkeypatch):
    """When the filter bar is shown, the filter input must actually have room
    to render its text, and the workspace list viewport must be unchanged
    whether the bar is hidden or shown (#1764).

    Two regressions this guards:
      (a) The global `Input { border-top: blank }` app-CSS rule used to win
          over `#filter_input { border: none }` (app CSS outranks widget
          DEFAULT_CSS by origin), giving the field a 1-row top border and —
          at height:1 — a content area of height 0, so the field painted
          nothing even though filtering worked.
      (b) Showing/hiding the docked filter bar must not resize the list.
    """

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha"), _wsobj("beta")]))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        bar = m.query_one("#filter_bar")
        inp = m.query_one("#filter_input", Input)
        owned = m.query_one("#owned_list", ListView)

        # Hidden by default; list has a real viewport.
        assert bar.display is False
        list_height_hidden = owned.content_region.height
        assert list_height_hidden > 0

        # Show via the '/' action.
        m.action_focus_filter()
        await pilot.pause()
        assert bar.display is True
        assert isinstance(app.focused, Input)

        # (a) The input's top border is neutralized (not 'blank'), so its
        #     content area is non-empty and its text can render.
        assert inp.styles.border_top[0] != "blank"
        assert inp.content_size.height > 0
        # And the bar occupies the bottom row on top of the docked chrome.
        assert bar.region.height == 1
        assert bar.region.y == app.size.height - 1

        # (b) The workspace list viewport is unchanged.
        assert owned.content_region.height == list_height_hidden


async def test_sort_button_click_cycles(monkeypatch):
    """Clicking the sort button cycles sort mode (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = Workspace(id="id-a", name="alpha", created_at="2025-01-01T00:00:00")
    b = Workspace(id="id-b", name="beta", created_at="2025-06-01T00:00:00")
    app = KlangkApp(_ws(owned=[a, b]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)

        # Default: created desc.
        assert [i.name for i in lv.query(ListItem)] == ["beta", "alpha"]

        # Click the sort button → created asc.
        btn = m.query_one("#sort_btn", Button)
        btn.press()
        await pilot.pause()
        assert [i.name for i in lv.query(ListItem)] == ["alpha", "beta"]
        assert "▲" in str(btn.label)


async def test_filter_submitted_returns_to_list(monkeypatch):
    """Pressing Enter in the filter returns focus to the workspace list (#1764)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        inp = m.query_one("#filter_input", Input)
        inp.focus()
        inp.value = "alph"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.focused, ListView)


async def test_down_from_tabs_enters_workspace_list(monkeypatch):
    # Down arrow from the tab strip focuses the workspace list (#1781).
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    b = _wsobj("beta")
    app = KlangkApp(_ws(owned=[a, b]))
    async with app.run_test() as pilot:
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)
        lv.index = None  # Reset so the Down handler sets it
        m.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, ListView)
        assert app.focused.index == 0  # first row, not the second


async def test_up_from_list_returns_to_tabs(monkeypatch):
    # Up arrow from the first workspace row returns focus to the tab strip.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test() as pilot:
        await pilot.pause()
        m = app.screen
        lv = m.query_one("#owned_list", ListView)
        lv.focus()
        lv.index = 0
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(app.focused, Tabs)


async def test_main_screen_select_opens_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.screen.on_list_view_selected(FakeSelected("alpha"))
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_main_screen_select_empty_no_push(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())  # empty lists -> placeholder rows
    async with app.run_test() as pilot:
        app.screen.on_list_view_selected(FakeSelected(""))
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_status_event_refreshes_on_change(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    calls = {"n": 0}

    def owned():
        calls["n"] += 1
        return [a]

    st = _ws(owned=[a])
    st.list_owned_workspaces = owned
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.screen._on_status_event({"type": "service_health"})
        await pilot.pause()
        assert app.live_extra == "live: service_health"
        before = calls["n"]
        app.screen._on_status_event({"type": "workspaces_changed"})
        await pilot.pause()
        assert calls["n"] > before  # list re-fetched


async def test_detail_loads_and_renders(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj(
        "alpha",
        running=True,
        health="healthy",
        health_message="ok",
        image="img",
        service_command="cmd",
        health_check="hc",
        mounts=["/h:/c"],
        env={"K": "v"},
        owner_email="o@x",
    )
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        d = app.screen
        body = str(d.query_one("#detail_body").render())
        # #1910: detail renders as a two-column table — labels and values
        # are separate columns, so assert each row's label + value.
        assert _detail_value(body, "id") == "id-alpha"
        assert _detail_value(body, "running") == "yes"
        assert _detail_value(body, "health") == "healthy"
        assert _detail_value(body, "health note") == "ok"
        assert _detail_value(body, "image") == "img"
        assert _detail_value(body, "service command") == "cmd"
        assert _detail_value(body, "health check") == "hc"
        assert _detail_value(body, "auto-start") == "off"
        assert _detail_value(body, "mounts") == "/h:/c"
        assert _detail_value(body, "environment") == "K=v"
        assert _detail_value(body, "owner") == "o@x"


async def test_detail_shows_full_id(monkeypatch):
    """#1899: the detail screen shows the full server-assigned workspace id,
    so it can be copied for CLI / log / support correlation."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    full_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    a = Workspace(
        id=full_id,
        name="alpha",
        created_at="x",
        running=True,
    )
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert _detail_value(body, "id") == full_id
        # The id row appears before the running row (near the top).
        assert body.index(full_id) < body.index("running")


async def test_detail_renders_allowed_domains(monkeypatch):
    # #1745: the current allowlist is shown in the detail view.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", allowed_domains=["github.com:443", "pypi.org"])
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert _detail_value(body, "allowed domains") is not None
        assert "github.com:443" in body
        assert "pypi.org" in body


async def test_detail_renders_aligned_two_column_table(monkeypatch):
    """#1910: the detail body renders as a two-column table — every value
    starts at the same column regardless of label length, so labels and
    values line up vertically."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj(
        "alpha",
        running=True,
        health="healthy",
        image="img",
        service_command="cmd",
        owner_email="o@x",
    )
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        # Every value starts at the same column: the label column is a fixed
        # width, so the longest label ("service command") and the shortest
        # ("id") align their values.
        starts = set()
        for label in ("id", "running", "image", "service command", "owner"):
            assert _detail_value(body, label) is not None, label
            line = next(
                ln
                for ln in body.splitlines()
                if ln.startswith(label)
                and ln[len(label) : len(label) + 2].isspace()
            )
            rest = line[len(label) :]
            starts.add(len(label) + len(rest) - len(rest.lstrip()))
        assert len(starts) == 1, f"detail values don't align: {starts}"


async def test_detail_shows_uptime(monkeypatch):
    """#1814: uptime is shown when the container is running."""
    import time as _time

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    # service_started_at 1 day 2 hours 30 minutes ago
    started = _time.time() - (86400 + 7200 + 1800)
    a = _wsobj("alpha", running=True, service_started_at=started)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert _detail_value(body, "uptime") == "1d 2h 30m"


async def test_detail_no_uptime_when_stopped(monkeypatch):
    """#1814: uptime is not shown when the container is stopped."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert "uptime" not in body


async def test_detail_action_edit_opens_form_and_refreshes(monkeypatch):
    # #1778: 'e' on the detail opens the edit form; a successful save
    # refreshes the detail (re-fetches the workspace).
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", image="base")
    finds = []
    st = _ws(
        list_images=lambda: {"default": "base", "allowed": ["base", "py:3"]},
        allow_autostart=lambda: True,
    )
    st.find_workspace = lambda n: finds.append(n) or a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EditWorkspaceScreen)
        # Simulate a successful save/disdismiss -> _on_edited refreshes.
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, WorkspaceDetailScreen)
        assert finds.count("alpha") >= 2  # initial load + post-edit reload


async def test_detail_and_edit_set_window_title(monkeypatch):
    # The app/window title reflects the active screen: "Klangk: Workspaces"
    # on the list, "Klangk: workspace <name>" on detail/edit, restored on pop.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", image="base")
    st = _ws(
        list_images=lambda: {"default": "base", "allowed": ["base", "py:3"]},
        allow_autostart=lambda: True,
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        assert app.title == "Klangk: Workspaces"
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert app.title == "Klangk: workspace alpha"
        app.screen.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.title == "Klangk: workspace alpha"  # edit (same workspace)
        app.pop_screen()  # edit -> detail
        await pilot.pause()
        assert app.title == "Klangk: workspace alpha"
        app.pop_screen()  # detail -> main
        await pilot.pause()
        assert app.title == "Klangk: Workspaces"


async def test_detail_load_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    st.find_workspace = lambda n: (_ for _ in ()).throw(RuntimeError("x"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert "Could not load" in str(
            app.screen.query_one("#detail_body").render()
        )


async def test_detail_restart_confirm_cancel_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    restarted = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.restart_workspace = lambda n: restarted.__setitem__("r", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_restart()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # cancel -> not restarted
        app.screen.dismiss(False)
        await pilot.pause()
        assert "r" not in restarted
        # confirm -> restarted
        app.screen.action_restart()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert restarted.get("r") == "alpha"
        assert "Restart requested" in str(
            app.screen.query_one("#detail_msg").render()
        )
        # service_started_at reset on restart (#1814).
        assert a.service_started_at is not None
        # error
        st.restart_workspace = lambda n: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        app.screen.action_restart()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "Restart failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_auto_starts_stopped_workspace(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    started = []
    st = _ws()
    st.find_workspace = lambda n: a
    st.restart_workspace = lambda n: started.append(n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert started == ["alpha"]
        assert "Container started" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_auto_start_skipped_when_running(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    started = []
    st = _ws()
    st.find_workspace = lambda n: a
    st.restart_workspace = lambda n: started.append(n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert started == []


async def test_detail_auto_start_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws()
    st.find_workspace = lambda n: a

    def boom(n):
        raise RuntimeError("podman down")

    st.restart_workspace = boom
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert "Auto-start failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_stop_when_running(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True, service_started_at=1000.0)
    stopped = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.stop_workspace = lambda n: stopped.__setitem__("s", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_stop()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)  # confirm stop
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert stopped.get("s") == "alpha"
        assert "Stop requested" in str(
            app.screen.query_one("#detail_msg").render()
        )
        # After stop, binding label should toggle to "Start".
        labels = [b.description for b in app.screen.BINDINGS if b.key == "s"]
        assert labels == ["Start"]
        # service_started_at cleared on stop (#1814).
        assert a.service_started_at is None
        assert "uptime" not in str(
            app.screen.query_one("#detail_body").render()
        )


async def test_detail_start_when_stopped(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    started = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.start_workspace = lambda n: started.__setitem__("s", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # No confirm dialog for start — goes straight through.
        app.screen.action_stop()
        await app.workers.wait_for_complete()
        assert started.get("s") == "alpha"
        assert "Start requested" in str(
            app.screen.query_one("#detail_msg").render()
        )
        # After start, binding label should toggle to "Stop".
        labels = [b.description for b in app.screen.BINDINGS if b.key == "s"]
        assert labels == ["Stop"]
        # service_started_at reset on start (#1814).
        assert a.service_started_at is not None
        assert "uptime" in str(app.screen.query_one("#detail_body").render())


async def test_detail_terminal_actions_inline_not_in_footer(monkeypatch):
    """#1860: terminal-scoped keys are hinted inline on the list header and
    hidden from the Footer; the workspace-delete key is labeled 'Del ws'."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    st.find_workspace = lambda n: _wsobj("alpha", running=True)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # (a) Terminal action hints render on the list header, with the
        # literal `[n]` keycap intact (not eaten as Rich markup).
        hints = str(app.screen.query_one("#term_hints").render())
        assert "[n]" in hints
        assert "new" in hints
        assert "delete" in hints

        bindings = {b.key: b for b in app.screen.BINDINGS}

        # (b) Terminal keys still exist (so the keys work) but are hidden
        # from the Footer.
        assert "n" in bindings and "delete" in bindings
        assert bindings["n"].show is False
        assert bindings["delete"].show is False

        # (c) Workspace-scoped keys remain visible ...
        for key in ("e", "r", "s", "u", "d"):
            assert bindings[key].show is True

        # (d) ... and the workspace-delete key is labeled 'Del ws' (#1860, #1888).
        assert bindings["d"].description == "Del ws"


async def test_detail_uptime_ticks(monkeypatch):
    """#1814: uptime display refreshes via the periodic timer."""
    import time as _time

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    started = _time.time() - 60
    a = _wsobj("alpha", running=True, service_started_at=started)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        body1 = str(app.screen.query_one("#detail_body").render())
        assert "uptime" in body1
        # Simulate time passing and trigger the tick.
        a.service_started_at = _time.time() - 180
        app.screen._tick_uptime()
        body2 = str(app.screen.query_one("#detail_body").render())
        assert _detail_value(body2, "uptime") == "3m"


async def test_detail_uptime_tick_noop_when_stopped(monkeypatch):
    """#1814: tick does not crash or re-render when container is stopped."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # Should be a no-op, no crash.
        app.screen._tick_uptime()
        assert "uptime" not in str(
            app.screen.query_one("#detail_body").render()
        )


async def test_detail_stop_ws_none(monkeypatch):
    """action_stop is a no-op when _ws is None (line 989)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    st.find_workspace = lambda n: None
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("gone"))
        await pilot.pause()
        # _ws is None because find_workspace returned None.
        app.screen.action_stop()
        await app.workers.wait_for_complete()
        # No crash, still on detail screen.
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_detail_stop_cancel(monkeypatch):
    """Cancelling the stop confirm dialog is a no-op (line 998)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    stopped = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.stop_workspace = lambda n: stopped.__setitem__("s", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_stop()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)  # cancel
        await pilot.pause()
        assert "s" not in stopped
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_detail_stop_error(monkeypatch):
    """Stop failure shows error message (lines 1001-1003)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a

    def boom(n):
        raise RuntimeError("container locked")

    st.stop_workspace = boom
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_stop()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)  # confirm
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "Stop failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_start_error(monkeypatch):
    """Start failure shows error message (lines 1020-1022)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws()
    st.find_workspace = lambda n: a

    def boom(n):
        raise RuntimeError("image missing")

    st.start_workspace = boom
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_stop()  # ws not running → _do_start
        await app.workers.wait_for_complete()
        assert "Start failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_delete_confirm_cancel_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    deleted = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.delete_workspace = lambda n: deleted.__setitem__("d", n)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # cancel -> stays on detail, not deleted
        app.screen.dismiss(False)
        await pilot.pause()
        assert "d" not in deleted
        assert isinstance(app.screen, WorkspaceDetailScreen)
        # confirm -> deleted, pops back to list
        app.screen.action_delete()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert deleted.get("d") == "alpha"
        assert isinstance(app.screen, MainScreen)


async def test_detail_delete_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws()
    st.find_workspace = lambda n: a
    st.delete_workspace = lambda n: (_ for _ in ()).throw(RuntimeError("boom"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_delete()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "Delete failed" in str(
            app.screen.query_one("#detail_msg").render()
        )
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_detail_duplicate_ok_cancel_input_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    duped = {}
    st = _ws()
    st.find_workspace = lambda n: a
    st.duplicate_workspace = lambda n, nn: duped.__setitem__("d", (n, nn))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # cancel via button
        app.screen.action_duplicate()
        await pilot.pause()
        assert isinstance(app.screen, DuplicateScreen)
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        assert "d" not in duped
        # ok via button (prefilled name)
        app.screen.action_duplicate()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert duped.get("d") == ("alpha", "alpha-copy")
        assert "Duplicated" in str(
            app.screen.query_one("#detail_msg").render()
        )
        # ok via input submit (enter)
        app.screen.action_duplicate()
        await pilot.pause()
        di = app.screen.query_one("#dup_name", Input)
        di.value = "alpha-copy2"
        app.screen.on_input_submitted(Input.Submitted(di, di.value))
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert duped.get("d") == ("alpha", "alpha-copy2")
        # empty name -> treated as cancel
        app.screen.action_duplicate()
        await pilot.pause()
        app.screen.query_one("#dup_name", Input).value = ""
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        assert duped.get("d") == ("alpha", "alpha-copy2")  # unchanged
        # error
        st.duplicate_workspace = lambda n, nn: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        app.screen.action_duplicate()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "Duplicate failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_refresh_workspaces_refreshes_main(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    calls = {"n": 0}

    def owned():
        calls["n"] += 1
        return [a]

    st = _ws(owned=[a])
    st.list_owned_workspaces = owned
    app = KlangkApp(st)
    async with app.run_test() as _pilot:
        before = calls["n"]
        app.refresh_workspaces()
        await app.workers.wait_for_complete()
        assert calls["n"] > before


async def test_main_screen_markup_name_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("x[red]y")
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test():
        lv = app.screen.query_one("#owned_list", ListView)
        assert len(lv.query(ListItem)) == 1
        prompt = app.screen._fmt_name(a)
        assert isinstance(prompt, Text)
        assert "x[red]y" in str(prompt)  # literal, not markup-parsed


async def test_detail_markup_name_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("x[red]y", image="[img]", health_message="[bad]")
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("x[red]y"))
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert "[img]" in body
        assert "[bad]" in body


async def test_detail_apply_status_event(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj(
        "alpha", running=False, health="unhealthy", health_message="down"
    )
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        d = app.screen
        # container_status flips running and carries service_started_at
        import time as _time

        started = _time.time() - 120
        d.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
                "service_started_at": started,
            }
        )
        assert a.running is True
        assert a.service_started_at == started
        assert (
            _detail_value(str(d.query_one("#detail_body").render()), "running")
            == "yes"
        )
        # service_health updates health + message
        d.apply_status_event(
            {
                "type": "service_health",
                "workspace_id": "id-alpha",
                "healthy": False,
                "health_message": "curl fail",
                "running": True,
            }
        )
        body = str(d.query_one("#detail_body").render())
        assert _detail_value(body, "health") == "unhealthy"
        assert _detail_value(body, "health note") == "curl fail"
        # non-matching workspace id is ignored
        d.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "other",
                "running": False,
            }
        )
        assert a.running is True  # unchanged
        # unknown event type -> no-op, no crash
        d.apply_status_event(
            {"type": "service_health_heartbeat", "workspace_id": "id-alpha"}
        )
        assert a.running is True  # unchanged


async def test_detail_container_restart_refreshes_terminals(monkeypatch):
    """#1924: a container restart re-fetches the terminal list so the
    detail screen reflects the new container's state."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True, service_started_at=1000.0)
    fetched = {"n": 0}

    async def track_terms(*_a, **_k):
        fetched["n"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        before = fetched["n"]
        # Simulate a container restart (new service_started_at).
        app.screen.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
                "service_started_at": 2000.0,
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fetched["n"] > before


async def test_detail_container_start_refreshes_terminals(monkeypatch):
    """#1924: a stopped container starting up fetches the terminal list."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    fetched = {"n": 0}

    async def track_terms(*_a, **_k):
        fetched["n"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        before = fetched["n"]
        # Container starts (was not running).
        app.screen.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
                "service_started_at": 1000.0,
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fetched["n"] > before


async def test_detail_container_status_no_refetch_when_unchanged(monkeypatch):
    """container_status with same service_started_at does not re-fetch."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True, service_started_at=1000.0)
    fetched = {"n": 0}

    async def track_terms(*_a, **_k):
        fetched["n"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        before = fetched["n"]
        # Same service_started_at — not a restart, no re-fetch.
        app.screen.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
                "service_started_at": 1000.0,
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fetched["n"] == before


async def test_detail_apply_status_event_reload(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        d = app.screen
        a.running = True  # mutated after load
        d.apply_status_event({"type": "workspaces_changed"})
        await pilot.pause()
        assert (
            _detail_value(str(d.query_one("#detail_body").render()), "running")
            == "yes"
        )


async def test_detail_apply_status_event_terminals_changed(monkeypatch):
    """terminals_changed without a windows payload (older server) falls
    back to a fetch — #1885's re-fetch behavior, now the backward-compat
    path under #1894's push."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a
    st.list_terminals = _async_empty  # initially no terminals
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        lv = d.query_one("#term_list", ListView)
        assert any(
            "(no terminals)" in str(it.render()) for it in lv.query(Label)
        )
        # Another surface adds a terminal; the nudge arrives over /ws.
        from unittest.mock import AsyncMock

        st.list_terminals = AsyncMock(
            return_value=[{"index": 0, "name": "build"}]
        )
        d.apply_status_event(
            {"type": "terminals_changed", "workspace_id": "id-alpha"}
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        lv = d.query_one("#term_list", ListView)
        assert any("build" in str(it.render()) for it in lv.query(Label))


async def test_detail_apply_status_event_terminals_changed_other_ws(
    monkeypatch,
):
    """A terminals_changed nudge for a different workspace is ignored."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a
    from unittest.mock import AsyncMock

    calls = []
    st.list_terminals = AsyncMock(
        side_effect=lambda name: calls.append(name) or []
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        after_mount = len(calls)  # _load_terminals ran once on mount
        d.apply_status_event(
            {"type": "terminals_changed", "workspace_id": "other-ws"}
        )
        await pilot.pause()
        # Not for this workspace -> no re-fetch.
        assert len(calls) == after_mount


async def test_detail_apply_status_event_terminals_changed_other_ws_with_windows(
    monkeypatch,
):
    """A terminals_changed push for a different workspace is ignored even
    when it carries a windows payload — the ws filter gates the push path
    too, not just the fallback (#1896)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a

    calls = []

    async def terms(name):
        calls.append(name)
        return [{"index": 0, "name": "main", "id": "@0"}]

    st.list_terminals = terms
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        after_mount = len(calls)  # _load_terminals ran once on mount
        # Push carries a windows payload but for a different workspace.
        d.apply_status_event(
            {
                "type": "terminals_changed",
                "workspace_id": "other-ws",
                "windows": [{"index": 9, "name": "impostor", "id": "@9"}],
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Rejected at the ws filter -> no extra fetch, no impostor row.
        assert len(calls) == after_mount
        lv = d.query_one("#term_list", ListView)
        assert not any(
            "impostor" in str(it.render()) for it in lv.query(Label)
        )


async def test_detail_terminal_push_falls_back_on_malformed_windows(
    monkeypatch,
):
    """A non-list windows payload falls back to a fetch instead of crashing
    — the push path validates the payload (#1896, restoring the resilience
    the old poll path had)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)

    calls = []

    async def fetch_terms(name):
        calls.append(name)
        return [{"index": 0, "name": "fetched", "id": "@0"}]

    st = _ws(list_terminals=fetch_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        after_mount = len(calls)
        # Malformed payload (not a list) -> isinstance check fails -> fetch.
        app.screen.apply_status_event(
            {
                "type": "terminals_changed",
                "workspace_id": "id-alpha",
                "windows": "not-a-list",
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(calls) == after_mount + 1  # fell back to a fetch
        lv = app.screen.query_one("#term_list", ListView)
        assert any("fetched" in str(it.render()) for it in lv.query(Label))


async def test_detail_apply_status_event_ws_none(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    st.find_workspace = lambda n: (_ for _ in ()).throw(RuntimeError("x"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # ws is None (load failed) -> safe no-op
        app.screen.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
            }
        )


async def test_status_event_routed_to_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        main._on_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
            }
        )
        await pilot.pause()
        assert a.running is True
        assert (
            _detail_value(
                str(app.screen.query_one("#detail_body").render()), "running"
            )
            == "yes"
        )


async def _async_terms(*a, **k):
    """Async stub returning two owned terminal windows."""
    return [
        {"index": 0, "name": "main", "id": "@0"},
        {"index": 1, "name": "build", "id": "@1"},
    ]


async def test_detail_terminals_listed(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()  # deterministic render
        await pilot.pause()
        tl = app.screen.query_one("#term_list", ListView)
        assert len(tl.query(ListItem)) == 2
        assert "main" in _lv_texts(tl)[0]
        assert "build" in _lv_texts(tl)[1]


async def test_detail_terminals_empty_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws()  # list_terminals -> _async_empty -> []
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        tl = app.screen.query_one("#term_list", ListView)
        assert len(tl.query(ListItem)) == 1  # the (no terminals) placeholder


async def test_detail_terminal_load_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def boom(*a, **k):
        raise RuntimeError("ws down")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=boom)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()  # swallows the error
        await pilot.pause()
        assert (
            len(app.screen.query_one("#term_list", ListView).query(ListItem))
            == 1
        )


async def test_detail_delete_terminal_guard_last(monkeypatch):
    async def noop(*a, **k):
        return None

    async def one(*a, **k):
        return [{"index": 0, "name": "only", "id": "@0"}]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=one, close_terminal=_async_empty)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 0
        d.action_delete_terminal()  # only terminal -> refused
        await app.workers.wait_for_complete()
        assert "Can't delete the last terminal" in str(
            d.query_one("#detail_msg").render()
        )


async def test_detail_delete_terminal_no_selection(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_async_empty)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        # nothing highlighted -> no-op
        d.query_one("#term_list").index = None
        d.action_delete_terminal()
        await app.workers.wait_for_complete()
        assert (
            len(d.query_one("#term_list", ListView).query(ListItem)) == 2
        )  # unchanged


async def test_detail_delete_terminal_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws()  # no terminals -> (no terminals) placeholder
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 0  # the placeholder
        d.action_delete_terminal()  # opt.id == "" -> no-op
        await app.workers.wait_for_complete()
        assert (
            len(d.query_one("#term_list", ListView).query(ListItem)) == 1
        )  # unchanged


async def test_detail_delete_terminal(monkeypatch):
    async def noop(*a, **k):
        return None

    closed = {}

    async def _close(name, index):
        closed["i"] = index
        return [{"index": 0, "name": "main", "id": "@0"}]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        d.action_delete_terminal()
        for _ in range(3):
            await pilot.pause()
        assert closed.get("i") == 1
        assert "Deleted terminal 1" in str(d.query_one("#detail_msg").render())
        assert len(d.query_one("#term_list", ListView).query(ListItem)) == 1


async def test_detail_delete_terminal_shows_inflight_msg(monkeypatch):
    """A 'Deleting terminal …' message shows while the close call is in
    flight, so the screen doesn't appear hung (#1863)."""

    import asyncio

    async def noop(*a, **k):
        return None

    gate = asyncio.Event()

    async def _close(name, index):
        await gate.wait()
        return [{"index": 0, "name": "main", "id": "@0"}]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        d.action_delete_terminal()
        # While close_terminal is blocked on the gate, the in-flight
        # message must already be visible.
        for _ in range(3):
            await pilot.pause()
        assert "Deleting terminal 1" in str(
            d.query_one("#detail_msg").render()
        )
        # Releasing the close call replaces it with the success message.
        gate.set()
        for _ in range(3):
            await pilot.pause()
        assert "Deleted terminal 1" in str(d.query_one("#detail_msg").render())


async def test_detail_delete_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _close(name, index):
        raise RuntimeError("boom")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        await d._do_delete_terminal(1)  # close raises
        await app.workers.wait_for_complete()
        assert "Delete failed" in str(d.query_one("#detail_msg").render())


async def test_detail_terminal_select_spawns_shell(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess, "run", lambda cmd, **k: spawned.append(cmd)
    )

    from contextlib import contextmanager

    @contextmanager
    def fake_suspend():
        yield

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        app.screen.on_list_view_selected(FakeSelected("0"))
        assert len(spawned) == 1
        assert spawned[0] == [
            scr_detail.sys.executable,
            "-m",
            "klangk.cli.main",
            "--server",
            "https://x.example",
            "shell",
            "alpha",
            "0",
        ]


async def test_detail_terminal_select_empty_name_ignored(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess, "run", lambda cmd, **k: spawned.append(cmd)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.on_list_view_selected(FakeSelected(""))
        assert spawned == []


async def test_main_screen_title(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test():
        assert app.title == "Klangk: Workspaces"


# --- reviewer findings (#1746/#1747 review) ---


async def test_confirm_screen_markup_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        app.push_screen(ConfirmScreen("Delete 'wip[/]' and its data?"))
        await pilot.pause()
        # message renders literally; no MarkupError
        rendered = str(app.screen.query_one(Static).render())
        assert "wip[/]" in rendered


async def test_duplicate_screen_markup_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        app.push_screen(DuplicateScreen("wip[/]"))
        await pilot.pause()
        rendered = str(app.screen.query_one(Static).render())
        assert "wip[/]" in rendered


async def test_status_bar_markup_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        app.live_extra = "live: foo[/]bar"
        app.screen._refresh_status()
        await pilot.pause()
        assert "foo[/]bar" in str(app.screen.query_one("#status").render())


async def test_main_screen_auth_expired_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def boom():
        raise AuthError("expired")

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test():
        lv = app.screen.query_one("#owned_list", ListView)
        assert len(lv.query(ListItem)) == 1
        assert "session expired" in _lv_texts(lv)[0].lower()


async def test_detail_auth_expired_message(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    st.find_workspace = lambda n: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert "Session expired" in str(
            app.screen.query_one("#detail_body").render()
        )


async def test_detail_pops_when_workspace_deleted(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    calls = {"n": 0}

    def find(n):
        calls["n"] += 1
        if calls["n"] == 1:
            return a
        raise WorkspaceNotFoundError("gone")

    st.find_workspace = find
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)
        app.screen.apply_status_event({"type": "workspaces_changed"})
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # popped back to the list


async def test_detail_delete_terminal_empty_result(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _close(name, index):
        return []  # close / refresh failed

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        await d._do_delete_terminal(1)
        await app.workers.wait_for_complete()
        assert "Delete failed" in str(d.query_one("#detail_msg").render())
        assert (
            len(d.query_one("#term_list", ListView).query(ListItem)) == 2
        )  # unchanged


async def test_detail_new_terminal(monkeypatch):
    async def noop(*a, **k):
        return None

    created = {}

    async def _create(name, window_name):
        created["name"] = window_name
        return [
            {"index": 0, "name": "main", "id": "@0"},
            {"index": 1, "name": "build", "id": "@1"},
            {"index": 2, "name": window_name, "id": "@2"},
        ]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(
        list_terminals=_async_terms,
        close_terminal=_async_empty,
        create_terminal=_create,
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.action_new_terminal()
        for _ in range(5):
            await pilot.pause()
        await app.workers.wait_for_complete()
        assert created["name"] == "term-2"
        assert len(d.query_one("#term_list", ListView).query(ListItem)) == 3
        assert "Created terminal" in str(d.query_one("#detail_msg").render())


async def test_detail_new_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _create(name, window_name):
        raise RuntimeError("boom")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(
        list_terminals=_async_terms,
        close_terminal=_async_empty,
        create_terminal=_create,
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        await d._do_new_terminal()
        await app.workers.wait_for_complete()
        assert "Create failed" in str(d.query_one("#detail_msg").render())


async def test_detail_new_terminal_empty_result(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _create(name, window_name):
        return []

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(
        list_terminals=_async_terms,
        close_terminal=_async_empty,
        create_terminal=_create,
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        await d._do_new_terminal()
        await app.workers.wait_for_complete()
        assert "Create failed" in str(d.query_one("#detail_msg").render())


async def test_detail_new_terminal_no_workspace(monkeypatch):
    async def noop(*a, **k):
        return None

    def _raise(n):
        raise RuntimeError("gone")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws(list_terminals=_async_terms, close_terminal=_async_empty)
    st.find_workspace = _raise
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        d = app.screen
        d.action_new_terminal()  # _ws is None -> no-op
        await app.workers.wait_for_complete()


# ---------------------------------------------------------------------------
# Create workspace form (#1748)
# ---------------------------------------------------------------------------


def _create_state(create=None, **extra):
    """Authed state with image/autostart/create stubs for create-screen tests."""
    base = dict(
        list_images=lambda: {
            "default": "base",
            "allowed": ["base", "py:3"],
        },
        allow_autostart=lambda: True,
        create_workspace=create or (lambda *a, **k: _wsobj("zzz")),
    )
    base.update(extra)
    return _ws(**base)


async def test_create_screen_renders_defaults(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        assert isinstance(cs, CreateWorkspaceScreen)
        cb = cs.query_one("#auto_start", Checkbox)
        assert cb.display is True  # shown (autostart allowed)
        assert cb.value is False  # off by default
        assert cs.query_one("#image", Select).value == "base"  # server default


async def test_create_screen_autostart_hidden_when_not_allowed(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state(allow_autostart=lambda: False))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cb = app.screen.query_one("#auto_start", Checkbox)
        assert cb.display is False
        assert cb.disabled is True


async def test_create_screen_mount_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # valid add
        cs.query_one("#mount_input").value = "/host:/c:ro"
        cs._add_mount()
        assert cs._mounts == ["/host:/c:ro"]
        assert cs.query_one("#mount_input").value == ""
        # invalid rejected, message shown
        cs.query_one("#mount_input").value = "badmount"
        cs._add_mount()
        assert cs._mounts == ["/host:/c:ro"]
        assert "source:dest" in str(cs.query_one("#create_msg").render())
        # empty input is a no-op
        cs._add_mount()
        # remove the highlighted entry
        cs.query_one("#mount_list").highlighted = 0
        cs._remove_mount()
        assert cs._mounts == []


async def test_create_screen_env_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#env_input").value = "FOO=bar"
        cs._add_env()
        assert cs._env == {"FOO": "bar"}
        # invalid rejected
        cs.query_one("#env_input").value = "NOEQ"
        cs._add_env()
        assert cs._env == {"FOO": "bar"}
        assert "KEY=VALUE" in str(cs.query_one("#create_msg").render())
        # duplicate key overwrites
        cs.query_one("#env_input").value = "FOO=baz"
        cs._add_env()
        assert cs._env == {"FOO": "baz"}
        # remove
        cs.query_one("#env_list").highlighted = 0
        cs._remove_env()
        assert cs._env == {}


async def test_create_screen_name_required(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    called = []
    app = KlangkApp(
        _create_state(create=lambda *a, **k: called.append(k) or _wsobj("z"))
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen._create()  # name empty
        assert called == []
        assert (
            "required"
            in str(app.screen.query_one("#create_msg").render()).lower()
        )


async def test_create_screen_submit_omits_default_image(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["name"] = name
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _create_state(create=create, allow_autostart=lambda: False)
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "myws"
        cs._create()  # default image kept -> omitted
        await app.workers.wait_for_complete()
        assert captured["name"] == "myws"
        assert captured["k"]["image"] is None
        assert captured["k"]["auto_start"] is False
        assert captured["k"]["mounts"] is None
        assert captured["k"]["env"] is None


async def test_create_screen_submit_custom_fields(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["name"] = name
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "myws"
        cs.query_one("#image", Select).value = "py:3"
        cs.query_one("#command").value = "sleep 1"
        cs.query_one("#health_check").value = "curl localhost"
        cs.query_one("#mount_input").value = "/h:/c"
        cs._add_mount()
        cs.query_one("#env_input").value = "A=1"
        cs._add_env()
        cs.query_one("#auto_start", Checkbox).value = True
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["image"] == "py:3"
        assert captured["k"]["service_command"] == "sleep 1"
        assert captured["k"]["health_check"] == "curl localhost"
        assert captured["k"]["mounts"] == ["/h:/c"]
        assert captured["k"]["env"] == {"A": "1"}
        assert captured["k"]["auto_start"] is True


async def test_create_screen_http_error_shows_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    resp = httpx.Response(
        400,
        json={"detail": "name taken"},
        request=httpx.Request("POST", "https://x.example"),
    )

    def create(name, **k):
        raise httpx.HTTPStatusError(
            "boom", request=resp.request, response=resp
        )

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "dup"
        cs._create()
        await app.workers.wait_for_complete()
        assert "name taken" in str(cs.query_one("#create_msg").render())
        assert isinstance(app.screen, CreateWorkspaceScreen)  # still on form


async def test_create_screen_auth_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def create(name, **k):
        raise AuthError("expired")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert "Session expired" in str(cs.query_one("#create_msg").render())


async def test_create_screen_images_unavailable(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    def boom():
        raise OSError("images endpoint down")

    app = KlangkApp(
        _create_state(create=create, list_images=boom, allow_autostart=boom)
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        assert cs._allowed == []
        assert cs.query_one("#auto_start", Checkbox).display is False
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["image"] is None  # omitted


async def test_create_screen_cancel_button(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # back to the list


async def test_create_screen_input_submit_routing(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # empty name submit -> required error (no dismiss)
        name = cs.query_one("#name")
        cs.on_input_submitted(Input.Submitted(name, ""))
        assert "required" in str(cs.query_one("#create_msg").render()).lower()
        # mount input submit -> add
        m = cs.query_one("#mount_input")
        m.value = "/h:/c"
        cs.on_input_submitted(Input.Submitted(m, m.value))
        assert cs._mounts == ["/h:/c"]
        # env input submit -> add
        e = cs.query_one("#env_input")
        e.value = "K=V"
        cs.on_input_submitted(Input.Submitted(e, e.value))
        assert cs._env == {"K": "V"}
        # allow input submit -> add
        a = cs.query_one("#allow_input")
        a.value = "github.com:443"
        cs.on_input_submitted(Input.Submitted(a, a.value))
        assert cs._allowed_domains == ["github.com:443"]


async def test_create_flow_offer_opens_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("new")))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "new"
        cs._create()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)  # "Open it now?"
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, WorkspaceDetailScreen)
        assert app.screen._name == "new"


async def test_create_flow_offer_declined(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("new")))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "new"
        cs._create()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_create_editor_guards(monkeypatch):
    """Empty input + nothing-highlighted are no-ops (guard returns)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # empty input -> no-op for both editors
        cs._add_mount()
        assert cs._mounts == []
        cs._add_env()
        assert cs._env == {}
        # nothing highlighted -> remove is a no-op
        cs.query_one("#mount_list").highlighted = None
        cs._remove_mount()
        cs.query_one("#env_list").highlighted = None
        cs._remove_env()
        assert cs._mounts == []
        assert cs._env == {}


async def test_create_screen_generic_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def create(name, **k):
        raise RuntimeError("boom")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert "Failed to create: boom" in str(
            cs.query_one("#create_msg").render()
        )


async def test_create_button_routing(monkeypatch):
    """on_button_pressed routes add/rm/create to the editor + create paths."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("ws")))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # add mount via button
        cs.query_one("#mount_input").value = "/h:/c"
        cs.on_button_pressed(FakeBtnPress("add_mount"))
        assert cs._mounts == ["/h:/c"]
        # remove mount via button
        cs.query_one("#mount_list").highlighted = 0
        cs.on_button_pressed(FakeBtnPress("rm_mount"))
        assert cs._mounts == []
        # add env via button
        cs.query_one("#env_input").value = "K=V"
        cs.on_button_pressed(FakeBtnPress("add_env"))
        assert cs._env == {"K": "V"}
        # remove env via button
        cs.query_one("#env_list").highlighted = 0
        cs.on_button_pressed(FakeBtnPress("rm_env"))
        assert cs._env == {}
        # add allowed-domain via button
        cs.query_one("#allow_input").value = "github.com:443"
        cs.on_button_pressed(FakeBtnPress("add_allow"))
        assert cs._allowed_domains == ["github.com:443"]
        # remove allowed-domain via button
        cs.query_one("#allow_list").highlighted = 0
        cs.on_button_pressed(FakeBtnPress("rm_allow"))
        assert cs._allowed_domains == []
        # create via button -> success -> offer
        cs.query_one("#name").value = "ws"
        cs.on_button_pressed(FakeBtnPress("create"))
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)


async def test_create_screen_image_names_markup_safe(monkeypatch):
    """Image names from the server flow into the Select as rich Text, so a
    name containing brackets can't raise MarkupError and crash the TUI."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    evil = "py[/]3"
    app = KlangkApp(
        _ws(
            list_images=lambda: {
                "default": "base",
                "allowed": ["base", evil, "[red]bad[/]"],
            },
            allow_autostart=lambda: False,
            create_workspace=lambda *a, **k: _wsobj("z"),
        )
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # mounted + initial render without MarkupError
        assert isinstance(app.screen, CreateWorkspaceScreen)
        sel = app.screen.query_one("#image", Select)
        # selecting each evil name renders its prompt (Text) literally — a bare
        # markup string here would raise MarkupError and fail the test.
        for name in (evil, "[red]bad[/]"):
            sel.value = name
            await pilot.pause()
        assert sel.value == "[red]bad[/]"  # survived without crashing


async def test_create_screen_http_error_non_json(monkeypatch):
    """A non-JSON error body (proxy HTML page / empty) must not crash the
    create handler — it should surface a failure message instead."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    resp = httpx.Response(
        502,
        text="<html>Bad Gateway</html>",
        request=httpx.Request("POST", "https://x.example"),
    )

    def create(name, **k):
        raise httpx.HTTPStatusError(
            "boom", request=resp.request, response=resp
        )

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert "Failed to create" in str(cs.query_one("#create_msg").render())
        assert isinstance(app.screen, CreateWorkspaceScreen)  # no crash


async def test_create_screen_default_not_in_allowed(monkeypatch):
    """When the server's default image isn't in the allowed list the picker
    starts unselected, and an untouched picker omits the image (matching the
    Flutter dialog); an explicit pick sends that image."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _ws(
            list_images=lambda: {
                "default": "ghost",
                "allowed": ["base", "py:3"],
            },
            allow_autostart=lambda: False,
            create_workspace=create,
        )
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # default not in allowed -> picker is unselected
        assert cs.query_one("#image", Select).value is Select.NULL
        cs.query_one("#name").value = "ws"
        cs._create()  # nothing picked -> image omitted
        await app.workers.wait_for_complete()
        assert captured["k"]["image"] is None


# ---------------------------------------------------------------------------
# Edit workspace form (#1778) + allowed_domains (#1745)
# ---------------------------------------------------------------------------


def _edit_state(ws, *, update=None, restart=None, **extra):
    """Authed state with image/autostart/update/restart stubs for edit tests."""
    base = dict(
        list_images=lambda: {"default": "base", "allowed": ["base", "py:3"]},
        allow_autostart=lambda: True,
        update_workspace=update or (lambda *a, **k: None),
        restart_workspace=restart or (lambda *a, **k: None),
    )
    base.update(extra)
    return _ws(**base)


def _edit_screen(app, ws, **kw):
    app.push_screen(
        EditWorkspaceScreen(
            workspace=ws,
            allowed=kw.get("allowed", ["base", "py:3"]),
            default=kw.get("default", "base"),
            allow_autostart=kw.get("allow_autostart", True),
        )
    )


async def test_create_screen_allowed_domains_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # valid add
        cs.query_one("#allow_input").value = "github.com:443"
        cs._add_allowed_domain()
        assert cs._allowed_domains == ["github.com:443"]
        assert cs.query_one("#allow_input").value == ""
        # invalid rejected
        cs.query_one("#allow_input").value = "bad spec"
        cs._add_allowed_domain()
        assert cs._allowed_domains == ["github.com:443"]
        assert "host:port" in str(cs.query_one("#create_msg").render())
        # duplicate add is a no-op; empty input is a no-op
        cs.query_one("#allow_input").value = "github.com:443"
        cs._add_allowed_domain()
        cs._add_allowed_domain()  # empty input
        assert cs._allowed_domains == ["github.com:443"]
        # remove with nothing highlighted is a no-op
        cs.query_one("#allow_list").highlighted = None
        cs._remove_allowed_domain()
        assert cs._allowed_domains == ["github.com:443"]
        # remove
        cs.query_one("#allow_list").highlighted = 0
        cs._remove_allowed_domain()
        assert cs._allowed_domains == []


async def test_edit_screen_pre_populates(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj(
        "alpha",
        image="py:3",
        mounts=["/h:/c"],
        env={"A": "1"},
        service_command="sh",
        health_check="hc",
        auto_start=True,
        allowed_domains=["github.com:443"],
    )
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        assert es.query_one("#name").value == "alpha"
        assert es.query_one("#image", Select).value == "py:3"
        assert es.query_one("#command").value == "sh"
        assert es.query_one("#health_check").value == "hc"
        assert es.query_one("#auto_start", Checkbox).value is True
        assert es._mounts == ["/h:/c"]
        assert es._env == {"A": "1"}
        assert es._allowed_domains == ["github.com:443"]


async def test_edit_screen_allowed_domains_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", allowed_domains=["github.com:443"])
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # duplicate add is a no-op
        es.query_one("#allow_input").value = "github.com:443"
        es._add_allowed_domain()
        assert es._allowed_domains == ["github.com:443"]
        # new entry
        es.query_one("#allow_input").value = "pypi.org:443"
        es._add_allowed_domain()
        assert es._allowed_domains == ["github.com:443", "pypi.org:443"]
        # invalid rejected
        es.query_one("#allow_input").value = "bad spec"
        es._add_allowed_domain()
        assert len(es._allowed_domains) == 2
        assert "host:port" in str(es.query_one("#edit_msg").render())
        # remove highlighted
        es.query_one("#allow_list").highlighted = 0
        es._remove_allowed_domain()
        assert es._allowed_domains == ["pypi.org:443"]


async def test_edit_screen_save_calls_update(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", image="base", running=False)
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#name").value = "renamed"
        es.query_one("#allow_input").value = "github.com:443"
        es._add_allowed_domain()
        es._save()
        await app.workers.wait_for_complete()
        assert captured["id"] == ws.id
        assert captured["name"] == "renamed"
        assert captured["allowed_domains"] == ["github.com:443"]
        # No restart offer: workspace not running.
        assert not isinstance(app.screen, ConfirmScreen)
        assert not isinstance(app.screen, EditWorkspaceScreen)  # dismissed


async def test_edit_screen_restart_needed_when_running_and_changed(
    monkeypatch,
):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    restarted = []
    ws = _wsobj("alpha", image="base", running=True)
    app = KlangkApp(
        _edit_state(ws, restart=lambda *a, **k: restarted.append(a))
    )
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#image", Select).value = "py:3"  # create-time change
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)  # restart offered
        # accept -> restart_workspace called + edit screen dismissed
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert restarted
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_no_restart_when_create_field_unchanged(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", image="base", running=True)
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # No create-time field changed (only a live-propagating field).
        es.query_one("#health_check").value = "curl x"
        es._save()
        await app.workers.wait_for_complete()
        assert not isinstance(app.screen, ConfirmScreen)
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_name_required(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    updated = []
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws, update=lambda *a, **k: updated.append(k)))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#name").value = ""
        es._save()
        assert updated == []
        assert "required" in str(es.query_one("#edit_msg").render()).lower()


async def test_edit_screen_save_http_error_shows_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    resp = httpx.Response(
        400,
        json={"detail": "name taken"},
        request=httpx.Request("PUT", "https://x.example"),
    )

    def update(wid, **f):
        raise httpx.HTTPStatusError(
            "boom", request=resp.request, response=resp
        )

    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es._save()
        await app.workers.wait_for_complete()
        assert "name taken" in str(es.query_one("#edit_msg").render())
        assert isinstance(app.screen, EditWorkspaceScreen)  # still on form


async def test_edit_screen_cancel_dismisses(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    updated = []
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws, update=lambda *a, **k: updated.append(k)))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.on_button_pressed(Button.Pressed(button=es.query_one("#cancel")))
        await pilot.pause()
        assert updated == []
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_current_image_not_in_allowed(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    # The workspace's image isn't in the server's allowed list — the picker
    # still shows + pre-selects it (untouched = no change).
    ws = _wsobj("alpha", image="custom:latest")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        assert es.query_one("#image", Select).value == "custom:latest"


async def test_edit_screen_mount_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#mount_input").value = "/host:/c:ro"
        es._add_mount()
        assert es._mounts == ["/host:/c:ro"]
        es.query_one("#mount_list").highlighted = 0
        es._remove_mount()
        assert es._mounts == []


async def test_edit_screen_editors_edge_cases(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")  # no mounts/env/allowed_domains
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # empty input is a no-op for each editor
        es._add_mount()
        es._add_env()
        es._add_allowed_domain()
        assert es._mounts == [] and es._env == {} and es._allowed_domains == []
        # invalid rejected
        es.query_one("#mount_input").value = "badmount"
        es._add_mount()
        assert "source:dest" in str(es.query_one("#edit_msg").render())
        es.query_one("#env_input").value = "NOEQ"
        es._add_env()
        assert "KEY=VALUE" in str(es.query_one("#edit_msg").render())
        # remove with nothing highlighted is a no-op
        es._remove_mount()
        es._remove_env()
        es._remove_allowed_domain()
        # env dup overwrites
        es.query_one("#env_input").value = "K=1"
        es._add_env()
        es.query_one("#env_input").value = "K=2"
        es._add_env()
        assert es._env == {"K": "2"}


async def test_edit_button_and_input_routing(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", running=False)
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # button routing: add/rm for each editor
        es.query_one("#mount_input").value = "/h:/c"
        es.on_button_pressed(FakeBtnPress("add_mount"))
        es.query_one("#mount_list").highlighted = 0
        es.on_button_pressed(FakeBtnPress("rm_mount"))
        assert es._mounts == []
        es.query_one("#env_input").value = "K=V"
        es.on_button_pressed(FakeBtnPress("add_env"))
        es.query_one("#env_list").highlighted = 0
        es.on_button_pressed(FakeBtnPress("rm_env"))
        assert es._env == {}
        es.query_one("#allow_input").value = "github.com:443"
        es.on_button_pressed(FakeBtnPress("add_allow"))
        es.query_one("#allow_list").highlighted = 0
        es.on_button_pressed(FakeBtnPress("rm_allow"))
        assert es._allowed_domains == []
        # input submit routing
        m = es.query_one("#mount_input")
        m.value = "/h:/c"
        es.on_input_submitted(Input.Submitted(m, m.value))
        assert es._mounts == ["/h:/c"]
        e = es.query_one("#env_input")
        e.value = "K=V"
        es.on_input_submitted(Input.Submitted(e, e.value))
        assert es._env == {"K": "V"}
        a = es.query_one("#allow_input")
        a.value = "pypi.org"
        es.on_input_submitted(Input.Submitted(a, a.value))
        assert es._allowed_domains == ["pypi.org"]
        # save via button + name submit
        es.on_button_pressed(FakeBtnPress("save"))
        await app.workers.wait_for_complete()
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_save_auth_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")
    app = KlangkApp(
        _edit_state(
            ws,
            update=lambda *a, **k: (_ for _ in ()).throw(AuthError("expired")),
        )
    )
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es._save()
        await app.workers.wait_for_complete()
        assert "Session expired" in str(es.query_one("#edit_msg").render())


async def test_edit_screen_save_generic_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def update(wid, **f):
        raise RuntimeError("boom")

    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es._save()
        await app.workers.wait_for_complete()
        assert "Failed to save: boom" in str(
            es.query_one("#edit_msg").render()
        )


async def test_edit_screen_save_http_error_non_json(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    resp = httpx.Response(
        502,
        text="<html>proxy</html>",
        request=httpx.Request("PUT", "https://x.example"),
    )

    def update(wid, **f):
        raise httpx.HTTPStatusError(
            "boom", request=resp.request, response=resp
        )

    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es._save()
        await app.workers.wait_for_complete()
        assert "proxy" in str(es.query_one("#edit_msg").render())


async def test_edit_screen_field_submit_saves(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    updated = []
    ws = _wsobj("alpha", running=False)
    app = KlangkApp(_edit_state(ws, update=lambda *a, **k: updated.append(k)))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        name = es.query_one("#name")
        es.on_input_submitted(Input.Submitted(name, name.value))  # -> _save
        await app.workers.wait_for_complete()
        assert updated  # update_workspace called
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_restart_declined(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    restarted = []
    ws = _wsobj("alpha", image="base", running=True)
    app = KlangkApp(
        _edit_state(ws, restart=lambda *a, **k: restarted.append(1))
    )
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#image", Select).value = "py:3"
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)  # decline restart
        await pilot.pause()
        assert restarted == []
        assert not isinstance(app.screen, EditWorkspaceScreen)


async def test_edit_screen_restart_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def boom(*a, **k):
        raise RuntimeError("restart down")

    ws = _wsobj("alpha", image="base", running=True)
    app = KlangkApp(_edit_state(ws, restart=boom))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#image", Select).value = "py:3"
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)  # accept restart -> fails
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "restart failed" in str(es.query_one("#edit_msg").render())


async def test_detail_action_edit_no_workspace(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(
        list_images=lambda: {"default": "base", "allowed": ["base"]},
        allow_autostart=lambda: True,
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        d = app.screen
        d._ws = None  # nothing loaded
        d.action_edit()  # early no-op
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_detail_action_edit_fetch_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")

    def boom():
        raise RuntimeError("down")

    st = _ws(list_images=boom, allow_autostart=boom)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # form still opens, with no images + autostart off
        assert isinstance(app.screen, EditWorkspaceScreen)
        assert app.screen._select_value == "(none)"


async def test_edit_screen_keyboard_remove(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj(
        "alpha",
        mounts=["/h:/c"],
        env={"K": "v"},
        allowed_domains=["github.com:443"],
    )
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        # Delete/remove acts on the list under the active tab (#1891). Switch
        # to each pane by focusing its input (TabbedContent syncs `active` to
        # the focused pane, so this is also the realistic path).
        for inp_id, lid, attr in (
            ("#mount_input", "#mount_list", "_mounts"),
            ("#env_input", "#env_list", "_env"),
            ("#allow_input", "#allow_list", "_allowed_domains"),
        ):
            es.query_one(inp_id).focus()
            await pilot.pause()
            assert (
                tabs.active
                == {
                    "#mount_input": "mounts_pane",
                    "#env_input": "env_pane",
                    "#allow_input": "netfilter_pane",
                }[inp_id]
            )
            es.query_one(lid).highlighted = 0
            es.action_remove_item()
            assert not getattr(es, attr)  # [] or {}
        # Active tab has no list (General) -> remove/edit are no-ops.
        es.query_one("#name").focus()
        await pilot.pause()
        assert tabs.active == "general_pane"
        es.action_remove_item()
        es.action_edit_item()


async def test_edit_screen_edit_in_place(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj(
        "alpha",
        mounts=["/h:/c"],
        env={"K": "v"},
        allowed_domains=["github.com:443"],
    )
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        # Switch panes by focusing each editor's input (TabbedContent syncs
        # `active` to the focused pane). 'e' loads the highlighted row.
        es.query_one("#mount_input").focus()
        await pilot.pause()
        assert tabs.active == "mounts_pane"
        es.query_one("#mount_list").highlighted = 0
        es.action_edit_item()
        assert es.query_one("#mount_input").value == "/h:/c"
        assert es._editing_mount == 0
        es.query_one("#mount_input").value = "/h:/c2"
        es._add_mount()  # replaces, not appends
        assert es._mounts == ["/h:/c2"]
        assert es._editing_mount is None
        # env edit (key tracked)
        es.query_one("#env_input").focus()
        await pilot.pause()
        assert tabs.active == "env_pane"
        es.query_one("#env_list").highlighted = 0
        es.action_edit_item()
        assert es.query_one("#env_input").value == "K=v"
        es.query_one("#env_input").value = "K=changed"
        es._add_env()
        assert es._env == {"K": "changed"}
        # allowed-domain edit
        es.query_one("#allow_input").focus()
        await pilot.pause()
        assert tabs.active == "netfilter_pane"
        es.query_one("#allow_list").highlighted = 0
        es.action_edit_item()
        assert es.query_one("#allow_input").value == "github.com:443"
        es.query_one("#allow_input").value = "pypi.org"
        es._add_allowed_domain()
        assert es._allowed_domains == ["pypi.org"]
        # nothing highlighted -> each _edit_* is a no-op (guard returns)
        es.query_one("#mount_list").highlighted = None
        es._edit_mount()
        es.query_one("#env_list").highlighted = None
        es._edit_env()
        es.query_one("#allow_list").highlighted = None
        es.action_edit_item()


async def test_edit_screen_tabbed_layout(monkeypatch):
    """Edit form groups fields under five tabs; Save/Cancel + #edit_msg stay
    pinned outside the tab content, always visible (#1891)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", image="py:3", service_command="sh", health_check="hc")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        # Five panes in the proposed order; General active on entry.
        assert tabs.active == "general_pane"
        for pane in (
            "general_pane",
            "mounts_pane",
            "env_pane",
            "netfilter_pane",
            "advanced_pane",
        ):
            es.query_one(f"#{pane}", TabPane)
        # No field dropped — a representative field per group is present.
        es.query_one("#name", Input)
        es.query_one("#image", Select)
        es.query_one("#auto_start", Checkbox)
        es.query_one("#mount_input", Input)
        es.query_one("#mount_list")
        es.query_one("#env_input", Input)
        es.query_one("#allow_input", Input)
        es.query_one("#command", Input)
        es.query_one("#health_check", Input)
        # Pinned outside the tab content: #edit_msg, #cancel, #save are
        # siblings of the TabbedContent (never inside a TabPane).
        for wid in ("#edit_msg", "#cancel", "#save"):
            assert not isinstance(es.query_one(wid).parent, TabPane)
        # Name is auto-focused on entry (General tab active).
        assert app.focused is es.query_one("#name")


async def test_edit_screen_tab_spatial_nav(monkeypatch):
    """Down from the strip enters the active pane; Up from the first field
    returns to the strip; Tab still cycles fields (#1891, #1781, #1783)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        # Up from Name (General's first field) -> focus the tab strip.
        es.query_one("#name").focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(app.focused, Tabs)
        # Down from the strip -> back into the active pane's first field.
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is es.query_one("#name")
        # Tab from Name advances to the next General field (Image).
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is es.query_one("#image")
        # Up from a non-first Input (Health, on the Advanced tab) is a no-op:
        # it doesn't match the pane's first field (Command), so focus stays.
        tabs.active = "advanced_pane"
        es.query_one("#command").focus()  # switch to Advanced via focus-sync
        await pilot.pause()
        es.query_one("#health_check").focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is es.query_one("#health_check")


async def test_edit_screen_tab_left_right_switches(monkeypatch):
    """Left/Right on the tab strip switches the active pane (#1891)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        assert tabs.active == "general_pane"
        es.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert tabs.active == "mounts_pane"
        await pilot.press("left")
        await pilot.pause()
        assert tabs.active == "general_pane"


async def test_create_screen_tabbed_layout(monkeypatch):
    """Create form groups fields under five tabs; Cancel/Create +
    #create_msg stay pinned outside the tab content, always visible (#1891)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        tabs = cs.query_one("#form_tabs", TabbedContent)
        # Five panes in the proposed order; General active on entry.
        assert tabs.active == "general_pane"
        for pane in (
            "general_pane",
            "mounts_pane",
            "env_pane",
            "netfilter_pane",
            "advanced_pane",
        ):
            cs.query_one(f"#{pane}", TabPane)
        # No field dropped — a representative field per group is present.
        cs.query_one("#name", Input)
        cs.query_one("#image", Select)
        cs.query_one("#auto_start", Checkbox)
        cs.query_one("#mount_input", Input)
        cs.query_one("#mount_list")
        cs.query_one("#env_input", Input)
        cs.query_one("#allow_input", Input)
        cs.query_one("#command", Input)
        cs.query_one("#health_check", Input)
        # Pinned outside the tab content: #create_msg, #cancel, #create are
        # siblings of the TabbedContent (never inside a TabPane).
        for wid in ("#create_msg", "#cancel", "#create"):
            assert not isinstance(cs.query_one(wid).parent, TabPane)
        # Name is auto-focused on entry (General tab active).
        assert app.focused is cs.query_one("#name")


async def test_create_screen_tab_spatial_nav(monkeypatch):
    """Down from the strip enters the active pane; Up from the first field
    returns to the strip; Tab still cycles fields (#1891, #1781, #1783)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        tabs = cs.query_one("#form_tabs", TabbedContent)
        # Up from Name (General's first field) -> focus the tab strip.
        cs.query_one("#name").focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(app.focused, Tabs)
        # Down from the strip -> back into the active pane's first field.
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is cs.query_one("#name")
        # Tab from Name advances to the next General field (Image).
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is cs.query_one("#image")
        # Up from a non-first Input (Health, on the Advanced tab) is a no-op:
        # it doesn't match the pane's first field (Command), so focus stays.
        tabs.active = "advanced_pane"
        cs.query_one("#command").focus()  # switch to Advanced via focus-sync
        await pilot.pause()
        cs.query_one("#health_check").focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is cs.query_one("#health_check")


async def test_create_screen_tab_left_right_switches(monkeypatch):
    """Left/Right on the tab strip switches the active pane (#1891)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        tabs = cs.query_one("#form_tabs", TabbedContent)
        assert tabs.active == "general_pane"
        cs.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert tabs.active == "mounts_pane"
        await pilot.press("left")
        await pilot.pause()
        assert tabs.active == "general_pane"


async def test_edit_rename_propagates_to_detail_and_list(monkeypatch):
    # #1778/#1768: renaming via the edit form must update the detail screen's
    # name (so it doesn't 404) and refresh the list (so the new name shows).
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    original = _wsobj("alpha", image="base", running=False)
    renamed = _wsobj("renamed", image="base")
    returns = [original, renamed]
    finds = []
    st = _ws(
        list_images=lambda: {"default": "base", "allowed": ["base", "py:3"]},
        allow_autostart=lambda: True,
        update_workspace=lambda wid, **f: None,
    )
    st.find_workspace = lambda n: finds.append(n) or returns.pop(0)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert app.screen._name == "alpha"
        app.screen.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        es = app.screen
        es.query_one("#name").value = "renamed"
        es._save()
        await app.workers.wait_for_complete()
        # back on detail; name adopted + reloaded by the new name
        assert isinstance(app.screen, WorkspaceDetailScreen)
        assert app.screen._name == "renamed"
        assert finds[-1] == "renamed"  # _load resolved the new name


async def test_create_screen_no_server_does_not_crash(monkeypatch):
    # `klangk` with no server configured: pressing `n` must not crash the TUI
    # (list_images raises ValueError, caught -> form opens with no images).
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def boom():
        raise ValueError("no server configured")

    app = KlangkApp(_create_state(list_images=boom, allow_autostart=boom))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()  # must not raise
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        assert isinstance(cs, CreateWorkspaceScreen)
        assert cs._allowed == []


async def test_confirm_screen_button_labels(monkeypatch):
    """ConfirmScreen's affirmative label/variant are parameterizable so the
    create-offer doesn't show a red 'Delete' button for 'Open'.'"""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        # default (delete actions) -> 'Del'
        app.push_screen(ConfirmScreen("sure?"))
        await pilot.pause()
        btns = {b.id: b for b in app.screen.query(Button)}
        assert "Del" in str(btns["yes"].label)
        # parameterized -> custom labels
        app.push_screen(
            ConfirmScreen(
                "open?",
                yes_label="Open",
                yes_variant="primary",
                no_label="Later",
            )
        )
        await pilot.pause()
        btns = {b.id: b for b in app.screen.query(Button)}
        assert "Open" in str(btns["yes"].label)
        assert "Later" in str(btns["no"].label)


# ---------------------------------------------------------------------------
# run_tui + bare-klangk launch wiring
# ---------------------------------------------------------------------------


def test_current_url_and_default_uds_pick_up_udsk(
    monkeypatch, redirect_xdg, tmp_path
):
    sock = tmp_path / "klangk.sock"
    sock.touch()
    monkeypatch.setattr(
        tui_state_mod, "default_server_uds_path", lambda: str(sock)
    )
    # no active server + no override -> the co-located UDS is used
    assert TuiState().current_url() == str(sock)
    assert TuiState().default_uds() == str(sock)
    # override still wins over the UDS fallback
    assert TuiState("https://other").current_url() == "https://other"


def test_derive_alias():
    assert (
        LoginScreen._derive_alias("https://newhost.example/x")
        == "newhost.example"
    )
    # scheme but no host -> falls back to the path tail
    assert LoginScreen._derive_alias("file:///some/path") == "path"
    # bare socket path -> tail
    assert LoginScreen._derive_alias("/a/b/sock") == "sock"
    # bare name -> itself
    assert LoginScreen._derive_alias("justname") == "justname"
    # empty after strip -> generic fallback
    assert LoginScreen._derive_alias("/") == "server"


async def test_login_server_picker(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    calls = {}

    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example")
        ],
        default_uds=lambda: "/tmp/klangk.sock",
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
        switch_server=lambda url: calls.__setitem__("switch", url),
        add_server=lambda alias, url, user=None: calls.__setitem__(
            "add", (alias, url)
        ),
    )
    app = KlangkApp(st)
    async with app.run_test() as _pilot:
        login = app.screen
        # no-server branch: prompt + disabled credentials
        assert "No server selected" in str(
            login.query_one("#server_line").render()
        )
        assert login.query_one("#login", Button).disabled

        # empty choice -> error message
        login._choose_server("   ")
        await app.workers.wait_for_complete()
        assert "Enter a server URL" in str(
            login.query_one("#message").render()
        )

        # known alias -> switch (routed through the option-selected handler)
        login.on_list_view_selected(FakeSelected("prod"))
        await app.workers.wait_for_complete()
        assert calls.get("switch") == "https://prod.example"

        # new URL -> added as an alias derived from its host
        login._choose_server("https://newhost.example/x")
        await app.workers.wait_for_complete()
        assert calls.get("add") == (
            "newhost.example",
            "https://newhost.example/x",
        )

        # UDS path -> also persisted as an alias (basename)
        srv_input = login.query_one("#server_input", Input)
        srv_input.value = "/var/run/other.sock"
        login.on_input_submitted(Input.Submitted(srv_input, srv_input.value))
        await app.workers.wait_for_complete()
        assert calls.get("add") == ("other.sock", "/var/run/other.sock")

        # "Add server" button also dispatches
        srv_input.value = "prod"
        login.on_button_pressed(FakeBtnPress("use_server"))
        await app.workers.wait_for_complete()
        assert calls.get("switch") == "https://prod.example"

        # after a successful pick the server line + enabled creds reflect it
        assert "Server:" in str(login.query_one("#server_line").render())
        assert not login.query_one("#login", Button).disabled


async def test_populate_servers_dedups_default_udsk(monkeypatch):
    """The auto-detected default UDS isn't double-listed when an alias
    already points at it (after the user persisted it)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    uds = "/tmp/klangk.sock"
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [tui_state_mod.ServerInfo("local", uds)],
        default_uds=lambda: uds,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test():
        ol = app.screen.query_one("#server_options", ListView)
        # only the persisted alias row; no separate "Local klangkd (UDS)" row
        assert len(ol.query(ListItem)) == 1


async def test_login_server_list_autofocused(monkeypatch):
    """#1826: first server in the list is focused on initial display."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example")
        ],
        default_uds=lambda: None,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test():
        lv = app.screen.query_one("#server_options", ListView)
        assert lv.index == 0


async def test_login_server_line_hugs_list_and_headers_styled(monkeypatch):
    """#1865: no blank row between the 'Server:' status line and the server
    picker, and the 'Server:' / notice headers are emphasized (accent color +
    bold) so they read as headers."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        current_url=lambda: "http://localhost:8997",
        known_servers=lambda: [],
        default_uds=lambda: None,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        login = app.screen
        line = login.query_one("#server_line")
        notice = login.query_one("#notice")
        opts = login.query_one("#server_options", ListView)
        message = login.query_one("#message")

        # No blank row: the picker's top edge meets the status line's bottom.
        assert opts.region.y == line.region.y + line.region.height

        # Both headers are emphasized as headers (bold + a non-default,
        # accent-derived color; exact rgb is fragile because Textual rounds
        # the resolved design token, so compare against the unstyled line).
        assert line.styles.text_style.bold
        assert notice.styles.text_style.bold
        assert line.styles.color != message.styles.color
        assert notice.styles.color != message.styles.color


async def test_login_server_hints_inline_not_in_footer(monkeypatch):
    """#1890: the server-list delete key is hinted inline on the server
    header and hidden from the Footer (matching the workspace-detail and
    switch-server screens)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        current_url=lambda: "http://localhost:8997",
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "http://localhost:8997"),
        ],
        default_uds=lambda: None,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        login = app.screen

        # (a) The delete hint renders on the server header, with the [d]
        # keycap intact (not eaten as Rich markup).
        hints = str(login.query_one("#server_hints").render())
        assert "[d]" in hints
        assert "delete" in hints

        bindings = {b.key: b for b in login.BINDINGS}

        # (b) The server key still exists (so it works) but is hidden
        # from the Footer.
        assert bindings["d"].show is False


async def test_login_server_list_empty_no_crash(monkeypatch):
    """#1826: no servers → no crash, focus degrades gracefully."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [],
        default_uds=lambda: None,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test():
        lv = app.screen.query_one("#server_options", ListView)
        assert lv.index is None


async def test_login_choose_server_duplicate_alias(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def _raise_conflict(alias, url, user=None):
        raise AliasConflictError(f"Alias '{alias}' already exists.")

    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [],
        default_uds=lambda: None,
        cfg=lambda: CLIConfig(),
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
        add_server=_raise_conflict,
    )
    app = KlangkApp(st)
    async with app.run_test() as _pilot:
        login = app.screen
        login._choose_server("https://dup.example")
        await app.workers.wait_for_complete()
        rendered = str(login.query_one("#message").render()).lower()
        assert "already exists" in rendered


async def test_login_url_switches_to_existing_alias(monkeypatch):
    """Entering a URL whose derived alias already exists switches to it (#1849)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    cfg = CLIConfig()
    cfg.servers = {"prod.example": ServerEntry(url="https://prod.example")}
    calls = {}

    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod.example", "https://prod.example")
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
        switch_server=lambda url: calls.__setitem__("switch", url),
        add_server=lambda alias, url, user=None: calls.__setitem__(
            "add", (alias, url)
        ),
    )
    app = KlangkApp(st)
    async with app.run_test():
        login = app.screen
        # Entering the URL should switch, not try to add a duplicate.
        login._choose_server("https://prod.example")
        await app.workers.wait_for_complete()
        assert calls.get("switch") == "https://prod.example"
        assert "add" not in calls


async def test_login_choose_invalid_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    added = {}
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [],
        default_uds=lambda: None,
        cfg=lambda: CLIConfig(),
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
        add_server=lambda alias, url, user=None: added.__setitem__(
            "a", (alias, url)
        ),
    )
    app = KlangkApp(st)
    async with app.run_test() as _pilot:
        login = app.screen
        login._choose_server("sdfsdf")
        await app.workers.wait_for_complete()
        # not persisted, and a sensible message is shown
        assert added.get("a") is None
        assert "URL" in str(login.query_one("#message").render())


async def test_add_server_rejects_invalid_url(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    added = {}
    st = _authed_state(
        add_server=lambda alias, url, user=None: added.__setitem__(
            "a", (alias, url)
        )
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        s = app.screen
        s.query_one("#alias", Input).value = "x"
        s.query_one("#url", Input).value = "sdfsdf"
        s._add()
        await app.workers.wait_for_complete()
        assert added.get("a") is None
        assert "http" in str(s.query_one("#add_msg").render()).lower()


async def test_add_server_rejects_duplicate_alias_screen(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def _raise_conflict(alias, url, user=None):
        raise AliasConflictError(f"Alias '{alias}' already exists.")

    st = _authed_state(add_server=_raise_conflict)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        s = app.screen
        s.query_one("#alias", Input).value = "prod"
        s.query_one("#url", Input).value = "https://prod.example"
        s._add()
        await app.workers.wait_for_complete()
        rendered = str(s.query_one("#add_msg").render()).lower()
        assert "already exists" in rendered


async def test_confirm_screen(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        app.push_screen(
            ConfirmScreen("Delete X?"),
            lambda r: captured.__setitem__("r", r),
        )
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # Cancel -> False
        app.screen.on_button_pressed(FakeBtnPress("no"))
        await pilot.pause()
        assert captured.get("r") is False
        # Delete -> True
        app.push_screen(
            ConfirmScreen("Delete X?"),
            lambda r: captured.__setitem__("r2", r),
        )
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("yes"))
        await pilot.pause()
        assert captured.get("r2") is True


async def test_login_delete_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    deleted = {}
    st = _st(
        current_url=lambda: "https://prod.example",
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example")
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
        delete_server=lambda url: deleted.__setitem__("u", url) or True,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        ol = login.query_one("#server_options", ListView)
        # nothing highlighted -> prompt to select (no dialog)
        ol.index = None
        login.action_delete_server()
        await pilot.pause()
        assert "Select a server" in str(login.query_one("#message").render())
        # highlight + action -> confirm dialog (not yet deleted)
        ol.index = 0
        login.action_delete_server()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "https://prod.example" in str(
            app.screen.query_one(Static).render()
        )
        assert "u" not in deleted
        # cancel -> not deleted
        app.screen.dismiss(False)
        await pilot.pause()
        assert "u" not in deleted
        # confirm -> deleted
        ol.index = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert deleted.get("u") == "https://prod.example"
        assert "Server deleted" in str(login.query_one("#message").render())
        # confirm but delete returns False -> "Not a saved alias"
        st.delete_server = lambda url: False
        ol.index = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "Not a saved alias" in str(login.query_one("#message").render())


async def test_login_delete_clears_to_no_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        current_url=lambda: "https://prod.example",
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example")
        ],
        default_uds=lambda: None,
        cfg=lambda: CLIConfig(),
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )

    def fake_delete(url):
        st.current_url = lambda: None
        st.known_servers = lambda: []
        return True

    st.delete_server = fake_delete
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        ol = login.query_one("#server_options", ListView)
        ol.index = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert "No server selected" in str(
            login.query_one("#server_line").render()
        )


async def test_login_down_from_last_server_to_input(monkeypatch):
    # Spatial nav: Down at the last server row moves focus to the URL input.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {
        "prod": ServerEntry(url="https://prod.example"),
        "staging": ServerEntry(url="https://staging.example"),
    }
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
            tui_state_mod.ServerInfo("staging", "https://staging.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        lv = login.query_one("#server_options", ListView)
        lv.focus()
        lv.index = 1  # last of 2 servers
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, Input)
        assert app.focused.id == "server_input"


async def test_login_up_from_input_to_server_list(monkeypatch):
    # Spatial nav: Up from the server-input field returns focus to the list.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        srv_input = login.query_one("#server_input", Input)
        srv_input.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(app.focused, ListView)


async def test_login_spatial_nav_full_chain(monkeypatch):
    # Down/Up traverses the entire login form including buttons (#1781):
    # server_input → use_server → identifier → password → login.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    st = _st(
        current_url=lambda: "https://prod.example",
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        await pilot.pause()
        login.query_one("#server_input", Input).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "use_server"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "identifier"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "password"
        await pilot.press("down")
        await pilot.pause()
        assert app.focused.id == "login"
        # Up back
        await pilot.press("up")
        await pilot.pause()
        assert app.focused.id == "password"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused.id == "identifier"
        await pilot.press("up")
        await pilot.pause()
        assert app.focused.id == "use_server"


async def test_workspace_list_up_from_nonzero_stays(monkeypatch):
    # Up from index 1 moves to index 0 (within the list — super path).
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha"), _wsobj("beta")]))
    async with app.run_test() as pilot:
        await pilot.pause()
        lv = app.screen.query_one("#owned_list", ListView)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert lv.index == 0
        assert isinstance(app.focused, ListView)


async def test_login_server_down_from_nonlast_stays(monkeypatch):
    # Down from index 0 (of 2) stays in the list — super path.
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {
        "prod": ServerEntry(url="https://prod.example"),
        "staging": ServerEntry(url="https://staging.example"),
    }
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
            tui_state_mod.ServerInfo("staging", "https://staging.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        lv = login.query_one("#server_options", ListView)
        lv.focus()
        lv.index = 0
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert lv.index == 1
        assert isinstance(app.focused, ListView)


async def test_switch_screen_delete_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    deleted = {}
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example")
        ],
        delete_server=lambda url: deleted.__setitem__("u", url),
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        switch = app.screen
        ol = switch.query_one("#server_options", ListView)
        # nothing highlighted -> no dialog, no delete
        ol.index = None
        switch.action_delete_server()
        await pilot.pause()
        assert app.screen is switch
        assert "u" not in deleted
        # highlight + action -> dialog; cancel -> not deleted
        ol.index = 0
        switch.action_delete_server()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert "u" not in deleted
        # confirm -> deleted
        ol.index = 0
        switch.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert deleted.get("u") == "https://prod.example"


# ---------------------------------------------------------------------------
# run_tui + bare-klangk launch wiring (continued)
# ---------------------------------------------------------------------------


async def test_login_button_dispatch_and_oidc_incomplete(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    # password success routed through on_button_pressed (#login)
    def fake_login(identifier, password):
        st.is_authenticated = lambda: True
        st.email = lambda: identifier
        st.token = lambda: "tok"
        return identifier

    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        known_servers=lambda: [],
        login_password=fake_login,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        login.query_one("#identifier", Input).value = "me@x"
        login.query_one("#password", Input).value = "pw"
        login.on_button_pressed(FakeBtnPress("login"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

    # oidc button dispatch + "did not complete" (oidc_login no-op)
    st2 = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "oidc",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        oidc_providers=lambda: [{"id": "google"}],
        oidc_login=lambda pid: None,
    )
    app2 = KlangkApp(st2)
    async with app2.run_test() as pilot:
        app2.screen.on_button_pressed(FakeBtnPress("oidc"))
        await app2.workers.wait_for_complete()
        assert "did not complete" in str(
            app2.screen.query_one("#message").render()
        )


async def test_main_screen_actions(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    out = {"o": False}

    def fake_logout():
        out["o"] = True
        st.is_authenticated = lambda: False
        st.token = lambda: None

    st = _authed_state(logout=fake_logout)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        main = app.screen
        main.action_switch_server()
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)
        app.pop_screen()
        await pilot.pause()
        main.action_logout()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
    assert out["o"] is True


async def test_add_server_event_handlers(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _authed_state()
    added = {}
    st.add_server = lambda alias, url, user=None: added.setdefault(
        "a", (alias, url)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AddServerScreen())
        await pilot.pause()
        add_screen = app.screen
        add_screen.query_one("#alias", Input).value = "prod"
        url_in = add_screen.query_one("#url", Input)
        url_in.value = "https://p.example"
        # button press dispatch
        add_screen.on_button_pressed(FakeBtnPress("add"))
        await app.workers.wait_for_complete()
        assert added["a"] == ("prod", "https://p.example")
        assert isinstance(app.screen, MainScreen)

    # input-submitted dispatch
    st2 = _authed_state()
    added2 = {}
    st2.add_server = lambda alias, url, user=None: added2.setdefault(
        "a", (alias, url)
    )
    app2 = KlangkApp(st2)
    async with app2.run_test() as pilot:
        app2.push_screen(AddServerScreen())
        await pilot.pause()
        add_screen = app2.screen
        url_in = add_screen.query_one("#url", Input)
        url_in.value = "https://q.example"
        add_screen.query_one("#alias", Input).value = "qa"
        add_screen.on_input_submitted(Input.Submitted(url_in, url_in.value))
        await app2.workers.wait_for_complete()
        assert added2["a"] == ("qa", "https://q.example")


# ---------------------------------------------------------------------------
# run_tui + bare-klangk launch wiring (original section follows)
# ---------------------------------------------------------------------------


def test_is_interactive_returns_bool():
    from klangk.cli import main as cli_main

    assert isinstance(cli_main._is_interactive(), bool)


def test_run_tui_invokes_app_run(monkeypatch):
    seen = {}

    def fake_run(self):
        seen["ran"] = True

    monkeypatch.setattr(KlangkApp, "run", fake_run)
    run_tui()
    assert seen["ran"] is True


def test_bare_klangk_non_tty_prints_help(monkeypatch):
    from typer.testing import CliRunner

    from klangk.cli import main as cli_main
    from klangk.cli.main import app

    launched = {"v": False}
    monkeypatch.setattr(
        tui_pkg,
        "run_tui",
        lambda server_url=None: launched.__setitem__("v", True),
    )
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: False)

    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert launched["v"] is False


def test_bare_klangk_tty_launches_tui(monkeypatch):
    from typer.testing import CliRunner

    from klangk.cli import main as cli_main
    from klangk.cli.main import app

    seen = {}
    monkeypatch.setattr(
        tui_pkg,
        "run_tui",
        lambda server_url=None: seen.__setitem__("s", server_url),
    )
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)

    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert seen["s"] is None


def test_bare_klangk_tty_crash_surfaces_error(monkeypatch):
    from typer.testing import CliRunner

    from klangk.cli import main as cli_main
    from klangk.cli.main import app

    def boom(server_url=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tui_pkg, "run_tui", boom)
    monkeypatch.setattr(cli_main, "_is_interactive", lambda: True)

    result = CliRunner().invoke(app, [])
    assert result.exit_code == 1
    assert "TUI error" in result.output


def test_subcommand_does_not_launch_tui(monkeypatch):
    from typer.testing import CliRunner

    from klangk.cli.main import app

    launched = {"v": False}
    monkeypatch.setattr(
        tui_pkg,
        "run_tui",
        lambda server_url=None: launched.__setitem__("v", True),
    )
    # Never let this in-process invoke POST a real logout at a live server.
    # The klangkc-tests conftest isolates CLI state to a tmp dir, but this
    # guard keeps the test safe even if a fixture pre-seeds credentials (#1900).
    monkeypatch.setattr("klangk.cli.main.do_logout", lambda *a, **k: None)
    CliRunner().invoke(app, ["logout"])
    assert launched["v"] is False


# ---------------------------------------------------------------------------
# SpatialNavScreen early returns (lines 160, 170)
# ---------------------------------------------------------------------------


async def test_spatial_up_early_return_no_chain_match(monkeypatch):
    """action_spatial_up returns early when focused widget not in chain."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        # Focus the server_options list which is NOT in the SPATIAL_CHAIN.
        lv = login.query_one("#server_options", ListView)
        lv.focus()
        await pilot.pause()
        # action_spatial_up should return early (line 160), no crash.
        login.action_spatial_up()
        await pilot.pause()
        # Focus didn't change — still on server_options.
        assert app.focused is lv


async def test_spatial_down_early_return_no_chain_match(monkeypatch):
    """action_spatial_down returns early when focused widget not in chain."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    cfg = CLIConfig()
    cfg.servers = {"prod": ServerEntry(url="https://prod.example")}
    st = _st(
        current_url=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo("prod", "https://prod.example"),
        ],
        default_uds=lambda: None,
        cfg=lambda: cfg,
        auth_mode=lambda: "password",
        email=lambda: None,
        token=lambda: None,
        is_authenticated=lambda: False,
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        login = app.screen
        lv = login.query_one("#server_options", ListView)
        lv.focus()
        await pilot.pause()
        # action_spatial_down should return early (line 170), no crash.
        login.action_spatial_down()
        await pilot.pause()
        assert app.focused is lv


# ---------------------------------------------------------------------------
# TabSkipMixin on_key branches (lines 199–214)
# ---------------------------------------------------------------------------


async def test_tab_skip_non_tab_key_returns(monkeypatch):
    """TabSkipMixin.on_key returns immediately for non-tab keys (line 200)."""
    from textual.events import Key

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["base", "py:3"], default="base", allow_autostart=True
            )
        )
        await pilot.pause()
        name_input = app.screen.query_one("#name", Input)
        name_input.focus()
        await pilot.pause()
        # Call on_key directly with a non-tab key — hits line 200.
        app.screen.on_key(Key("a", "a"))
        await pilot.pause()
        assert app.focused.id == "name"


async def test_tab_skip_not_in_order(monkeypatch):
    """TabSkipMixin.on_key returns when focused widget not in _TAB_ORDER (line 204)."""
    from textual.events import Key

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["base", "py:3"], default="base", allow_autostart=True
            )
        )
        await pilot.pause()
        # Simulate focus on a widget not in _TAB_ORDER by patching focused.
        btn = app.screen.query_one("#add_mount", Button)
        orig_focused = type(app.screen).focused
        monkeypatch.setattr(
            type(app.screen), "focused", property(lambda self: btn)
        )
        # Call on_key directly with tab — hits line 204 (base not in TAB_ORDER).
        app.screen.on_key(Key("tab", "\t"))
        monkeypatch.setattr(type(app.screen), "focused", orig_focused)


async def test_tab_skip_cycles_fields(monkeypatch):
    """Tab cycles through _TAB_ORDER fields (lines 205-214)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["base", "py:3"], default="base", allow_autostart=True
            )
        )
        await pilot.pause()
        name_input = app.screen.query_one("#name", Input)
        name_input.focus()
        await pilot.pause()
        # Tab should advance to the next field in _TAB_ORDER.
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused.id != "name"


# ---------------------------------------------------------------------------
# TuiState account wrappers (#1753) — real methods, not stubbed
# ---------------------------------------------------------------------------


def _tuistate_with_client(
    client, *, url="https://x.example", email="me@x.example"
):
    from klangk.cli.config import CLIState

    st = TuiState()
    st.current_url = lambda: url
    st.client = lambda: client
    cli_state = CLIState()
    if url is not None and email is not None:
        cli_state.set_credentials(url, email, "tok")
    cli_state.save = lambda: None
    st.state = lambda: cli_state
    return st


def _status_err(msg, status=400):
    req = httpx.Request("POST", "https://x")
    return httpx.HTTPStatusError(
        msg, request=req, response=httpx.Response(status, request=req)
    )


_ACCOUNT_METHODS = [
    ("get_me", ()),
    ("change_password", ("old", "new")),
    ("change_handle", ("h", "pw")),
    ("change_email", ("e@x.example", "pw")),
]


@pytest.mark.parametrize("method,args", _ACCOUNT_METHODS)
def test_tuistate_account_no_server(method, args):
    from unittest.mock import MagicMock

    st = TuiState()
    st.current_url = lambda: None
    st.client = lambda: MagicMock()
    with pytest.raises(LoginError, match="No server"):
        getattr(st, method)(*args)


@pytest.mark.parametrize("method,args", _ACCOUNT_METHODS)
def test_tuistate_account_http_status_error(method, args):
    from unittest.mock import MagicMock

    client = MagicMock()
    getattr(client, method).side_effect = _status_err("409: conflict")
    st = _tuistate_with_client(client)
    with pytest.raises(LoginError, match="409"):
        getattr(st, method)(*args)


@pytest.mark.parametrize("method,args", _ACCOUNT_METHODS)
def test_tuistate_account_http_error(method, args):
    from unittest.mock import MagicMock

    client = MagicMock()
    getattr(client, method).side_effect = httpx.ConnectError("down")
    st = _tuistate_with_client(client)
    with pytest.raises(LoginError, match="could not reach"):
        getattr(st, method)(*args)


def test_tuistate_get_me_success():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.get_me.return_value = {"id": "u1", "email": "e", "handle": "h"}
    st = _tuistate_with_client(client)
    assert st.get_me() == {"id": "u1", "email": "e", "handle": "h"}


def test_tuistate_get_me_session_expired():
    # get_me uses check_auth, so a 401 surfaces as AuthError — the wrapper
    # converts it to a friendly LoginError (not a raw traceback).
    from unittest.mock import MagicMock

    from klangk.cli.client import AuthError

    client = MagicMock()
    client.get_me.side_effect = AuthError(
        "Session expired — run `klangk login`"
    )
    st = _tuistate_with_client(client)
    with pytest.raises(LoginError, match="Session expired"):
        st.get_me()


def test_tuistate_change_password_success():
    from unittest.mock import MagicMock

    client = MagicMock()
    st = _tuistate_with_client(client)
    st.change_password("old", "new")
    client.change_password.assert_called_once_with("old", "new")


def test_tuistate_change_handle_success():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.change_handle.return_value = "newh"
    st = _tuistate_with_client(client)
    assert st.change_handle("newh", "pw") == "newh"


def test_tuistate_change_email_rekeys_state():
    from unittest.mock import MagicMock

    client = MagicMock()
    st = _tuistate_with_client(client, email="old@x.example")
    st.change_email("new@x.example", "pw")
    client.change_email.assert_called_once_with("new@x.example", "pw")
    # Token preserved, active user re-keyed to the new address.
    assert st.state().get_email("https://x.example") == "new@x.example"
    assert st.state().get_token("https://x.example") == "tok"


def test_tuistate_change_email_same_address_no_rekey():
    from unittest.mock import MagicMock

    client = MagicMock()
    st = _tuistate_with_client(client, email="same@x.example")
    st.change_email("same@x.example", "pw")
    client.change_email.assert_called_once_with("same@x.example", "pw")


# ---------------------------------------------------------------------------
# Account self-service screen (#1753)
# ---------------------------------------------------------------------------


def _account_state(**extra):
    """Authed TuiState with account methods stubbed for AccountScreen tests."""
    base = dict(
        is_authenticated=lambda: True,
        current_url=lambda: "https://x.example",
        email=lambda: "me@x.example",
        token=lambda: "tok",
        known_servers=lambda: [],
        list_owned_workspaces=lambda: [],
        list_shared_workspaces=lambda: [],
        get_me=lambda: {
            "id": "u1",
            "email": "me@x.example",
            "handle": "me",
        },
        change_password=lambda current, new: None,
        change_handle=lambda handle, pw: handle,
        change_email=lambda email, pw: None,
    )
    base.update(extra)
    return _st(**base)


async def test_account_screen_loads_profile(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        profile = str(app.screen.query_one("#profile").render())
        assert "me@x.example" in profile
        assert "@me" in profile


async def test_account_screen_change_password_success(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    calls = {}
    st = _account_state(
        change_password=lambda current, new: calls.__setitem__(
            "pw", (current, new)
        )
    )
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#pw_current", Input).value = "oldpw"
        s.query_one("#pw_new", Input).value = "newpw12"
        s.query_one("#pw_confirm", Input).value = "newpw12"
        s.on_button_pressed(FakeBtnPress("pw_submit"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls.get("pw") == ("oldpw", "newpw12")
        assert "Password updated" in str(s.query_one("#pw_msg").render())
        # Fields cleared on success.
        assert s.query_one("#pw_current", Input).value == ""


async def test_account_screen_password_mismatch(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _account_state(change_password=lambda c, n: None)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#pw_current", Input).value = "oldpw"
        s.query_one("#pw_new", Input).value = "newpw12"
        s.query_one("#pw_confirm", Input).value = "different"
        s.on_button_pressed(FakeBtnPress("pw_submit"))
        await pilot.pause()
        assert "do not match" in str(s.query_one("#pw_msg").render())


async def test_account_screen_password_too_short(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 12
    )
    app = KlangkApp(_account_state(change_password=lambda c, n: None))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#pw_current", Input).value = "oldpw"
        s.query_one("#pw_new", Input).value = "short"
        s.query_one("#pw_confirm", Input).value = "short"
        s.on_button_pressed(FakeBtnPress("pw_submit"))
        # min-length is now checked in the worker (off the event loop).
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "at least 12" in str(s.query_one("#pw_msg").render())


async def test_account_screen_password_backend_error(monkeypatch):
    async def noop(*a, **k):
        return None

    def boom(current, new):
        raise LoginError("401: Current password is incorrect")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(_account_state(change_password=boom))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#pw_current", Input).value = "oldpw"
        s.query_one("#pw_new", Input).value = "newpw12"
        s.query_one("#pw_confirm", Input).value = "newpw12"
        s.on_button_pressed(FakeBtnPress("pw_submit"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Current password is incorrect" in str(
            s.query_one("#pw_msg").render()
        )


async def test_account_screen_change_handle_success(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    calls = {}
    st = _account_state(
        change_handle=lambda handle, pw: (
            calls.__setitem__("h", (handle, pw)) or "newhandle"
        )
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "newhandle"
        s.query_one("#handle_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        # Confirm dialog pushed.
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls.get("h") == ("newhandle", "pw")
        assert "Handle updated to @newhandle" in str(
            s.query_one("#handle_msg").render()
        )


async def test_account_screen_handle_uses_server_accepted(monkeypatch):
    """#1869 review: the TUI must adopt the server's accepted handle, not
    the user's input, when they differ (e.g. uniqueness suffixing)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _account_state(change_handle=lambda handle, pw: "accepted")
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "newhandle"
        s.query_one("#handle_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        app.screen.dismiss(True)
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Server returned "accepted", not the requested "newhandle".
        assert "@accepted" in str(s.query_one("#handle_msg").render())
        assert "@accepted" in str(s.query_one("#profile").render())


async def test_account_screen_handle_invalid(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_handle=lambda h, pw: h))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "Bad Handle!"
        s.query_one("#handle_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        assert "lowercase" in str(s.query_one("#handle_msg").render())


async def test_account_screen_handle_cancelled(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    called = {}
    st = _account_state(change_handle=lambda h, pw: called.__setitem__("h", h))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "newhandle"
        s.query_one("#handle_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "h" not in called  # not invoked


async def test_account_screen_change_email_success(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    calls = {}
    st = _account_state(
        change_email=lambda email, pw: calls.__setitem__("e", (email, pw))
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#email_new", Input).value = "new@x.example"
        s.query_one("#email_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("email_submit"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls.get("e") == ("new@x.example", "pw")
        assert "Email updated" in str(s.query_one("#email_msg").render())
        assert "new@x.example" in str(s.query_one("#profile").render())


async def test_account_screen_email_invalid(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_email=lambda e, pw: None))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#email_new", Input).value = "not-an-email"
        s.query_one("#email_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("email_submit"))
        await pilot.pause()
        assert "valid email" in str(s.query_one("#email_msg").render())


async def test_account_screen_password_required_fields(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(_account_state(change_password=lambda c, n: None))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        # Empty current/new -> rejected before any request.
        s.query_one("#pw_current", Input).value = ""
        s.query_one("#pw_new", Input).value = ""
        s.query_one("#pw_confirm", Input).value = ""
        s.on_button_pressed(FakeBtnPress("pw_submit"))
        await pilot.pause()
        assert "required" in str(s.query_one("#pw_msg").render())


async def test_account_screen_handle_requires_password(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_handle=lambda h, pw: h))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "newhandle"
        s.query_one("#handle_pw", Input).value = ""
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        assert "Password is required" in str(
            s.query_one("#handle_msg").render()
        )


async def test_account_screen_handle_backend_error(monkeypatch):
    async def noop(*a, **k):
        return None

    def boom(handle, pw):
        raise LoginError("400: Handle taken")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_handle=boom))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#handle_new", Input).value = "newhandle"
        s.query_one("#handle_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("handle_submit"))
        await pilot.pause()
        app.screen.dismiss(True)  # confirm
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "Handle taken" in str(s.query_one("#handle_msg").render())


async def test_account_screen_email_requires_password(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_email=lambda e, pw: None))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#email_new", Input).value = "new@x.example"
        s.query_one("#email_pw", Input).value = ""
        s.on_button_pressed(FakeBtnPress("email_submit"))
        await pilot.pause()
        assert "Password is required" in str(
            s.query_one("#email_msg").render()
        )


async def test_account_screen_email_backend_error(monkeypatch):
    async def noop(*a, **k):
        return None

    def boom(email, pw):
        raise LoginError("400: Email already in use")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state(change_email=boom))
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#email_new", Input).value = "new@x.example"
        s.query_one("#email_pw", Input).value = "pw"
        s.on_button_pressed(FakeBtnPress("email_submit"))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "already in use" in str(s.query_one("#email_msg").render())


async def test_account_screen_enter_submits_section(monkeypatch):
    """Enter in a section's last field submits that section."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    called = {}
    st = _account_state(change_email=lambda e, pw: called.__setitem__("e", e))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        s.query_one("#email_new", Input).value = "new@x.example"
        email_pw = s.query_one("#email_pw", Input)
        email_pw.value = "pw"
        s.on_input_submitted(Input.Submitted(email_pw, email_pw.value))
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert called.get("e") == "new@x.example"


async def test_account_screen_load_profile_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _account_state(
        get_me=lambda: (_ for _ in ()).throw(LoginError("nope"))
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "nope" in str(app.screen.query_one("#profile").render())


async def test_main_screen_action_account_opens_screen(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.screen.action_account()
        await pilot.pause()
        assert isinstance(app.screen, AccountScreen)


# ---------------------------------------------------------------------------
# Account screen tabbed DOM (#1898): three tabs (Password / Handle /
# Email), profile pinned above the tabs, spatial nav across tabs.
# ---------------------------------------------------------------------------


def _acct_pane_ids(screen):
    """Ids of the TabPanes inside the account TabbedContent, in order."""
    tc = screen.query_one("#acct_tabs", TabbedContent)
    return [p.id for p in tc.query(TabPane)]


async def test_account_screen_has_three_tabs_with_all_fields(monkeypatch):
    """Three tabs (Password / Handle / Email); no field dropped (#1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(
        scr_account._account_mod, "password_min_length", lambda url: 4
    )
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        tc = s.query_one("#acct_tabs", TabbedContent)
        # Exactly three panes, in the documented order.
        assert _acct_pane_ids(s) == ["pw_pane", "handle_pane", "email_pane"]
        # Every per-section field/button is mounted (under its own pane).
        for fid, pane_id in [
            ("pw_current", "pw_pane"),
            ("pw_new", "pw_pane"),
            ("pw_confirm", "pw_pane"),
            ("pw_msg", "pw_pane"),
            ("pw_submit", "pw_pane"),
            ("handle_new", "handle_pane"),
            ("handle_pw", "handle_pane"),
            ("handle_msg", "handle_pane"),
            ("handle_submit", "handle_pane"),
            ("email_new", "email_pane"),
            ("email_pw", "email_pane"),
            ("email_msg", "email_pane"),
            ("email_submit", "email_pane"),
        ]:
            field = s.query_one(f"#{fid}")
            pane = s.query_one(f"#{pane_id}", TabPane)
            assert pane in field.ancestors, f"{fid} not under {pane_id}"
        # The submit flows still resolve (handlers unchanged).
        assert tc.active == "pw_pane"  # Password is the initial tab


async def test_account_screen_profile_pinned_above_tabs(monkeypatch):
    """#profile sits above the TabbedContent, not inside any tab pane, so
    the current @handle / email stays visible on every tab (#1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        profile = s.query_one("#profile")
        tc = s.query_one("#acct_tabs", TabbedContent)
        # Profile is a sibling of the TabbedContent, both inside #account_box.
        assert profile.parent.id == "account_box"
        assert tc.parent.id == "account_box"
        # And profile is NOT a descendant of the TabbedContent.
        with pytest.raises(Exception):
            tc.query_one("#profile")


async def test_account_screen_left_right_switch_tabs(monkeypatch):
    """Left/Right on the tab strip switches the active tab; switching
    drops focus into the new tab's first field (#1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen
        tc = s.query_one("#acct_tabs", TabbedContent)
        s.query_one(Tabs).focus()
        await pilot.pause()

        await pilot.press("right")
        await pilot.pause()
        assert tc.active == "handle_pane"
        assert app.focused is s.query_one("#handle_new", Input)

        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert tc.active == "email_pane"
        assert app.focused is s.query_one("#email_new", Input)

        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert tc.active == "handle_pane"
        assert app.focused is s.query_one("#handle_new", Input)


async def test_account_screen_spatial_chain_follows_active_tab(monkeypatch):
    """SPATIAL_CHAIN returns the active tab's fields, so the inherited
    Up/Down handler walks only the visible tab (#1898).

    Tab switches are driven via the keyboard (Left/Right on the strip)
    rather than assigning ``tc.active`` directly: the programmatic set
    races the TabActivated message under load, while the keyboard path
    settles reliably (it's the real user interaction)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen

        # Password tab is active on mount.
        assert s.SPATIAL_CHAIN == [
            "pw_current",
            "pw_new",
            "pw_confirm",
            "pw_submit",
        ]

        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")  # -> handle_pane
        await pilot.pause()
        assert s._active_tab_id() == "handle_pane"
        assert s.SPATIAL_CHAIN == ["handle_new", "handle_pw", "handle_submit"]

        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")  # -> email_pane
        await pilot.pause()
        assert s._active_tab_id() == "email_pane"
        assert s.SPATIAL_CHAIN == ["email_new", "email_pw", "email_submit"]


async def test_account_screen_down_from_strip_enters_tab(monkeypatch):
    """Down from the tab strip focuses the first field of the active tab
    (#1781, #1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen

        # Password tab (default).
        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_current", Input)

        # Handle tab — switch via the keyboard (reliable settling).
        s.query_one(Tabs).focus()
        await pilot.pause()
        await pilot.press("right")  # -> handle_pane
        await pilot.pause()
        s.query_one(Tabs).focus()  # back to the strip
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#handle_new", Input)


async def test_account_screen_up_from_first_field_returns_to_strip(
    monkeypatch,
):
    """Up from the first field of a tab returns focus to the tab strip so
    Left/Right can switch tabs — no focus trap (#1781, #1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen

        s.query_one("#pw_current", Input).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert isinstance(app.focused, Tabs)

        # Non-first field: Up stays inside the chain (no jump to strip).
        s.query_one("#pw_new", Input).focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_current", Input)


async def test_account_screen_spatial_up_down_within_tab(monkeypatch):
    """Up/Down walks the active tab's chain top-to-bottom and stops at the
    last field (no trap downward) (#1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen

        s.query_one("#pw_current", Input).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_new", Input)
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_confirm", Input)
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_submit", Button)
        # Past the last field: focus stays put (no trap, no wrap).
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is s.query_one("#pw_submit", Button)


async def test_account_screen_spatial_up_noop_off_chain(monkeypatch):
    """action_spatial_up is a no-op when focus isn't on a chain widget
    (e.g. on the tab strip itself) — the early-return branch (#1898)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_account_state())
    async with app.run_test() as pilot:
        app.push_screen(AccountScreen())
        await app.workers.wait_for_complete()
        await pilot.pause()
        s = app.screen

        # The tab strip is not in any tab's SPATIAL_CHAIN, so Up from it
        # returns early without crashing or moving focus.
        s.query_one(Tabs).focus()
        await pilot.pause()
        s.action_spatial_up()
        await pilot.pause()
        assert isinstance(app.focused, Tabs)


# ---------------------------------------------------------------------------
# Terminal list push (#1894)
# ---------------------------------------------------------------------------


async def test_detail_terminal_push_updates_list(monkeypatch):
    """terminals_changed carrying the window list updates the detail
    screen directly, with no terminal_start re-enumeration (#1894)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        # Server pushes a new window list (e.g. a terminal was added in
        # the Flutter UI). The detail screen adopts it verbatim, no fetch.
        app.screen.apply_status_event(
            {
                "type": "terminals_changed",
                "windows": [
                    {"index": 0, "name": "main", "id": "@0"},
                    {"index": 1, "name": "build", "id": "@1"},
                    {"index": 2, "name": "logs", "id": "@2"},
                ],
            }
        )
        await pilot.pause()
        lv = app.screen.query_one("#term_list", ListView)
        assert len(lv.query(ListItem)) == 3


async def test_detail_terminal_push_falls_back_when_no_windows(monkeypatch):
    """terminals_changed without a payload falls back to a fetch
    (backward compat with older servers) (#1894)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    a = _wsobj("alpha", running=True)
    added = {"extra": False}

    async def growing_terms(*a, **k):
        terms = [{"index": 0, "name": "main", "id": "@0"}]
        if added["extra"]:
            terms.append({"index": 1, "name": "build", "id": "@1"})
        return terms

    st = _ws(list_terminals=growing_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        lv = app.screen.query_one("#term_list", ListView)
        assert len(lv.query(ListItem)) == 1
        # Older server: no windows payload -> re-fetch.
        added["extra"] = True
        app.screen.apply_status_event({"type": "terminals_changed"})
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(lv.query(ListItem)) == 2


# ---------------------------------------------------------------------------
# Session expiry / token refresh (#1877)
# ---------------------------------------------------------------------------


async def test_session_expired_redirects_to_login(monkeypatch):
    """KlangkApp.session_expired() pops to login with a notification."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    logged_out = []
    st.logout = lambda: logged_out.append(True)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, MainScreen)
        app.session_expired()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert logged_out == [True]


async def test_session_expired_noop_on_login_screen(monkeypatch):
    """session_expired() is a no-op when already on the login screen."""
    st = TuiState()
    st.current_url = lambda: None
    st.cfg = lambda: type("C", (), {"servers": {}})()
    st.state = lambda: type(
        "S",
        (),
        {
            "active_server": None,
            "get_token": lambda self, u: None,
        },
    )()
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        assert isinstance(app.screen, LoginScreen)
        app.session_expired()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)


async def test_workspace_load_generic_error_surfaces_message(monkeypatch):
    """WorkspaceDetailScreen._load() shows the error for non-auth failures."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _ws()

    def _raise_rt(*a, **k):
        raise RuntimeError("db connection lost")

    st.find_workspace = _raise_rt
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("ws"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        body = str(app.screen.query_one("#detail_body").render())
        assert "db connection lost" in body


async def test_run_token_refresh_loop_returns_expired_on_failure(monkeypatch):
    """run_token_refresh_loop returns 'expired' when refresh fails."""
    import time as _time

    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)
    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_MARGIN", 99999)

    fake_token_payload = {
        "sub": "uid",
        "exp": int(_time.time()) + 60,
    }
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps(fake_token_payload).encode()
    ).rstrip(b"=")
    fake_jwt = f"{header.decode()}.{payload.decode()}.sig"

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return fake_jwt

    monkeypatch.setattr(scr_main, "_refresh_token", lambda url, tok: None)
    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "expired"


async def test_run_token_refresh_loop_returns_no_token(monkeypatch):
    """run_token_refresh_loop returns 'no_token' when token disappears."""
    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return None

    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "no_token"


def _fake_jwt(exp=None):
    """Build a fake JWT with the given ``exp`` claim (or none)."""
    import base64
    import json

    payload = {} if exp is None else {"exp": exp}
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    return f"{header.decode()}.{body.decode()}.sig"


async def test_run_token_refresh_loop_skips_when_exp_missing(monkeypatch):
    """A token with no ``exp`` claim is skipped (continue); exits on no-token."""
    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)
    tokens = iter([_fake_jwt(exp=None), None])

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return next(tokens)

    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "no_token"


async def test_run_token_refresh_loop_skips_when_far_from_expiry(monkeypatch):
    """A token not near expiry is skipped (continue); exits on no-token."""
    import time as _time

    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)
    tokens = iter([_fake_jwt(exp=int(_time.time()) + 3600), None])

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return next(tokens)

    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "no_token"


async def test_run_token_refresh_loop_refreshes_near_expiry(monkeypatch):
    """A near-expiry token is refreshed (success branch); exits on no-token."""
    import time as _time

    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)
    tokens = iter([_fake_jwt(exp=int(_time.time()) + 60), None])

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return next(tokens)

    monkeypatch.setattr(scr_main, "_refresh_token", lambda url, tok: "newtok")
    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "no_token"


async def test_status_loop_token_disappears_mid_retry(monkeypatch):
    """Token vanishing between retries redirects to login (inner no-token)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    calls = {"n": 0}

    def token():
        calls["n"] += 1
        return "tok" if calls["n"] == 1 else None

    app = KlangkApp(_authed_state(token=token))
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        await app.screen._status_loop()
        await pilot.pause()
    assert expired


async def test_status_loop_auth_error_expires_session(monkeypatch):
    """An AuthError from the status WS redirects to login."""

    async def auth_err(*a, **k):
        raise AuthError("401")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", auth_err)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_authed_state())
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        await app.screen._status_loop()
        await pilot.pause()
    assert expired


async def test_token_refresh_loop_expires_session(monkeypatch):
    """_token_refresh_loop redirects to login when refresh fails ('expired')."""

    async def expired(*a, **k):
        return "expired"

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "run_token_refresh_loop", expired)
    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    fired = []
    monkeypatch.setattr(app, "session_expired", lambda: fired.append(1))
    async with app.run_test():
        await app.screen._token_refresh_loop()
    assert fired


async def test_session_expired_is_re_entrant_safe(monkeypatch):
    """Concurrent session_expired() calls fire the redirect exactly once."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    st.logout = lambda: None  # avoid real credential I/O
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.session_expired()  # first call: sets _expiring, spawns worker
        app.session_expired()  # second call: bails on _expiring
        await pilot.pause()
        logins = [
            s for s in app.screen_stack if isinstance(s, scr.LoginScreen)
        ]
        assert len(logins) == 1


async def test_run_token_refresh_loop_concurrent_rotation(monkeypatch):
    """If the token was rotated concurrently, don't expire — keep running."""
    import time as _time

    monkeypatch.setattr(scr_main, "_TOKEN_REFRESH_POLL", 0)
    near = _fake_jwt(exp=int(_time.time()) + 60)
    # token() returns: the near-expiry token, then a *different* one (the
    # mitigation re-reads state and sees the rotation), then None (exit).
    tokens = iter([near, "rotated-by-other-refresher", None])

    class FakeState:
        def current_url(self):
            return "https://x.example"

        def token(self):
            return next(tokens)

    monkeypatch.setattr(scr_main, "_refresh_token", lambda url, tok: None)
    result = await _real_run_token_refresh_loop(FakeState())
    assert result == "no_token"


async def test_create_screen_editor_add_buttons_clickable(monkeypatch):
    """Regression (#1891): the Add buttons inside each editor tab must be
    reachable by mouse. A greedy default-width Input used to push Add/Remove
    past the tab pane's clip region, so clicks silently missed — typing a
    mount/env/domain and clicking Add then Create saved nothing. Also covers
    the Advanced-tab text inputs (command/health)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def fake_create(*a, **k):
        captured["k"] = k
        return _wsobj("zzz")

    app = KlangkApp(_create_state(create=fake_create))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        tabs = cs.query_one("#form_tabs", TabbedContent)
        # General tab is active on entry — set the required name first.
        cs.query_one("#name", Input).value = "myws"
        await pilot.pause()
        # Mounts tab: input + Add button must both be inside the pane.
        tabs.active = "mounts_pane"
        await pilot.pause()
        cs.query_one("#mount_input", Input).value = "/host:/c"
        await pilot.pause()
        assert await pilot.click("#add_mount")  # True == landed on the button
        await pilot.pause()
        # Environment tab
        tabs.active = "env_pane"
        await pilot.pause()
        cs.query_one("#env_input", Input).value = "A=1"
        await pilot.pause()
        assert await pilot.click("#add_env")
        await pilot.pause()
        # Netfilter tab
        tabs.active = "netfilter_pane"
        await pilot.pause()
        cs.query_one("#allow_input", Input).value = "github.com:443"
        await pilot.pause()
        assert await pilot.click("#add_allow")
        await pilot.pause()
        # Advanced tab text inputs (field-row; never had the overflow, but
        # confirm they're reachable and flow through to Create).
        tabs.active = "advanced_pane"
        await pilot.pause()
        cs.query_one("#command", Input).value = "./run"
        cs.query_one("#health_check", Input).value = "curl localhost"
        await pilot.pause()
        # Create — every field must be persisted.
        assert await pilot.click("#create")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert captured["k"]["mounts"] == ["/host:/c"]
        assert captured["k"]["env"] == {"A": "1"}
        assert captured["k"]["allowed_domains"] == ["github.com:443"]
        assert captured["k"]["service_command"] == "./run"
        assert captured["k"]["health_check"] == "curl localhost"


async def test_edit_screen_editor_add_buttons_clickable(monkeypatch):
    """Regression (#1891): same as the create-screen case but for the edit
    form — Add buttons inside each editor tab must be clickable and the
    entries must reach the PUT on Save."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", running=False)  # not running -> no restart prompt
    captured = {}

    def fake_update(*a, **k):
        captured["k"] = k

    app = KlangkApp(_edit_state(ws, update=fake_update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        tabs.active = "mounts_pane"
        await pilot.pause()
        es.query_one("#mount_input", Input).value = "/host:/c"
        await pilot.pause()
        assert await pilot.click("#add_mount")
        await pilot.pause()
        tabs.active = "env_pane"
        await pilot.pause()
        es.query_one("#env_input", Input).value = "A=1"
        await pilot.pause()
        assert await pilot.click("#add_env")
        await pilot.pause()
        tabs.active = "netfilter_pane"
        await pilot.pause()
        es.query_one("#allow_input", Input).value = "github.com:443"
        await pilot.pause()
        assert await pilot.click("#add_allow")
        await pilot.pause()
        assert await pilot.click("#save")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert captured["k"]["mounts"] == ["/host:/c"]
        assert captured["k"]["env"] == {"A": "1"}
        assert captured["k"]["allowed_domains"] == ["github.com:443"]


async def test_edit_running_env_saved_before_restart_prompt(monkeypatch):
    """Editing a RUNNING workspace's env persists the change *before* the
    restart-needed prompt appears; dismissing the prompt (Skip) must not
    drop it (#1891). The update PUT fires unconditionally in _do_save,
    ahead of the ConfirmScreen."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", running=True, env={"OLD": "x"})
    captured = {}

    def fake_update(*a, **k):
        captured["k"] = k

    app = KlangkApp(_edit_state(ws, update=fake_update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        tabs = es.query_one("#form_tabs", TabbedContent)
        tabs.active = "env_pane"
        await pilot.pause()
        es.query_one("#env_input", Input).value = "a=1"
        await pilot.pause()
        assert await pilot.click("#add_env")  # Add button reachable
        await pilot.pause()
        assert es._env == {"OLD": "x", "a": "1"}
        assert await pilot.click("#save")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The PUT fired with the merged env before any prompt.
        assert captured["k"]["env"] == {"OLD": "x", "a": "1"}
        # Restart-needed prompt is on top — Skip it.
        assert await pilot.click("#no")
        await pilot.pause()
        assert captured["k"]["env"] == {"OLD": "x", "a": "1"}  # unchanged
