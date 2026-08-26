"""Tests for the klangk TUI foundation (issue #1746).

Covers the textual app shell, login/server-switch flows, the live state
bridge, the WebSocket status listener, the bare-``klangk`` launch wiring,
and the ``add_server_to_config`` helper — under the 100% coverage gate.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta, timezone
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
    OptionList,
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
    AddServerScreen,
    CheatsheetScreen,
    ConfirmScreen,
    CreateWorkspaceScreen,
    DuplicateScreen,
    EditServerScreen,
    EditWorkspaceScreen,
    InputScreen,
    LoginScreen,
    MainScreen,
    ServerDownScreen,
    ServerSwitchScreen,
    SessionExpiredScreen,
    StatusScreen,
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
    """Stand-in for ListView.Selected carrying an item name + source list."""

    def __init__(self, name, control_id="term_list"):
        self.item = type("Item", (), {"name": name})()
        self.control = type("Ctrl", (), {"id": control_id})()


def _lv_texts(list_view):
    """Rendered text of each Label in a ListView (content assertions)."""
    return [str(lab.render()) for lab in list_view.query(Label)]


class FakeBtnPress:
    """Stand-in for Button.Pressed carrying a button id."""

    def __init__(self, button_id):
        self.button = type("B", (), {"id": button_id})()


def _st(**methods):
    """A TuiState with the given methods overridden (for Pilot tests).

    Defaults ``list_owned_workspaces`` / ``list_shared_workspaces`` to empty
    so MainScreen's on-mount ``refresh_lists`` (which calls them via
    ``_safe_list``) doesn't make a real, timing-out HTTP call to the fake
    test URL in tests that don't otherwise stub them (#1989). Callers that
    need real list data use ``_ws(owned=...)`` / ``_authed_state(...)``,
    which override these.
    """
    st = TuiState()
    defaults = {
        "list_owned_workspaces": lambda: [],
        "list_shared_workspaces": lambda: [],
    }
    for k, v in {**defaults, **methods}.items():
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
        list_shared_terminals=_async_empty,
        current_user_id=lambda: None,
        close_terminal=_async_empty,
        restart_workspace=lambda n: None,
        # Stubbed so MainScreen._do_create doesn't make a real (timing-out)
        # HTTP call for the allowed-domains list (#1989).
        default_allowed_domains=lambda: [],
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
        list_shared_terminals=_async_empty,
        current_user_id=lambda: None,
        close_terminal=_async_empty,
        restart_workspace=lambda n: None,
        # Stubbed so MainScreen._do_create doesn't make a real (timing-out)
        # HTTP call for the allowed-domains list (#1989).
        default_allowed_domains=lambda: [],
    )
    base.update(extra)
    return _st(**base)


def _wsobj(name, **k):
    return Workspace(id="id-" + name, name=name, created_at="x", **k)


def _attach_notify_spy(app) -> list:
    """Spy on ``app.notify`` and return the list of toasted messages (#2019).

    Operational success / in-progress feedback on the detail screen is now
    shown via auto-dismissing toasts (``app.notify``) rather than a lingering
    in-page line; tests assert on the captured messages. Mirrors the inline
    pattern the export test used for its completion toast (#1758).
    """
    notified: list = []
    orig = app.notify

    def spy(message="", *a, **k):
        notified.append(message)
        return orig(message, *a, **k)

    app.notify = spy
    return notified


def _detail_value(body: str, label: str) -> str | None:
    """Return the value-column text for ``label``'s row in the workspace
    detail table, or None if no such row.

    The detail body renders as a two-column table (#1910): a label, then a
    column of padding (>=2 spaces), then the value. Labels are bold and
    right-aligned, and rows carry zebra-stripe ANSI, so both are normalized
    away before parsing (#2193). A label is matched only when it's followed
    by that padding gap, so ``health`` doesn't match the ``health note`` /
    ``health check`` rows. Multi-line value cells (mounts / environment /
    allowed domains) are rejoined with newlines; a continuation is indented
    past its label's right edge, which also stops right-aligned label rows
    from being read as continuations."""
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in body.splitlines()]
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(label):
            continue
        rest = stripped[len(label) :]
        # "health" must not match "health note" (rest starts with a word,
        # not the column gap). The gap is always >=2 spaces.
        if rest.strip() and not rest[:2].isspace():
            continue
        label_leading = len(line) - len(line.lstrip(" "))
        parts = [rest.strip()] if rest.strip() else []
        for cont in lines[i + 1 :]:
            cont_leading = len(cont) - len(cont.lstrip(" "))
            # A continuation is indented past the label's right edge
            # (label_leading + len(label)); the next row's right-aligned
            # label sits at or before that edge, so it ends the scan.
            if cont_leading <= label_leading + len(label):
                break
            if cont.strip():
                parts.append(cont.strip())
        return "\n".join(parts)
    return None
    return None


async def _async_empty(*a, **k):
    """Async stub for TuiState terminal methods (returns no terminals)."""
    return []


# Capture the real worker loops before any test stubs them, so direct-call
# coverage tests can still exercise them despite the autouse class-method
# stub below. The two method refs are unbound (saved off the class at import
# time); direct-call tests pass the screen explicitly.
_real_run_token_refresh_loop = scr_main.run_token_refresh_loop
_real_status_loop = MainScreen._status_loop
_real_token_refresh_loop = MainScreen._token_refresh_loop
_real_load_last_login = MainScreen._load_last_login


@pytest.fixture(autouse=True)
def _stub_tui_bg_workers(monkeypatch):
    """Stub MainScreen's on-mount bg workers (status-WS + token-refresh
    loops + the one-shot last-login fetch) to no-ops for every TUI test.

    on_mount spawns workers — ``self._status_loop`` and
    ``self._token_refresh_loop``. Left real, the status loop reconnects up to
    4× (max_retries=3) with a 2s sleep whenever its ``listen_for_status``
    returns cleanly, costing ~8s per mounted MainScreen — which dominated TUI
    test runtime (#1989). The old fixture stubbed the *leaf*
    ``listen_for_status`` function, but that still ran the loop body and its
    real reconnect sleeps; stubbing the loop *methods* instead makes the
    workers complete instantly. ``_load_last_login`` (#2583) is stubbed for
    the same reason: on fakes it would call the real ``TuiState`` method,
    consuming side-effecting ``token()`` stubs (and timing out on a real
    HTTP hit to the fake server URL). Tests exercising the real loop logic
    call the saved ``_real_status_loop`` / ``_real_token_refresh_loop`` /
    ``_real_run_token_refresh_loop`` / ``_real_load_last_login`` directly
    (they stub the leaf functions themselves as needed).
    """

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(MainScreen, "_status_loop", _noop)
    monkeypatch.setattr(MainScreen, "_token_refresh_loop", _noop)
    monkeypatch.setattr(MainScreen, "_load_last_login", _noop)


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


def test_current_user_id_cached(monkeypatch, redirect_xdg):
    from unittest.mock import MagicMock

    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.save()
    t = TuiState()
    calls = []

    def fake_req(url, method, path, **k):
        calls.append(url)
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"id": "user-123", "email": "me@x"}
        return r

    monkeypatch.setattr(tui_state_mod, "http_request", fake_req)
    assert t.current_user_id() == "user-123"
    # Cached — a second call must not refetch.
    assert t.current_user_id() == "user-123"
    assert len(calls) == 1


def test_current_user_id_no_server(redirect_xdg):
    # Nothing configured -> current_url() is None -> None.
    assert TuiState().current_user_id() is None


def test_current_user_id_no_token(redirect_xdg):
    # Server configured + active, but no credentials -> token() is None.
    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.active_server = "https://srv.example"
    st.save()
    assert TuiState().current_user_id() is None


def test_current_user_id_failures(monkeypatch, redirect_xdg):
    import httpx
    from unittest.mock import MagicMock

    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.save()

    def mk(*, status=200, body=None, exc=None):
        def fake_req(url, method, path, **k):
            if exc is not None:
                raise exc
            r = MagicMock()
            r.status_code = status
            r.json.return_value = body if body is not None else {}
            return r

        return fake_req

    # HTTP error -> None.
    t = TuiState()
    monkeypatch.setattr(
        tui_state_mod, "http_request", mk(exc=httpx.HTTPError("x"))
    )
    assert t.current_user_id() is None
    # Non-200 -> None.
    monkeypatch.setattr(tui_state_mod, "http_request", mk(status=401))
    assert TuiState().current_user_id() is None
    # 200 but id not a string -> None.
    monkeypatch.setattr(
        tui_state_mod, "http_request", mk(status=200, body={"id": 123})
    )
    assert TuiState().current_user_id() is None
    # 200 but body not a dict -> None.
    monkeypatch.setattr(
        tui_state_mod, "http_request", mk(status=200, body=["nope"])
    )
    assert TuiState().current_user_id() is None


def test_current_user_id_refetch_on_server_switch(monkeypatch, redirect_xdg):
    from unittest.mock import MagicMock

    add_server_to_config("a", "https://a.example")
    add_server_to_config("b", "https://b.example")
    st = CLIState()
    st.set_credentials("https://a.example", "me@x", "tok1")
    st.set_credentials("https://b.example", "me@x", "tok2")
    st.save()
    t = TuiState()
    seen = []

    def fake_req(url, method, path, **k):
        seen.append(url)
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"id": "id-" + url}
        return r

    monkeypatch.setattr(tui_state_mod, "http_request", fake_req)
    st.active_server = "https://a.example"
    st.save()
    assert t.current_user_id() == "id-https://a.example"
    st.active_server = "https://b.example"
    st.save()
    # Different active server -> cache miss -> refetch.
    assert t.current_user_id() == "id-https://b.example"
    assert len(seen) == 2


def test_last_login_at_from_profile(monkeypatch, redirect_xdg):
    """last_login_at reads the cached /auth/me profile (#2583)."""
    from unittest.mock import MagicMock

    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.save()
    t = TuiState()
    calls = []

    def fake_req(url, method, path, **k):
        calls.append(url)
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {
            "id": "user-123",
            "last_login_at": "2026-08-20T10:00:00+00:00",
        }
        return r

    monkeypatch.setattr(tui_state_mod, "http_request", fake_req)
    assert t.last_login_at() == "2026-08-20T10:00:00+00:00"
    # Shares the profile cache — no second fetch.
    assert t.last_login_at() == "2026-08-20T10:00:00+00:00"
    assert len(calls) == 1


def test_last_login_at_missing_or_non_string(monkeypatch, redirect_xdg):
    """A profile without last_login_at (or with a non-string value)
    degrades to None (#2583)."""
    from unittest.mock import MagicMock

    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.save()

    def mk(body):
        def fake_req(url, method, path, **k):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = body
            return r

        return fake_req

    t = TuiState()
    monkeypatch.setattr(tui_state_mod, "http_request", mk({"id": "user-123"}))
    assert t.last_login_at() is None
    monkeypatch.setattr(
        tui_state_mod,
        "http_request",
        mk({"id": "user-123", "last_login_at": 42}),
    )
    assert TuiState().last_login_at() is None


def test_current_user_id_cleared_on_logout(monkeypatch, redirect_xdg):
    from unittest.mock import MagicMock

    add_server_to_config("srv", "https://srv.example")
    st = CLIState()
    st.set_credentials("https://srv.example", "me@x", "tok123")
    st.active_server = "https://srv.example"
    st.save()
    t = TuiState()
    seen = []
    ids = iter(["user-A", "user-B"])

    def fake_req(url, method, path, **k):
        seen.append(url)
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"id": next(ids)}
        return r

    monkeypatch.setattr(tui_state_mod, "http_request", fake_req)
    assert t.current_user_id() == "user-A"
    # User A logs out, then user B logs in on the SAME server (same URL).
    # logout must drop the cached id so B isn't served A's id (#2164 review).
    t.logout()
    st.set_credentials("https://srv.example", "b@x", "tokB")
    st.save()
    assert t.current_user_id() == "user-B"
    assert len(seen) == 2


def test_list_shared_terminals_delegates(monkeypatch):
    """TuiState.list_shared_terminals delegates to the client."""

    t = TuiState("https://x.example")
    captured = []

    class FakeClient:
        async def list_shared_terminals(self, name):
            captured.append(name)
            return [{"handle": "a", "window_name": "w"}]

    monkeypatch.setattr(t, "client", lambda: FakeClient())
    import asyncio

    assert asyncio.run(t.list_shared_terminals("alpha")) == [
        {"handle": "a", "window_name": "w"}
    ]
    assert captured == ["alpha"]


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


def test_default_per_handle_home(monkeypatch, redirect_xdg):
    # #2721: the create form pre-reflects the deploy default. Unknown
    # (fetch failure / no server) is None — the caller then hides the
    # checkbox and omits the field so the server applies its own default
    # (#2737 review). A fetched config that merely lacks the key is an
    # OLD server, whose behavior is per-handle (True).
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"default_per_handle_home": False},
    )
    assert TuiState("https://x.example").default_per_handle_home() is False
    monkeypatch.setattr(
        tui_state_mod,
        "fetch_config",
        lambda url: {"default_per_handle_home": True},
    )
    assert TuiState("https://x.example").default_per_handle_home() is True
    # missing field (old server) -> per-handle
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: {})
    assert TuiState("https://x.example").default_per_handle_home() is True
    # non-dict / no server -> unknown (None)
    monkeypatch.setattr(tui_state_mod, "fetch_config", lambda url: None)
    assert TuiState("https://x.example").default_per_handle_home() is None
    assert TuiState().default_per_handle_home() is None


def test_default_allowed_domains(monkeypatch, redirect_xdg):
    # netfilter_default_domains is auth-gated on /api/v1/config (absent from
    # the pre-auth payload), so default_allowed_domains() reads it via the
    # authed client, not fetch_config. Seed list verbatim; non-list / absent
    # -> [] (no regression) (#1931).
    from unittest.mock import MagicMock

    t = TuiState("https://x.example")
    fake = MagicMock()
    monkeypatch.setattr(t, "client", lambda: fake)
    fake.config.return_value = {
        "netfilter_default_domains": ["github.com:443", "pypi.org:443"]
    }
    assert t.default_allowed_domains() == ["github.com:443", "pypi.org:443"]
    # absent key -> None -> not a list -> []
    fake.config.return_value = {}
    assert t.default_allowed_domains() == []
    # wrong shape -> []
    fake.config.return_value = {"netfilter_default_domains": "github.com"}
    assert t.default_allowed_domains() == []


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


async def test_rename_terminal_delegates(monkeypatch, redirect_xdg):

    renamed = {}

    async def fake_rename(name, index, new_name):
        renamed.update(name=name, index=index, new=new_name)
        return [
            {"index": 0, "name": "main"},
            {"index": 1, "name": new_name},
        ]

    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.rename_terminal = fake_rename
    t = TuiState("https://x.example")
    monkeypatch.setattr(t, "client", lambda: fake_client)
    result = await t.rename_terminal("ws1", 1, "ci")
    assert renamed == {"name": "ws1", "index": 1, "new": "ci"}
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
    connected = []
    frames = [
        '{"type": "workspaces_changed"}',
        "not-json",
        "123",  # valid JSON but not a dict
        '{"type": "service_health"}',
    ]
    monkeypatch.setattr(
        ws_mod, "ws_connect", lambda *a, **k: FakeCM(FakeWS(frames))
    )
    await listen_for_status(
        "/sock",
        "tok",
        on_event=collected.append,
        on_connect=lambda: connected.append(1),
    )
    assert connected == [1]  # fires once after connect, before any frames
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


async def test_app_uses_klangk_theme():
    """#2003: the TUI defaults to the custom klangk theme (matching the web
    UI's GitHub-dark palette) rather than Textual's built-in ansi-light. The
    built-in themes stay registered so they remain selectable."""
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
    assert app.theme == "klangk"
    async with app.run_test() as pilot:
        await pilot.pause()
        # Still klangk after mount (no on_mount override flips it).
        assert app.theme == "klangk"
        assert KLANGK_THEME.name == "klangk"
        # The built-in themes stay registered — switching to ansi-light must
        # not raise (Textual raises ThemeError for an unknown theme name).
        app.theme = "ansi-light"
        await pilot.pause()
        assert app.theme == "ansi-light"


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
        # A genuinely unhandled type takes the debug pulse (#2690: types
        # with a dedicated UI surface are silent — see the coexistence
        # test below).
        screen._on_status_event({"type": "mystery_broadcast"})
        await pilot.pause()
        assert app.live_extra == "live: mystery_broadcast"
        assert "live: mystery_broadcast" in str(
            screen.query_one("#status").render()
        )


async def test_status_silent_events_leave_segment(monkeypatch):
    """#2690: container_status / workspaces_changed / terminals_changed /
    service_health never write the live segment — after a stop/recycle
    drain the bar shows no stale `live: …`, and a pending #2661 countdown
    survives the routine broadcasts that follow."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha", running=True)]))
    async with app.run_test() as pilot:
        screen = app.screen
        # Seed a pending schedule countdown (#2661).
        fire_at = (datetime.now() + timedelta(hours=1)).isoformat()
        screen._on_status_event(
            {
                "type": "server_schedule",
                "schedules": [{"action": "stop", "fire_at": fire_at}],
            }
        )
        await pilot.pause()
        countdown = app.live_extra
        assert countdown.startswith("server: stop at")
        # The drain-noise types must not clobber it.
        for etype in ("container_status", "workspaces_changed"):
            screen._on_status_event(
                {"type": etype, "workspace_id": "id-alpha", "running": False}
            )
            await pilot.pause()
            assert app.live_extra == countdown
        for etype in ("terminals_changed", "service_health"):
            screen._on_status_event({"type": etype})
            await pilot.pause()
            assert app.live_extra == countdown


async def test_status_no_stale_segment_after_cycle(monkeypatch):
    """#2690: after a schedule fires and the drain's container_status
    broadcasts arrive, the bar keeps the fired notice — no raw
    `live: container_status` residue."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha", running=True)]))
    async with app.run_test() as pilot:
        screen = app.screen
        screen._on_status_event(
            {"type": "server_schedule_fired", "action": "stop"}
        )
        await pilot.pause()
        assert app.live_extra == "server: scheduled stop running"
        # Server comes back; workspaces auto-start — a burst of silent
        # broadcasts. None may overwrite the notice with a raw type name.
        for _ in range(3):
            screen._on_status_event(
                {"type": "container_status", "running": True}
            )
            await pilot.pause()
        assert app.live_extra == "server: scheduled stop running"
        rendered = str(screen.query_one("#status").render())
        assert "live: container_status" not in rendered


