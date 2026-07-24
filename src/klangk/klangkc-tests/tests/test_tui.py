"""Tests for the klangk TUI foundation (issue #1746).

Covers the textual app shell, login/server-switch flows, the live state
bridge, the WebSocket status listener, the bare-``klangk`` launch wiring,
and the ``add_server_to_config`` helper — under the 100% coverage gate.
"""

from __future__ import annotations

import httpx
import pytest
from rich.text import Text
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static

from klangk.cli import config as cfgmod
from klangk.cli import tui as tui_pkg
from klangk.cli.client import AuthError, Workspace, WorkspaceNotFoundError
from klangk.cli.tui import screens as scr
from klangk.cli.tui import state as tui_state_mod
from klangk.cli.tui import ws as ws_mod
from klangk.cli.tui.app import KlangkApp, run_tui
from klangk.cli.config import (
    CLIConfig,
    CLIState,
    ServerEntry,
    add_server_to_config,
    remove_server_from_config,
)
from klangk.cli.tui.screens import (
    AddServerScreen,
    ConfirmScreen,
    CreateWorkspaceScreen,
    DuplicateScreen,
    LoginScreen,
    MainScreen,
    ServerSwitchScreen,
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
    )
    base.update(extra)
    return _st(**base)


def _wsobj(name, **k):
    return Workspace(id="id-" + name, name=name, created_at="x", **k)


async def _async_empty(*a, **k):
    """Async stub for TuiState terminal methods (returns no terminals)."""
    return []


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


def test_remove_server_from_config(redirect_xdg):
    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    assert remove_server_from_config("a") is True
    assert set(CLIConfig.load().servers) == {"b"}
    # removing an absent alias is a no-op (False)
    assert remove_server_from_config("zzz") is False


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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        screen = app.screen
        screen._on_status_event({"type": "service_health"})
        await pilot.pause()
        assert app.live_extra == "live: service_health"
        assert "live: service_health" in str(
            screen.query_one("#status").render()
        )


async def test_status_loop_no_token_returns_early(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_authed_state(token=lambda: None))
    async with app.run_test():
        await app.screen._status_loop()  # no token -> early return


async def test_status_loop_handles_disconnect(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("ws died")

    monkeypatch.setattr(scr, "listen_for_status", boom)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await app.screen._status_loop()
        await pilot.pause()
        assert "status: disconnected" in app.live_extra


async def test_login_password_flow_success(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

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
    async with app.run_test() as pilot:
        login = app.screen
        login._attempt_password()  # empty fields
        await pilot.pause()
        assert "required" in str(login.query_one("#message").render())

        st.login_password = lambda a, b: (_ for _ in ()).throw(
            LoginError("bad creds")
        )
        login.query_one("#identifier", Input).value = "me@x"
        login.query_one("#password", Input).value = "pw"
        login._attempt_password()
        await pilot.pause()
        assert "bad creds" in str(login.query_one("#message").render())
        assert isinstance(app.screen, LoginScreen)


async def test_login_input_submitted_triggers_password(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

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
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_login_oidc_flow(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

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
        await pilot.pause()
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
        await pilot.pause()
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


async def test_logout_returns_to_login(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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


async def test_server_switch_and_add(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

    # switch screen with servers -> selecting one switches + returns to main
    st = _authed_state(
        known_servers=lambda: [
            tui_state_mod.ServerInfo("a", "https://a.example"),
            tui_state_mod.ServerInfo("b", "https://b.example"),
        ],
        current_url=lambda: "https://a.example",
    )
    switched = {}
    st.switch_server = lambda url: switched.setdefault("url", url)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)
        app.screen.on_option_list_option_selected(
            FakeOptionSelected("https://b.example")
        )
        await pilot.pause()
        assert switched["url"] == "https://b.example"
        assert isinstance(app.screen, MainScreen)

    # switch screen with no servers -> hint message
    app2 = KlangkApp(_authed_state(known_servers=lambda: []))
    async with app2.run_test() as pilot:
        app2.push_screen(ServerSwitchScreen())
        await pilot.pause()
        assert "No servers" in str(
            app2.screen.query_one("#switch_msg").render()
        )

    # add server screen -> add succeeds, returns to main
    st3 = _authed_state()
    added = {}
    st3.add_server = lambda alias, url, user=None: added.setdefault(
        "a", (alias, url)
    )
    app3 = KlangkApp(st3)
    async with app3.run_test() as pilot:
        app3.push_screen(AddServerScreen())
        await pilot.pause()
        add_screen = app3.screen
        add_screen.query_one("#alias", Input).value = "prod"
        add_screen.query_one("#url", Input).value = "https://p.example"
        add_screen._add()
        await pilot.pause()
        assert added["a"] == ("prod", "https://p.example")
        assert isinstance(app3.screen, MainScreen)

    # add server with empty fields -> error message
    app4 = KlangkApp(_authed_state())
    async with app4.run_test() as pilot:
        app4.push_screen(AddServerScreen())
        await pilot.pause()
        app4.screen._add()
        await pilot.pause()
        assert "required" in str(app4.screen.query_one("#add_msg").render())


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
    st.delete_workspace("a")
    assert st.duplicate_workspace("a", "c") == {"id": "3", "name": "c"}
    fake.restart_workspace.assert_called_once_with("a")
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
    )
    fake.list_images.assert_called_once_with()


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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert m.query_one("#owned_list").option_count == 2
        assert m.query_one("#shared_list").option_count == 1
        status = str(m.query_one("#status").render())
        assert "https://x.example" in status
        assert "me@x.example" in status


async def test_main_screen_list_error_shows_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

    def boom():
        raise RuntimeError("net")

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test():
        m = app.screen
        assert m.query_one("#owned_list").option_count == 1
        assert m.query_one("#shared_list").option_count == 1


async def test_main_screen_select_opens_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.screen.on_option_list_option_selected(FakeOptionSelected("alpha"))
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_main_screen_select_empty_no_push(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_ws())  # empty lists -> placeholder rows
    async with app.run_test() as pilot:
        app.screen.on_option_list_option_selected(FakeOptionSelected(""))
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_status_event_refreshes_on_change(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert "alpha" in str(d.query_one("#detail_title").render())
        body = str(d.query_one("#detail_body").render())
        for s in [
            "running: yes",
            "health: healthy",
            "health note: ok",
            "image: img",
            "service command: cmd",
            "health check: hc",
            "auto-start: off",
            "mounts:",
            "/h:/c",
            "environment:",
            "K=v",
            "owner: o@x",
        ]:
            assert s in body, s


async def test_detail_load_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
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
        assert restarted.get("r") == "alpha"
        assert "Restart requested" in str(
            app.screen.query_one("#detail_msg").render()
        )
        # error
        st.restart_workspace = lambda n: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        app.screen.action_restart()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert "Restart failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_detail_delete_confirm_cancel_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert deleted.get("d") == "alpha"
        assert isinstance(app.screen, MainScreen)


async def test_detail_delete_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert "Delete failed" in str(
            app.screen.query_one("#detail_msg").render()
        )
        assert isinstance(app.screen, WorkspaceDetailScreen)


async def test_detail_duplicate_ok_cancel_input_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert "Duplicate failed" in str(
            app.screen.query_one("#detail_msg").render()
        )


async def test_refresh_workspaces_refreshes_main(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
    calls = {"n": 0}

    def owned():
        calls["n"] += 1
        return [a]

    st = _ws(owned=[a])
    st.list_owned_workspaces = owned
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        before = calls["n"]
        app.refresh_workspaces()
        await pilot.pause()
        assert calls["n"] > before


async def test_main_screen_markup_name_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("x[red]y")
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test():
        ol = app.screen.query_one("#owned_list")
        assert ol.option_count == 1
        prompt = app.screen._fmt(a)
        assert isinstance(prompt, Text)
        assert "x[red]y" in str(prompt)  # literal, not markup-parsed


async def test_detail_markup_name_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("x[red]y", image="[img]", health_message="[bad]")
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("x[red]y"))
        await pilot.pause()
        title = str(app.screen.query_one("#detail_title").render())
        body = str(app.screen.query_one("#detail_body").render())
        assert "x[red]y" in title  # literal, not markup-parsed
        assert "[img]" in body
        assert "[bad]" in body


async def test_detail_apply_status_event(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        # container_status flips running
        d.apply_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
            }
        )
        assert a.running is True
        assert "running: yes" in str(d.query_one("#detail_body").render())
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
        assert "health: unhealthy" in body
        assert "health note: curl fail" in body
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


async def test_detail_apply_status_event_reload(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert "running: yes" in str(d.query_one("#detail_body").render())


async def test_detail_apply_status_event_ws_none(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        assert "running: yes" in str(
            app.screen.query_one("#detail_body").render()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()  # deterministic render
        await pilot.pause()
        tl = app.screen.query_one("#term_list")
        assert tl.option_count == 2
        assert "main" in str(tl.get_option_at_index(0).prompt)
        assert "build" in str(tl.get_option_at_index(1).prompt)


async def test_detail_terminals_empty_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws()  # list_terminals -> _async_empty -> []
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        tl = app.screen.query_one("#term_list")
        assert tl.option_count == 1  # the (no terminals) placeholder


async def test_detail_terminal_load_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def boom(*a, **k):
        raise RuntimeError("ws down")

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=boom)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()  # swallows the error
        await pilot.pause()
        assert app.screen.query_one("#term_list").option_count == 1


async def test_detail_delete_terminal_guard_last(monkeypatch):
    async def noop(*a, **k):
        return None

    async def one(*a, **k):
        return [{"index": 0, "name": "only", "id": "@0"}]

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        d.query_one("#term_list").highlighted = 0
        d.action_delete_terminal()  # only terminal -> refused
        await pilot.pause()
        assert "Can't delete the last terminal" in str(
            d.query_one("#detail_msg").render()
        )


async def test_detail_delete_terminal_no_selection(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        d.query_one("#term_list").highlighted = None
        d.action_delete_terminal()
        await pilot.pause()
        assert d.query_one("#term_list").option_count == 2  # unchanged


async def test_detail_delete_terminal_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        d.query_one("#term_list").highlighted = 0  # the placeholder
        d.action_delete_terminal()  # opt.id == "" -> no-op
        await pilot.pause()
        assert d.query_one("#term_list").option_count == 1  # unchanged


async def test_detail_delete_terminal(monkeypatch):
    async def noop(*a, **k):
        return None

    closed = {}

    async def _close(name, index):
        closed["i"] = index
        return [{"index": 0, "name": "main", "id": "@0"}]

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        d.query_one("#term_list").highlighted = 1
        d.action_delete_terminal()
        for _ in range(3):
            await pilot.pause()
        assert closed.get("i") == 1
        assert "Deleted terminal 1" in str(d.query_one("#detail_msg").render())
        assert d.query_one("#term_list").option_count == 1


async def test_detail_delete_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _close(name, index):
        raise RuntimeError("boom")

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        await pilot.pause()
        assert "Delete failed" in str(d.query_one("#detail_msg").render())


async def test_main_screen_title(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test():
        assert app.title == "Klangk: Workspaces"


# --- reviewer findings (#1746/#1747 review) ---


async def test_confirm_screen_markup_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        app.push_screen(DuplicateScreen("wip[/]"))
        await pilot.pause()
        rendered = str(app.screen.query_one(Static).render())
        assert "wip[/]" in rendered


async def test_status_bar_markup_safe(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_ws())
    async with app.run_test() as pilot:
        app.live_extra = "live: foo[/]bar"
        app.screen._refresh_status()
        await pilot.pause()
        assert "foo[/]bar" in str(app.screen.query_one("#status").render())


async def test_main_screen_auth_expired_placeholder(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

    def boom():
        raise AuthError("expired")

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test():
        ol = app.screen.query_one("#owned_list")
        assert ol.option_count == 1
        assert (
            "session expired" in str(ol.get_option_at_index(0).prompt).lower()
        )


async def test_detail_auth_expired_message(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    a = _wsobj("alpha")
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        await pilot.pause()
        assert "Delete failed" in str(d.query_one("#detail_msg").render())
        assert d.query_one("#term_list").option_count == 2  # unchanged


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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state(allow_autostart=lambda: False))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cb = app.screen.query_one("#auto_start", Checkbox)
        assert cb.display is False
        assert cb.disabled is True


async def test_create_screen_mount_editor(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    called = []
    app = KlangkApp(
        _create_state(create=lambda *a, **k: called.append(k) or _wsobj("z"))
    )
    async with app.run_test() as pilot:
        app.screen.action_create()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["name"] = name
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _create_state(create=create, allow_autostart=lambda: False)
    )
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "myws"
        cs._create()  # default image kept -> omitted
        await pilot.pause()
        assert captured["name"] == "myws"
        assert captured["k"]["image"] is None
        assert captured["k"]["auto_start"] is False
        assert captured["k"]["mounts"] is None
        assert captured["k"]["env"] is None


async def test_create_screen_submit_custom_fields(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["name"] = name
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(_create_state(create=create))
    async with app.run_test() as pilot:
        app.screen.action_create()
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
        await pilot.pause()
        assert captured["k"]["image"] == "py:3"
        assert captured["k"]["service_command"] == "sleep 1"
        assert captured["k"]["health_check"] == "curl localhost"
        assert captured["k"]["mounts"] == ["/h:/c"]
        assert captured["k"]["env"] == {"A": "1"}
        assert captured["k"]["auto_start"] is True


async def test_create_screen_http_error_shows_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "dup"
        cs._create()
        assert "name taken" in str(cs.query_one("#create_msg").render())
        assert isinstance(app.screen, CreateWorkspaceScreen)  # still on form


async def test_create_screen_auth_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

    def create(name, **k):
        raise AuthError("expired")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "ws"
        cs._create()
        assert "Session expired" in str(cs.query_one("#create_msg").render())


async def test_create_screen_images_unavailable(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    def boom():
        raise RuntimeError("images endpoint down")

    app = KlangkApp(
        _create_state(create=create, list_images=boom, allow_autostart=boom)
    )
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        assert cs._allowed == []
        assert cs.query_one("#auto_start", Checkbox).display is False
        cs.query_one("#name").value = "ws"
        cs._create()
        await pilot.pause()
        assert captured["k"]["image"] is None  # omitted


async def test_create_screen_cancel_button(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        app.screen.on_button_pressed(FakeBtnPress("cancel"))
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # back to the list


async def test_create_screen_input_submit_routing(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
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


async def test_create_flow_offer_opens_detail(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("new")))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "new"
        cs._create()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)  # "Open it now?"
        app.screen.dismiss(True)
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)
        assert app.screen._name == "new"


async def test_create_flow_offer_declined(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("new")))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "new"
        cs._create()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_create_editor_guards(monkeypatch):
    """Empty input + nothing-highlighted are no-ops (guard returns)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test() as pilot:
        app.screen.action_create()
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

    monkeypatch.setattr(scr, "listen_for_status", noop)

    def create(name, **k):
        raise RuntimeError("boom")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test() as pilot:
        app.screen.action_create()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name").value = "ws"
        cs._create()
        assert "Failed to create: boom" in str(
            cs.query_one("#create_msg").render()
        )


async def test_create_button_routing(monkeypatch):
    """on_button_pressed routes add/rm/create to the editor + create paths."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
    app = KlangkApp(_create_state(create=lambda *a, **k: _wsobj("ws")))
    async with app.run_test() as pilot:
        app.screen.action_create()
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
        # create via button -> success -> offer
        cs.query_one("#name").value = "ws"
        cs.on_button_pressed(FakeBtnPress("create"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)


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

    monkeypatch.setattr(scr, "listen_for_status", noop)

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
    async with app.run_test() as pilot:
        login = app.screen
        # no-server branch: prompt + disabled credentials
        assert "No server selected" in str(
            login.query_one("#server_line").render()
        )
        assert login.query_one("#login", Button).disabled

        # empty choice -> error message
        login._choose_server("   ")
        await pilot.pause()
        assert "Enter a server URL" in str(
            login.query_one("#message").render()
        )

        # known alias -> switch (routed through the option-selected handler)
        login.on_option_list_option_selected(FakeOptionSelected("prod"))
        await pilot.pause()
        assert calls.get("switch") == "https://prod.example"

        # new URL -> added as an alias derived from its host
        login._choose_server("https://newhost.example/x")
        await pilot.pause()
        assert calls.get("add") == (
            "newhost.example",
            "https://newhost.example/x",
        )

        # UDS path -> also persisted as an alias (basename)
        srv_input = login.query_one("#server_input", Input)
        srv_input.value = "/var/run/other.sock"
        login.on_input_submitted(Input.Submitted(srv_input, srv_input.value))
        await pilot.pause()
        assert calls.get("add") == ("other.sock", "/var/run/other.sock")

        # "Use server" button also dispatches
        srv_input.value = "prod"
        login.on_button_pressed(FakeBtnPress("use_server"))
        await pilot.pause()
        assert calls.get("switch") == "https://prod.example"

        # after a successful pick the server line + enabled creds reflect it
        assert "Server:" in str(login.query_one("#server_line").render())
        assert not login.query_one("#login", Button).disabled


async def test_populate_servers_dedups_default_udsk(monkeypatch):
    """The auto-detected default UDS isn't double-listed when an alias
    already points at it (after the user persisted it)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        ol = app.screen.query_one("#server_options", OptionList)
        # only the persisted alias row; no separate "Local klangkd (UDS)" row
        assert len(ol._options) == 1


async def test_login_choose_invalid_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
    async with app.run_test() as pilot:
        login = app.screen
        login._choose_server("sdfsdf")
        await pilot.pause()
        # not persisted, and a sensible message is shown
        assert added.get("a") is None
        assert "URL" in str(login.query_one("#message").render())


async def test_add_server_rejects_invalid_url(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        await pilot.pause()
        assert added.get("a") is None
        assert "http" in str(s.query_one("#add_msg").render()).lower()


async def test_confirm_screen(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        ol = login.query_one("#server_options", OptionList)
        # nothing highlighted -> prompt to select (no dialog)
        ol.highlighted = None
        login.action_delete_server()
        await pilot.pause()
        assert "Select a server" in str(login.query_one("#message").render())
        # highlight + action -> confirm dialog (not yet deleted)
        ol.highlighted = 0
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
        ol.highlighted = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert deleted.get("u") == "https://prod.example"
        assert "Server deleted" in str(login.query_one("#message").render())
        # confirm but delete returns False -> "Not a saved alias"
        st.delete_server = lambda url: False
        ol.highlighted = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert "Not a saved alias" in str(login.query_one("#message").render())


async def test_login_delete_clears_to_no_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        ol = login.query_one("#server_options", OptionList)
        ol.highlighted = 0
        login.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert "No server selected" in str(
            login.query_one("#server_line").render()
        )


async def test_switch_screen_delete_server(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        ol = switch.query_one("#server_options", OptionList)
        # nothing highlighted -> no dialog, no delete
        ol.highlighted = None
        switch.action_delete_server()
        await pilot.pause()
        assert app.screen is switch
        assert "u" not in deleted
        # highlight + action -> dialog; cancel -> not deleted
        ol.highlighted = 0
        switch.action_delete_server()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(False)
        await pilot.pause()
        assert "u" not in deleted
        # confirm -> deleted
        ol.highlighted = 0
        switch.action_delete_server()
        await pilot.pause()
        app.screen.dismiss(True)
        await pilot.pause()
        assert deleted.get("u") == "https://prod.example"


# ---------------------------------------------------------------------------
# run_tui + bare-klangk launch wiring (continued)
# ---------------------------------------------------------------------------


async def test_login_button_dispatch_and_oidc_incomplete(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)

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
        await pilot.pause()
        assert "did not complete" in str(
            app2.screen.query_one("#message").render()
        )


async def test_main_screen_actions(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr, "listen_for_status", noop)
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

    monkeypatch.setattr(scr, "listen_for_status", noop)
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
        await pilot.pause()
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
        await pilot.pause()
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
    CliRunner().invoke(app, ["logout"])
    assert launched["v"] is False