async def test_main_screen_host_events_update_live_extra(monkeypatch):
    """#2527: host lifecycle notices render as a status line (and a
    toast), without touching reconnect state."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        screen = app.screen
        screen._on_status_event({"type": "host_shutdown"})
        await pilot.pause()
        assert app.live_extra == "server: shutting down"
        screen._on_status_event(
            {"type": "server_recycle", "phase": "draining"}
        )
        await pilot.pause()
        assert app.live_extra == "server: preparing to recycle"
        screen._on_status_event(
            {"type": "server_recycle", "phase": "recycling"}
        )
        await pilot.pause()
        assert app.live_extra == "server: recycling"
        screen._on_status_event({"type": "host_started"})
        await pilot.pause()
        assert app.live_extra == "server: back up"
        # Reconnect state untouched by the notices.
        assert screen._reconnect_attempt == 0


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
        await _real_status_loop(app.screen)  # no token -> early return


async def test_status_loop_retries_indefinitely_on_error(monkeypatch):
    """Transient errors trigger unlimited retries; the loop only exits when
    the token disappears (session_expired)."""
    attempts = {"n": 0}

    async def boom(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("ws died")

    monkeypatch.setattr(scr_main, "listen_for_status", boom)
    import asyncio as _asyncio

    _real_sleep = _asyncio.sleep

    async def _no_wait(_t):
        await _real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", _no_wait)
    # Token present for more than 3 iterations (old limit), then removed.
    tokens = iter(["tok"] * 7 + [None])
    app = KlangkApp(_authed_state(token=lambda: next(tokens)))
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        await _real_status_loop(app.screen)
        await pilot.pause()
    # Must have retried more than the old 3-retry cap.
    assert attempts["n"] > 3
    assert expired


async def test_status_loop_clean_close_reconnects(monkeypatch):
    """A clean WS close (listen_for_status returns normally) runs the
    reconnect branch; the loop then exits when the token drops on the next
    check. Covers the clean-close body (previously exercised only by the
    on-mount bg worker, which the autouse fixture now stubs, #1989)."""

    async def clean_close(*a, **k):
        return None  # server restart / idle timeout — clean close

    monkeypatch.setattr(scr_main, "listen_for_status", clean_close)
    # Token present for the pre-loop read and iteration 1 (so the loop enters
    # and gets a clean close), then None on iteration 2 -> session_expired.
    tokens = iter(["tok", "tok", None])
    app = KlangkApp(_authed_state(token=lambda: next(tokens)))
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        await _real_status_loop(app.screen)
        await pilot.pause()
    # The clean-close reconnect branch ran before the token dropped.
    assert "status: reconnecting" in (app.live_extra or "")
    assert expired  # iteration 2 saw no token -> session_expired


async def test_status_loop_resets_backoff_after_success(monkeypatch):
    """A successful WS connection (on_connect) resets the reconnect counter so
    subsequent failures get a fresh backoff sequence (#2033, #2052)."""
    import asyncio as _asyncio

    _real_sleep = _asyncio.sleep

    async def _nowait(_t):
        await _real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", _nowait)

    # Spy on the backoff arg (= _reconnect_attempt after each increment) so we
    # can see the counter climb, reset on connect, then re-climb.
    orig_backoff = scr_main._reconnect_backoff
    seen = []

    def spy_backoff(attempt):
        seen.append(attempt)
        return orig_backoff(attempt)

    monkeypatch.setattr(scr_main, "_reconnect_backoff", spy_backoff)

    calls = {"n": 0}

    async def mixed(*a, **k):
        calls["n"] += 1
        n = calls["n"]
        if n <= 2:
            raise RuntimeError("err")  # first 2 connection failures
        if n == 3:
            # Backend returns: connect (fires _on_ws_connected -> reset),
            # then clean close.
            on_connect = k.get("on_connect")
            if on_connect is not None:
                on_connect()
            return None
        if n <= 5:
            raise RuntimeError("err")  # 2 more failures after reset
        return None  # clean close before token drops

    monkeypatch.setattr(scr_main, "listen_for_status", mixed)
    tokens = iter(["tok"] * 8 + [None])
    app = KlangkApp(_authed_state(token=lambda: next(tokens)))
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        # Neutralise the list refresh _on_ws_connected triggers, so the
        # counter reset under test is the only thing touching
        # _reconnect_attempt.
        monkeypatch.setattr(app.screen, "refresh_lists", lambda: None)
        await _real_status_loop(app.screen)
        await pilot.pause()
    assert expired
    assert calls["n"] >= 5
    # Without the reset the counter climbs 1,2,3,4,5,6. The success at call 3
    # collapses it back to 0, so the third backoff is a fresh attempt 1.
    assert seen[:2] == [1, 2]
    assert seen[2] == 1


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
        rejected_domains=None,
        settings=None,
        egress_mode=None,
        per_handle_home=None,
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
    assert asyncio.run(st.close_terminal("a", "@0")) == []
    fake.list_terminals.assert_called_once_with("a")
    fake.close_terminal.assert_called_once_with("a", "@0")


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


async def test_confirm_screen_arrow_nav(monkeypatch):
    """ConfirmScreen: Left/Right move between Cancel/confirm; arrows are
    sufficient (no Tab needed) (#2016)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    cap = {}
    async with app.run_test() as pilot:
        app.push_screen(
            ConfirmScreen("Delete 'alpha'?"),
            lambda r: cap.__setitem__("r", r),
        )
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.query_one("#no").focus()
        await pilot.pause()
        assert app.screen.focused.id == "no"

        # Right moves Cancel -> confirm.
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.focused.id == "yes"
        # Right at the edge stays put.
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.focused.id == "yes"

        # Left moves confirm -> Cancel.
        await pilot.press("left")
        await pilot.pause()
        assert app.screen.focused.id == "no"
        # Left at the edge stays put.
        await pilot.press("left")
        await pilot.pause()
        assert app.screen.focused.id == "no"

        # No input above the row — Up/Down are no-ops.
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.focused.id == "no"
        # Escape cancels (dismisses False) (#2016).
        await pilot.press("escape")
        await pilot.pause()
        assert cap["r"] is False


async def test_input_screen_arrow_nav(monkeypatch):
    """InputScreen: Down leaves the input for the button row; Left/Right
    move between Cancel/OK; Up returns to the input (#2016)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    cap = {}
    async with app.run_test() as pilot:
        app.push_screen(
            InputScreen("Path:"),
            lambda r: cap.__setitem__("r", r),
        )
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.focused.id == "inp_value"

        # Down from the input enters the first button (Cancel).
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.focused.id == "cancel"

        # Right moves Cancel -> OK; at the edge stays put.
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.focused.id == "ok"
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.focused.id == "ok"

        # Up from a button returns to the input.
        await pilot.press("left")
        await pilot.pause()
        assert app.screen.focused.id == "cancel"
        await pilot.press("up")
        await pilot.pause()
        assert app.screen.focused.id == "inp_value"
        # Escape cancels (dismisses None) (#2016).
        await pilot.press("escape")
        await pilot.pause()
        assert cap["r"] is None


async def test_duplicate_screen_arrow_nav(monkeypatch):
    """DuplicateScreen: Down from the name input enters the button row;
    Left/Right move between Cancel/Dup (#2016)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_ws(owned=[_wsobj("alpha")]))
    cap = {}
    async with app.run_test() as pilot:
        app.push_screen(
            DuplicateScreen("alpha"),
            lambda r: cap.__setitem__("r", r),
        )
        await pilot.pause()
        assert isinstance(app.screen, DuplicateScreen)
        app.screen.query_one("#dup_name").focus()
        await pilot.pause()
        assert app.screen.focused.id == "dup_name"
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.focused.id == "cancel"
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.focused.id == "ok"
        # Escape cancels (dismisses None) (#2016).
        await pilot.press("escape")
        await pilot.pause()
        assert cap["r"] is None


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
    notified = _attach_notify_spy(app)
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


async def test_main_screen_edit_find_auth_error_shows_overlay(monkeypatch):
    """AuthError in find_workspace during _do_edit (main screen) triggers
    session-expired overlay (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_main_screen_edit_auth_error_shows_overlay(monkeypatch):
    """AuthError in list_images during _do_edit (main screen) triggers
    session-expired overlay instead of opening the form with defaults (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.list_images = lambda: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_main_screen_create_auth_error_shows_overlay(monkeypatch):
    """AuthError in list_images during _do_create (main screen) triggers the
    session-expired overlay instead of opening the form with defaults — parity
    with the edit path (#2035, #2234)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _create_state(
        list_images=lambda: (_ for _ in ()).throw(AuthError("expired"))
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_main_screen_edit_autostart_auth_error_shows_overlay(
    monkeypatch,
):
    """AuthError fetching allow_autostart in _do_edit (main screen) triggers
    the session-expired overlay (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha")
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.list_images = lambda: {"default": "base", "allowed": ["base"]}
    st.allow_autostart = lambda: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        m = await _highlight_first(pilot, app)
        m.action_edit()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


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


async def test_status_running_not_regressed_by_stale_refresh(monkeypatch):
    """A container_status running update isn't clobbered by a list refresh
    that races behind it (#2032).

    The fetch always returns a stale ``running=False`` snapshot; after a
    ``container_status(running=True)`` broadcast, every subsequent refresh
    must keep the list's running dot (the running overlay is re-applied).
    """

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)

    def stale_owned():
        # Each fetch returns a NEW snapshot that's behind the broadcast.
        return [_wsobj("alpha", running=False)]

    st = _ws(owned=[_wsobj("alpha", running=False)])
    st.list_owned_workspaces = stale_owned
    st.list_shared_workspaces = lambda: []
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        # Backend reports the workspace is now running.
        m._on_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
            }
        )
        await pilot.pause()
        # A refresh lands stale data (running=False) — must not regress.
        m.refresh_lists()
        await app.workers.wait_for_complete()
        await pilot.pause()
        ws = m._ws_by_id.get("id-alpha")
        assert ws is not None
        assert ws.running is True
        # The overlay carries the freshest state across the refresh.
        assert m._running_overlay.get("id-alpha") is True


async def test_status_running_overlay_applied_on_first_refresh(monkeypatch):
    """A container_status that lands before the workspace is in the snapshot
    is recorded and applied when the refresh brings it in (#2032)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)

    calls = {"n": 0}

    def owned():
        calls["n"] += 1
        # First fetch (mount): no workspaces yet.
        if calls["n"] == 1:
            return []
        # Later fetches: the workspace exists but the snapshot is stale
        # (running=False, behind the broadcast).
        return [_wsobj("alpha", running=False)]

    st = _ws(owned=[])
    st.list_owned_workspaces = owned
    st.list_shared_workspaces = lambda: []
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        m = app.screen
        # Broadcast arrives before the workspace is in the snapshot.
        m._on_status_event(
            {
                "type": "container_status",
                "workspace_id": "id-alpha",
                "running": True,
            }
        )
        await pilot.pause()
        # The refresh that brings the workspace in re-applies the overlay.
        m.refresh_lists()
        await app.workers.wait_for_complete()
        await pilot.pause()
        ws = m._ws_by_id.get("id-alpha")
        assert ws is not None
        assert ws.running is True


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


def test_reconnect_backoff_is_bounded():
    """_reconnect_backoff stays within [0, _MAX_BACKOFF_SECONDS] for every
    attempt and respects the cap once the exponential ramp exceeds it (#2012)."""
    delays = [scr_main._reconnect_backoff(a) for a in range(1, 30)]
    assert all(0.0 <= d <= scr_main._MAX_BACKOFF_SECONDS for d in delays)
    # The exponential base (1 << attempt) quickly exceeds the cap; the cap
    # (not the raw exponential) must bound the result from there on.
    assert scr_main._reconnect_backoff(50) <= scr_main._MAX_BACKOFF_SECONDS


def test_is_unreachable_classifies_transport_errors():
    """Only transport-layer failures count as 'server down' — auth and HTTP
    status errors mean the server responded, so they are reachable (#2012)."""
    assert scr_main._is_unreachable(httpx.ConnectError("refused"))
    assert scr_main._is_unreachable(httpx.ConnectTimeout("slow"))
    assert scr_main._is_unreachable(ConnectionRefusedError())  # OSError
    req = httpx.Request("GET", "https://x.example/")
    resp = httpx.Response(500, request=req)
    assert not scr_main._is_unreachable(
        httpx.HTTPStatusError("boom", request=req, response=resp)
    )
    assert not scr_main._is_unreachable(RuntimeError("net"))
    assert not scr_main._is_unreachable(scr_main.AuthError("expired"))


async def test_main_screen_server_down_shows_indicator(monkeypatch):
    """A transport failure on the mount-time list fetch enters the 'server
    down' state — an explicit 'server unreachable' row instead of a misleading
    'no workspaces' — and surfaces the app-wide overlay (#2012, #2052).

    The WS loop is now the single reachability signal (#2052); the autouse
    fixture stubs it here, so this test drives only the mount-time REST fetch.
    The attempt-counter / reconnect path is exercised by the dedicated
    ``_status_loop`` tests below."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    def down():
        raise httpx.ConnectError("refused")

    app = KlangkApp(
        _ws(list_owned_workspaces=down, list_shared_workspaces=down)
    )
    async with app.run_test() as pilot:
        # Let the mount-time refresh_lists fail and enter unreachable.
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        assert main._server_unreachable is True
        owned_lv = main.query_one("#owned_list", ListView)
        shared_lv = main.query_one("#shared_list", ListView)
        assert "server unreachable" in _lv_texts(owned_lv)[0].lower()
        assert "server unreachable" in _lv_texts(shared_lv)[0].lower()
        assert "unreachable" in (app.live_extra or "").lower()
        # App-wide overlay covers whatever page is active (#2012).
        assert isinstance(app.screen, ServerDownScreen)
        assert "server unreachable" in app.screen._message.lower()


async def test_rest_blip_while_ws_up_keeps_list(monkeypatch):
    """A transient REST list-fetch failure while the status WS is connected
    does NOT flag the server unreachable — the WS proves the backend is
    reachable, so the last good list is kept and refreshed on the next
    broadcast / reconnect (#2052)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)

    alpha = _wsobj("alpha")
    up = {"yes": True}

    def owned():
        if up["yes"]:
            return [alpha]
        raise httpx.ConnectError("refused")

    app = KlangkApp(
        _ws(list_owned_workspaces=owned, list_shared_workspaces=owned)
    )
    async with app.run_test() as pilot:
        screen = app.screen
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # WS is connected; list populated.
        screen._ws_connected = True
        assert (
            "alpha" in _lv_texts(screen.query_one("#owned_list", ListView))[0]
        )
        assert screen._server_unreachable is False
        # A REST blip while the WS is up must NOT flag the server unreachable…
        up["yes"] = False
        screen.refresh_lists()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen._server_unreachable is False
        assert not isinstance(app.screen, ServerDownScreen)
        # …and the last good list is retained (not replaced with an
        # "unreachable" label or cleared to "no workspaces").
        assert (
            "alpha" in _lv_texts(screen.query_one("#owned_list", ListView))[0]
        )


async def test_main_screen_http_error_is_not_unreachable(monkeypatch):
    """An HTTP error status means the server *is* up — keep the historical
    empty-list rendering rather than flagging it as down (#2012)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    req = httpx.Request("GET", "https://x.example/api/workspaces")
    resp = httpx.Response(500, request=req)

    def boom():
        raise httpx.HTTPStatusError("boom", request=req, response=resp)

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert screen._server_unreachable is False
        owned_lv = screen.query_one("#owned_list", ListView)
        assert "no workspaces" in _lv_texts(owned_lv)[0].lower()


async def _fast_reconnect(monkeypatch):
    """Make the reconnect loop instant: zero backoff + no real sleep."""
    monkeypatch.setattr(scr_main, "_reconnect_backoff", lambda attempt: 0.0)

    async def _nowait(_t):
        return None

    monkeypatch.setattr(scr_main, "_reconnect_sleep", _nowait)


async def test_reconnect_recovers_when_server_returns(monkeypatch):
    """Once the backend comes back, the status WS reconnects and its
    on-connect callback clears the unreachable state and repopulates the
    lists — no logout/relogin needed (#2012, #2052)."""

    await _fast_reconnect(monkeypatch)

    calls = {"n": 0}
    alpha = _wsobj("alpha")

    def owned():
        calls["n"] += 1
        # Mount fetch (call 1) fails -> enter unreachable; the recovery
        # fetch from _on_ws_connected (call 2+) succeeds.
        if calls["n"] <= 1:
            raise httpx.ConnectError("refused")
        return [alpha]

    ws_calls = {"n": 0}

    async def ws_script(*a, **k):
        ws_calls["n"] += 1
        if ws_calls["n"] == 1:
            raise RuntimeError("ws refused")  # still down
        # Backend is back: connect (fires _on_ws_connected -> recovery),
        # then clean close.
        on_connect = k.get("on_connect")
        if on_connect is not None:
            on_connect()
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", ws_script)
    tokens = iter(["tok"] * 3 + [None])
    app = KlangkApp(
        _ws(
            list_owned_workspaces=owned,
            list_shared_workspaces=owned,
            token=lambda: next(tokens),
        )
    )
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        # Mount fetch fails -> unreachable (overlay pushed, so grab the
        # MainScreen explicitly, not app.screen).
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        assert screen._server_unreachable is True
        # Drive the WS loop: attempt 1 fails (grace), attempt 2 reconnects
        # (fires _on_ws_connected -> clears unreachable + refreshes lists).
        await _real_status_loop(screen)
        await app.workers.wait_for_complete()  # let recovery refresh finish
        await pilot.pause()
        assert screen._server_unreachable is False
        assert (
            "alpha" in _lv_texts(screen.query_one("#owned_list", ListView))[0]
        )


async def test_reconnect_gives_up_after_cap(monkeypatch):
    """After the attempt cap the WS reconnect loop stops and tells the user
    to act (#2012, #2052)."""

    async def fail(*a, **k):
        raise RuntimeError("ws refused")

    monkeypatch.setattr(scr_main, "listen_for_status", fail)
    monkeypatch.setattr(scr_main, "_MAX_RECONNECT_ATTEMPTS", 2)
    await _fast_reconnect(monkeypatch)

    def down():
        raise httpx.ConnectError("refused")

    app = KlangkApp(
        _ws(list_owned_workspaces=down, list_shared_workspaces=down)
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        # Drive the WS loop: it burns through the (low) attempt cap and
        # returns on its own via the give-up branch (no token-drop bound).
        await _real_status_loop(main)
        await pilot.pause()
        assert main._server_unreachable is False
        assert main._gave_up is True
        owned_lv = main.query_one("#owned_list", ListView)
        label = _lv_texts(owned_lv)[0].lower()
        assert "server down" in label
        assert "switch server" in label
        assert "gave up" in (app.live_extra or "").lower()
        # The app-wide overlay carries the give-up message too (#2012).
        assert isinstance(app.screen, ServerDownScreen)
        assert "couldn't reach" in app.screen._message.lower()


async def test_server_down_overlay_dismiss_then_no_repop(monkeypatch):
    """Esc dismisses the app-wide overlay; once dismissed the reconnect loop
    won't re-pop it for the rest of this outage (#2012)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "_reconnect_backoff", lambda attempt: 999.0)

    def down():
        raise httpx.ConnectError("refused")

    app = KlangkApp(
        _ws(list_owned_workspaces=down, list_shared_workspaces=down)
    )
    async with app.run_test() as pilot:
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, ServerDownScreen)
        await pilot.press("escape")
        for _ in range(3):
            await pilot.pause()
        # Overlay gone; the app flagged it dismissed for this outage.
        assert not isinstance(app.screen, ServerDownScreen)
        assert app._server_down_dismissed is True
        # A subsequent set_server_down call is a no-op (won't re-pop).
        app.set_server_down("again")
        assert not isinstance(app.screen, ServerDownScreen)


async def test_server_down_overlay_c_opens_switch_server(monkeypatch):
    """'c' from the overlay closes it and opens the server-switch screen (#2012)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "_reconnect_backoff", lambda attempt: 999.0)

    def down():
        raise httpx.ConnectError("refused")

    app = KlangkApp(
        _ws(list_owned_workspaces=down, list_shared_workspaces=down)
    )
    async with app.run_test() as pilot:
        for _ in range(8):
            await pilot.pause()
        assert isinstance(app.screen, ServerDownScreen)
        await pilot.press("c")
        for _ in range(4):
            await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)
        assert app._server_down_dismissed is True


async def test_server_down_overlay_covers_any_active_page(monkeypatch):
    """The app-level overlay lands on top of whatever screen is active — not
    just the workspaces page — so a WS-detected drop while a detail/form page
    is open is still signalled everywhere (#2012, #2052)."""

    async def fail(*a, **k):
        raise RuntimeError("ws refused")

    monkeypatch.setattr(scr_main, "listen_for_status", fail)
    await _fast_reconnect(monkeypatch)

    alpha = _wsobj("alpha")

    def owned():
        return [alpha]

    tokens = iter(["tok"] * 10 + [None])
    app = KlangkApp(
        _ws(
            list_owned_workspaces=owned,
            list_shared_workspaces=owned,
            token=lambda: next(tokens),
        )
    )
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        # Initial REST fetch succeeds; lists populate.
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Navigate "away" from the workspaces page onto a detail/form page.
        from textual.screen import Screen as _Screen

        detail = _Screen()
        app.push_screen(detail)
        await pilot.pause()
        assert app.screen is detail
        # The status WS can't connect (server down); the WS reconnect loop
        # (run on the still-mounted MainScreen) detects it and pushes the
        # overlay on TOP of the detail page. Token drops to None to bound the
        # loop (session_expired is stubbed so it doesn't replace the overlay).
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        await _real_status_loop(main)
        await pilot.pause()
        assert isinstance(app.screen, ServerDownScreen)
        # The detail page is still in the stack, underneath the overlay.
        assert detail in app.screen_stack


async def test_ws_detects_drop_after_initial_load(monkeypatch):
    """The status WS is the single reachability signal (#2052): a backend that
    drops after the page is already shown surfaces 'server unreachable' when
    the WS reconnect loop's second attempt fails — one mechanism covering the
    mid-session drop."""

    async def fail(*a, **k):
        raise RuntimeError("ws refused")

    monkeypatch.setattr(scr_main, "listen_for_status", fail)
    await _fast_reconnect(monkeypatch)

    alpha = _wsobj("alpha")

    def owned():
        return [alpha]

    tokens = iter(["tok"] * 8 + [None])
    app = KlangkApp(
        _ws(
            list_owned_workspaces=owned,
            list_shared_workspaces=owned,
            token=lambda: next(tokens),
        )
    )
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        screen = app.screen
        # Initial REST fetch succeeds; lists populate, not unreachable.
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert screen._server_unreachable is False
        assert (
            "alpha" in _lv_texts(screen.query_one("#owned_list", ListView))[0]
        )
        # The status WS can't connect (server down); the reconnect loop
        # detects it on the second failed attempt and surfaces unreachable.
        await _real_status_loop(screen)
        await pilot.pause()
        assert screen._server_unreachable is True
        assert (
            "server unreachable"
            in _lv_texts(screen.query_one("#owned_list", ListView))[0].lower()
        )


async def test_status_loop_exits_when_screen_popped(monkeypatch):
    """If the MainScreen leaves the stack (logout / server switch) while the
    WS loop is running, the top-of-iteration guard stops it instead of
    mutating a dead screen (#2052)."""

    async def fail(*a, **k):
        raise RuntimeError("ws died")

    monkeypatch.setattr(scr_main, "listen_for_status", fail)
    await _fast_reconnect(monkeypatch)
    app = KlangkApp(_ws())  # list contents irrelevant — loop returns early
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)
        # Pretend the screen was already removed (e.g. logout completed).
        from textual.app import App as _App

        monkeypatch.setattr(_App, "screen_stack", property(lambda self: []))
        expired = []
        monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
        await _real_status_loop(screen)
        assert expired == []  # screen-pop exit, not session_expired


async def test_status_loop_exits_when_screen_popped_during_backoff(
    monkeypatch,
):
    """A screen pop during the backoff sleep is caught by the next
    iteration's top guard — the loop exits without a render on the dead
    screen (#2052)."""

    async def fail(*a, **k):
        raise RuntimeError("ws died")

    monkeypatch.setattr(scr_main, "listen_for_status", fail)
    monkeypatch.setattr(scr_main, "_reconnect_backoff", lambda attempt: 0.0)
    rendered: list = []
    app = KlangkApp(_ws())
    async with app.run_test():
        screen = app.screen
        assert isinstance(screen, MainScreen)

        async def pop_during_sleep(_delay):
            # Simulate the screen being popped while the loop is parked.
            from textual.app import App as _App

            monkeypatch.setattr(
                _App, "screen_stack", property(lambda self: [])
            )

        monkeypatch.setattr(scr_main, "_reconnect_sleep", pop_during_sleep)
        monkeypatch.setattr(
            screen, "_render_unreachable", lambda *a, **k: rendered.append(1)
        )
        await _real_status_loop(screen)
        # iter 1 fails -> grace (no render) -> sleep pops the screen; iter 2's
        # top guard exits before a second attempt can render on the dead
        # screen.
        assert rendered == []


async def test_reconnect_auth_failure_redirects_to_login(monkeypatch):
    """An auth failure surfacing on the status WS triggers session_expired
    (#2012, #2052)."""

    async def auth_fail(*a, **k):
        raise AuthError("expired")

    monkeypatch.setattr(scr_main, "listen_for_status", auth_fail)
    await _fast_reconnect(monkeypatch)
    app = KlangkApp(_ws())
    expired = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test():
        await _real_status_loop(app.screen)
        assert expired  # WS saw AuthError -> session_expired


async def test_status_loop_exits_when_screen_popped_after_listen(monkeypatch):
    """A screen pop that lands while listen_for_status is in flight (so it
    returns normally) is caught by the post-listen guard — the loop exits
    without a reconnect attempt on the dead screen (#2052)."""

    from textual.app import App as _App

    async def pop_then_close(*a, **k):
        # Screen popped (logout) while "reading frames"; listen returns.
        monkeypatch.setattr(_App, "screen_stack", property(lambda self: []))
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", pop_then_close)
    await _fast_reconnect(monkeypatch)
    app = KlangkApp(_ws())
    rendered = []
    async with app.run_test():
        screen = app.screen
        monkeypatch.setattr(
            screen, "_render_unreachable", lambda *a, **k: rendered.append(1)
        )
        await _real_status_loop(screen)
        # Post-listen guard fired before the reconnect branch could run.
        assert rendered == []


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
    """Pressing 'o' cycles sort through created/name/running (#1764, #1912)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = Workspace(
        id="id-a", name="alpha", created_at="2025-01-01T00:00:00", running=True
    )
    b = Workspace(
        id="id-b", name="beta", created_at="2025-06-01T00:00:00", running=False
    )
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

        # 2nd press: name desc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["beta", "alpha"]
        assert "name" in str(m.query_one("#sort_btn", Button).label)
        assert "▼" in str(m.query_one("#sort_btn", Button).label)

        # 3rd press: name asc.
        m.action_cycle_sort()
        await pilot.pause()
        assert names() == ["alpha", "beta"]
        assert "▲" in str(m.query_one("#sort_btn", Button).label)

        # 4th press: running desc (running-first = stopped first when
        # reverse=True, i.e. higher sort key first → stopped=1 before
        # running=0).
        m.action_cycle_sort()
        await pilot.pause()
        assert "running" in str(m.query_one("#sort_btn", Button).label)
        assert "▼" in str(m.query_one("#sort_btn", Button).label)
        assert names() == [
            "beta",
            "alpha",
        ]  # beta stopped(1), alpha running(0)

        # 5th press: running asc (running-first: running=0 before stopped=1).
        m.action_cycle_sort()
        await pilot.pause()
        assert "running" in str(m.query_one("#sort_btn", Button).label)
        assert "▲" in str(m.query_one("#sort_btn", Button).label)
        assert names() == [
            "alpha",
            "beta",
        ]  # alpha running(0), beta stopped(1)

        # 6th press: back to created desc.
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
        app.screen._on_status_event({"type": "mystery_broadcast"})
        await pilot.pause()
        assert app.live_extra == "live: mystery_broadcast"
        before = calls["n"]
        app.screen._on_status_event({"type": "workspaces_changed"})
        await pilot.pause()
        assert calls["n"] > before  # list re-fetched
        # Silent type: the segment is untouched (#2690).
        assert app.live_extra == "live: mystery_broadcast"


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


async def test_focus_term_list_noop_while_modal_open(monkeypatch):
    """#1956: _focus_term_list must not yank focus to the terminals list
    while a modal dialog is open over the detail screen."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        detail = app.screen
        app.push_screen(ConfirmScreen("sure?"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        # Guard contract: a focus re-assert while the modal is up is a no-op.
        detail._focus_term_list()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert getattr(app.focused, "id", None) != "term_list"


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
    a = _wsobj(
        "alpha",
        allowed_domains=["github.com:443", "pypi.org"],
        rejected_domains=["evil.example.com"],
    )
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
        assert _detail_value(body, "rejected domains") is not None
        assert "evil.example.com" in body


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
                for ln in (
                    re.sub(r"\x1b\[[0-9;]*m", "", x) for x in body.splitlines()
                )
                if ln.lstrip().startswith(label)
                and ln.lstrip()[len(label) : len(label) + 2].isspace()
            )
            lead = len(line) - len(line.lstrip())
            after_label = line.lstrip()[len(label) :]
            gap = len(after_label) - len(after_label.lstrip())
            starts.add(lead + len(label) + gap)
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
    notified = _attach_notify_spy(app)
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
        assert any("Restart requested" in m for m in notified)
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
    notified = _attach_notify_spy(app)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert started == ["alpha"]
        assert any("Container started" in m for m in notified)


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
    notified = _attach_notify_spy(app)
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
        assert any("Stop requested" in m for m in notified)
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
    notified = _attach_notify_spy(app)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # No confirm dialog for start — goes straight through.
        app.screen.action_stop()
        await app.workers.wait_for_complete()
        assert started.get("s") == "alpha"
        assert any("Start requested" in m for m in notified)
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
        assert "n" in bindings and "m" in bindings and "t" in bindings
        assert bindings["n"].show is False
        assert bindings["m"].show is False
        assert bindings["t"].show is False

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
    notified = _attach_notify_spy(app)
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
        assert any("Duplicated" in m for m in notified)
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


async def test_detail_container_restart_full_reload(monkeypatch):
    """#1924: a container restart does a full reload (workspace metadata
    + terminal list), not just a terminal re-fetch."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True, service_started_at=1000.0)
    calls = {"find": 0, "terms": 0}

    def track_find(name):
        calls["find"] += 1
        return a

    async def track_terms(*_a, **_k):
        calls["terms"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = track_find
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        before_find = calls["find"]
        before_terms = calls["terms"]
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
        # Both workspace metadata and terminal list re-fetched.
        assert calls["find"] > before_find
        assert calls["terms"] > before_terms


async def test_detail_container_start_full_reload(monkeypatch):
    """#1924: a stopped container starting up does a full reload."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=False)
    calls = {"find": 0, "terms": 0}

    def track_find(name):
        calls["find"] += 1
        return a

    async def track_terms(*_a, **_k):
        calls["terms"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = track_find
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        before_find = calls["find"]
        before_terms = calls["terms"]
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
        assert calls["find"] > before_find
        assert calls["terms"] > before_terms


async def test_detail_container_status_no_reload_when_unchanged(monkeypatch):
    """container_status with same service_started_at does not reload."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True, service_started_at=1000.0)
    calls = {"find": 0, "terms": 0}

    def track_find(name):
        calls["find"] += 1
        return a

    async def track_terms(*_a, **_k):
        calls["terms"] += 1
        return [{"index": 0, "name": "main", "id": "@0"}]

    st = _ws(list_terminals=track_terms)
    st.find_workspace = track_find
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        before_find = calls["find"]
        before_terms = calls["terms"]
        # Same service_started_at — not a restart, no reload.
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
        assert calls["find"] == before_find
        assert calls["terms"] == before_terms


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


async def test_detail_focuses_term_list_on_mount(monkeypatch):
    """#1956: the Terminals list gets focus on entry and is navigable via
    arrow keys (spatial-nav rule, AGENTS.md) — not mouse-only."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)

    async def terms(_name):
        return [{"index": 0, "name": "main"}, {"index": 1, "name": "build"}]

    st = _ws(list_terminals=terms)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        lv = d.query_one("#term_list", ListView)
        # Focus landed on the list with the first row highlighted.
        assert app.focused is lv
        assert lv.index == 0
        # Arrow keys navigate within the list (reachable via keyboard).
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is lv
        assert lv.index == 1


async def test_detail_focuses_term_list_when_empty(monkeypatch):
    """#1956: focus reaches the Terminals list on entry even when the
    workspace has no terminals — the empty-render path keeps focus on the
    list and highlights the placeholder row instead of stranding it."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()  # list_terminals defaults to _async_empty -> no terminals
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        lv = d.query_one("#term_list", ListView)
        assert app.focused is lv
        assert lv.index == 0  # placeholder row highlighted, not None


async def test_detail_terminals_reload_under_modal_keeps_focus(monkeypatch):
    """#1956: a terminals_changed reload arriving while a modal (e.g. a
    confirm dialog) is open over the detail screen must not yank focus
    out of the modal onto the Terminals list."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_empty)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        # Open a confirm dialog over the detail screen.
        app.push_screen(ConfirmScreen("sure?"))
        await pilot.pause()
        assert app.screen is not d  # modal on top
        modal_focused = app.focused
        # A terminals_changed push arrives while the modal is open.
        d.apply_status_event(
            {
                "type": "terminals_changed",
                "workspace_id": "id-alpha",
                "windows": [
                    {"index": 0, "name": "main"},
                    {"index": 1, "name": "build"},
                ],
            }
        )
        await app.workers.wait_for_complete()
        await pilot.pause()
        lv = d.query_one("#term_list", ListView)
        assert app.focused is not lv  # focus stayed in the modal
        assert app.focused is modal_focused
        # The list still updated underneath.
        assert any("build" in str(it.render()) for it in lv.query(Label))


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

    async def _close(name, window_id):
        closed["i"] = window_id
        return [{"index": 0, "name": "main", "id": "@0"}]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    notified = _attach_notify_spy(app)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.query_one("#term_list").index = 1
        d.action_delete_terminal()
        for _ in range(3):
            await pilot.pause()
        assert closed.get("i") == "@1"
        assert any("Deleted terminal build" in m for m in notified)
        assert len(d.query_one("#term_list", ListView).query(ListItem)) == 1


async def test_detail_delete_terminal_refuses_when_id_unresolvable(
    monkeypatch,
):
    """A row whose window has no resolvable id refuses to close and
    refreshes the list, rather than risking the wrong window (#1965)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    calls = {"close": 0, "list": 0}

    async def _terms_no_id(*a, **k):
        calls["list"] += 1
        return [
            {"index": 0, "name": "main", "id": "@0"},
            {"index": 1, "name": "build"},  # no id — contract violation
        ]

    async def _close(name, window_id):
        calls["close"] += 1
        return []

    st = _ws(list_terminals=_terms_no_id, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        before = calls["list"]
        d.action_delete_terminal()
        for _ in range(4):
            await pilot.pause()
        # No close sent, and the list was refreshed.
        assert calls["close"] == 0
        assert calls["list"] > before
        assert "no longer exists" in str(d.query_one("#detail_msg").render())


async def test_detail_delete_terminal_refreshes_on_server_failure(
    monkeypatch,
):
    """A close that fails server-side (id gone) refreshes the list so the
    dead row self-heals (#1965)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    calls = {"list": 0}

    async def _tracked_terms(*a, **k):
        calls["list"] += 1
        return await _async_terms(*a, **k)

    async def _close(name, window_id):
        raise RuntimeError("no such window")

    st = _ws(list_terminals=_tracked_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        before = calls["list"]
        await d._do_delete_terminal("@1")
        assert any("Delete failed" in m for m in notified)
        # Failure triggered a refresh.
        assert calls["list"] > before


async def test_detail_delete_terminal_shows_inflight_msg(monkeypatch):
    """A 'Deleting terminal …' message shows while the close call is in
    flight, so the screen doesn't appear hung (#1863)."""

    import asyncio

    async def noop(*a, **k):
        return None

    gate = asyncio.Event()

    async def _close(name, window_id):
        await gate.wait()
        return [{"index": 0, "name": "main", "id": "@0"}]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    notified = _attach_notify_spy(app)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.query_one("#term_list").index = 1
        d.action_delete_terminal()
        # While close_terminal is blocked on the gate, the in-flight
        # toast must already have fired.
        for _ in range(3):
            await pilot.pause()
        assert any("Deleting terminal build" in m for m in notified)
        # Releasing the close call fires the success toast.
        gate.set()
        for _ in range(3):
            await pilot.pause()
        assert any("Deleted terminal build" in m for m in notified)


async def test_detail_delete_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _close(name, window_id):
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
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        await d._do_delete_terminal("@1")  # close raises
        await app.workers.wait_for_complete()
        assert any("Delete failed" in m for m in notified)


def test_detail_window_id_for_resolves_index_and_falls_back():
    """Select target resolves a window index to its stable @N id (#1954).

    Non-resolvable keys return None so the caller refuses to spawn —
    falling back to the raw index would reproduce the duplicate-window
    bug (#1955 review).
    """
    d = WorkspaceDetailScreen("alpha")
    d._terminals = [
        {"index": 0, "name": "main", "id": "@0"},
        {"index": 1, "name": "build", "id": "@1"},
    ]
    assert d._window_id_for("0") == "@0"
    assert d._window_id_for("1") == "@1"
    # Unknown index → None (don't risk selecting by index).
    assert d._window_id_for("9") is None
    # Non-numeric selector → None.
    assert d._window_id_for("build") is None


def test_detail_window_id_for_warns_when_id_missing(caplog):
    """A window matching the index but lacking an id is a server-contract
    violation — refuse to select and log loudly (#1955 review)."""
    d = WorkspaceDetailScreen("alpha")
    d._terminals = [{"index": 0, "name": "main"}]  # no "id"
    with caplog.at_level(
        "WARNING",
        logger="klangk.cli.tui.screens.workspace_detail",
    ):
        assert d._window_id_for("0") is None
    assert "no window id" in caplog.text


def test_detail_terminal_label_for():
    """Delete-message label prefers the window name, falling back to the
    index/key (#1966 review UX nit)."""
    d = WorkspaceDetailScreen("alpha")
    d._terminals = [
        {"index": 0, "name": "main", "id": "@0"},
        {"index": 1, "name": "", "id": "@1"},  # empty name → index
    ]
    assert d._terminal_label_for("0") == "main"
    assert d._terminal_label_for("1") == "1"  # empty name falls back
    assert d._terminal_label_for("9") == "9"  # unknown index
    assert d._terminal_label_for("build") == "build"  # non-numeric


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
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            spawned.append(cmd)
            or scr_detail.subprocess.CompletedProcess(cmd, 0)
        ),
    )

    import io
    import sys
    from contextlib import contextmanager

    captured = []

    @contextmanager
    def fake_suspend():
        # Real suspend() owns the terminal's stdout; model that here by
        # redirecting to a buffer so the "Connecting…" line (#2010)
        # doesn't leak past pytest's capture during run_test().
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            captured.append(sys.stdout.getvalue())
            sys.stdout = saved

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
            "@0",
        ]
        # The flash-fix clears the primary screen before klangk shell
        # attaches (#2010). No "Connecting…" line of our own — klangk
        # shell prints one on attach.
        assert captured[0].startswith("\033[2J\033[H")
        assert "Connecting to alpha" not in captured[0]


async def test_detail_terminal_select_external_terminal(monkeypatch):
    """With terminal-open-cmd configured, the shell spawns in a new
    terminal window via Popen (argv appended after the configured command)
    and the TUI is not suspended (#2685)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setenv("KLANGKC_TERMINAL_OPEN_CMD", "konsole --hold -e")
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"

    class FakeProc:
        returncode = 0

    popped = []

    def fake_popen(argv, **k):
        popped.append((argv, k))
        return FakeProc()

    monkeypatch.setattr(scr_detail.subprocess, "Popen", fake_popen)
    ran = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            ran.append(cmd) or scr_detail.subprocess.CompletedProcess(cmd, 0)
        ),
    )

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        app.screen.on_list_view_selected(FakeSelected("0"))
        # Spawned via Popen with the configured terminal command prefixed,
        # in its own session so the window outlives the TUI, and with
        # launcher output discarded so a post-exec failure can't trash
        # the TUI's screen (#2686 review).
        assert len(popped) == 1
        argv, kwargs = popped[0]
        assert argv[:3] == ["konsole", "--hold", "-e"]
        assert argv[3:] == [
            scr_detail.sys.executable,
            "-m",
            "klangk.cli.main",
            "--server",
            "https://x.example",
            "shell",
            "alpha",
            "@0",
        ]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdout") == scr_detail.subprocess.DEVNULL
        assert kwargs.get("stderr") == scr_detail.subprocess.DEVNULL
        # Not run inline — the TUI stays up, no suspend.
        assert ran == []


async def test_detail_terminal_select_external_failure_falls_back(
    monkeypatch,
):
    """A broken terminal-open-cmd (command missing) shows an inline error
    and falls back to the inline suspend-and-run path (#2685)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setenv("KLANGKC_TERMINAL_OPEN_CMD", "no-such-terminal -e")
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"

    def boom(argv, **k):
        raise FileNotFoundError("no-such-terminal not found")

    monkeypatch.setattr(scr_detail.subprocess, "Popen", boom)
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            spawned.append(cmd)
            or scr_detail.subprocess.CompletedProcess(cmd, 0)
        ),
    )

    import io
    import sys
    from contextlib import contextmanager

    @contextmanager
    def fake_suspend():
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            sys.stdout = saved

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        app.screen.on_list_view_selected(FakeSelected("0"))
        # The inline fallback is deferred (call_after_refresh) so the error message
        # paints before suspend() blanks the screen — not launched yet.
        assert spawned == []
        msg = str(
            app.screen.query_one("#detail_msg", scr_detail.Static).render()
        )
        assert "terminal-open-cmd failed" in msg
        # Let the timer fire, then the deferred inline launch runs.
        await pilot.pause()
        await pilot.pause()
        assert len(spawned) == 1
        assert spawned[0][-3:] == ["shell", "alpha", "@0"]


async def test_detail_shared_terminal_select_external_terminal(monkeypatch):
    """Shared-terminal joins honor terminal-open-cmd too (#2685)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setenv("KLANGKC_TERMINAL_OPEN_CMD", "konsole --hold -e")
    a = _wsobj("alpha", running=True)
    st = _ws(list_shared_terminals=_async_shared)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"

    class FakeProc:
        returncode = 0

    popped = []

    def fake_popen(argv, **k):
        popped.append(argv)
        return FakeProc()

    monkeypatch.setattr(scr_detail.subprocess, "Popen", fake_popen)
    ran = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            ran.append(cmd) or scr_detail.subprocess.CompletedProcess(cmd, 0)
        ),
    )

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen.on_list_view_selected(
            FakeSelected("alice:build", control_id="shared_term_list")
        )
        assert len(popped) == 1
        assert popped[0][-3:] == ["shell", "alpha", "alice:build"]
        assert popped[0][:3] == ["konsole", "--hold", "-e"]
        assert ran == []


async def test_detail_terminal_select_refuses_when_id_unresolvable(
    monkeypatch,
):
    """A row whose index has no resolvable id refuses to spawn and
    refreshes the list, rather than risking a duplicate (#1955 review)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: spawned.append(cmd),
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
        refreshed = []

        async def fake_load():
            refreshed.append(True)

        monkeypatch.setattr(app.screen, "_load_terminals", fake_load)
        # Index 9 isn't in the list → no resolvable id → refuse + refresh.
        app.screen.on_list_view_selected(FakeSelected("9"))
        await pilot.pause()
        await pilot.pause()
        assert spawned == []
        assert refreshed == [True]


async def test_detail_terminal_select_failed_spawn_refreshes_list(
    monkeypatch,
):
    """A non-zero shell exit (e.g. window vanished server-side mid-spawn)
    triggers a list refresh so the dead row self-heals (#1955 review)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: scr_detail.subprocess.CompletedProcess(cmd, 1),
    )

    import io
    import sys
    from contextlib import contextmanager

    @contextmanager
    def fake_suspend():
        # See test_detail_terminal_select_spawns_shell: redirect stdout
        # so the "Connecting…" line (#2010) doesn't leak during run_test().
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            sys.stdout = saved

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        refreshed = []

        async def fake_load():
            refreshed.append(True)

        monkeypatch.setattr(app.screen, "_load_terminals", fake_load)
        app.screen.on_list_view_selected(FakeSelected("0"))
        await pilot.pause()
        await pilot.pause()
        assert refreshed == [True]


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


async def _async_shared(*a, **k):
    """Async stub returning a mixed shared-terminal list (service + other
    user + the caller's own shared window)."""
    return [
        {
            "user_id": "agent",
            "handle": "klangk",
            "window_name": "service-cmd",
            "window_id": "@0",
            "is_service": True,
        },
        {
            "user_id": "alice-id",
            "handle": "alice",
            "window_name": "build",
            "window_id": "@1",
            "is_service": False,
        },
        {
            "user_id": "me",
            "handle": "me",
            "window_name": "notes",
            "window_id": "@2",
            "is_service": False,
        },
    ]


async def test_detail_shared_terminals_listed_and_filtered(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(
        list_shared_terminals=_async_shared,
        current_user_id=lambda: "me",
    )
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_shared_terminals()
        await pilot.pause()
        rows = _lv_texts(app.screen.query_one("#shared_term_list", ListView))
    # Service window is labelled distinctly; alice's window is shown by
    # handle:window; my own shared window ("me: notes") is filtered out.
    assert any("Service" in r for r in rows)
    assert any("alice: build" in r for r in rows)
    assert not any("notes" in r for r in rows)


async def test_detail_shared_terminal_select_joins(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_shared_terminals=_async_shared)
    st.find_workspace = lambda n: a
    st.current_url = lambda: "https://x.example"
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            spawned.append(cmd)
            or scr_detail.subprocess.CompletedProcess(cmd, 0)
        ),
    )
    import io
    import sys
    from contextlib import contextmanager

    @contextmanager
    def fake_suspend():
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            sys.stdout = saved

    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        # Selecting a shared row issues ``klangk shell <ws> <handle>:<win>``
        # — the same join_shared_terminal path the browser uses (#2164).
        app.screen.on_list_view_selected(
            FakeSelected("alice:build", control_id="shared_term_list")
        )
        assert len(spawned) == 1
        assert spawned[0][-3:] == ["shell", "alpha", "alice:build"]


async def test_detail_shared_terminal_select_bad_key_refreshes(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    refreshed = []
    st = _ws(list_shared_terminals=_async_shared)
    st.find_workspace = lambda n: a
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess, "run", lambda cmd, **k: spawned.append(cmd)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        orig = app.screen._load_shared_terminals

        async def _refresh():
            refreshed.append(1)

        app.screen._load_shared_terminals = _refresh
        # A row keyed without a ``handle:window`` colon must not spawn a
        # shell — refresh the shared list instead.
        app.screen.on_list_view_selected(
            FakeSelected("bogus", control_id="shared_term_list")
        )
        await pilot.pause()
        assert spawned == []
        assert refreshed == [1]
        app.screen._load_shared_terminals = orig


async def _async_boom(*a, **k):
    raise RuntimeError("boom")


async def test_detail_load_shared_terminals_auth_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a

    async def _auth_err(*a, **k):
        raise scr_detail.AuthError

    st.list_shared_terminals = _auth_err
    app = KlangkApp(st)
    expired = []
    async with app.run_test() as pilot:
        # Patch session_expired BEFORE push: the mount worker also calls
        # _load_shared_terminals (and would hit the AuthError), so it must
        # not push the real SessionExpiredScreen out from under us.
        monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_shared_terminals()
        await pilot.pause()
    assert expired  # AuthError -> session_expired invoked


async def test_detail_load_shared_terminals_generic_error(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_shared_terminals=_async_boom)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # A generic exception degrades to an empty shared list.
        await app.screen._load_shared_terminals()
        await pilot.pause()
        assert app.screen._shared_terminals == []


async def test_detail_shared_terminal_select_no_ws_ignored(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _ws()
    # No workspace -> _ws stays None -> shared launch is a no-op.
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess, "run", lambda cmd, **k: spawned.append(cmd)
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        app.screen._ws = None
        app.screen.on_list_view_selected(
            FakeSelected("alice:build", control_id="shared_term_list")
        )
        # Empty target is also ignored.
        app.screen.on_list_view_selected(
            FakeSelected("", control_id="shared_term_list")
        )
        assert spawned == []


async def test_detail_shared_terminal_failed_spawn_refreshes(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(list_shared_terminals=_async_shared)
    st.find_workspace = lambda n: a
    spawned = []
    monkeypatch.setattr(
        scr_detail.subprocess,
        "run",
        lambda cmd, **k: (
            spawned.append(cmd)
            or scr_detail.subprocess.CompletedProcess(cmd, 1)
        ),
    )
    import io
    import sys
    from contextlib import contextmanager

    @contextmanager
    def fake_suspend():
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            yield
        finally:
            sys.stdout = saved

    refreshed = []
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)

        async def _refresh():
            refreshed.append(1)

        app.screen._load_shared_terminals = _refresh
        # A non-zero shell exit refreshes the shared list (the window may
        # have been unshared server-side).
        app.screen.on_list_view_selected(
            FakeSelected("alice:build", control_id="shared_term_list")
        )
        await pilot.pause()
        assert len(spawned) == 1
        assert refreshed == [1]


async def test_detail_term_modify_actions_noop_when_shared_focused(
    monkeypatch,
):
    """[n]/[m]/[t] must not act on the own-terminal list while the shared
    list is focused (#2164) — otherwise they'd delete/rename the wrong
    terminal."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    closed = []
    created = []
    st = _ws(list_terminals=_async_terms)
    st.find_workspace = lambda n: a
    st.close_terminal = lambda *a, **k: closed.append(1)
    st.create_terminal = lambda *a, **k: created.append(1)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        # Focus the shared list — all three modify actions must no-op.
        app.screen.query_one("#shared_term_list", ListView).focus()
        await pilot.pause()
        app.screen.action_delete_terminal()
        app.screen.action_rename_terminal()
        app.screen.action_new_terminal()
        await pilot.pause()
        assert closed == []
        assert created == []


async def test_detail_focus_defaults_to_own_list(monkeypatch):
    """When neither list has focus, _focus_term_list reclaims it for the
    own-terminal list (the screen's primary widget)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws()
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        # Move focus off both lists, then reclaim. (Footer.focus() is a no-op
        # in textual, so clear focus directly to reach the reclaim branch.)
        app.screen.set_focus(None)
        await pilot.pause()
        assert app.screen.focused is None
        app.screen._focus_term_list()
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "term_list"


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


async def test_status_bar_last_login_segment(monkeypatch):
    """set_state renders the last-login segment when present and omits
    it otherwise (#2583). Widgets need an active app to update, so this
    drives a mounted StatusBar."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(last_login_at=lambda: None))
    async with app.run_test() as pilot:
        await pilot.pause()  # let mount-time app-level refreshes land
        status = app.screen.query_one("#status", StatusBar)
        app.last_login = "2026-08-20 10:00"
        app.refresh_status()
        await pilot.pause()
        assert "last login: 2026-08-20 10:00" in str(status.render())
        # Without a last login the segment is absent.
        app.last_login = None
        app.refresh_status()
        await pilot.pause()
        assert "last login" not in str(status.render())


async def test_status_bar_extra_segment_leads(monkeypatch):
    """The live `extra` segment renders FIRST (#2661): appended last, the
    schedule countdown fell off the right edge of a typical terminal
    (server/user/last-login alone span ~76 cols) — an invisible
    countdown on exactly the screens that need it."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_ws(last_login_at=lambda: None))
    async with app.run_test() as pilot:
        await pilot.pause()  # let mount-time app-level refreshes land
        status = app.screen.query_one("#status", StatusBar)
        app.live_extra = "server: stop at 19:00 (in 1h 12m)"
        app.last_login = "2026-08-20 10:00"
        app.refresh_status()
        await pilot.pause()
        rendered = str(status.render())
        assert rendered.startswith("server: stop at 19:00 (in 1h 12m)")
        assert "user: me@x.example" in rendered
        # Static segments still render without an extra.
        app.live_extra = ""
        app.refresh_status()
        await pilot.pause()
        assert str(status.render()).startswith("server: https://x.example")


async def test_status_bar_on_every_screen(monkeypatch):
    """Every full-page screen mounts the StatusBar and renders the
    server/user line from App-level state (#2689) — not just the
    workspaces list."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    wsobj = _wsobj("alpha", running=True)
    st = _ws(owned=[wsobj])
    st.find_workspace = lambda n: wsobj
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        # MainScreen (workspaces list).
        main = app.screen
        rendered = str(main.query_one("#status", StatusBar).render())
        assert "server: https://x.example" in rendered
        assert "user: me@x.example" in rendered

        # Workspace detail screen.
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "server: https://x.example" in rendered
        assert "user: me@x.example" in rendered

        # Create form.
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["img"],
                default="img",
                allow_autostart=True,
                default_allowed_domains=[],
                nix_available=False,
            )
        )
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "user: me@x.example" in rendered

        # Server-switch screen.
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "server: https://x.example" in rendered

        # Pop back — the line doesn't blank on navigation (#2689).
        app.pop_screen()
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "server: https://x.example" in rendered


async def test_status_bar_on_login_screen(monkeypatch):
    """The login screen also shows the status row (server segment; user
    renders as '(not logged in)' pre-auth) (#2689)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        known_servers=lambda: [],
        list_owned_workspaces=lambda: [],
        list_shared_workspaces=lambda: [],
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "server: https://x.example" in rendered
        assert "(not logged in)" in rendered


async def test_countdown_visible_on_detail_screen(monkeypatch):
    """A #2661 schedule event arriving while the detail screen is on top
    renders on its StatusBar — live segment leading the row — and survives
    pop-back to the list (#2689)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    wsobj = _wsobj("alpha", running=True)
    st = _ws(owned=[wsobj])
    st.find_workspace = lambda n: wsobj
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        main = app.screen
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        fire_at = (datetime.now() + timedelta(hours=1)).isoformat()
        # The status WS handler lives on the MainScreen underneath; the
        # event must still reach the detail screen's bar.
        main._on_status_event(
            {
                "type": "server_schedule",
                "schedules": [{"action": "stop", "fire_at": fire_at}],
            }
        )
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert rendered.startswith("server: stop at")
        assert "(in" in rendered
        assert "user: me@x.example" in rendered
        # Pop back — the countdown doesn't blank on navigation.
        app.pop_screen()
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert rendered.startswith("server: stop at")


def test_status_screen_default_body_empty():
    """The StatusScreen base's compose_body default yields nothing — every
    real screen overrides it (#2689)."""
    assert list(StatusScreen().compose_body()) == []


async def test_status_dock_layout_every_screen(monkeypatch):
    """The shared #status_dock is exactly two rows, pinned to the bottom,
    on every full-page screen — including the screens whose SECOND base
    is StatusScreen (LoginScreen, TabSkipMixin forms). Textual scopes
    DEFAULT_CSS to the defining class's type name and that scope only
    follows a screen's first base chain, so these rules must live in the
    App CSS; in StatusScreen.DEFAULT_CSS the dock silently grew to 1fr
    and squeezed the login form's server list to a single row (#2689)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        # Single inheritance: MainScreen(StatusScreen).
        dock = app.screen.query_one("#status_dock")
        assert dock.size.height == 2
        assert dock.region.y == 22
        # Multiple inheritance: CreateWorkspaceScreen(TabSkipMixin,
        # StatusScreen).
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["img"],
                default="img",
                allow_autostart=True,
                default_allowed_domains=[],
                nix_available=False,
            )
        )
        await pilot.pause()
        dock = app.screen.query_one("#status_dock")
        assert dock.size.height == 2
        assert dock.region.y == 22


async def test_login_server_list_not_compressed(monkeypatch):
    """Regression (#2689): the login screen's server list shows every
    known entry, not one squeezed row — the dock chrome must not eat the
    body's space."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        known_servers=lambda: [
            tui_state_mod.ServerInfo(alias="a", url="https://a.example"),
            tui_state_mod.ServerInfo(alias="b", url="https://b.example"),
        ],
        default_uds=lambda: None,
        list_owned_workspaces=lambda: [],
        list_shared_workspaces=lambda: [],
    )
    app = KlangkApp(st)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        lv = app.screen.query_one("#server_options", ListView)
        items = lv.query(ListItem)
        assert len(items) == 2
        # Every entry row sits inside the visible region: the list is at
        # least as tall as its rows, not clipped to one.
        assert lv.size.height >= 2
        for item in items:
            assert lv.region.contains_region(item.region)


def test_fmt_login_ts_invalid():
    """Unparseable timestamps render empty (#2583)."""
    assert MainScreen._fmt_login_ts("not-a-date") == ""
    assert MainScreen._fmt_login_ts("") == ""


def test_fmt_login_ts_localizes():
    """A UTC ISO timestamp renders in the local timezone, not UTC —
    computed via the local offset rather than the implementation's own
    expression (#2583)."""
    from datetime import datetime, timezone

    utc_naive = datetime(2026, 8, 20, 10)
    local = utc_naive.replace(tzinfo=timezone.utc).astimezone()
    expected = (utc_naive + local.utcoffset()).strftime("%Y-%m-%d %H:%M")
    assert MainScreen._fmt_login_ts("2026-08-20T10:00:00+00:00") == expected


async def test_main_screen_shows_last_login(monkeypatch):
    """The main screen fetches the last login once on mount and shows it
    in the status bar (#2583)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    # The autouse fixture stubs _load_last_login; this test covers it.
    monkeypatch.setattr(MainScreen, "_load_last_login", _real_load_last_login)
    iso = "2026-08-20T10:00:00+00:00"
    st = _ws(owned=[_wsobj("alpha")], last_login_at=lambda: iso)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert "last login:" in rendered
        assert MainScreen._fmt_login_ts(iso) in rendered


async def test_main_screen_last_login_none_omitted(monkeypatch):
    """A server that reports no last login shows no segment (#2583)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    # The autouse fixture stubs _load_last_login; this test covers it.
    monkeypatch.setattr(MainScreen, "_load_last_login", _real_load_last_login)
    st = _ws(owned=[_wsobj("alpha")], last_login_at=lambda: None)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "last login" not in str(
            app.screen.query_one("#status", StatusBar).render()
        )


async def test_reload_last_login_refetches_after_server_switch(monkeypatch):
    """reload_last_login re-fetches the stamp, so a server switch
    doesn't keep the previous server's (possibly another user's) login
    time beside the new identity (#2583)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    monkeypatch.setattr(MainScreen, "_load_last_login", _real_load_last_login)
    iso_a = "2026-08-20T10:00:00+00:00"
    iso_b = "2026-08-21T12:00:00+00:00"
    current = {"iso": iso_a}
    st = _ws(owned=[_wsobj("alpha")], last_login_at=lambda: current["iso"])
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert MainScreen._fmt_login_ts(iso_a) in rendered

        # "Switch servers": the state now reports the new identity's
        # (different) stamp; the reload replaces the shown one.
        current["iso"] = iso_b
        app.screen.reload_last_login()
        await app.workers.wait_for_complete()
        await pilot.pause()
        rendered = str(app.screen.query_one("#status", StatusBar).render())
        assert MainScreen._fmt_login_ts(iso_b) in rendered
        assert MainScreen._fmt_login_ts(iso_a) not in rendered


async def test_main_screen_auth_expired_shows_overlay(monkeypatch):
    """An expired session on the workspaces fetch shows the app-wide overlay,
    not a small inline label (#2025)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)

    def boom():
        raise AuthError("expired")

    app = KlangkApp(
        _ws(list_owned_workspaces=boom, list_shared_workspaces=boom)
    )
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # App-wide overlay covers the page (#2025).
        assert isinstance(app.screen, SessionExpiredScreen)
        # The underlying lists are cleared — no misleading inline label.
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        owned_lv = main.query_one("#owned_list", ListView)
        assert "no workspaces" in _lv_texts(owned_lv)[0].lower()


async def test_detail_auth_expired_shows_overlay(monkeypatch):
    """An expired session on the workspace detail load shows the app-wide
    overlay, not a small inline message (#2025)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _ws()
    st.find_workspace = lambda n: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_detail_load_terminals_auth_error_shows_overlay(monkeypatch):
    """AuthError in _load_terminals triggers the session-expired overlay
    instead of silently showing an empty terminal list (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a

    async def bad_terminals(n):
        raise AuthError("expired")

    st.list_terminals = bad_terminals
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_detail_edit_auth_error_in_images_shows_overlay(monkeypatch):
    """AuthError fetching images in _do_edit (detail screen) triggers the
    session-expired overlay instead of opening the form with defaults (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.list_images = lambda: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Trigger edit from the detail screen.
        screen = next(
            s for s in app.screen_stack if isinstance(s, WorkspaceDetailScreen)
        )
        screen.run_worker(screen._do_edit, exit_on_error=False)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


async def test_detail_edit_auth_error_in_autostart_shows_overlay(monkeypatch):
    """AuthError fetching allow_autostart in _do_edit (detail screen) triggers
    the session-expired overlay (#2035)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.list_images = lambda: {"default": "base", "allowed": ["base"]}
    st.allow_autostart = lambda: (_ for _ in ()).throw(AuthError("expired"))
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = next(
            s for s in app.screen_stack if isinstance(s, WorkspaceDetailScreen)
        )
        screen.run_worker(screen._do_edit, exit_on_error=False)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)


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

    async def _close(name, window_id):
        return []  # close / refresh failed

    calls = {"list": 0}

    async def _tracked_terms(*a, **k):
        calls["list"] += 1
        return await _async_terms(*a, **k)

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_tracked_terms, close_terminal=_close)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        before = calls["list"]
        await d._do_delete_terminal("@1")
        await app.workers.wait_for_complete()
        # Let the refresh's clear/append reconcile in the DOM.
        await pilot.pause()
        assert any("Delete failed" in m for m in notified)
        assert (
            len(d.query_one("#term_list", ListView).query(ListItem)) == 2
        )  # unchanged
        # The empty-result failure triggered a list refresh (#1966 review).
        assert calls["list"] > before


async def test_detail_new_terminal(monkeypatch):
    async def noop(*a, **k):
        return None

    created = {}

    async def _create(name, window_name=None):
        created["name"] = window_name
        return [
            {"index": 0, "name": "main", "id": "@0"},
            {"index": 1, "name": "build", "id": "@1"},
            {"index": 2, "name": "bash", "id": "@2"},
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
    notified = _attach_notify_spy(app)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.action_new_terminal()
        for _ in range(5):
            await pilot.pause()
        await app.workers.wait_for_complete()
        assert created["name"] is None
        assert len(d.query_one("#term_list", ListView).query(ListItem)) == 3
        assert any("Created terminal" in m for m in notified)


async def test_detail_new_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _create(name, window_name=None):
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
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        await d._do_new_terminal()
        await app.workers.wait_for_complete()
        assert any("Create failed" in m for m in notified)


async def test_detail_new_terminal_empty_result(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _create(name, window_name=None):
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
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        await d._do_new_terminal()
        await app.workers.wait_for_complete()
        assert any("Create failed" in m for m in notified)


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


async def test_detail_rename_terminal(monkeypatch):
    async def noop(*a, **k):
        return None

    renamed = {}

    async def _rename(name, index, new_name):
        renamed.update(name=name, index=index, new=new_name)
        return [
            {"index": 0, "name": "main", "id": "@0"},
            {"index": 1, "name": new_name, "id": "@1"},
        ]

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_rename)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.action_rename_terminal()
        await pilot.pause()
        # InputScreen pushed, prefilled with the current name "build".
        assert isinstance(app.screen, InputScreen)
        assert app.screen.query_one("#inp_value", Input).value == "build"
        app.screen.query_one("#inp_value", Input).value = "ci"
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        for _ in range(4):
            await pilot.pause()
        assert renamed == {"name": "alpha", "index": 1, "new": "ci"}
        assert any("Renamed terminal to 'ci'" in m for m in notified)


async def test_detail_rename_terminal_no_selection(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_async_empty)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = None
        d.action_rename_terminal()  # nothing highlighted -> no-op
        await app.workers.wait_for_complete()
        await pilot.pause()
        # No InputScreen pushed — still on the detail screen.
        assert app.screen is d


async def test_detail_rename_terminal_cancel(monkeypatch):
    async def noop(*a, **k):
        return None

    renamed = []

    async def _rename(*a, **k):
        renamed.append(True)
        return []

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_rename)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        d.action_rename_terminal()
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        app.screen.on_button_pressed(FakeBtnPress("cancel"))  # -> None
        for _ in range(3):
            await pilot.pause()
        assert renamed == []
        assert app.screen is d


async def test_detail_rename_terminal_failure(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _rename(name, index, new_name):
        raise RuntimeError("boom")

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_rename)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.action_rename_terminal()
        await pilot.pause()
        app.screen.query_one("#inp_value", Input).value = "ci"
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        for _ in range(4):
            await pilot.pause()
        assert any("Rename failed" in m for m in notified)


async def test_detail_rename_terminal_empty_result(monkeypatch):
    async def noop(*a, **k):
        return None

    async def _rename(name, index, new_name):
        return []

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_rename)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        notified = []
        monkeypatch.setattr(
            app, "notify", lambda *a, **k: notified.append(a[0] if a else "")
        )
        d.action_rename_terminal()
        await pilot.pause()
        app.screen.query_one("#inp_value", Input).value = "ci"
        app.screen.on_button_pressed(FakeBtnPress("ok"))
        for _ in range(4):
            await pilot.pause()
        assert any("could not refresh" in m for m in notified)


async def test_detail_rename_terminal_appends_to_default(monkeypatch):
    """The rename input appends (select_on_focus=False): typing into a
    prefilled 'build' field yields 'build2', not a replace (#2020)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha")
    st = _ws(list_terminals=_async_terms, rename_terminal=_async_empty)
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load_terminals()
        await pilot.pause()
        d = app.screen
        d.query_one("#term_list").index = 1
        d.action_rename_terminal()
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        inp = app.screen.query_one("#inp_value", Input)
        assert inp.value == "build"
        await pilot.press("2")
        await pilot.pause()
        assert inp.value == "build2"  # appended, not replaced
        app.screen.dismiss(None)


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
        default_allowed_domains=lambda: [],
        default_per_handle_home=lambda: True,
        # The create flow opens the new workspace's detail screen, whose
        # _mount_async calls find_workspace — stub it so the flow test
        # doesn't make a real (timing-out) HTTP call (#1989).
        find_workspace=lambda n: _wsobj(n),
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


async def test_create_screen_egress_mode_default_and_selectable(monkeypatch):
    """#2409: the Netfilter tab has an egress-mode picker (interactive by
    default), and the chosen mode reaches the create request body."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # Default is interactive (the server default for new workspaces).
        assert cs.query_one("#egress_mode", Select).value == "interactive"
        # Switch to allow and submit; the selection reaches the request.
        cs.query_one("#egress_mode", Select).value = "allow"
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["egress_mode"] == "allow"


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


async def test_create_screen_seeds_default_allowed_domains(monkeypatch):
    """#1931: the Netfilter tab is pre-filled with the deploy default
    (KLANGKD_NETFILTER_DEFAULT_DOMAINS) as a starting set the user can
    edit/remove — parity with the Flutter create dialog."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(
        _create_state(
            default_allowed_domains=lambda: [
                "github.com:443",
                "pypi.org:443",
            ]
        )
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # Seeded verbatim from the deploy default, and rendered into the
        # Netfilter tab's list (option ids a0 / a1, not the placeholder).
        assert cs._allowed_domains == ["github.com:443", "pypi.org:443"]
        ol = cs.query_one("#allow_list", OptionList)
        assert ol.get_option_at_index(0).id == "a0"
        assert ol.get_option_at_index(1).id == "a1"
        # Editable: remove the first seeded entry (a starting set, not a floor).
        ol.highlighted = 0
        cs._remove_allowed_domain()
        assert cs._allowed_domains == ["pypi.org:443"]
        # Editable: add a new entry on top of the seed.
        cs.query_one("#allow_input").value = "registry.npmjs.org:443"
        cs._add_allowed_domain()
        assert cs._allowed_domains == [
            "pypi.org:443",
            "registry.npmjs.org:443",
        ]


async def test_create_screen_allowed_domains_empty_when_no_default(
    monkeypatch,
):
    """#1931: with no deploy default the Netfilter tab still starts empty
    (no regression) — the list shows the inert (unrestricted) placeholder."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())  # default_allowed_domains -> []
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        assert cs._allowed_domains == []
        ol = cs.query_one("#allow_list", OptionList)
        assert ol.get_option_at_index(0).id == ""  # (unrestricted) placeholder


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


async def test_create_screen_per_handle_home_default_and_toggle(monkeypatch):
    """#2721: the create form's Per-handle home checkbox pre-reflects the
    deploy default (KLANGKD_PER_HANDLE_HOME) and the toggle reaches the
    create request — an untouched form submits the server default."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _create_state(create=create, default_per_handle_home=lambda: False)
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        # Pre-reflects the deploy default (shared here).
        assert cs.query_one("#per_handle_home", Checkbox).value is False
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["per_handle_home"] is False

    # Fresh app for the toggle case (the first form dismissed on create).
    app2 = KlangkApp(_create_state(create=create))
    async with app2.run_test(size=(140, 40)) as pilot:
        app2.screen.action_create()
        await app2.workers.wait_for_complete()
        await pilot.pause()
        cs = app2.screen
        assert cs.query_one("#per_handle_home", Checkbox).value is True
        cs.query_one("#per_handle_home", Checkbox).value = False
        cs.query_one("#name").value = "ws2"
        cs._create()
        await app2.workers.wait_for_complete()
        assert captured["k"]["per_handle_home"] is False


async def test_create_screen_per_handle_home_unknown_omits(monkeypatch):
    """#2737 review: when the deploy default is UNKNOWN (fetch failure),
    the checkbox is hidden and the field omitted — the server applies
    its own default instead of a possibly-wrong forced value."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _create_state(create=create, default_per_handle_home=lambda: None)
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cb = cs.query_one("#per_handle_home", Checkbox)
        assert cb.display is False  # unknown default -> hidden
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        # None = omit: the client drops the key so the server default
        # applies (asserted at the client level in test_cli.py).
        assert captured["k"]["per_handle_home"] is None


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
        await pilot.pause()
        # AuthError surfaces the app-wide overlay, not an inline form message (#2025).
        assert isinstance(app.screen, SessionExpiredScreen)


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
        _create_state(
            create=create,
            list_images=boom,
            allow_autostart=boom,
            default_allowed_domains=boom,
            default_per_handle_home=boom,
        )
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        assert cs._allowed == []
        assert cs._allowed_domains == []  # fetch failed -> empty seed
        assert cs.query_one("#auto_start", Checkbox).display is False
        # Fetch failed -> layout default unknown -> checkbox hidden and
        # the field omitted (#2737 review).
        assert cs.query_one("#per_handle_home", Checkbox).display is False
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["image"] is None  # omitted
        assert captured["k"]["per_handle_home"] is None  # omitted too


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


async def test_create_screen_nix_hidden_when_not_available(monkeypatch):
    """#2233: the Mount /nix dir toggle is hidden unless the server reports
    nix_available (a nix_seed backend is configured)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())  # list_images omits nix_available
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cb = app.screen.query_one("#nix", Checkbox)
        assert cb.display is False
        assert cb.disabled is True


async def test_create_screen_nix_shown_and_sent_when_checked(monkeypatch):
    """#2233: when nix is available the toggle shows; checking it sends
    settings.nix=True on create."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def create(name, **k):
        captured["k"] = k
        return _wsobj(name)

    app = KlangkApp(
        _create_state(
            create=create,
            list_images=lambda: {
                "default": "base",
                "allowed": ["base", "py:3"],
                "nix_available": True,
            },
        )
    )
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        nix = cs.query_one("#nix", Checkbox)
        assert nix.display is True
        nix.value = True
        cs.query_one("#name").value = "ws"
        cs._create()
        await app.workers.wait_for_complete()
        assert captured["k"]["settings"] == {"nix": True}


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
            nix_available=kw.get("nix_available", False),
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


async def test_edit_screen_egress_mode_pre_populates_and_saves(monkeypatch):
    """#2409: the edit form seeds the picker from the workspace's current
    egress_mode and sends a change through the update body."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", egress_mode="static", running=False)
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # Seeded from the workspace (static), not the default interactive.
        assert es.query_one("#egress_mode", Select).value == "static"
        es.query_one("#egress_mode", Select).value = "allow"
        es._save()
        await app.workers.wait_for_complete()
        assert captured["egress_mode"] == "allow"
        # Not running => no restart offer.
        assert not isinstance(app.screen, ConfirmScreen)


async def test_edit_screen_per_handle_home_pre_populates_and_saves(
    monkeypatch,
):
    """#2721: the edit form seeds the checkbox from the workspace's home
    layout and sends a flip through the update body — without a restart
    offer (a flip applies from the next connect/start, #2719)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", image="base", per_handle_home=True, running=True)
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        # Seeded from the workspace (per-handle).
        assert es.query_one("#per_handle_home", Checkbox).value is True
        es.query_one("#per_handle_home", Checkbox).value = False
        es._save()
        await app.workers.wait_for_complete()
        assert captured["per_handle_home"] is False
        # Running workspace, but a layout flip applies from the next
        # connect — never a restart-needed field.
        assert not isinstance(app.screen, ConfirmScreen)


async def test_edit_screen_restart_needed_when_egress_mode_changed(
    monkeypatch,
):
    """#2409: egress_mode is a container-create-time field, so changing it on
    a running workspace offers a restart (parity with image/mounts)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    restarted = []
    ws = _wsobj("alpha", egress_mode="interactive", running=True)
    app = KlangkApp(
        _edit_state(ws, restart=lambda *a, **k: restarted.append(a))
    )
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#egress_mode", Select).value = "static"
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)  # restart offered


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


async def test_edit_screen_nix_hidden_when_not_available(monkeypatch):
    """#2233: the Mount /nix dir toggle is hidden in edit unless the server
    reports nix_available."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha")
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)  # nix_available defaults False
        await pilot.pause()
        cb = app.screen.query_one("#nix", Checkbox)
        assert cb.display is False
        assert cb.disabled is True


async def test_edit_screen_nix_prepopulated_and_sent(monkeypatch):
    """#2233: the edit toggle reflects settings.nix and sends it on save."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", settings={"nix": True})
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws, nix_available=True)
        await pilot.pause()
        es = app.screen
        nix = es.query_one("#nix", Checkbox)
        assert nix.display is True
        assert nix.value is True  # pre-populated from settings.nix
        es._save()
        await app.workers.wait_for_complete()
        assert captured["settings"] == {"nix": True}


async def test_edit_screen_nix_off_clears_setting(monkeypatch):
    """#2233: unchecking nix emits an explicit settings.nix=False so the
    full-replace PUT actually clears the mount — omitting the key would
    leave the stale bag (a silent no-op)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", settings={"nix": True})
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws, nix_available=True)
        await pilot.pause()
        es = app.screen
        nix = es.query_one("#nix", Checkbox)
        assert nix.value is True  # pre-populated from settings.nix
        nix.value = False  # turn it off
        es._save()
        await app.workers.wait_for_complete()
        assert captured["settings"] == {"nix": False}


async def test_edit_screen_nix_preserves_unmanaged_settings(monkeypatch):
    """#2234 re-review: PUT settings is a full-replace bag. With a nix
    backend configured the save now always emits settings, so it must seed
    from the existing bag to preserve API-only keys the form does not
    represent (e.g. bridge_timeout) instead of silently wiping them."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    captured = {}

    def update(wid, **f):
        captured["id"] = wid
        captured.update(f)

    ws = _wsobj("alpha", settings={"bridge_timeout": 60, "nix": False})
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws, nix_available=True)
        await pilot.pause()
        es = app.screen
        # leave the nix checkbox untouched (pre-populated False) and save
        es._save()
        await app.workers.wait_for_complete()
        assert captured["settings"]["bridge_timeout"] == 60
        assert captured["settings"]["nix"] is False


async def test_edit_screen_nix_off_prompts_restart(monkeypatch):
    """#2234 re-review: turning nix OFF on a running workspace must also
    offer a restart (the /nix mount is created at container create time, so
    unmounting needs a restart) — symmetric to turning it on."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", running=True, settings={"nix": True})
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws, nix_available=True)
        await pilot.pause()
        es = app.screen
        es.query_one("#nix", Checkbox).value = False  # turn nix off
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)  # restart offered


async def test_edit_screen_nix_change_prompts_restart(monkeypatch):
    """#2233: toggling nix on a running workspace prompts a restart (the
    /nix mount is set up at container create time)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", image="base", running=True)  # nix off in stored bag
    app = KlangkApp(_edit_state(ws))
    async with app.run_test() as pilot:
        _edit_screen(app, ws, nix_available=True)
        await pilot.pause()
        es = app.screen
        es.query_one("#nix", Checkbox).value = True  # turn nix on
        es._save()
        await app.workers.wait_for_complete()
        assert isinstance(app.screen, ConfirmScreen)  # restart offered


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
        await pilot.pause()
        # AuthError surfaces the app-wide overlay, not an inline form message (#2025).
        assert isinstance(app.screen, SessionExpiredScreen)


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
    monkeypatch.setattr("klangk.cli.authcmds.do_logout", lambda *a, **k: None)
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


async def test_session_expired_shows_overlay_then_redirects(monkeypatch):
    """session_expired() shows the overlay; acknowledging it logs out and
    redirects to login (#2025)."""

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
        await pilot.pause()
        # The overlay is shown app-wide; no logout yet.
        assert isinstance(app.screen, SessionExpiredScreen)
        assert logged_out == []
        # Acknowledge (Enter) — logs out and redirects to login.
        await pilot.press("enter")
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
        await _real_status_loop(app.screen)
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
        await _real_status_loop(app.screen)
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
        await _real_token_refresh_loop(app.screen)
    assert fired


async def test_session_expired_is_re_entrant_safe(monkeypatch):
    """Concurrent session_expired() calls show the overlay exactly once."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    st.logout = lambda: None  # avoid real credential I/O
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.session_expired()  # first call: sets _expiring, pushes overlay
        app.session_expired()  # second call: bails on _expiring
        await pilot.pause()
        overlays = [
            s for s in app.screen_stack if isinstance(s, SessionExpiredScreen)
        ]
        assert len(overlays) == 1


async def test_session_expired_overlay_esc_redirects(monkeypatch):
    """Esc on the overlay logs out and redirects to login (#2025)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    logged_out = []
    st.logout = lambda: logged_out.append(True)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.session_expired()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert logged_out == [True]


async def test_session_expired_overlay_button_redirects(monkeypatch):
    """The 'Log in again' button logs out and redirects to login (#2025)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    logged_out = []
    st.logout = lambda: logged_out.append(True)
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.session_expired()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)
        await pilot.press("enter")  # button is focused on mount
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert logged_out == [True]


async def test_session_expired_overlay_covers_any_active_page(monkeypatch):
    """The overlay lands on top of whatever screen is active — not just the
    workspaces page — so an expired session is signalled everywhere (#2025)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        # Navigate "away" from the workspaces page onto a detail page.
        from textual.screen import Screen as _Screen

        detail = _Screen()
        app.push_screen(detail)
        await pilot.pause()
        assert app.screen is detail
        # Session expires; the overlay is pushed on TOP of the detail page.
        app.session_expired()
        await pilot.pause()
        assert isinstance(app.screen, SessionExpiredScreen)
        # The detail page is still in the stack, underneath the overlay.
        assert detail in app.screen_stack


# --- safe screen-stack teardown (#2034) ---
# confirm_session_expired / server_changed / server_changed_needs_login used
# to pop screens in a ``while top is not X: pop_screen()`` loop, which is
# fragile (a side effect that pushes a screen mid-teardown can extend or
# loop it) and crashes (ScreenStackError) when the target screen isn't in
# the stack. They now route through KlangkApp._pop_above, a snapshot-guarded
# helper. These tests pin the new behavior.


async def test_pop_above_returns_false_when_target_absent(monkeypatch):
    """_pop_above is a no-op (returns False) when target isn't on the stack."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    from textual.screen import Screen as _Screen

    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        before = list(app.screen_stack)
        result = app._pop_above(_Screen())  # never pushed -> absent
        assert result is False
        assert app.screen_stack == before  # nothing popped


async def test_pop_above_stops_when_top_changes_mid_teardown(monkeypatch):
    """If the live top is no longer the screen ``_pop_above`` planned to pop
    next, it stops instead of popping a screen it didn't plan to remove.

    ``pop_screen`` is synchronous, so a real call never changes the top out
    from under the loop; the condition is forced here by stubbing
    ``pop_screen`` to push a sentinel, which is the only way to exercise the
    defensive guard. It documents the guard's contract, not a production
    path (#2034)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    from textual.screen import Screen as _Screen

    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Stack: [base, MainScreen, a, b]; tear down to MainScreen.
        a = _Screen()
        b = _Screen()
        app.push_screen(a)
        app.push_screen(b)
        await pilot.pause()
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        sentinel = _Screen()
        original = app.pop_screen
        calls = {"n": 0}

        def patched():
            result = original()
            calls["n"] += 1
            if calls["n"] == 1:
                # Force the guard's condition: a new screen lands on top.
                app.push_screen(sentinel)
            return result

        monkeypatch.setattr(app, "pop_screen", patched)
        result = app._pop_above(main)
        # b was popped, then sentinel was pushed -> the top is no longer the
        # planned 'a', so the loop stops with MainScreen NOT exposed.
        assert result is False
        assert sentinel in app.screen_stack
        assert a in app.screen_stack  # not torn down
        assert b not in app.screen_stack


async def test_server_changed_pops_to_main_and_refreshes(monkeypatch):
    """server_changed pops back to MainScreen and refreshes it."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    refreshed = []
    monkeypatch.setattr(
        MainScreen, "refresh_lists", lambda self: refreshed.append(True)
    )
    reloaded = []
    monkeypatch.setattr(
        MainScreen, "reload_last_login", lambda self: reloaded.append(True)
    )
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        assert isinstance(app.screen, ServerSwitchScreen)
        app.server_changed()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert not any(
            isinstance(s, ServerSwitchScreen) for s in app.screen_stack
        )
        assert refreshed  # MainScreen refreshed after the switch
        assert reloaded  # last-login stamp re-fetched for the new identity


async def test_server_changed_clears_stack_when_main_absent(monkeypatch):
    """When no MainScreen is in the stack, server_changed clears down to the
    base and pushes a fresh MainScreen — it does NOT strand the screens below
    it (#2034).

    This path is reachable: the server-switch / add-server workers are
    fire-and-forget and aren't cancelled when their screen is popped, so one
    can resume after a concurrent session-expiry teardown has removed the
    MainScreen, then call server_changed(). Pushing on top of the leftover
    login screen would strand it and corrupt the next logout."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate the race: a session-expiry teardown leaves the stack at
        # [base, LoginScreen] (no MainScreen) when the late switch worker
        # calls server_changed().
        app.server_changed_needs_login()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert not any(isinstance(s, MainScreen) for s in app.screen_stack)
        app.server_changed()
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        # The login screen was cleared, not stranded underneath MainScreen.
        assert not any(isinstance(s, LoginScreen) for s in app.screen_stack)
        # Final stack is exactly [base, MainScreen].
        assert len(app.screen_stack) == 2


async def test_server_changed_needs_login_clears_to_login(monkeypatch):
    """server_changed_needs_login tears down MainScreen + any modals and
    lands on LoginScreen."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ServerSwitchScreen())
        await pilot.pause()
        app.server_changed_needs_login()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert not any(isinstance(s, MainScreen) for s in app.screen_stack)
        assert not any(
            isinstance(s, ServerSwitchScreen) for s in app.screen_stack
        )


async def test_confirm_session_expired_clears_every_screen_above_base(
    monkeypatch,
):
    """Acknowledging the expiry overlay tears down every screen above the
    base (not just one), landing cleanly on login (#2034)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    monkeypatch.setattr(scr_main, "run_token_refresh_loop", noop)
    st = _authed_state()
    st.logout = lambda: None  # no real credential I/O
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.screen import Screen as _Screen

        # Stack: [base, MainScreen, detail, sub].
        detail = _Screen()
        sub = _Screen()
        app.push_screen(detail)
        app.push_screen(sub)
        await pilot.pause()
        app.session_expired()
        await pilot.pause()
        await pilot.press("enter")  # acknowledge the overlay
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        # Everything above the base is gone.
        assert detail not in app.screen_stack
        assert sub not in app.screen_stack
        assert not any(isinstance(s, MainScreen) for s in app.screen_stack)


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


async def test_create_screen_rejected_domains_editor(monkeypatch):
    """#2386: the create form's rejected-domains editor reaches create_workspace."""

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
        cs.query_one("#name", Input).value = "myws"
        tabs = cs.query_one("#form_tabs", TabbedContent)
        tabs.active = "netfilter_pane"
        await pilot.pause()
        # Add a rejected domain via the Add button handler (#2386).
        cs.query_one("#reject_input", Input).value = "evil.example.com"
        cs.on_button_pressed(FakeBtnPress("add_reject"))
        await pilot.pause()
        assert cs._rejected_domains == ["evil.example.com"]
        # A CIDR is rejected client-side (name-level NXDOMAIN deny-list).
        cs.query_one("#reject_input", Input).value = "10.0.0.0/8"
        cs.on_button_pressed(FakeBtnPress("add_reject"))
        await pilot.pause()
        assert cs._rejected_domains == ["evil.example.com"]  # unchanged
        assert cs.query_one("#reject_input", Input).value == "10.0.0.0/8"
        # Remove via the Remove button handler, then re-add.
        cs.query_one("#reject_list", OptionList).highlighted = 0
        cs.on_button_pressed(FakeBtnPress("rm_reject"))
        await pilot.pause()
        assert cs._rejected_domains == []
        cs.query_one("#reject_input", Input).value = "bad.example.com"
        cs.on_button_pressed(FakeBtnPress("add_reject"))
        await pilot.pause()
        # Edge branches: empty input is a no-op; Enter in the input adds too.
        cs.query_one("#reject_input", Input).value = ""
        cs._add_rejected_domain()
        assert cs._rejected_domains == ["bad.example.com"]
        rinp = cs.query_one("#reject_input", Input)
        rinp.value = "extra.example.com"
        cs.on_input_submitted(Input.Submitted(input=rinp, value=rinp.value))
        await pilot.pause()
        assert cs._rejected_domains == ["bad.example.com", "extra.example.com"]
        # Remove with nothing highlighted is a no-op; then remove index 0.
        cs.query_one("#reject_list", OptionList).highlighted = None
        cs.on_button_pressed(FakeBtnPress("rm_reject"))
        assert cs._rejected_domains == ["bad.example.com", "extra.example.com"]
        cs.query_one("#reject_list", OptionList).highlighted = 0
        cs.on_button_pressed(FakeBtnPress("rm_reject"))
        await pilot.pause()
        assert cs._rejected_domains == ["extra.example.com"]
        # Create from a shorter pane so #create is in view (#1891).
        tabs.active = "advanced_pane"
        await pilot.pause()
        assert await pilot.click("#create")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert captured["k"]["rejected_domains"] == ["extra.example.com"]


async def test_edit_screen_rejected_domains_editor(monkeypatch):
    """#2386: the edit form's rejected-domains editor (add/remove/edit) reaches
    the PUT body, and the focus-aware Delete/Edit dispatches to the reject list."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", running=False, rejected_domains=["old.example.com"])
    captured = {}

    def fake_update(*a, **k):
        captured["k"] = k

    app = KlangkApp(_edit_state(ws, update=fake_update))
    async with app.run_test() as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        assert es._rejected_domains == ["old.example.com"]
        tabs = es.query_one("#form_tabs", TabbedContent)
        tabs.active = "netfilter_pane"
        await pilot.pause()
        # Edit-in-place: 'e' on the focused reject list loads the entry.
        rl = es.query_one("#reject_list", OptionList)
        rl.highlighted = 0
        rl.focus()
        await pilot.pause()
        # Focus-aware dispatch: _list_handlers picks the reject pair when the
        # reject list owns focus; 'e' then runs _edit_rejected_domain.
        assert es._list_handlers() == (
            "_remove_rejected_domain",
            "_edit_rejected_domain",
        )
        es.action_edit_item()
        await pilot.pause()
        assert es._editing_reject == 0
        assert es.query_one("#reject_input", Input).value == "old.example.com"
        es.query_one("#reject_input", Input).value = "new.example.com"
        es.on_button_pressed(FakeBtnPress("add_reject"))  # Add replaces it
        await pilot.pause()
        assert es._rejected_domains == ["new.example.com"]
        # Edge branches: empty input no-op; a CIDR is rejected; a plain append
        # (no edit in progress) hits the append branch; Enter in the input adds.
        es.query_one("#reject_input", Input).value = ""
        es._add_rejected_domain()
        assert es._rejected_domains == ["new.example.com"]
        es.query_one("#reject_input", Input).value = "10.0.0.0/8"
        es._add_rejected_domain()
        assert es._rejected_domains == ["new.example.com"]  # CIDR rejected
        erinp = es.query_one("#reject_input", Input)
        erinp.value = "added.example.com"
        es.on_input_submitted(Input.Submitted(input=erinp, value=erinp.value))
        await pilot.pause()
        assert es._rejected_domains == ["new.example.com", "added.example.com"]
        # Remove/edit with nothing highlighted are no-ops; then button-remove.
        es.query_one("#reject_list", OptionList).highlighted = None
        es._remove_rejected_domain()
        es._edit_rejected_domain()
        assert es._rejected_domains == ["new.example.com", "added.example.com"]
        es.query_one("#reject_list", OptionList).highlighted = 0
        es.on_button_pressed(FakeBtnPress("rm_reject"))
        await pilot.pause()
        assert es._rejected_domains == ["added.example.com"]
        # Delete (focus-aware) removes the focused reject entry.
        es.query_one("#reject_list", OptionList).highlighted = 0
        es.query_one("#reject_list", OptionList).focus()  # reclaim focus
        await pilot.pause()
        es.action_remove_item()
        await pilot.pause()
        assert es._rejected_domains == []
        # animate=False: scroll_visible() animates by default, and a single
        # pilot.pause() doesn't reliably complete the scroll animation before
        # the click — the button stays below the fold and the click misses.
        # Deterministic on the slower-scheduled macOS runner (see #2426).
        es.query_one("#save").scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click("#save")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert captured["k"]["rejected_domains"] is None


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
        # animate=False so the scroll completes within one pilot.pause();
        # the default animated scroll races the click (#2426).
        es.query_one("#save").scroll_visible(animate=False)
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


def _cheatsheet_text(screen) -> str:
    """Join every rendered key/desc/group cell of a CheatsheetScreen."""
    cells = []
    for sel in (".cs_group", ".cs_key", ".cs_desc"):
        for w in screen.query(sel):
            cells.append(str(w.render()))
    return " | ".join(cells)


async def test_main_screen_cheatsheet_modal():
    """`?` on the workspace list opens a cheatsheet of MainScreen bindings;
    Escape or `?` again dismisses it (#1802)."""
    a = _wsobj("alpha", running=True, service_started_at=1.0)
    app = KlangkApp(_ws(owned=[a]))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, CheatsheetScreen)
        text = _cheatsheet_text(app.screen)
        # Navigation + workspaces + highlighted-row bindings all shown.
        for key in (
            "↑ ↓",
            "Tab",
            "Enter",
            "/",
            "c",
            "n",
            "i",
            "o",
            "l",
            "e",
            "r",
            "s",
            "u",
            "d",
        ):
            assert key in text, f"missing {key!r} in cheatsheet"
        assert "Open the highlighted workspace" in text

        # Escape dismisses.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

        # `?` opens again, and `?` again dismisses.
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, CheatsheetScreen)
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_detail_screen_cheatsheet_modal():
    """`?` on the detail screen opens a cheatsheet of WorkspaceDetailScreen
    bindings; Escape dismisses (#1802). Also confirms the `?` binding survives
    the per-display BINDINGS rebuild in _display()."""
    a = _wsobj("alpha", running=True, service_started_at=1.0)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, CheatsheetScreen)
        text = _cheatsheet_text(app.screen)
        for key in (
            "Esc",
            "Enter",
            "e",
            "r",
            "s",
            "u",
            "d",
            "x",
            "n",
            "m",
            "t",
        ):
            assert key in text, f"missing {key!r} in cheatsheet"
        assert "Back to workspace list" in text

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, WorkspaceDetailScreen)


def test_render_detail_indents_wrapped_values_to_value_column():
    """A long value folds onto continuation lines aligned under the value
    column (a hanging indent), not back at column 0 under the label (#2190)."""
    rows = [
        (
            "service command",
            "cd /app/meetmin && devenv shell -- meetmin-ingest-multi "
            "--root /app/custrag/ --watch --workers 4",
        )
    ]
    out = WorkspaceDetailScreen._render_detail(rows, 60)
    # _render_detail emits ANSI (zebra row backgrounds + bold labels, #2193);
    # strip it so these assertions check column layout, not styling.
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in out.splitlines()]
    # First line: label, then the start of the value.
    assert lines[0].startswith("service command  cd /app/meetmin")
    # The value wrapped onto further lines...
    assert len(lines) > 1
    # ...and each continuation line is indented into the value column
    # (past the label column + padding), never back at column 0.
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    for cont in lines[1:]:
        assert cont.startswith(" " * indent), (
            f"continuation not aligned to value column: {cont!r}"
        )
        assert cont == cont.rstrip(), (
            f"trailing whitespace would risk a re-wrap: {cont!r}"
        )


async def test_detail_display_renders_at_body_width_not_screen_width(
    monkeypatch,
):
    """#detail_body is narrower than the screen (horizontal chrome); _display
    must render the detail table at the body's content width, or the Static
    re-wraps the pre-folded lines and drops the value-column indent (#2190)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj(
        "alpha",
        running=True,
        service_command=(
            "cd /app/meetmin && devenv shell -- meetmin-ingest-multi "
            "--root /app/custrag/ --watch --workers 4"
        ),
    )
    st = _ws()
    st.find_workspace = lambda n: a
    captured = {}
    real = WorkspaceDetailScreen._render_detail

    def spy(rows, width):
        captured["width"] = width
        return real(rows, width)

    monkeypatch.setattr(
        WorkspaceDetailScreen, "_render_detail", staticmethod(spy)
    )
    app = KlangkApp(st)
    async with app.run_test(size=(130, 40)) as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.screen._load()
        await pilot.pause()
        app.screen._display()
        await pilot.pause()
        body = app.screen.query_one("#detail_body", Static)
        screen_w = app.screen.size.width
        body_w = body.container_size.width
    # Rendered at the body's width, and the body is narrower than the screen
    # (otherwise this regression can't occur and the test is meaningless).
    assert captured["width"] == body_w
    assert body_w < screen_w


def test_render_detail_zebra_stripes():
    """The workspace-detail table zebra-stripes alternating rows; a
    multi-line value inherits its row's stripe on every wrapped line; and
    markup-like values (e.g. "[img]") stay literal through the ANSI
    round-trip into the Static (#2193)."""
    rows = [
        ("id", "abc"),  # even -> base
        ("running", "yes"),  # odd  -> stripe
        ("image", "[img]"),  # even -> base (markup-like value)
        ("mounts", "/a\n/b"),  # odd  -> stripe (multi-line)
    ]
    out = WorkspaceDetailScreen._render_detail(rows, width=40)
    # #161B22 == rgb(22, 27, 34); the truecolor background params, robust to
    # how Rich groups the escape sequence.
    bg = "48;2;22;27;34"
    lines = out.splitlines()
    striped = [ln for ln in lines if bg in ln]
    plain = [ln for ln in lines if bg not in ln]
    # "running" (1 line) + "mounts" (2 wrapped lines) are striped = 3;
    # "id" (1) + "image" (1) are base = 2. The multi-line value shares the
    # row's stripe on both wrapped lines.
    assert len(striped) == 3
    assert len(plain) == 2
    # markup safety: "[img]" is not eaten as Textual/Rich markup by from_ansi
    assert "[img]" in Text.from_ansi(out).plain


def test_render_detail_label_column_bold_and_right_aligned():
    """Key names are bold and right-aligned; values are neither (#2193)."""
    rows = [("id", "abc"), ("running", "yes"), ("uptime", "2h 0m")]
    out = WorkspaceDetailScreen._render_detail(rows, width=40)
    t = Text.from_ansi(out)

    def is_bold_at(substr: str) -> bool:
        idx = t.plain.index(substr)
        end = idx + len(substr)
        return any(
            s.style and s.style.bold and not (end <= s.start or idx >= s.end)
            for s in t.spans
        )

    # labels are bold, values are not
    assert is_bold_at("id")
    assert is_bold_at("running")
    assert is_bold_at("uptime")
    assert not is_bold_at("abc")
    assert not is_bold_at("yes")

    # right-aligned: every label's right edge sits at the same column
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    end_cols = set()
    for lab in ("id", "running", "uptime"):
        pos = plain.index(lab)
        line_start = plain.rfind("\n", 0, pos) + 1
        end_cols.add(pos + len(lab) - line_start)
    assert len(end_cols) == 1


async def test_create_screen_collects_settings(monkeypatch):
    """Resource fields on the create form populate the settings dict (#2217)."""
    from klangk.cli.tui.screens.workspace_form import _collect_settings

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.push_screen(
            CreateWorkspaceScreen(
                allowed=["base"], default="base", allow_autostart=True
            )
        )
        await pilot.pause()
        cs = app.screen
        # Empty fields → None
        assert _collect_settings(cs) is None
        # Fill in resource fields
        cs.query_one("#idle_timeout", Input).value = "600"
        cs.query_one("#cpu_limit", Input).value = "1.5"
        cs.query_one("#memory_limit", Input).value = "4g"
        cs.query_one("#pids_limit", Input).value = "256"
        cs.query_one("#tmp_size", Input).value = "2g"
        result = _collect_settings(cs)
        assert result == {
            "idle_timeout": 600,
            "cpu_limit": 1.5,
            "memory_limit": "4g",
            "pids_limit": 256,
            "tmp_size": "2g",
        }


async def test_edit_screen_save_includes_settings(monkeypatch):
    """Edit form includes settings dict in update body when filled (#2217)."""

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
        es.query_one("#cpu_limit", Input).value = "2.0"
        es.query_one("#pids_limit", Input).value = "512"
        es.query_one("#tmp_size", Input).value = "1g"
        es._save()
        await app.workers.wait_for_complete()
        assert captured["settings"] == {
            "cpu_limit": 2.0,
            "pids_limit": 512,
            "tmp_size": "1g",
        }


async def test_edit_screen_prepopulates_settings(monkeypatch):
    """Edit form pre-populates resource fields from workspace settings (#2217)."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj(
        "test-ws",
        settings={"cpu_limit": 2.0, "idle_timeout": 300, "tmp_size": "3g"},
    )
    app = KlangkApp(_create_state())
    async with app.run_test(size=(140, 40)) as pilot:
        app.push_screen(
            EditWorkspaceScreen(
                workspace=ws,
                allowed=["base"],
                default="base",
                allow_autostart=True,
            )
        )
        await pilot.pause()
        es = app.screen
        assert es.query_one("#cpu_limit", Input).value == "2.0"
        assert es.query_one("#idle_timeout", Input).value == "300"
        assert es.query_one("#tmp_size", Input).value == "3g"
        assert es.query_one("#memory_limit", Input).value == ""
        assert es.query_one("#pids_limit", Input).value == ""


async def test_main_screen_server_schedule_events(monkeypatch):
    """#2661: pending server schedules render as a status line with fire
    time + remaining; an empty snapshot clears it; firing notifies."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        screen = app.screen
        fire_at = (
            datetime.now(timezone.utc) + timedelta(hours=1, minutes=5)
        ).isoformat()
        screen._on_status_event(
            {
                "type": "server_schedule",
                "schedules": [{"action": "stop", "fire_at": fire_at}],
            }
        )
        await pilot.pause()
        assert app.live_extra.startswith("server: stop at ")
        assert re.search(r"\(in 1h \d+m\)", app.live_extra)
        # Empty snapshot clears the server: line only.
        screen._on_status_event({"type": "server_schedule", "schedules": []})
        await pilot.pause()
        assert app.live_extra == ""
        # A non-server line is never clobbered by an empty snapshot.
        app.live_extra = "live: other"
        screen._on_status_event({"type": "server_schedule", "schedules": []})
        await pilot.pause()
        assert app.live_extra == "live: other"
        # Firing warns.
        screen._on_status_event(
            {"type": "server_schedule_fired", "action": "stop"}
        )
        await pilot.pause()
        assert app.live_extra == "server: scheduled stop running"


def test_server_schedule_line_formats():
    soon = (
        datetime.now(timezone.utc) + timedelta(minutes=2, seconds=30)
    ).isoformat()
    line = scr_main._server_schedule_line(
        {"action": "recycle", "fire_at": soon}
    )
    assert line.startswith("server: recycle at ")
    assert "(in 2m)" in line
    hours = (
        datetime.now(timezone.utc) + timedelta(hours=2, minutes=3, seconds=45)
    ).isoformat()
    assert "(in 2h 3m)" in scr_main._server_schedule_line(
        {"action": "stop", "fire_at": hours}
    )
    # Bad/absent fire_at degrades to a static line, never raises.
    assert (
        scr_main._server_schedule_line({"action": "stop", "fire_at": "x"})
        == "server: stop scheduled"
    )
    assert (
        scr_main._server_schedule_line({"action": "recycle"})
        == "server: recycle scheduled"
    )
    # Naive (no-tz) fire_at is treated as local time, not rejected.
    naive = (datetime.now() + timedelta(minutes=2, seconds=30)).isoformat()
    assert "(in 2m)" in scr_main._server_schedule_line(
        {"action": "stop", "fire_at": naive}
    )
    # Sub-minute remaining renders seconds.
    seconds = (datetime.now(timezone.utc) + timedelta(seconds=45)).isoformat()
    assert re.search(
        r"\(in 4\ds\)",
        scr_main._server_schedule_line({"action": "stop", "fire_at": seconds}),
    )


async def test_mount_does_not_cancel_status_ws_worker(monkeypatch):
    """#2612 regression: the mount-time ``last-login`` worker must not
    cancel the status-WS / token-refresh workers.

    ``last-login`` runs ``exclusive=True``; in textual, exclusivity is
    per (node, group). All three workers are spawned on the app node, so
    leaving them in the default group made the last-login spawn cancel
    the just-started status-WS and token-refresh loops — the status WS
    never ran, so no live events (and none of #2052's reachability
    machinery) worked. The last-login worker now uses its own group.
    """

    flags = {"entered": False, "cancelled": False}

    async def slow_status_loop(self):
        flags["entered"] = True
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            flags["cancelled"] = True
            raise

    monkeypatch.setattr(MainScreen, "_status_loop", slow_status_loop)

    app = KlangkApp(_authed_state())
    async with app.run_test() as pilot:
        # Let the mount-time workers start and a few loop ticks pass —
        # the old bug cancelled status-ws during on_mount's own
        # last-login spawn.
        for _ in range(5):
            await pilot.pause()
            await asyncio.sleep(0.05)
        assert flags["entered"], "status-ws worker never started"
        assert not flags["cancelled"], (
            "status-ws worker was cancelled at mount (last-login "
            "exclusive in the default group — #2612 regression)"
        )
        running = [w.name for w in app.workers if w.is_running]
        assert "status-ws" in running


# ---------------------------------------------------------------------------
# #2029 audit: async blocking / correctness / robustness / performance
# ---------------------------------------------------------------------------


async def test_status_loop_rereads_url_after_server_switch(monkeypatch):
    """#2029: a server switch reuses the MainScreen (no re-mount), so the
    status-WS loop must re-read the url every iteration. A url pinned at
    mount kept dialing the OLD server with the NEW server's token — a
    guaranteed auth reject, 25 reconnect attempts, and a false "server
    down" overlay while REST on the new server worked fine."""
    await _fast_reconnect(monkeypatch)
    box = {"url": "https://a.example", "token": "tok"}
    dialed: list[str] = []

    async def capture(url, token, **k):
        dialed.append(url)
        if len(dialed) == 1:
            # A server switch lands mid-run (App.server_changed reuses this
            # screen; on_mount does not re-fire).
            box["url"] = "https://b.example"
        if len(dialed) >= 3:
            box["token"] = None  # end the loop on the next iteration
        return None  # clean close

    monkeypatch.setattr(scr_main, "listen_for_status", capture)
    app = KlangkApp(
        _authed_state(
            current_url=lambda: box["url"],
            token=lambda: box["token"],
        )
    )
    expired: list = []
    monkeypatch.setattr(app, "session_expired", lambda: expired.append(1))
    async with app.run_test() as pilot:
        main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
        await _real_status_loop(main)
        await pilot.pause()
    assert dialed[0] == "https://a.example"
    # Every dial after the switch targets the NEW server.
    assert dialed[1:] == ["https://b.example", "https://b.example"]
    assert expired  # loop exited via the token-drop guard


async def test_detail_reload_on_status_pops_modal_above(monkeypatch):
    """#2029: when the workspace is deleted out from under an open detail
    screen, the reload must pop any modal ABOVE it first — a bare
    pop_screen() dismissed only the modal and left the dead detail page
    mounted underneath."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
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
        assert isinstance(d, WorkspaceDetailScreen)
        app.push_screen(ConfirmScreen("Really?"))
        await pilot.pause()

        def gone(n):
            raise WorkspaceNotFoundError("gone")

        st.find_workspace = gone
        d.apply_status_event({"type": "workspaces_changed"})
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Both the modal AND the detail screen are gone; list on top.
        assert not any(
            isinstance(s, WorkspaceDetailScreen) for s in app.screen_stack
        )
        assert not any(isinstance(s, ConfirmScreen) for s in app.screen_stack)
        assert isinstance(app.screen, MainScreen)


async def test_detail_status_event_ignores_string_started_at(monkeypatch):
    """#2029: a malformed (string) service_started_at payload must not be
    adopted — it would crash the uptime math (int(time.time() - str))."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
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
        # Malformed string stamp: not adopted, no crash on _display().
        d.apply_status_event(
            {
                "type": "container_status",
                "running": True,
                "service_started_at": "2026-01-01T00:00:00",
            }
        )
        await pilot.pause()
        assert d._ws.service_started_at is None
        # A well-formed numeric stamp still adopts.
        d.apply_status_event(
            {
                "type": "container_status",
                "running": True,
                "service_started_at": 1234.5,
            }
        )
        await pilot.pause()
        assert d._ws.service_started_at == 1234.5


async def test_create_screen_settings_validation_inline_error(monkeypatch):
    """#2029: non-numeric resource inputs show an inline error instead of
    crashing the app out of the button handler."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    created: list = []

    def create(*a, **k):
        created.append((a, k))
        return _wsobj("made")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name", Input).value = "valid-name"
        # Cycle each numeric field through garbage: each names its field.
        for field, label in (
            ("#idle_timeout", "Idle timeout"),
            ("#cpu_limit", "CPU limit"),
            ("#pids_limit", "PIDs limit"),
        ):
            cs.query_one(field, Input).value = "garbage"
            cs._create()
            await pilot.pause()
            assert created == []  # never submitted
            assert label in str(
                cs.query_one("#create_msg", Static).render()
            ), f"{field} error did not name its field"
            cs.query_one(field, Input).value = ""


async def test_edit_screen_settings_validation_inline_error(monkeypatch):
    """#2029: same validation on the edit form's _save path."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    updated: dict = {}

    def update(wid, **f):
        updated["id"] = wid
        updated.update(f)

    ws = _wsobj("alpha", image="base", running=False)
    app = KlangkApp(_edit_state(ws, update=update))
    async with app.run_test(size=(140, 40)) as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        es.query_one("#cpu_limit", Input).value = "fast"
        es._save()
        await app.workers.wait_for_complete()
        assert updated == {}  # never submitted
        assert "CPU limit" in str(es.query_one("#edit_msg", Static).render())


async def test_listen_for_status_isolates_callback_errors(monkeypatch):
    """#2029: a bug in the UI's on_event/on_connect must not tear down the
    status WS — an exception escaping the listener reads as a connection
    loss to _status_loop and would churn reconnects forever."""
    got: list[str] = []
    frames = [
        '{"type": "a"}',
        '{"type": "b"}',
        '{"type": "c"}',
    ]
    monkeypatch.setattr(
        ws_mod, "ws_connect", lambda *a, **k: FakeCM(FakeWS(frames))
    )

    def bad_connect():
        raise RuntimeError("connect bug")

    def on_event(ev):
        got.append(ev["type"])
        if ev["type"] == "a":
            raise RuntimeError("ui bug")

    await listen_for_status(
        "/sock", "tok", on_event=on_event, on_connect=bad_connect
    )
    # The failing callback was isolated; later frames still delivered.
    assert got == ["a", "b", "c"]


async def test_login_oidc_malformed_provider_degrades(monkeypatch):
    """#2029: a malformed provider payload (non-dict entry, missing,
    non-string, or empty id) degrades to the "no provider" message instead
    of a KeyError crash or a bogus dial. (The well-formed handoff is covered
    by test_login_oidc_flow.)"""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    called: list = []
    st = _st(
        is_authenticated=lambda: False,
        auth_mode=lambda: "password",
        current_url=lambda: "https://x.example",
        email=lambda: None,
        token=lambda: None,
        oidc_providers=lambda: [
            "garbage",
            {"no_id": True},
            {"id": 42},
            {"id": ""},
        ],
        oidc_login=lambda pid: called.append(pid),
    )
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, LoginScreen)
        await screen._do_login_oidc()
        await pilot.pause()
        assert "No SSO provider configured." in str(
            screen.query_one("#message", Static).render()
        )
        assert called == []  # never handed a bogus provider id


def test_state_stamp_cache_serves_same_object(redirect_xdg):
    """#2029: repeated state() calls between writes hit the stamp cache —
    the StatusBar refresh path reads it 3+ times per event on the UI
    thread, and each used to be a full read+parse of the state YAML."""
    st = TuiState()
    s1 = st.state()
    s2 = st.state()
    assert s1 is s2


def test_state_stamp_cache_reloads_on_external_write(redirect_xdg):
    """#2029: an external write (a concurrent `klangk login`) changes the
    stamp, so the next state() reloads — the freshness contract holds."""
    cpath, spath = redirect_xdg
    st = TuiState()
    s1 = st.state()
    ext = CLIState()
    ext.set_credentials("https://x.example", "u@x", "tok2")
    ext.save()
    s2 = st.state()
    assert s2 is not s1
    assert st.token() == "tok2"


def test_state_stamp_cache_sync_after_own_save(redirect_xdg):
    """#2029: our own save() writes straight back into the cache, so
    load->mutate->save->load yields the same object with new content."""
    st = TuiState()
    st.switch_server("https://a.example")
    s = st.state()
    assert s.active_server == "https://a.example"
    assert st.state() is s


def test_state_stamp_cache_reload_when_file_removed(redirect_xdg):
    """#2029: the file vanishing (stamp -> None) forces a reload that
    yields the default instance, not a stale cached copy."""
    cpath, spath = redirect_xdg
    st = TuiState()
    st.switch_server("https://a.example")
    spath.unlink()
    assert st.state().active_server is None


async def test_create_screen_rejects_nonfinite_cpu(monkeypatch):
    """#2029 review: NaN/Inf pass float() AND the server's positive check
    (NaN <= 0 is False), then podman rejects --cpus nan at container
    start — a cryptic failure long after submit. The form rejects them
    inline with a field-named error."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    created: list = []

    def create(*a, **k):
        created.append((a, k))
        return _wsobj("made")

    app = KlangkApp(_create_state(create=create))
    async with app.run_test(size=(140, 40)) as pilot:
        app.screen.action_create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        cs = app.screen
        cs.query_one("#name", Input).value = "valid-name"
        for bad in ("nan", "inf", "-inf", "NaN"):
            cs.query_one("#cpu_limit", Input).value = bad
            cs._create()
            await pilot.pause()
            assert created == []  # never submitted
            assert "CPU limit" in str(
                cs.query_one("#create_msg", Static).render()
            ), f"{bad} was not rejected inline"
        # A finite value still passes through to the request.
        cs.query_one("#cpu_limit", Input).value = "2.0"
        cs._create()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(created) == 1
        assert created[0][1]["settings"] == {"cpu_limit": 2.0}


def test_oidc_login_drops_stamp_cache_on_save_failure(
    monkeypatch, redirect_xdg
):
    """#2029 review: the browser flow mutates + saves the state object
    itself. If its save() fails the file never changed, but the mutated
    object IS the cached one — phantom credentials served forever.
    oidc_login must drop the cache either way so state() reloads exactly
    what is on disk."""

    def fake_flow(url, provider_id, state):
        # Mutate the shared object but do NOT save (simulates a failed
        # save: the file on disk is unchanged).
        state.set_credentials(url, "ghost@x", "ghost-token")
        return None

    monkeypatch.setattr(tui_state_mod, "_oidc_browser_login", fake_flow)
    st = TuiState("https://x.example")
    st.oidc_login("google")
    # Cache dropped: state() reloaded from disk and does NOT serve the
    # phantom credentials.
    assert st.token() is None
    assert st.email() is None


def test_oidc_login_keeps_credentials_after_successful_save(
    monkeypatch, redirect_xdg
):
    """The cache drop must not lose a genuinely saved login: after a
    successful browser flow (mutate + save), state() reloads the same
    credentials from disk."""

    def fake_flow(url, provider_id, state):
        state.set_credentials(url, "real@x", "real-token")
        state.save()

    monkeypatch.setattr(tui_state_mod, "_oidc_browser_login", fake_flow)
    st = TuiState("https://x.example")
    st.oidc_login("google")
    assert st.token() == "real-token"
    assert st.email() == "real@x"


# ---------------------------------------------------------------------------
# #2029 review round 2: stamp-cache serialization, save-failure drop,
# guarded dismiss/pop
# ---------------------------------------------------------------------------


def test_state_cache_serializes_concurrent_writers(monkeypatch, redirect_xdg):
    """#2029 r2: mutators run on worker threads while the UI thread reads,
    and an interleaved load->mutate->save->sync could pair one writer's
    stale object with another writer's fresh stamp — served forever. The
    whole mutator sequence is atomic under _state_lock, so writer C cannot
    complete (or even start its mutation) while writer B is parked mid-save;
    final cache and disk both reflect B-then-C."""
    b_in_save = threading.Event()
    release_b = threading.Event()
    real_save = CLIState.save

    def gated_save(self):
        if self.active_server == "https://b.example":
            b_in_save.set()
            assert release_b.wait(timeout=5)
        return real_save(self)

    monkeypatch.setattr(CLIState, "save", gated_save)
    st = TuiState()
    tb = threading.Thread(target=lambda: st.switch_server("https://b.example"))
    tb.start()
    assert b_in_save.wait(timeout=5)  # B holds the lock, parked in save()
    tc = threading.Thread(target=lambda: st.switch_server("https://c.example"))
    tc.start()
    time.sleep(0.2)  # give C every chance to (wrongly) get in
    release_b.set()  # let B finish; C must follow it
    tb.join(timeout=5)
    tc.join(timeout=5)
    assert not tb.is_alive() and not tc.is_alive()
    # Both serialized in order: disk and cache are v_C, consistent.
    assert CLIState.load().active_server == "https://c.example"
    assert st.state().active_server == "https://c.example"


def test_mutator_save_failure_drops_cache(monkeypatch, redirect_xdg):
    """#2029 r2: a failed save() in ANY mutator must drop the cache — the
    in-memory mutation exists nowhere on disk and must not be served as
    phantom state (the oidc_login rule, generalized)."""

    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(CLIState, "save", boom)
    st = TuiState("https://x.example")
    with pytest.raises(OSError):
        st.switch_server("https://y.example")
    # Cache dropped: state() reloads from disk — no phantom active server.
    assert st.state().active_server is None
    assert st.current_url() is None or st.current_url() != "https://y.example"


async def test_edit_save_dismiss_guarded_when_screen_already_popped(
    monkeypatch,
):
    """#2029 r2: Screen.dismiss unconditionally pops the top screen. If the
    edit form was popped underneath an in-flight save worker (workspace
    deleted + status reload), the unguarded dismiss ate the MainScreen and
    left a blank base. _safe_dismiss no-ops instead."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    ws = _wsobj("alpha", image="base", running=False)
    app = KlangkApp(_edit_state(ws))
    async with app.run_test(size=(140, 40)) as pilot:
        _edit_screen(app, ws)
        await pilot.pause()
        es = app.screen
        assert isinstance(es, EditWorkspaceScreen)
        # Form still on the stack: the guarded dismiss runs for real.
        es._safe_dismiss(True)
        await pilot.pause()
        assert es not in app.screen_stack
        # Form already popped: the guarded dismiss is a no-op — the
        # MainScreen underneath survives.
        _edit_screen(app, ws)
        await pilot.pause()
        es2 = app.screen
        app.pop_screen()  # external actor pops it first
        await pilot.pause()
        es2._safe_dismiss(True)
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)


async def test_detail_delete_pop_guarded_when_screen_already_popped(
    monkeypatch,
):
    """#2029 r2: the delete worker's own workspaces_changed broadcast can
    pop the detail screen before the worker resumes; the guarded pop must
    no-op instead of eating the MainScreen below."""

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(scr_main, "listen_for_status", noop)
    a = _wsobj("alpha", running=True)
    st = _ws(owned=[a])
    st.find_workspace = lambda n: a
    st.delete_workspace = lambda n: None
    app = KlangkApp(st)
    async with app.run_test() as pilot:
        app.push_screen(WorkspaceDetailScreen("alpha"))
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        d = app.screen
        assert isinstance(d, WorkspaceDetailScreen)
        app.pop_screen()  # external actor pops it first
        await pilot.pause()
        before = list(app.screen_stack)
        await d._do_delete()  # guarded pop no-ops
        await pilot.pause()
        assert list(app.screen_stack) == before
        assert isinstance(app.screen, MainScreen)
