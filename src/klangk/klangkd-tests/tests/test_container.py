"""Tests for container: idle timeout parsing, activity tracking, callbacks, port allocation."""

import asyncio
import os
import time
import types
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk import (
    auth as auth_mod,
    container,
    files as files_mod,
    nix as nix_mod,
    podman,
    ssl_trust,
    util as util_mod,
)
from klangk.container.spec import ensure_volumes, nix_binds
from klangk.model.container_events import CAUSE_API, CAUSE_DRAIN
from _helpers import make_settings


def _make_app_state(registry=None, sockets=None):
    """Build a minimal app_state for tests."""
    from klangk.podman import Podman
    from klangk.wshandler.session import WebSocketState

    settings = make_settings({})
    # Two-phase: build the namespace shell first so the owned instances
    # (sockets, registry, terminal, features) can take app_state at
    # construction and reach collaborators via self.app_state (#1426).
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    podman_inst = Podman(app_state)
    app_state.state.podman = podman_inst
    if sockets is None:
        sockets = WebSocketState(app_state)
    app_state.state.sockets = sockets
    if registry is None:
        registry = container.ContainerRegistry(app_state)
    app_state.state.container_registry = registry
    # #1480: container.py reaches set_workspace_token via app_state.state.terminal.
    from klangk.terminal import Terminal
    from klangk import features as features_mod

    app_state.state.terminal = Terminal(app_state)
    app_state.state.features = features_mod.Features(app_state)
    from klangk.workspaces import Workspaces

    app_state.state.workspaces = Workspaces(app_state)
    app_state.state.files = files_mod.Files(app_state)
    # #1503: container.py reaches derive_hosting_info via app_state.state.util.
    app_state.state.util = util_mod.Util(app_state)
    # #1567: ContainerRegistry reaches the cert-dir resolver via
    # app_state.state.ssl_trust (the settings-dependent SSL trust surface).
    app_state.state.ssl_trust = ssl_trust.SSLTrust(app_state)
    # #1365: ContainerRegistry reaches the egress-filter builder via
    # app_state.state.netfilter (the settings-dependent netfilter surface).
    from klangk import netfilter as netfilter_mod

    app_state.state.netfilter = netfilter_mod.NetFilter(app_state)
    # #2201: container start reaches app_state.state.nix for the /nix bind.
    app_state.state.nix = nix_mod.Nix(app_state)

    app_state.state.auth = auth_mod.Auth(app_state)
    # #1572: ContainerRegistry reaches app_state.state.model.ports; Auth reaches
    # app_state.state.model.{tokens,login_attempts}. Wire db + model onto the
    # namespace (the ContextVar backstop binds the same DB for the rest).
    from _helpers import wire_db_and_model

    wire_db_and_model(app_state)
    return app_state


@pytest.fixture(autouse=True)
def _stub_bringup(monkeypatch):
    """Block every container-mechanics test from spawning real processes.

    ``start_container`` calls ``ContainerRegistry.bringup`` at the create
    choke point (#1244), which otherwise materializes the agent home and
    spawns a real ``podman exec`` subprocess. These tests exercise
    port/sudo/reuse mechanics against a fake ``new-cid`` — they must never
    touch real podman. Bring-up has its own dedicated coverage
    (test_bringup.py).
    """
    monkeypatch.setattr(container.ContainerRegistry, "bringup", AsyncMock())


class TestParseIdleTimeout:
    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        return container.ContainerRegistry(app_state)

    def test_default_values(self):
        reg = self._registry({})
        assert reg.idle_timeout_seconds == 60 * 60
        assert reg.check_interval_seconds == max(10, min(60, 60 * 60 // 3))

    def test_custom_value(self):
        reg = self._registry({"KLANGKD_IDLE_TIMEOUT_SECONDS": "120"})
        assert reg.idle_timeout_seconds == 120
        assert reg.check_interval_seconds == max(10, min(60, 120 // 3))

    def test_invalid_value_uses_default(self):
        reg = self._registry({"KLANGKD_IDLE_TIMEOUT_SECONDS": "not_a_number"})
        assert reg.idle_timeout_seconds == 60 * 60

    def test_small_value_clamps_interval(self):
        reg = self._registry({"KLANGKD_IDLE_TIMEOUT_SECONDS": "15"})
        assert reg.idle_timeout_seconds == 15
        assert reg.check_interval_seconds == 10  # clamped to min 10

    def test_large_value_clamps_interval(self):
        reg = self._registry({"KLANGKD_IDLE_TIMEOUT_SECONDS": "3600"})
        assert reg.idle_timeout_seconds == 3600
        assert reg.check_interval_seconds == 60  # clamped to max 60


class TestSslCertDir:
    """Runtime SSL/CA certificate injection (#1181): ssl_cert_dir() resolver."""

    def test_unset_returns_none(self):
        assert self._trust(make_settings({})).ssl_cert_dir() is None

    def test_missing_certs_dir_returns_none(self, tmp_path):
        # customize_dir set, but no certs/ subdir -> None.
        s = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert self._trust(s).ssl_cert_dir() is None

    def test_dir_with_pem_returns_path(self, tmp_path):
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "ca.pem").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert self._trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_dir_with_crt_returns_path(self, tmp_path):
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "my-ca.crt").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert self._trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_extension_case_insensitive(self, tmp_path):
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "CA.PEM").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert self._trust(s).ssl_cert_dir() == str(certs.resolve())

    def test_empty_certs_dir_returns_none(self, tmp_path):
        # certs/ exists but contains no .pem/.crt -> None.
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "readme.txt").write_text("no certs here")
        s = make_settings({"KLANGKD_CUSTOMIZE_DIR": str(tmp_path)})
        assert self._trust(s).ssl_cert_dir() is None

    def test_ssl_env_vars_empty_without_dir(self):
        assert container.ssl_env_vars(None) == []

    def test_ssl_env_vars_point_at_bundle(self):
        vars_ = container.ssl_env_vars("/some/dir")
        assert vars_ == [
            "SSL_CERT_FILE=/tmp/klangk/ca-bundle.crt",
            "REQUESTS_CA_BUNDLE=/tmp/klangk/ca-bundle.crt",
            "CURL_CA_BUNDLE=/tmp/klangk/ca-bundle.crt",
            "NODE_EXTRA_CA_CERTS=/tmp/klangk/ca-bundle.crt",
        ]

    @staticmethod
    def _trust(s) -> ssl_trust.SSLTrust:
        """SSLTrust owning the given settings (#1567)."""
        return ssl_trust.SSLTrust(
            types.SimpleNamespace(state=types.SimpleNamespace(settings=s))
        )


class TestImagePullPolicy:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_default_is_never(self):
        assert self.registry.image_pull_policy() == "never"

    def test_valid_override(self):
        # Rebuild registry with env override
        import types as types_mod

        settings = make_settings({"KLANGKD_IMAGE_PULL_POLICY": "missing"})
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        reg = container.ContainerRegistry(app_state)
        assert reg.image_pull_policy() == "missing"

    def test_invalid_falls_back_to_never(self, caplog):
        import types as types_mod

        settings = make_settings({"KLANGKD_IMAGE_PULL_POLICY": "sometimes"})
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        reg = container.ContainerRegistry(app_state)
        with caplog.at_level("WARNING"):
            assert reg.image_pull_policy() == "never"
        assert "Invalid KLANGKD_IMAGE_PULL_POLICY" in caplog.text


class TestActivityTracking:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def testtrack_activity(self):
        self.registry.track_activity("cid-1", "ws-1")
        assert "ws-1" in self.registry.states
        state = self.registry.states["ws-1"]
        assert state.container_id == "cid-1"
        assert state.last_activity <= time.time()

    def test_record_activity_updates_time(self):
        self.registry.track_activity("cid-1", "ws-1")
        old_time = self.registry.states["ws-1"].last_activity
        time.sleep(0.01)
        self.registry.record_activity("cid-1")
        new_time = self.registry.states["ws-1"].last_activity
        assert new_time > old_time

    def test_record_activity_unknown_container(self):
        # Should not raise
        self.registry.record_activity("nonexistent")
        assert "nonexistent" not in self.registry.states

    def testtrack_activity_overwrites(self):
        self.registry.track_activity("cid-1", "ws-1")
        self.registry.track_activity("cid-1", "ws-2")
        assert self.registry.states["ws-2"].container_id == "cid-1"

    def test_track_activity_same_workspace_updates_container(self):
        self.registry.track_activity("cid-1", "ws-1")
        self.registry.track_activity("cid-1", "ws-1")
        assert self.registry.states["ws-1"].container_id == "cid-1"

    def test_track_activity_threads_per_handle_home(self):
        # The home-layout flag rides track_activity onto ContainerState
        # (#2720) the same way health_check/owner_id/setup_state do, so
        # the health monitor can branch without a DB lookup per tick.
        self.registry.track_activity("cid-1", "ws-1")
        # Default is the hardened direction (shared) — #3135.
        assert self.registry.states["ws-1"].per_handle_home is False
        self.registry.track_activity("cid-1", "ws-1", per_handle_home=True)
        assert self.registry.states["ws-1"].per_handle_home is True
        # Untouched when not passed (e.g. test harness call sites) — the
        # previous value survives, mirroring owner_id/setup_state.
        self.registry.track_activity("cid-1", "ws-1")
        assert self.registry.states["ws-1"].per_handle_home is True

    def test_track_activity_fires_status_changed_on_new(self):
        calls = []
        self.registry.set_on_container_status_changed(
            lambda ws_id, running, started_at=None: calls.append(
                (ws_id, running)
            )
        )
        try:
            self.registry.track_activity("cid-new", "ws-new")
            assert calls == [("ws-new", True)]
            # Second call for same workspace should NOT fire again
            self.registry.track_activity("cid-new", "ws-new")
            assert calls == [("ws-new", True)]
        finally:
            self.registry.on_container_status_changed = None

    async def test_remove_state_cleans_up_reverse_mapping(self):
        self.registry.track_activity("cid-rm", "ws-rm")
        assert "cid-rm" in self.registry._cid_to_wsid
        await self.registry.remove_state("ws-rm")
        assert "ws-rm" not in self.registry.states
        assert "cid-rm" not in self.registry._cid_to_wsid

    async def test_remove_state_retains_workspace_lock(self):
        # remove_state must NOT pop _workspace_locks[ws] (#1258): doing so
        # would let a subsequent _get_workspace_lock hand out a fresh,
        # non-mutually-exclusive lock object while a coroutine may still be
        # waiting on the original. The lock entry is cheap to retain.
        lock = self.registry._get_workspace_lock("ws-lock-rm")
        assert "ws-lock-rm" in self.registry._workspace_locks
        await self.registry.remove_state("ws-lock-rm")
        assert "ws-lock-rm" in self.registry._workspace_locks
        assert self.registry._workspace_locks["ws-lock-rm"] is lock

    def test_get_state_returns_state(self):
        self.registry.track_activity("cid-1", "ws-1")
        state = self.registry.get_state("ws-1")
        assert state is not None
        assert state.container_id == "cid-1"

    def test_get_state_returns_none_for_unknown(self):
        assert self.registry.get_state("nonexistent") is None

    def test_track_activity_stores_health_metadata(self):
        # health_check, owner_id, and setup_state are cached on the
        # ContainerState for the health monitor to read on each poll.
        self.registry.track_activity(
            "cid-hm",
            "ws-hm",
            health_check="curl -sf http://localhost:8080/health",
            owner_id="uid-owner",
            setup_state="complete",
        )
        state = self.registry.states["ws-hm"]
        assert state.health_check == ("curl -sf http://localhost:8080/health")
        assert state.owner_id == "uid-owner"
        assert state.setup_state == "complete"
        # The home layout rides along too (#2720); defaults to the
        # hardened shared direction (#3135) when a caller doesn't pass it.
        assert state.per_handle_home is False


def _noop_callback(ws):
    pass


class TestIdleCallbacks:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_on_idle_stop_registers(self):
        self.registry.track_activity("cid-1", "ws-1")
        self.registry.on_idle_stop("ws-1", _noop_callback)
        assert _noop_callback in self.registry.states["ws-1"].idle_callbacks

    def test_multiple_callbacks(self):
        def cb2(ws):
            pass

        self.registry.track_activity("cid-1", "ws-1")
        self.registry.on_idle_stop("ws-1", _noop_callback)
        self.registry.on_idle_stop("ws-1", cb2)
        assert len(self.registry.states["ws-1"].idle_callbacks) == 2

    def test_remove_idle_callback(self):
        self.registry.track_activity("cid-1", "ws-1")
        self.registry.on_idle_stop("ws-1", _noop_callback)
        self.registry.remove_idle_callback("ws-1", _noop_callback)
        assert (
            _noop_callback not in self.registry.states["ws-1"].idle_callbacks
        )

    def test_remove_idle_callback_not_registered(self):
        self.registry.track_activity("cid-1", "ws-1")
        self.registry.remove_idle_callback("ws-1", _noop_callback)
        assert (
            _noop_callback not in self.registry.states["ws-1"].idle_callbacks
        )

    def test_remove_idle_callback_unknown_workspace(self):
        self.registry.remove_idle_callback("nonexistent", _noop_callback)
        assert "nonexistent" not in self.registry.states

    def test_callbacks_per_workspace(self):
        def cb2(ws):
            pass

        self.registry.track_activity("cid-1", "ws-1")
        self.registry.track_activity("cid-2", "ws-2")
        self.registry.on_idle_stop("ws-1", _noop_callback)
        self.registry.on_idle_stop("ws-2", cb2)
        assert _noop_callback in self.registry.states["ws-1"].idle_callbacks
        assert cb2 in self.registry.states["ws-2"].idle_callbacks
        assert (
            _noop_callback not in self.registry.states["ws-2"].idle_callbacks
        )


class TestPortAllocation:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_allocate_ports(self, workspace, app_state):
        ports = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 3, self.registry.port_range_start
        )
        assert len(ports) == 3
        assert all(p >= self.registry.port_range_start for p in ports)

    async def test_allocate_ports_avoids_used(
        self, workspace, user, app_state
    ):
        # Allocate some ports for workspace 1
        ports1 = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 3, self.registry.port_range_start
        )
        # Create second workspace and allocate
        ws2 = await app_state.state.model.workspaces.create_workspace(
            user["id"], "ws2"
        )
        ports2 = await app_state.state.model.ports.find_and_allocate_ports(
            ws2["id"], 3, self.registry.port_range_start
        )
        # No overlap
        assert set(ports1).isdisjoint(set(ports2))

    async def test_get_workspace_ports(self, workspace, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 2, self.registry.port_range_start
        )
        retrieved = await registry.get_workspace_ports(workspace["id"])
        assert retrieved == sorted(allocated)

    async def test_get_workspace_ports_empty(self, workspace, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ports = await registry.get_workspace_ports(workspace["id"])
        assert ports == []


class TestDnsConfig:
    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        return container.ContainerRegistry(app_state)

    def test_no_env_returns_empty(self):
        assert self._registry({}).container_dns_config() == []

    def test_single_server(self):
        assert self._registry(
            {"KLANGKD_DNS_SERVERS": "100.100.100.100"}
        ).container_dns_config() == ["100.100.100.100"]

    def test_multiple_servers(self):
        result = self._registry(
            {"KLANGKD_DNS_SERVERS": "100.100.100.100, 8.8.8.8"}
        ).container_dns_config()
        assert result == ["100.100.100.100", "8.8.8.8"]

    def test_empty_string(self):
        assert (
            self._registry({"KLANGKD_DNS_SERVERS": ""}).container_dns_config()
            == []
        )


class TestDnsSearchConfig:
    """settings.dns_search → container_dns_search_config() (#2055)."""

    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        return container.ContainerRegistry(app_state)

    def test_no_env_returns_empty(self):
        assert self._registry({}).container_dns_search_config() == []

    def test_single_domain(self):
        assert self._registry(
            {"KLANGKD_DNS_SEARCH": "corp.example"}
        ).container_dns_search_config() == ["corp.example"]

    def test_multiple_domains(self):
        result = self._registry(
            {"KLANGKD_DNS_SEARCH": "corp.example, svc.example"}
        ).container_dns_search_config()
        assert result == ["corp.example", "svc.example"]

    def test_empty_string(self):
        assert (
            self._registry(
                {"KLANGKD_DNS_SEARCH": ""}
            ).container_dns_search_config()
            == []
        )


class TestConstants:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_port_range_start(self):
        assert self.registry.port_range_start == 9000

    def test_container_port_start(self):
        assert container.CONTAINER_PORT_START == 8000

    def test_default_ports_per_workspace(self):
        assert container.DEFAULT_PORTS_PER_WORKSPACE == 5


class TestPortsPerWorkspaceCap:
    """KLANGKD_HOSTED_PORTS_PER_WORKSPACE resolver (#1237)."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        return container.ContainerRegistry(app_state)

    def test_default_when_unset(self):
        assert self._registry({}).ports_per_workspace_cap() == 5

    def test_override(self):
        assert (
            self._registry(
                {"KLANGKD_HOSTED_PORTS_PER_WORKSPACE": "3"}
            ).ports_per_workspace_cap()
            == 3
        )

    def test_zero_disables(self):
        assert (
            self._registry(
                {"KLANGKD_HOSTED_PORTS_PER_WORKSPACE": "0"}
            ).ports_per_workspace_cap()
            == 0
        )

    def test_none_falls_back_to_default(self, monkeypatch):
        # #2603: the field is int-typed; None (explicitly emptied) means
        # the default at the cap property. Garbage and negatives are
        # rejected at construction (see test_settings.py).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "hosted_ports_per_workspace",
            None,
        )
        assert self.registry.ports_per_workspace_cap() == 5

    def test_zero_disables_via_property(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "hosted_ports_per_workspace",
            0,
        )
        assert self.registry.ports_per_workspace_cap() == 0


# --- Container lifecycle tests (mocked) ---


@contextmanager
def patch_podman(registry=None, **overrides):
    """Patch the podman.* calls container.py makes.

    container.py reaches the CLI wrappers via ``self.registry.app.state.podman.X``
    (#1468); this patches the methods on that instance. Yields a namespace
    of the AsyncMocks so tests can assert on them. Override any default by
    passing ``name=AsyncMock(...)``.
    """
    defaults = {
        "inspect_container": AsyncMock(return_value=None),
        "container_logs": AsyncMock(
            return_value="dns-proxy listening on 127.0.0.1:15353"
        ),
        "create_container": AsyncMock(return_value="new-cid"),
        "start_container": AsyncMock(),
        "wait_for_container_ready": AsyncMock(),
        "remove_container": AsyncMock(),
        "list_containers": AsyncMock(return_value=[]),
        "exec_container": AsyncMock(return_value=(0, "", "")),
        "inspect_volume": AsyncMock(return_value=None),
        "create_volume": AsyncMock(
            return_value={"Name": "v", "CreatedAt": ""}
        ),
    }
    mocks = {**defaults, **overrides}
    target = registry.app.state.podman if registry is not None else podman
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(target, name, mock))
        yield SimpleNamespace(**mocks)


def _running(value=True):
    """An inspect_container mock returning a container in the given state."""
    return AsyncMock(return_value={"State": {"Running": value}})


def _sudo_call(p):
    """Return the ``exec_container`` call that configures sudo.

    ``start_container`` also invokes ``terminal.set_workspace_token`` which,
    since terminal.py adopted ``podman.exec_container``, shows up as an
    additional ``exec_container`` call.  Identify the sudoers call by its
    command rather than assuming it is the only (or last) call.
    """
    for call in p.exec_container.call_args_list:
        cmd = call.args[1] if len(call.args) > 1 else []
        if "klangk-configure-sudo" in cmd:
            return call
    raise AssertionError(
        "no klangk-configure-sudo exec_container call in "
        f"{p.exec_container.call_args_list}"
    )


class TestWorkspaceNameSlug:
    """Pure-function tests for container.workspace_name_slug (#2286)."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("my-dev-env", "my-dev-env"),
            ("My Cool WS", "my-cool-ws"),
            ("  a__b!!c  ", "a-b-c"),
            ("UPPER", "upper"),
            ("a.b/c:d", "a-b-c-d"),
            ("!!!---!!!", ""),
            ("", ""),
            ("x" * 40, "x" * 24),
            ("café", "caf"),  # non-ascii collapses
            ("name with   multiple   gaps", "name-with-multiple-gaps"),
        ],
    )
    def test_slug(self, name, expected):
        assert container.workspace_name_slug(name) == expected

    def test_slug_none_is_empty(self):
        assert container.workspace_name_slug(None) == ""


class TestStartContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_create_new_container(self, workspace):
        with patch_podman(self.registry) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        assert cid == "new-cid"
        assert status == "created"
        p.start_container.assert_awaited_once_with("new-cid", hooks_dir=None)
        assert workspace["id"] in self.registry.states

    async def test_shared_home_dir_materialized_before_start(self, workspace):
        """<home>/klangk exists on the HOST before ``podman start`` (#2725).

        The image WORKDIR is /home/klangk but the home volume mounts at
        /home — without a pre-start mkdir, podman either auto-creates
        the cwd as container-root (unwritable by the klangk user) or,
        for a legacy dangling `klangk` symlink, refuses to start
        (chdir ENOENT). Order, not just occurrence, is the contract.
        """
        calls: list[str] = []
        real_dir = self.registry.app.state.workspaces.ensure_shared_home_dir

        async def spy_dir(ws_id):
            calls.append("ensure_dir")
            return await real_dir(ws_id)

        self.registry.app.state.workspaces.ensure_shared_home_dir = spy_dir
        with patch_podman(self.registry):

            async def spy_start(*a, **k):
                calls.append("podman_start")

            self.registry.app.state.podman.start_container = AsyncMock(
                side_effect=spy_start
            )
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert calls == ["ensure_dir", "podman_start"]

    async def test_spec_threads_per_handle_home_onto_state(self, workspace):
        # #2720: the layout rides the spec through every start path
        # (create / reuse / adopt) onto ContainerState, so the health
        # monitor can branch without a DB lookup per poll. Default is the
        # hardened shared direction (#3135); an explicit per-handle value
        # threads through untouched.
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert self.registry.states[workspace["id"]].per_handle_home is (False)
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    per_handle_home=True,
                )
            )
        assert self.registry.states[workspace["id"]].per_handle_home is True
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    per_handle_home=False,
                )
            )
        assert self.registry.states[workspace["id"]].per_handle_home is False

    async def test_egress_filter_no_domains_is_noop(self, workspace):
        # No allowed_domains -> no annotation or hooks-dir; the container
        # keeps podman's default hooks-dir behavior (unrestricted). #1365
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert "annotations" not in kwargs
        assert "hooks_dir" not in kwargs
        # #2347: even an unfiltered workspace drops net_raw (the drop is
        # unconditional).
        assert kwargs["cap_drop"] == ["net_raw"]

    # --- #2286: correlation + human-readable names ---

    async def test_workspace_container_name_includes_slug_and_labels(
        self, workspace
    ):
        # #2286: the workspace container name carries the slugified workspace
        # name (for `podman ps | grep <partial-name>`) and a shared
        # klangk.workspace + klangk.role=workspace label (for correlation with
        # the sidecar).
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        iid = self.registry.app.state.util.instance_id()
        slug = container.workspace_name_slug(workspace["name"])
        kwargs = p.create_container.call_args.kwargs
        assert p.create_container.call_args.args[0] == (
            f"klangk-{iid}-{slug}-{workspace['id'][:8]}"
        )
        assert kwargs["labels"]["klangk.workspace"] == workspace["id"]
        assert kwargs["labels"]["klangk.role"] == "workspace"
        assert kwargs["labels"]["klangk.workspace-name"] == slug

    async def test_empty_workspace_name_falls_back_to_id_only_name(self, user):
        # #2286: a name that slugifies to nothing (all symbols) falls back to
        # an id-only name so the container is still valid + unique.
        ws = await self.registry.app.state.model.workspaces.create_workspace(
            user["id"], "!!!"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(ws["id"], "/tmp/home")
            )
        iid = self.registry.app.state.util.instance_id()
        assert p.create_container.call_args.args[0] == (
            f"klangk-{iid}-{ws['id'][:8]}"
        )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["labels"]["klangk.workspace-name"] == ""

    async def test_filtered_start_correlates_workspace_and_sidecar(
        self, workspace, monkeypatch
    ):
        # #2286: a workspace and its sidecar share a klangk.workspace label,
        # carry the same slug + workspace_id[:8] tail in their names (so a
        # partial-name or id-prefix grep matches the pair), and are
        # distinguishable by klangk.role.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, **kw})
            return "net-cid" if name.startswith("klangk-net-") else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                )
            )
        assert len(creates) == 2
        sidecar, ws_container = creates[0], creates[1]
        iid = self.registry.app.state.util.instance_id()
        slug = container.workspace_name_slug(workspace["name"])
        tail = workspace["id"][:8]
        # Both names carry the slug and the same id[:8] tail.
        assert sidecar["name"] == f"klangk-net-{slug}-{tail}"
        assert ws_container["name"] == f"klangk-{iid}-{slug}-{tail}"
        # Shared correlation label + role distinction.
        assert sidecar["labels"]["klangk.workspace"] == workspace["id"]
        assert ws_container["labels"]["klangk.workspace"] == workspace["id"]
        assert sidecar["labels"]["klangk.role"] == "network-sidecar"
        assert ws_container["labels"]["klangk.role"] == "workspace"
        assert sidecar["labels"]["klangk.workspace-name"] == slug
        assert ws_container["labels"]["klangk.workspace-name"] == slug
        # #2342: both carry klangk.managed + the creating daemon's PID, so the
        # dead-owner reap can cull either if its klangkd dies uncleanly.
        for c in (sidecar, ws_container):
            assert c["labels"]["klangk.managed"] == "true"
            assert c["labels"]["klangk.pid"].isdigit()

    # --- #2254: FQDN network sidecar lifecycle ---

    def test_network_sidecar_enabled_by_default(self, monkeypatch):
        # Defaults to the published network sidecar image name (#2254 review); set
        # network_sidecar_image="" to disable egress filtering entirely.
        assert self.registry.network_sidecar_enabled()
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        assert not self.registry.network_sidecar_enabled()

    def test_network_sidecar_enabled_when_image_set(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "img",
        )
        assert self.registry.network_sidecar_enabled()

    async def test_start_network_sidecar_creates_and_starts(self, monkeypatch):
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            cid = await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        assert cid == "new-cid"
        kwargs = p.create_container.call_args.kwargs
        assert p.create_container.call_args.args[0].startswith("klangk-net-")
        # NET_RAW forges the eager-deny RST (#2345).
        assert kwargs["cap_add"] == ["NET_ADMIN", "NET_RAW"]
        assert kwargs["dns"] == ["1.1.1.1"]
        assert "KLANGKNETWORK_EGRESS_ALLOW=github.com:443" in kwargs["env"]
        assert "KLANGKNETWORK_EGRESS_UPSTREAM=8.8.8.8" in kwargs["env"]
        # #3041: the egress mode is passed explicitly (default static) so the
        # sidecar never infers it from consent-client presence.
        assert "KLANGKNETWORK_EGRESS_MODE=static" in kwargs["env"]
        # #2254 review: the network sidecar is labelled with this klangk instance so
        # the startup reaper culls any leftover network sidecar at boot.
        assert (
            kwargs["labels"]["klangk.instance"]
            == self.registry.app.state.util.instance_id()
        )
        # #2342: managed + the creating daemon's PID, same as a workspace
        # container, so the dead-owner reap covers sidecars too.
        assert kwargs["labels"]["klangk.managed"] == "true"
        assert kwargs["labels"]["klangk.pid"].isdigit()
        # #2286: shared klangk.workspace + role labels correlate the sidecar
        # with its workspace (supersedes the old klangk.network-sidecar).
        assert kwargs["labels"]["klangk.workspace"] == ws_id
        assert kwargs["labels"]["klangk.role"] == "network-sidecar"
        p.start_container.assert_awaited_once_with("new-cid", hooks_dir=None)

    async def test_start_network_sidecar_forwards_ttl_tuning(
        self, monkeypatch
    ):
        # The learned-IP TTL floor + sweep cadence are forwarded to the sidecar
        # when set (absent -> the sidecar's own defaults). Lets a deployment or
        # test shorten them: the egress smoketest lowers both so a 5s verdict
        # expires in seconds rather than the 30s floor (#2363, subsumed by #2392).
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_MIN_TTL", "1")
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_SWEEP_INTERVAL", "1")
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        kwargs = p.create_container.call_args.kwargs
        assert "KLANGKNETWORK_EGRESS_MIN_TTL=1" in kwargs["env"]
        assert "KLANGKNETWORK_EGRESS_SWEEP_INTERVAL=1" in kwargs["env"]

    async def test_start_network_sidecar_upstream_env_override(
        self, monkeypatch
    ):
        # KLANGKNETWORK_EGRESS_UPSTREAM, when set in klangkd's env, pins the
        # sidecar's upstream verbatim (operator-pinnable workspace DNS, and the
        # path the egress smoketest's controlled-DNS fixture uses, #2424).
        # Mirrors the MIN_TTL/SWEEP forwarding: set -> honored, absent -> the
        # detected resolver (the next test pins the fallback explicitly).
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        # Detection would return 8.8.8.8; the override must win.
        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        monkeypatch.setenv("KLANGKNETWORK_EGRESS_UPSTREAM", "10.88.0.23")
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        kwargs = p.create_container.call_args.kwargs
        assert "KLANGKNETWORK_EGRESS_UPSTREAM=10.88.0.23" in kwargs["env"]
        assert not any(
            e.startswith("KLANGKNETWORK_EGRESS_UPSTREAM=8.8.8.8")
            for e in kwargs["env"]
        )

    async def test_start_network_sidecar_publishes_host_ports(
        self, monkeypatch
    ):
        # #2267: the sidecar owns the netns the workspace shares, so the
        # workspace's host ports are published on the sidecar (--publish is
        # inert on the workspace under --network container:).
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            cid = await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"], publish=[(18080, 8000)]
            )
        assert cid == "new-cid"
        assert p.create_container.call_args.kwargs["publish"] == [
            (18080, 8000)
        ]

    async def test_start_network_sidecar_default_publish_is_none(
        self, monkeypatch
    ):
        # A filtered workspace with no host ports publishes nothing.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        assert p.create_container.call_args.kwargs["publish"] is None

    async def test_start_network_sidecar_recovers_from_port_conflict(
        self, monkeypatch
    ):
        # #2293: the sidecar start reuses the workspace path's port-conflict
        # recovery. A host-port bind conflict (a TOCTOU between the DB
        # allocator's probe and pasta's bind) removes the stale holder and
        # retries, instead of failing the filtered workspace's start.
        ws_id = "abcdef1234567890"
        conflict_port = 18080
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])

        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(
                    500,
                    f"Bind for 0.0.0.0:{conflict_port} failed: "
                    "port is already allocated",
                )

        async def list_side_effect(label):
            # clear-on-start (klangk.workspace=) finds no stale sidecar; the
            # port-conflict resolver (klangk.instance=) finds the holder.
            if label.startswith("klangk.instance="):
                return [{"Id": "stale-cid", "Labels": {}}]
            return []

        stale_info = {
            "HostConfig": {
                "PortBindings": {
                    "8000/tcp": [{"HostPort": str(conflict_port)}]
                }
            }
        }
        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(side_effect=list_side_effect),
            inspect_container=AsyncMock(return_value=stale_info),
        ) as p:
            cid = await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"], publish=[(conflict_port, 8000)]
            )
        assert cid == "new-cid"
        # The conflict was retried (initial + 1 retry).
        assert len(start_calls) == 2
        # The stale holder of conflict_port was removed.
        assert "stale-cid" in [
            c.args[0] for c in p.remove_container.call_args_list
        ]

    async def test_start_network_sidecar_clears_lingering_by_label(
        self, monkeypatch
    ):
        # #2265 + #2286: start_network_sidecar removes any sidecar from a
        # prior generation before creating, so a restart (or an external kill
        # that left the old sidecar running) does not collide and fail-closed.
        # Removal is by the klangk.workspace label + role=network-sidecar, so a
        # sidecar whose name carries a now-stale slug (a rename) is still found.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        stale = {
            "Id": "stale-cid",
            "Names": ["klangk-net-oldslug-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry, list_containers=AsyncMock(return_value=[stale])
        ) as p:
            await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        # The stale sidecar is found by label and removed by id (not by the
        # stale-slug name) before the create.
        p.list_containers.assert_awaited_with(f"klangk.workspace={ws_id}")
        assert p.remove_container.await_args_list[0].args == ("stale-cid",)
        assert p.remove_container.await_args_list[0].kwargs == {"force": True}
        # And the fresh sidecar is still created + started.
        assert p.create_container.await_count == 1

    async def test_start_network_sidecar_ignores_force_remove_error(
        self, monkeypatch
    ):
        # #2265 + #2286: the pre-create removal of a lingering sidecar is
        # best-effort -- if the per-container remove errors, the state is
        # judged by a re-list (#2676): the rm error here is the already-gone
        # race (list saw it, rm 404s, the re-list no longer does), so the
        # create proceeds normally.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        stale = {
            "Id": "stale-cid",
            "Names": ["klangk-net-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            # First list (the clear) sees the stale sidecar; the post-failure
            # re-list no longer does — the already-gone race.
            list_containers=AsyncMock(side_effect=[[stale], []]),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "not found")
            ),
        ) as p:
            cid = await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        assert (
            cid == "new-cid"
        )  # create still happened despite the remove error
        assert p.create_container.await_count == 1

    async def test_start_network_sidecar_clears_dependents_then_creates(
        self, monkeypatch
    ):
        # #2676: podman refuses to rm -f a sidecar whose workspace container
        # is still joined to its netns ("has dependent containers"). The
        # clear removes THIS workspace's own role=workspace containers (the
        # dependents) and retries the sidecar removal, so a create-path start
        # against a stale live container+sidecar pair succeeds instead of
        # dying on the raw dependent-containers refusal.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        sidecar = {
            "Id": "sidecar-cid",
            "Names": ["klangk-net-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        ws_container = {
            "Id": "ws-cid",
            "Names": ["klangk-ws-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }

        async def remove(ident, force=True):
            if ident == "sidecar-cid":
                # First sidecar rm: refused (dependent). Retry after the
                # dependent was removed: succeeds.
                if remove.sidecar_calls == 0:
                    remove.sidecar_calls += 1
                    raise podman.PodmanError(
                        500,
                        "container sidecar-cid has dependent containers "
                        "which must be removed before it: ws-cid",
                    )
                return

        remove.sidecar_calls = 0

        with patch_podman(
            self.registry,
            # 1) the clear's list (sees pair), 2) the dependent listing
            # (sees pair), 3) would be the survivor re-list — never reached
            # because the retry removes the sidecar.
            list_containers=AsyncMock(
                side_effect=[[sidecar, ws_container]] * 3
            ),
            remove_container=AsyncMock(side_effect=remove),
        ) as p:
            cid = await self.registry.start_network_sidecar(
                ws_id, ["github.com:443"]
            )
        assert cid == "new-cid"
        removed = [c.args[0] for c in p.remove_container.call_args_list]
        # Dependent (this workspace's container) removed before the sidecar
        # retry, never the other way around.
        assert removed == ["sidecar-cid", "ws-cid", "sidecar-cid"]
        assert p.create_container.await_count == 1

    async def test_start_network_sidecar_clean_error_when_sidecar_survives(
        self, monkeypatch
    ):
        # #2676: when the dependent can't be removed (here: the rm keeps
        # refusing — e.g. a foreign container joined to the netns), the
        # create path refuses with a clear, actionable error instead of
        # swallowing the refusal and letting create_container --replace hit
        # podman's raw dependent-containers 500.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        sidecar = {
            "Id": "sidecar-cid",
            "Names": ["klangk-net-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        ws_container = {
            "Id": "ws-cid",
            "Names": ["klangk-ws-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        dep_refusal = podman.PodmanError(
            500,
            "container sidecar-cid has dependent containers which must "
            "be removed before it: foreign-cid",
        )
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=[[sidecar, ws_container]] * 4
            ),
            remove_container=AsyncMock(side_effect=dep_refusal),
        ) as p:
            with pytest.raises(podman.PodmanError) as excinfo:
                await self.registry.start_network_sidecar(
                    ws_id, ["github.com:443"]
                )
        assert "cannot remove the existing network sidecar" in str(
            excinfo.value
        )
        assert "dependent" in str(excinfo.value)
        # The create never ran — no collision attempt against the survivor.
        p.create_container.assert_not_awaited()

    async def test_remove_network_sidecar_true_when_clear(self, monkeypatch):
        # #2676: the bool contract — nothing to remove means cleared.
        ws_id = "abcdef1234567890"
        with patch_podman(self.registry) as p:
            assert await self.registry._remove_network_sidecar(ws_id) is True
        p.remove_container.assert_not_awaited()

    async def test_remove_network_sidecar_survivor_relist_error_proceeds(
        self, monkeypatch
    ):
        # #2676: when the survivor re-list itself errors after a refused
        # removal, the state is unknowable — proceed (the old best-effort
        # semantics) instead of refusing the create on a podman hiccup.
        ws_id = "abcdef1234567890"
        sidecar = {
            "Id": "sidecar-cid",
            "Names": ["klangk-net-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=[
                    [sidecar],
                    podman.PodmanError(500, "podman down"),
                ]
            ),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "refused")
            ),
        ):
            assert await self.registry._remove_network_sidecar(ws_id) is True

    async def test_remove_dependents_only_own_workspace_role(
        self, monkeypatch
    ):
        # #2676: the dependent sweep touches only containers carrying both
        # this workspace's label and role=workspace — a sidecar (or any
        # other role) in the same listing is skipped — and an unknowable
        # listing (podman down) is a quiet no-op.
        ws_id = "abcdef1234567890"
        ws_container = {
            "Id": "ws-cid",
            "Names": ["klangk-ws-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        sidecar = {
            "Id": "sidecar-cid",
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        # role=workspace but no id/name at all — skipped, not removed.
        nameless = {
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[sidecar, nameless, ws_container]
            ),
        ) as p:
            await self.registry._remove_dependent_workspace_containers(ws_id)
        p.remove_container.assert_awaited_once_with("ws-cid", force=True)

        # Unknowable listing: no removals, no raise.
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "podman down")
            ),
        ) as p:
            await self.registry._remove_dependent_workspace_containers(ws_id)
        p.remove_container.assert_not_awaited()

    async def test_start_network_sidecar_failure_raises(self, monkeypatch):
        # #2254 review B2: a network sidecar that can't start must surface the failure
        # (raise), not return "" — the caller fail-closes rather than starting
        # the workspace unrestricted.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        with patch_podman(
            self.registry,
            create_container=AsyncMock(
                side_effect=podman.PodmanError(500, "no image")
            ),
        ):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_network_sidecar(
                    ws_id, ["github.com:443"]
                )

    async def test_start_network_sidecar_raises_without_image(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        with pytest.raises(podman.PodmanError):
            await self.registry.start_network_sidecar(
                "abcd1234", ["github.com:443"]
            )

    async def test_stop_network_sidecar_removes_by_label(self, monkeypatch):
        # #2286: stop removes the sidecar by its klangk.workspace label +
        # role=network-sidecar, leaving the workspace container alone and
        # working even when the sidecar's name carries a stale slug.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "img",
        )
        sidecar = {
            "Id": "net-cid",
            "Names": ["klangk-net-renamed-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        workspace_container = {
            "Id": "ws-cid",
            "Names": ["klangk-renamed-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[sidecar, workspace_container]
            ),
        ) as p:
            await self.registry.stop_network_sidecar(ws_id)
        p.list_containers.assert_awaited_with(f"klangk.workspace={ws_id}")
        # Only the sidecar (by id) is removed; the workspace container is left
        # for stop_and_remove_container's own remove call.
        p.remove_container.assert_awaited_once_with("net-cid", force=True)

    async def test_stop_network_sidecar_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        with patch_podman(self.registry) as p:
            await self.registry.stop_network_sidecar("abcd1234")
        p.remove_container.assert_not_awaited()

    async def test_start_network_sidecar_no_consent_env_without_monitor(
        self, monkeypatch
    ):
        # #2242: consent recording is gated on the monitor being wired, not on
        # egress_mode. Without a consent_sweeper, no CONSENT_URL/NFQUEUE env.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                "abcdef12", ["github.com:443"], egress_mode="interactive"
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any("CONSENT_URL" in e for e in env)
        assert not any("NFQUEUE_NUM" in e for e in env)

    async def test_start_network_sidecar_passes_consent_env(
        self, monkeypatch, tmp_path
    ):
        # #2242: consent recording runs for every filtered workspace (here in
        # static mode) when the monitor is wired -- mode-independent. The
        # workspace-token file is bind-mounted in (rotated), not baked in env.
        import types as _types

        WS = "abcdef12-abcd-1234-5678-aaaaaaaaaaaa"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "egress_port", "8997"
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        monkeypatch.setattr(
            self.registry.app.state,
            "consent_sweeper",
            _types.SimpleNamespace(),
            raising=False,
        )
        monkeypatch.setattr(
            self.registry.app.state,
            "auth",
            _types.SimpleNamespace(create_workspace_token=lambda ws: "ws-tok"),
            raising=False,
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                WS, ["github.com:443"], egress_mode="interactive"
            )
        kwargs = p.create_container.call_args.kwargs
        env = kwargs["env"]
        assert any(
            "KLANGKNETWORK_EGRESS_CONSENT_URL=" in e and "8997" in e
            for e in env
        )
        assert "KLANGKNETWORK_EGRESS_NFQUEUE_NUM=5139" in env
        assert "KLANGKNETWORK_EGRESS_MODE=interactive" in env
        assert not any("TOKEN" in e for e in env)
        # the workspace token was written to the bind-mounted file ...
        assert (tmp_path / "ws-tokens" / WS).read_text() == "ws-tok"
        # ... and bind-mounted read-only into the sidecar
        assert any("workspace-token:ro" in b for b in kwargs.get("binds", []))

    async def test_start_network_sidecar_static_mode_env_with_consent(
        self, monkeypatch, tmp_path
    ):
        # #3041: the exact misconfiguration the bug lived in -- a STATIC
        # workspace with the consent stack wired (every filtered workspace
        # gets CONSENT_URL, #2242/#2311). The mode env must say static so the
        # sidecar NXDOMAINs off-list names despite the consent client being
        # present (it used to infer interactive from client presence and
        # resolve them -- a resolution oracle + DNS exfil channel).
        import types as _types

        WS = "abcdef12-abcd-1234-5678-bbbbbbbbbbbb"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "net-img",
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "egress_port", "8997"
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        monkeypatch.setattr(
            self.registry.app.state,
            "consent_sweeper",
            _types.SimpleNamespace(),
            raising=False,
        )
        monkeypatch.setattr(
            self.registry.app.state,
            "auth",
            _types.SimpleNamespace(create_workspace_token=lambda ws: "ws-tok"),
            raising=False,
        )
        from klangk import netfilter as _nf

        monkeypatch.setattr(_nf, "detect_host_resolvers", lambda: ["8.8.8.8"])
        with patch_podman(self.registry) as p:
            await self.registry.start_network_sidecar(
                WS, ["github.com:443"], egress_mode="static"
            )
        env = p.create_container.call_args.kwargs["env"]
        # consent stack wired ...
        assert any("KLANGKNETWORK_EGRESS_CONSENT_URL=" in e for e in env)
        # ... AND the mode says static -- the pair the sidecar keys on.
        assert "KLANGKNETWORK_EGRESS_MODE=static" in env

    def test_write_sidecar_token_atomic(self, monkeypatch, tmp_path):
        # The sidecar reads this file per POST; writes must be atomic (no
        # half-written token) and overwritable on rotation.
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        self.registry.write_sidecar_token("ws-id", "tok-A")
        path = tmp_path / "ws-tokens" / "ws-id"
        assert path.read_text() == "tok-A"
        self.registry.write_sidecar_token("ws-id", "tok-B")  # rotation
        assert path.read_text() == "tok-B"
        assert not (tmp_path / "ws-tokens" / "ws-id.tmp").exists()

    async def test_stop_network_sidecar_swallows_error(self, monkeypatch):
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "img",
        )
        sidecar = {
            "Id": "net-cid",
            "Names": ["klangk-net-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[sidecar]),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "not found")
            ),
        ):
            # The per-container remove error is swallowed (sidecar gone).
            await self.registry.stop_network_sidecar(ws_id)

    async def test_remove_network_sidecar_swallows_list_error(
        self, monkeypatch
    ):
        # #2286: if list_containers itself errors (podman down), removal is
        # best-effort: log + return, never raise, and never call remove.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "img",
        )
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "podman down")
            ),
        ) as p:
            await self.registry._remove_network_sidecar(ws_id)
        p.remove_container.assert_not_awaited()

    async def test_remove_network_sidecar_uses_name_when_no_id(
        self, monkeypatch
    ):
        # #2286: a sidecar missing the Id/ID field is still removed by its
        # (first) name; a sidecar with neither id nor name is skipped.
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "img",
        )
        no_id_with_name = {
            "Names": ["klangk-net-slug-abcdef12"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        no_id_no_name = {
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[no_id_with_name, no_id_no_name]
            ),
        ) as p:
            await self.registry._remove_network_sidecar(ws_id)
        # The name-only entry is removed by its name; the empty entry skipped.
        p.remove_container.assert_awaited_once_with(
            "klangk-net-slug-abcdef12", force=True
        )

    async def test_filtered_workspace_uses_network_sidecar_when_enabled(
        self, workspace, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                )
            )
        assert len(creates) == 2
        assert creates[0]["name"].startswith("klangk-net-")
        # NET_RAW forges the eager-deny RST (#2345).
        assert creates[0]["cap_add"] == ["NET_ADMIN", "NET_RAW"]
        assert "KLANGKNETWORK_EGRESS_ALLOW=github.com:443" in creates[0]["env"]
        assert any(
            e.startswith("KLANGKNETWORK_EGRESS_BACKEND_PORT=")
            for e in creates[0]["env"]
        )
        # #2282: the fwmark is passed explicitly (single source of truth) so
        # proxy.py and entrypoint.sh can't diverge.
        assert any(
            e.startswith("KLANGKNETWORK_EGRESS_MARK=")
            for e in creates[0]["env"]
        ), creates[0]["env"]
        assert creates[1]["network"] == "container:net-cid"
        assert "annotations" not in creates[1]
        # #2254 B1: --add-host is rejected and --publish is discarded under
        # --network container:, so both (plus dns/dns-search) are popped from
        # the workspace kwargs.
        assert "add_hosts" not in creates[1]
        assert "publish" not in creates[1]
        assert "dns" not in creates[1]
        assert "dns_search" not in creates[1]

    async def test_reject_only_workspace_starts_network_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2367: a workspace with rejected_domains but NO allowed_domains still
        # starts the network sidecar (the reject list is enforced by NXDOMAIN in
        # the sidecar's proxy), so the trigger is `allowed_domains OR
        # rejected_domains`. The sidecar gets KLANGKNETWORK_EGRESS_REJECT and an
        # empty ALLOW (allowed_domains is None here, so the join must tolerate it).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    rejected_domains=["evil.com:443"],
                )
            )
        assert len(creates) == 2
        assert creates[0]["name"].startswith("klangk-net-")
        assert "KLANGKNETWORK_EGRESS_REJECT=evil.com:443" in creates[0]["env"]
        assert any(
            e == "KLANGKNETWORK_EGRESS_ALLOW=" for e in creates[0]["env"]
        ), creates[0]["env"]
        assert creates[1]["network"] == "container:net-cid"

    async def test_interactive_workspace_starts_sidecar_without_lists(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2325: a workspace in interactive mode gets the network sidecar EVEN
        # WITH EMPTY allowed_domains/rejected_domains -- every not-yet-
        # approved egress is held for a consent decision (the "ask first"
        # default posture). The sidecar gets an empty ALLOW (nothing
        # pre-approved) and an empty REJECT; the proxy + NFQUEUE hold do the
        # rest. The workspace runs --network container:<sidecar> just like a
        # list-filtered workspace.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="interactive",
                )
            )
        assert len(creates) == 2  # sidecar + workspace
        assert creates[0]["name"].startswith("klangk-net-")
        # Empty allow/reject lists: nothing pre-approved, nothing pre-rejected.
        assert any(
            e == "KLANGKNETWORK_EGRESS_ALLOW=" for e in creates[0]["env"]
        ), creates[0]["env"]
        assert any(
            e == "KLANGKNETWORK_EGRESS_REJECT=" for e in creates[0]["env"]
        ), creates[0]["env"]
        assert creates[1]["network"] == "container:net-cid"
        # Tracked so a later stop tears the sidecar down.
        assert workspace["id"] in self.registry._ws_with_network_sidecar

    async def test_static_workspace_no_lists_starts_unrestricted(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2325: static mode with NO lists is the one case that still starts
        # unrestricted (no filtering requested). Only one container is
        # created (the workspace) -- no network sidecar.
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="static",
                )
            )
        assert len(creates) == 1  # workspace only, no sidecar
        assert not creates[0]["name"].startswith("klangk-net-")
        # No sidecar => the workspace is not --network container:<sidecar>.
        assert not creates[0].get("network", "").startswith("container:")
        assert workspace["id"] not in self.registry._ws_with_network_sidecar

    async def test_allow_workspace_starts_sidecar_when_configured(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2406: allow mode is default-permit but still runs the sidecar WHEN
        # one is configured (so off-list egress is logged via the consent
        # pipeline and rejected_domains is enforced at the sidecar DNS layer).
        # The workspace runs --network container:<sidecar> like an interactive /
        # list-filtered workspace.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="allow",
                )
            )
        assert len(creates) == 2  # sidecar + workspace
        assert creates[0]["name"].startswith("klangk-net-")
        assert creates[1]["network"] == "container:net-cid"
        # Tracked so a later stop tears the sidecar down.
        assert workspace["id"] in self.registry._ws_with_network_sidecar

    async def test_allow_workspace_degrades_to_unrestricted_without_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2406: allow mode requests permissiveness, not filtering, so it NEVER
        # fail-closes -- unlike interactive / list-declaring workspaces. With
        # the sidecar image unset it degrades to plain unrestricted (one
        # container, no sidecar), the same surface a static-no-list workspace
        # gets. This is what keeps `klangk sandbox` working on deployments
        # without the network sidecar configured.
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="allow",
                )
            )
        assert len(creates) == 1  # workspace only, no sidecar
        assert not creates[0]["name"].startswith("klangk-net-")
        assert not creates[0].get("network", "").startswith("container:")
        assert workspace["id"] not in self.registry._ws_with_network_sidecar

    async def test_allow_workspace_degrades_to_unrestricted_without_userns(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2406: an empty userns would reopen the #2264 SO_MARK bypass for a
        # FILTERED workspace, so interactive/lists fail-close on it. Allow
        # mode instead degrades to unrestricted (no sidecar) -- it never asked
        # to be filtered, so it does not insist on the sidecar's isolation.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(self.registry.app.state.settings, "userns", "")
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="allow",
                )
            )
        assert len(creates) == 1  # degraded to unrestricted, no sidecar
        assert workspace["id"] not in self.registry._ws_with_network_sidecar

    async def test_allow_workspace_passes_rejected_domains_to_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2406: allow mode is default-permit but rejected_domains is STILL
        # enforced -- the reject list is NXDOMAIN'd at the sidecar DNS layer
        # (proxy rejected_for), upstream of the consent allow path. Pin that an
        # allow-mode workspace passes KLANGKNETWORK_EGRESS_REJECT into the
        # sidecar env so the proxy can enforce it (a regression here would let
        # allow mode silently drop the deny-list).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    egress_mode="allow",
                    rejected_domains=["evil.com:443"],
                )
            )
        assert len(creates) == 2  # sidecar + workspace
        assert creates[0]["name"].startswith("klangk-net-")
        assert "KLANGKNETWORK_EGRESS_REJECT=evil.com:443" in creates[0]["env"]
        assert creates[1]["network"] == "container:net-cid"

    async def test_interactive_workspace_without_sidecar_image_refuses_to_start(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2325: an interactive workspace needs the sidecar (it asked for "ask
        # first"), so a missing sidecar image must fail-closed -- NEVER start
        # unrestricted (that would silently disable the interactive posture).
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        with patch_podman(self.registry):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        egress_mode="interactive",
                    )
                )

    async def test_interactive_workspace_refuses_empty_userns(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2325: an interactive workspace runs behind the same sidecar, so the
        # #2264 SO_MARK-bypass userns-isolation guard applies to it too. An
        # empty KLANGKD_USERNS would share the sidecar's userns and reopen the
        # bypass -- fail-closed.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(self.registry.app.state.settings, "userns", "")
        with patch_podman(self.registry):
            with pytest.raises(podman.PodmanError) as exc:
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        egress_mode="interactive",
                    )
                )
        assert "KLANGKD_USERNS" in str(exc.value)

    async def test_interactive_workspace_with_allow_sudo_drops_net_raw(
        self, workspace, tmp_path, monkeypatch, caplog
    ):
        # #2325 + #2276 (B): an interactive workspace is egress-filtered (every
        # connection held), so the sudo->root net_raw/SO_MARK defense-in-depth
        # applies: net_raw is dropped, not added.
        import logging

        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )
        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            with caplog.at_level(logging.INFO, logger="klangk.container"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        egress_mode="interactive",
                    )
                )
        ws = creates[1]  # creates[0] is the network sidecar
        assert ws["cap_drop"] == ["net_raw"]
        assert "net_raw" not in ws.get("cap_add", [])

    async def test_reuse_running_interactive_container_retracks_sidecar(
        self, workspace, monkeypatch
    ):
        # #2325: reconnecting to a running INTERACTIVE workspace (no lists)
        # must re-track its sidecar so a later stop tears it down instead of
        # leaking it. Mirror of the filtered re-track test, but via egress_mode.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        assert workspace["id"] not in self.registry._ws_with_network_sidecar
        with patch_podman(
            self.registry, inspect_container=_running(True)
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="existing-cid",
                    egress_mode="interactive",
                )
            )
        assert cid == "existing-cid"
        assert status == "connected"
        p.create_container.assert_not_awaited()
        assert workspace["id"] in self.registry._ws_with_network_sidecar

    async def test_filtered_workspace_publishes_host_ports_on_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2267: a filtered workspace's host ports are published on the network
        # sidecar (the netns owner), not the workspace. The workspace shares the
        # sidecar's netns, so the sidecar's --publish forwards into it and reaches
        # the workspace's listener -- letting filtered workspaces host apps.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        # Deterministic host ports (the workspace requests two).
        async def _fake_reconcile(workspace_id, num_ports):
            return [18080, 18081]

        monkeypatch.setattr(self.registry, "_reconcile_ports", _fake_reconcile)

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                    num_ports=2,
                )
            )
        assert len(creates) == 2
        # Host ports land on the SIDECAR (container-side ports are
        # CONTAINER_PORT_START+i = 8000/8001).
        assert creates[0]["publish"] == [(18080, 8000), (18081, 8001)]
        # The workspace still publishes nothing under --network container:.
        assert "publish" not in creates[1]

    async def test_filtered_workspace_no_ports_publishes_nothing_on_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2267: a filtered workspace that requests NO host ports publishes
        # nothing on the sidecar (publish=[]). Pinned because the empty-ports
        # path produces [] (not None) and must neither emit a stray -p nor
        # change the sidecar's network setup.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        async def _fake_reconcile(workspace_id, num_ports):
            return []

        monkeypatch.setattr(self.registry, "_reconcile_ports", _fake_reconcile)

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                    num_ports=0,
                )
            )
        assert len(creates) == 2
        # No host ports -> the sidecar publishes nothing.
        assert creates[0]["publish"] == []
        # The workspace publishes nothing (filtered, under --network container:).
        assert "publish" not in creates[1]

    async def test_filtered_workspace_userns_isolates_netns(
        self, workspace, tmp_path, monkeypatch
    ):
        # Review #1/#2 of the egress stack: the SO_MARK-bypass guard is
        # user-namespace isolation. The workspace MUST launch in a user
        # namespace distinct from the one that owns the network sidecar's netns.
        # The sidecar launches with NO --userns (podman default); the workspace
        # launches with the configured settings.userns (keep-id by default) --
        # so the workspace's caps are not valid in the sidecar's netns and
        # setsockopt(SO_MARK) EPERMs. If they ever share a userns the FQDN
        # allow-list is defeated.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                )
            )
        sidecar, ws = creates[0], creates[1]
        # The sidecar (netns owner) launches with NO --userns (podman default).
        assert not sidecar.get("userns")
        # The workspace launches in the configured (non-default) userns.
        assert ws["userns"] == self.registry.app.state.settings.userns
        assert ws["userns"].startswith("keep-id")
        # They differ -> isolation holds (the SO_MARK guard).
        assert ws["userns"] != sidecar.get("userns")

    async def test_filtered_workspace_refuses_empty_userns(
        self, workspace, tmp_path, monkeypatch
    ):
        # Review #2: an empty KLANGKD_USERNS would emit no --userns, putting the
        # workspace in podman's default userns -- the same one the network
        # sidecar owns its netns in -- reopening the SO_MARK egress bypass.
        # Fail-closed: refuse to start a filtered workspace.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(self.registry.app.state.settings, "userns", "")
        with patch_podman(self.registry):
            with pytest.raises(podman.PodmanError) as exc:
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )
        assert "KLANGKD_USERNS" in str(exc.value)

    async def test_filtered_workspace_with_allow_sudo_drops_net_raw(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2347 (folding #2276 B into an unconditional drop): a filtered
        # workspace (allowed_domains) created with allow_sudo on cannot
        # setsockopt(SO_MARK) to bypass the egress filter — net_raw is dropped
        # from the bounding set for EVERY workspace, so even root (via sudo)
        # can't acquire it (NET_ADMIN is never granted).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                )
            )
        ws = creates[1]  # creates[0] is the network sidecar
        # net_raw is dropped, never added (podman rejects a cap in both).
        assert ws["cap_drop"] == ["net_raw"]
        assert "net_raw" not in ws.get("cap_add", [])

    async def test_filtered_workspace_locked_down_drops_net_raw(
        self, workspace, tmp_path, monkeypatch, app_state
    ):
        """#2017 + #2347: a filtered workspace locked out of sudo
        (settings.allow_sudo true, per-workspace allow_sudo false) still
        gets net_raw dropped — the drop is unconditional, not gated on
        sudo posture."""
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": False}
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        creates = []

        async def _fake_create(name, image, **kw):
            creates.append({"name": name, "image": image, **kw})
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    allowed_domains=["github.com:443"],
                )
            )
        ws = creates[1]  # creates[0] is the network sidecar
        # The drop is independent of the sudo vector: locked-down or not,
        # the workspace never holds net_raw (#2347).
        assert ws["cap_drop"] == ["net_raw"]
        assert "net_raw" not in ws.get("cap_add", [])

    async def test_start_network_sidecar_raises_if_proxy_exits_before_ready(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2277: if the sidecar exits before the DNS proxy binds, refuse to
        # start (fail-closed) rather than let the workspace join a netns whose
        # OUTPUT is still ACCEPT (entrypoint mid-flight).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        async def _fake_create(name, image, **kw):
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry,
            create_container=AsyncMock(side_effect=_fake_create),
            container_logs=AsyncMock(return_value=""),
            inspect_container=AsyncMock(
                return_value={"State": {"Status": "exited"}}
            ),
        ):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )

    async def test_start_network_sidecar_raises_on_readiness_timeout(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2277: if the proxy never prints its listening line within the
        # readiness window, refuse to start (fail-closed). Monkeypatch the
        # timeout/poll constants small so the test doesn't wait 30s.
        import klangk.container.sidecar as _c_mod

        monkeypatch.setattr(_c_mod, "NETWORK_SIDECAR_READY_TIMEOUT", 0.05)
        monkeypatch.setattr(_c_mod, "NETWORK_SIDECAR_READY_POLL", 0.01)
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(
            _nf_mod, "detect_host_resolvers", lambda: ["8.8.8.8"]
        )

        async def _fake_create(name, image, **kw):
            return "net-cid" if "klangk-net-" in name else "ws-cid"

        with patch_podman(
            self.registry,
            create_container=AsyncMock(side_effect=_fake_create),
            container_logs=AsyncMock(return_value=""),
            inspect_container=AsyncMock(
                return_value={"State": {"Status": "running"}}
            ),
        ):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )

    async def test_network_sidecar_failure_refuses_to_start(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2254 review B2: fail-CLOSED. A workspace that declared an allow-list
        # must never start unrestricted — a network sidecar that fails to start raises
        # rather than letting the workspace run unfiltered.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(_nf_mod, "detect_host_resolvers", lambda: [])

        async def _fake_create(name, image, **kw):
            if "klangk-net-" in name:
                raise podman.PodmanError(500, "no image")
            return "ws-cid"

        with patch_podman(
            self.registry, create_container=AsyncMock(side_effect=_fake_create)
        ):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )

    async def test_workspace_create_failure_cleans_up_sidecar(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2255 review: if the workspace container fails to create AFTER the
        # network sidecar already started, the sidecar must be torn down so
        # it doesn't leak (NET_ADMIN + proxy) until the next startup reap.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        from klangk import netfilter as _nf_mod

        monkeypatch.setattr(_nf_mod, "detect_host_resolvers", lambda: [])

        async def _fake_create(name, image, **kw):
            # Sidecar create succeeds; workspace create fails.
            if "klangk-net-" in name:
                return "net-cid"
            raise podman.PodmanError(500, "workspace image pull failed")

        net_sidecar = {
            "Id": "net-cid",
            "Names": [f"klangk-net-{workspace['id'][:8]}"],
            "Labels": {
                "klangk.workspace": workspace["id"],
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            create_container=AsyncMock(side_effect=_fake_create),
            list_containers=AsyncMock(return_value=[net_sidecar]),
        ) as p:
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )
        # #2286: the sidecar was removed (by label/id) on the workspace-create
        # failure.
        p.remove_container.assert_awaited_with("net-cid", force=True)
        # And the workspace is no longer tracked as having a live sidecar.
        assert workspace["id"] not in self.registry._ws_with_network_sidecar

    async def test_allowed_domains_without_network_sidecar_refuses_to_start(
        self, workspace, tmp_path, monkeypatch
    ):
        # #2254 review B2: allowed_domains declared but the network sidecar image is
        # not configured -> refuse to start (fail-closed), never unrestricted.
        monkeypatch.setattr(
            self.registry.app.state.settings, "network_sidecar_image", ""
        )
        with patch_podman(self.registry):
            with pytest.raises(podman.PodmanError):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        allowed_domains=["github.com:443"],
                    )
                )

    async def test_resource_limits_defaults_emit_flags(self, workspace):
        # #2030: with no deploy limits configured, the built-in protective
        # defaults (2 CPUs / 8g / 16384 PIDs) still flow through to podman as
        # --cpus / --memory / --pids-limit — a fresh install is bounded out
        # of the box. (Setting an env var to "" disables one cap -> None ->
        # no flag; see test_settings.py.)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 2.0
        assert kwargs["memory"] == "8g"
        assert kwargs["pids_limit"] == 16384
        # #2378: /tmp tmpfs defaults to the pre-#2378 2g size.
        assert kwargs["tmpfs"]["/tmp"] == "rw,exec,nosuid,size=2g"

    async def test_tmp_size_deploy_default_passed_to_tmpfs(
        self, workspace, monkeypatch
    ):
        # #2378: a deploy-wide KLANGKD_CONTAINER_TMP_SIZE flows into the
        # /tmp tmpfs mount's size= option.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_tmp_size", "4g")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["tmpfs"]["/tmp"] == "rw,exec,nosuid,size=4g"

    async def test_tmp_size_workspace_override_wins(
        self, workspace, monkeypatch
    ):
        # #2378: settings.tmp_size overrides the deploy default (override >
        # default), same precedence as the other resource limits.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_tmp_size", "4g")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={"tmp_size": "512m"},
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["tmpfs"]["/tmp"] == "rw,exec,nosuid,size=512m"

    async def test_tmp_size_none_omits_size_option(
        self, workspace, monkeypatch
    ):
        # #2378: an explicit unset (empty env) -> None -> /tmp mounted with
        # no size= option (podman sizes it at half of RAM). The /run and
        # /var/log tmpfs mounts are unchanged.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_tmp_size", None)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["tmpfs"]["/tmp"] == "rw,exec,nosuid"
        assert kwargs["tmpfs"]["/run"] == "rw,noexec,nosuid,size=256m"

    async def test_net_raw_dropped_by_default(self, workspace):
        # #2347: the old enable_ping grant (#2045) is gone — the workspace
        # container never holds CAP_NET_RAW, under any configuration. The
        # default (unfiltered) start drops it from the bounding set.
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cap_drop"] == ["net_raw"]
        assert "cap_add" not in kwargs

    async def test_resource_limits_passed_through(
        self, workspace, monkeypatch
    ):
        # #34: deploy-wide limits read live off app.state.settings are
        # forwarded to podman.create as the cpus / memory / pids_limit
        # kwargs (which podman.create turns into --cpus / --memory /
        # --pids-limit flags).
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", 1.5)
        monkeypatch.setattr(settings, "container_memory_limit", "2g")
        monkeypatch.setattr(settings, "container_pids_limit", 512)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 1.5
        assert kwargs["memory"] == "2g"
        assert kwargs["pids_limit"] == 512

    async def test_workspace_settings_override_resource_limits(
        self, workspace, monkeypatch
    ):
        # #864: a workspace's settings.cpu_limit / memory_limit /
        # pids_limit override the deploy-wide defaults (override >
        # default > none), applied as-is with no clamping (#34).
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", 1.5)
        monkeypatch.setattr(settings, "container_memory_limit", "2g")
        monkeypatch.setattr(settings, "container_pids_limit", 512)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={
                        "cpu_limit": 4.0,
                        "memory_limit": "8g",
                        "pids_limit": 2048,
                    },
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 4.0
        assert kwargs["memory"] == "8g"
        assert kwargs["pids_limit"] == 2048

    async def test_workspace_settings_override_can_go_smaller(
        self, workspace, monkeypatch
    ):
        # #34: a deploy default is a plain default, not a cap or floor —
        # a creator may go larger OR smaller. A smaller override is
        # applied as-is (no clamping up to the deploy value).
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", 4.0)
        monkeypatch.setattr(settings, "container_pids_limit", 2048)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={"cpu_limit": 0.5, "pids_limit": 100},
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 0.5
        assert kwargs["pids_limit"] == 100

    async def test_workspace_settings_partial_override_falls_back(
        self, workspace, monkeypatch
    ):
        # Override applies per-key: a bag that sets only cpu_limit leaves
        # memory + pids at the deploy default.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", 1.5)
        monkeypatch.setattr(settings, "container_memory_limit", "2g")
        monkeypatch.setattr(settings, "container_pids_limit", 512)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={"cpu_limit": 3.0},
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 3.0
        assert kwargs["memory"] == "2g"
        assert kwargs["pids_limit"] == 512

    async def test_workspace_settings_override_when_no_deploy_default(
        self, workspace, monkeypatch
    ):
        # No deploy default + an override -> the override applies; the
        # other two limits stay None (no flag). The deploy defaults are
        # non-empty out of the box now (#2030), so null them out here to
        # exercise the genuine "no deploy default" path.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", None)
        monkeypatch.setattr(settings, "container_memory_limit", None)
        monkeypatch.setattr(settings, "container_pids_limit", None)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={"cpu_limit": 2.0},
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 2.0
        assert kwargs["memory"] is None
        assert kwargs["pids_limit"] is None

    async def test_workspace_settings_empty_bag_uses_deploy_default(
        self, workspace, monkeypatch
    ):
        # An empty/None bag is a no-op: deploy defaults apply unchanged.
        settings = self.registry.app.state.settings
        monkeypatch.setattr(settings, "container_cpu_limit", 1.5)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    workspace_settings={},
                )
            )
        kwargs = p.create_container.call_args.kwargs
        assert kwargs["cpus"] == 1.5

    async def test_sudo_disabled_by_default(self, workspace):
        # #3047: an absent bag key means sudo is OFF — a fresh deploy
        # grants nothing. The opt-in (bag allow_sudo=true on a
        # sudo-enabled deploy) is pinned by the tests below.
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        call = _sudo_call(p)
        assert call.kwargs.get("user") == "root"
        assert "!ALL" in str(call.args[1])

    async def test_sudo_enabled_explicit_opt_in(
        self, workspace, app_state, monkeypatch
    ):
        """#3047: sudo needs an explicit bag opt-in; the deploy flag is
        only a ceiling, so it alone grants nothing (the absent-key case
        is pinned by test_sudo_disabled_by_default)."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": True}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        call = _sudo_call(p)
        assert call.kwargs.get("user") == "root"
        assert "NOPASSWD:ALL" in str(call.args[1])

    async def test_sudo_workspace_lockdown_overrides_deploy_on(
        self, workspace, app_state, monkeypatch
    ):
        """#2017: settings.allow_sudo=false locks a single workspace down
        even on a deploy where allow_sudo is on."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": False}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_workspace_true_cannot_raise_deploy_off(
        self, workspace, app_state, monkeypatch
    ):
        """#2017: the deploy setting is a ceiling — a workspace
        allow_sudo=true never grants sudo on a forbidding deploy."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "false"
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": True}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_workspace_absent_means_off(
        self, workspace, monkeypatch
    ):
        """#3047: no bag key = OFF even on a deploy whose allow_sudo
        ceiling is on — the flag is only a permission to check the box,
        not a posture default."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_disabled(self, workspace, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "0"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_disabled_false(self, workspace, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "false"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_toggled_off_to_on(
        self, workspace, monkeypatch, app_state
    ):
        """#3047: start locked down (absent bag), restart after an
        explicit opt-in — the bag flip takes effect on the new start."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

        # "Restart" — remove container state so start_container creates a new one
        self.registry.states.clear()
        await app_state.state.model.workspaces.update_workspace_container(
            workspace["id"], None
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": True}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "NOPASSWD:ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_toggled_on_to_off(
        self, workspace, monkeypatch, app_state
    ):
        """#3047: start opted in (bag true), restart after the lock-down —
        the bag flip takes effect on the new start."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allow_sudo", "true"
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": True}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "NOPASSWD:ALL" in str(_sudo_call(p).args[1])

        self.registry.states.clear()
        await app_state.state.model.workspaces.update_workspace_container(
            workspace["id"], None
        )
        await app_state.state.model.workspaces.update_workspace_settings(
            workspace["id"], workspace["user_id"], {"allow_sudo": False}
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_container_id_persisted_before_start(
        self, workspace, user, app_state
    ):
        # If `start` fails, the id created just before it must already be on
        # record so the next connect can inspect/recreate it rather than
        # orphaning a created-but-unrecorded container.
        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await self.registry.start_container(
                    container.ContainerStartSpec(workspace["id"], "/tmp/home")
                )
        ws = await app_state.state.model.workspaces.get_workspace(
            workspace["id"], user["id"]
        )
        assert ws["container_id"] == "new-cid"
        assert workspace["id"] in self.registry.states

    async def test_cancel_during_start_still_persists(
        self, workspace, user, app_state
    ):
        # The connecting client can disconnect mid-startup, cancelling this
        # coroutine. The shield must let create+persist+start finish so a
        # running container is never orphaned with a NULL container_id.
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_start(_cid, **kwargs):
            started.set()
            await release.wait()

        with patch_podman(
            self.registry, start_container=AsyncMock(side_effect=slow_start)
        ) as p:
            task = asyncio.create_task(
                self.registry.start_container(
                    container.ContainerStartSpec(workspace["id"], "/tmp/home")
                )
            )
            await started.wait()
            task.cancel()  # client disconnects mid-startup
            release.set()  # let the shielded inner run to completion
            with pytest.raises(asyncio.CancelledError):
                await task

        # Despite the cancel, the container was started and recorded.
        ws = await app_state.state.model.workspaces.get_workspace(
            workspace["id"], user["id"]
        )
        assert ws["container_id"] == "new-cid"
        p.start_container.assert_awaited_once_with("new-cid", hooks_dir=None)
        assert workspace["id"] in self.registry.states

    async def test_reuse_running_container(self, workspace):
        with patch_podman(
            self.registry, inspect_container=_running(True)
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="existing-cid",
                )
            )
        assert cid == "existing-cid"
        assert status == "connected"
        p.start_container.assert_not_awaited()
        p.create_container.assert_not_awaited()

    async def test_stale_id_adopts_running_labeled_container(
        self, workspace, user
    ):
        # #2676: after an unclean host restart the id carried by the
        # caller's snapshot can be stale while a (differently-id'd)
        # workspace container is actually running. The start path must
        # reconcile against live podman state by label — adopting the
        # running container exactly like a matching-id reconnect —
        # instead of racing the create path into the live pair.
        ws_id = workspace["id"]
        live = {
            "Id": "live-cid",
            "Names": ["klangk-ws"],
            "State": "running",
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        with patch_podman(
            self.registry,
            inspect_container=AsyncMock(return_value=None),
            list_containers=AsyncMock(return_value=[live]),
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    ws_id,
                    "/tmp/home",
                    existing_container_id="stale-cid",
                )
            )
        assert cid == "live-cid"
        assert status == "connected"
        p.create_container.assert_not_awaited()
        p.start_container.assert_not_awaited()
        p.remove_container.assert_not_awaited()
        # The DB id is re-persisted so the staleness heals for every
        # later caller (restart, API start, reconnect).
        fresh = await self.registry.app.state.model.workspaces.get_workspace(
            ws_id, user["id"]
        )
        assert fresh["container_id"] == "live-cid"

    async def test_stale_id_adopt_skips_unidentifiable_entries(
        self, workspace
    ):
        # #2676: a ps entry with no usable ident (no Id/ID/Names) is
        # skipped, not crashed on.
        ws_id = workspace["id"]
        identless = {
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
            # no Id / ID / Names
        }
        with patch_podman(
            self.registry,
            inspect_container=AsyncMock(return_value=None),
            list_containers=AsyncMock(return_value=[identless]),
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    ws_id,
                    "/tmp/home",
                    existing_container_id="stale-cid",
                )
            )
        # Nothing to adopt — a fresh container was created.
        assert cid == "new-cid"
        assert status == "created"
        p.remove_container.assert_not_awaited()

    async def test_stale_id_adopt_runs_fips_gate(self, workspace, monkeypatch):
        # #2676: adoption is the handle_existing_container running branch
        # — the FIPS gate (#2626) must run on the label-adopted container
        # too, not only on a matching-id adopt.
        ws_id = workspace["id"]
        monkeypatch.setattr(
            self.registry.app.state.settings, "fips_mode", True
        )
        live = {
            "Id": "live-cid",
            "State": "running",
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        gate = AsyncMock()
        with (
            patch_podman(
                self.registry,
                inspect_container=AsyncMock(return_value=None),
                list_containers=AsyncMock(return_value=[live]),
            ),
            patch.object(self.registry, "_fips_gate", gate),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    ws_id,
                    "/tmp/home",
                    existing_container_id="stale-cid",
                )
            )
        gate.assert_awaited_once_with(ws_id, "live-cid")

    async def test_stale_id_removes_stopped_labeled_container(self, workspace):
        # #2676: a STOPPED labeled container found by the reconcile scan is
        # removed before the create proceeds — a present dependent would
        # make the sidecar pre-remove's rm fail with "dependent containers".
        ws_id = workspace["id"]
        stopped = {
            "Id": "stopped-cid",
            "Names": ["klangk-ws"],
            "State": "exited",
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "workspace",
            },
        }
        sidecar = {
            "Id": "sidecar-cid",
            "Names": ["klangk-net"],
            "State": "exited",
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            inspect_container=AsyncMock(return_value=None),
            list_containers=AsyncMock(return_value=[stopped, sidecar]),
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    ws_id,
                    "/tmp/home",
                    existing_container_id="stale-cid",
                )
            )
        assert cid == "new-cid"
        assert status == "created"
        # Only the workspace container was removed — the scan's role
        # filter leaves the sidecar to the sidecar paths.
        assert p.remove_container.await_args_list[0].args == ("stopped-cid",)
        assert len(p.remove_container.await_args_list) == 1

    async def test_stale_id_scan_failure_falls_through_to_create(
        self, workspace
    ):
        # #2676: the label reconcile is best-effort — a failed `podman ps`
        # must not break a start that would legitimately create a fresh
        # container.
        with patch_podman(
            self.registry,
            inspect_container=AsyncMock(return_value=None),
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "ps failed")
            ),
        ):
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="stale-cid",
                )
            )
        assert cid == "new-cid"
        assert status == "created"

    async def test_reuse_running_filtered_container_retracks_sidecar(
        self, workspace, monkeypatch
    ):
        # #2248 review nit: _ws_with_network_sidecar is in-memory (lost on a
        # process restart). Reconnecting to a running FILTERED workspace must
        # re-track its sidecar so a later stop tears it down instead of leaking
        # it (only the create path added it before).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        assert workspace["id"] not in self.registry._ws_with_network_sidecar
        with patch_podman(
            self.registry, inspect_container=_running(True)
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="existing-cid",
                    allowed_domains=["github.com:443"],
                )
            )
        assert cid == "existing-cid"
        assert status == "connected"
        p.start_container.assert_not_awaited()
        p.create_container.assert_not_awaited()
        # Re-tracked: a later stop will now tear the sidecar down.
        assert workspace["id"] in self.registry._ws_with_network_sidecar

    async def test_recreate_stopped_container(self, workspace):
        with patch_podman(
            self.registry, inspect_container=_running(False)
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="old-cid",
                )
            )
        assert cid == "new-cid"
        assert status == "created"
        p.remove_container.assert_awaited_once_with("old-cid")

    async def test_missing_container_creates_new(self, workspace):
        # inspect_container returns None (default) → treated as gone.
        with patch_podman(self.registry) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="gone-cid",
                )
            )
        assert cid == "new-cid"
        assert status == "created"
        p.remove_container.assert_not_awaited()

    async def test_create_clears_restart_duration_verdicts(
        self, workspace, user
    ):
        # #2346: a fresh container (re)start reaps the workspace's
        # restart-duration verdicts -- the sidecar's in-memory rules died
        # with the previous container, so list_active must not report them.
        ec = self.registry.app.state.model.egress_consent
        a = await ec.create_request(workspace["id"], "stale.com", 443)
        await ec.decide(a["id"], "allowed", user["id"], "tilrestart")
        # sanity: in effect before the start
        assert {
            r["dest_host"] for r in await ec.list_active(workspace["id"])
        } == {"stale.com"}
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert await ec.list_active(workspace["id"]) == []

    async def test_reuse_running_container_keeps_restart_verdicts(
        self, workspace, user
    ):
        # #2346: the "connected" path (already running) must NOT reap -- the
        # container didn't restart, so its in-memory rules are still alive.
        ec = self.registry.app.state.model.egress_consent
        a = await ec.create_request(workspace["id"], "live.com", 443)
        await ec.decide(a["id"], "allowed", user["id"], "tilrestart")
        with patch_podman(self.registry, inspect_container=_running(True)):
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    existing_container_id="existing-cid",
                )
            )
        assert status == "connected"
        assert {
            r["dest_host"] for r in await ec.list_active(workspace["id"])
        } == {"live.com"}

    async def test_disallowed_image_raises(self, workspace):
        with pytest.raises(ValueError, match="not in the allowed list"):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"], "/work", "/home", image="evil:latest"
                )
            )

    async def test_llm_proxy_env_vars(self, workspace, monkeypatch):
        """Container gets proxy URL, not real API keys."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "egress_port", "8995"
        )

        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        kwargs = p.create_container.call_args.kwargs
        env = kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["KLANGKWS_LLM_PROXY_URL"] == (
            "http://host.containers.internal:8995/llm-proxy"
        )
        # The agent's home is injected at container start so every exec
        # process (terminals, service command, health check) inherits it.
        # Fixed identity: the agent is 'klangk' (#2718).
        assert env_dict["KLANGKWS_AGENT_HOME"] == "/home/klangk"
        assert (
            env_dict["KLANGKWS_BRIDGE_URL"]
            == "http://host.containers.internal:8995"
        )
        # API keys should NOT be in the container env
        assert not any(e.startswith("KLANGKD_LLM_API_KEY=") for e in env)
        assert not any(e.startswith("ANTHROPIC_API_KEY=") for e in env)
        # host.containers.internal must be resolvable
        assert "host.containers.internal:host-gateway" in kwargs["add_hosts"]

    async def test_user_logname_env_vars(self, workspace, monkeypatch):
        """USER/LOGNAME are set so tools inside the container see the
        correct UNIX user (#2153)."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["USER"] == "klangk"
        assert env_dict["LOGNAME"] == "klangk"

    async def test_workspace_token_written_to_container(
        self, workspace, app_state
    ):
        """Workspace token is written to the container via set_workspace_token."""
        with (
            patch_podman(self.registry),
            patch.object(
                self.registry.app.state.terminal,
                "set_workspace_token",
                new_callable=AsyncMock,
            ) as mock_set,
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        mock_set.assert_called_once()
        cid, token = mock_set.call_args.args
        assert cid == "new-cid"
        assert cid == "new-cid"
        decoded_ws = self.registry.app.state.auth.decode_workspace_token(token)
        assert decoded_ws == workspace["id"]

    async def test_pull_policy_default_never(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert p.create_container.call_args.kwargs["pull"] == "never"

    async def test_pull_policy_from_env(self, workspace, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "image_pull_policy", "missing"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert p.create_container.call_args.kwargs["pull"] == "missing"

    async def test_config_mount_added(self, workspace):
        """Container gets read-only config mount when config_path is set."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    config_path="/tmp/config",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/config:/opt/klangk/config:ro" in binds

    async def test_no_config_mount_without_config_path(self, workspace):
        """Container has no config mount when config_path is not set."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("config" in b for b in binds)

    async def test_home_mounted_at_slash_home(self, workspace):
        """Home path is mounted at /home (not /home/klangk)."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/home:/home" in binds

    async def test_hosting_env_vars(self, workspace, monkeypatch):
        monkeypatch.setattr(self.registry.app.state.settings, "port", "8997")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    hosting_hostname="example.com",
                    hosting_proto="https",
                    hosting_base_path="/klangk",
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGKWS_HOSTING_HOSTNAME=example.com" in env
        assert "KLANGKWS_HOSTING_PROTO=https" in env
        assert "KLANGKWS_HOSTING_BASE_PATH=/klangk" in env

    async def test_hosting_env_vars_default_gains_browser_port(
        self, workspace, monkeypatch
    ):
        """Omitted hosting_* resolves through derive_hosting_info (#1240, #2732).

        This is the path ``eager_start_workspace`` takes (autostart /
        workspace create have no request to derive from). Before #1240 the
        eager path bypassed ``derive_hosting_info`` entirely, so a deployer
        who set ``KLANGKD_HOSTING_HOSTNAME`` saw it ignored. #2732: the
        synthetic loopback floor now carries the browser-listener port, so
        the hosted URL baked at setup names the listener that actually
        serves ``/hosted/`` — never the egress port (#1240) and never bare
        port-80 localhost.
        """
        monkeypatch.setattr(self.registry.app.state.settings, "port", "8997")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGKWS_HOSTING_HOSTNAME=localhost:8997" in env
        assert "KLANGKWS_HOSTING_PROTO=http" in env
        assert "KLANGKWS_HOSTING_BASE_PATH=" in env

    async def test_headless_omits_hosting_env(self, workspace):
        """Headless (KLANGKD_PORT unset) suppresses the hosting env (#2732).

        ``/hosted/`` is served by the browser listener, which headless mode
        does not render — any hosted URL baked now would be dead on arrival.
        Same clean-error outcome as the cap-0 case: klangk-hosted-url /
        get_hosted_url error out, non-hosting env is untouched.
        """
        # Fixture settings are headless by default (no KLANGKD_PORT).
        assert self.registry.app.state.settings.port is None
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=5,
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("KLANGKWS_PORT_MAPPINGS=") for e in env)
        assert not any(e.startswith("KLANGKWS_HOSTING_") for e in env)
        assert any(e.startswith("KLANGKWS_WORKSPACE_ID=") for e in env)
        assert any(e.startswith("KLANGKWS_LLM_PROXY_URL=") for e in env)

    async def test_terminal_banner_default_empty(self, workspace):
        """Default terminal banner is empty, so env var is not passed."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("KLANGKWS_TERMINAL_BANNER=") for e in env)

    async def test_terminal_banner_custom(self, workspace, monkeypatch):
        """Deployer can set a terminal banner via env var."""
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "terminal_banner",
            "Custom warning",
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGKWS_TERMINAL_BANNER=Custom warning" in env

    async def test_ssl_trust_mounted_when_cert_dir_configured(
        self, workspace, monkeypatch, tmp_path
    ):
        """A populated <customize_dir>/certs is bind-mounted ro and env set (#1181)."""
        customize = tmp_path / "custom"
        ssl_dir = customize / "certs"
        ssl_dir.mkdir(parents=True)
        (ssl_dir / "corp-ca.pem").write_text("-----BEGIN CERTIFICATE-----")
        monkeypatch.setattr(
            self.registry.app.state.settings, "customize_dir", str(customize)
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert f"{ssl_dir.resolve()}:/opt/klangk/ssl:ro" in binds
        env = p.create_container.call_args.kwargs["env"]
        assert "SSL_CERT_FILE=/tmp/klangk/ca-bundle.crt" in env
        assert "REQUESTS_CA_BUNDLE=/tmp/klangk/ca-bundle.crt" in env
        assert "CURL_CA_BUNDLE=/tmp/klangk/ca-bundle.crt" in env
        assert "NODE_EXTRA_CA_CERTS=/tmp/klangk/ca-bundle.crt" in env

    async def test_no_ssl_trust_when_cert_dir_unset(self, workspace):
        """Without certs in <customize_dir>/certs/ there is no mount and no trust env."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("/opt/klangk/ssl" in b for b in binds)
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("SSL_CERT_FILE=") for e in env)

    async def test_no_ssl_trust_when_dir_has_no_certs(
        self, workspace, tmp_path, monkeypatch
    ):
        """A certs dir with no .pem/.crt is not mounted (#1181)."""
        customize = tmp_path / "custom"
        ssl_dir = customize / "certs"
        ssl_dir.mkdir(parents=True)
        (ssl_dir / "notes.txt").write_text("not a cert")
        # Point the registry's SSLTrust at the certs dir (via customize_dir)
        # so ssl_cert_dir() actually evaluates it.
        monkeypatch.setattr(
            self.registry.app.state.settings, "customize_dir", str(customize)
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("/opt/klangk/ssl" in b for b in binds)
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("SSL_CERT_FILE=") for e in env)

    async def test_port_allocation_on_create(self, workspace):
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=3,
                )
            )
        # Ports should have been allocated
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_excess_ports_trimmed(self, workspace, app_state):
        # Pre-allocate more ports than needed
        await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=2,
                )
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 2

    async def test_cap_clamps_allocation_down(self, workspace, monkeypatch):
        """KLANGKD_HOSTED_PORTS_PER_WORKSPACE clamps num_ports down (#1237)."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "hosted_ports_per_workspace", 3
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=5,  # DB default; cap is 3
                )
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_cap_zero_releases_existing_ports(
        self, workspace, monkeypatch, app_state
    ):
        """cap=0 trims an existing workspace's allocations on next start."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "hosted_ports_per_workspace", 0
        )
        await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=5,
                )
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_cap_zero_omits_hosting_env(self, workspace, monkeypatch):
        """cap=0 suppresses KLANGKWS_PORT_MAPPINGS / KLANGKWS_HOSTING_* (#1237)."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "hosted_ports_per_workspace", 0
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=5,
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("KLANGKWS_PORT_MAPPINGS=") for e in env)
        assert not any(e.startswith("KLANGKWS_HOSTING_") for e in env)
        # Non-hosting env is still present.
        assert any(e.startswith("KLANGKWS_WORKSPACE_ID=") for e in env)
        assert any(e.startswith("KLANGKWS_LLM_PROXY_URL=") for e in env)

    async def test_cap_zero_blocks_creation_allocation(
        self, workspace, monkeypatch
    ):
        """cap=0 means allocate_ports (creation path) inserts nothing (#1237).

        Distinct from the reconcile/trim path: this is the entry point
        ``workspaces.create_workspace`` uses at workspace-creation time,
        so a cap of 0 must keep port_allocations empty from the start —
        not just trim on the container's first start.
        """
        monkeypatch.setattr(
            self.registry.app.state.settings, "hosted_ports_per_workspace", 0
        )
        await self.registry.allocate_ports(workspace["id"], 5)
        assert await self.registry.get_workspace_ports(workspace["id"]) == []

    async def test_hosting_env_present_when_enabled(
        self, workspace, monkeypatch
    ):
        """Sanity: with the default cap and a browser listener, hosting env is injected."""
        monkeypatch.setattr(self.registry.app.state.settings, "port", "8997")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    num_ports=5,
                )
            )
        env = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["KLANGKWS_PORT_MAPPINGS"].count(",") == 4  # 5 mappings
        assert "KLANGKWS_HOSTING_HOSTNAME" in env_dict
        assert "KLANGKWS_HOSTING_PROTO" in env_dict
        assert "KLANGKWS_HOSTING_BASE_PATH" in env_dict

    async def test_container_config_structure(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                )
            )
        args, kwargs = p.create_container.call_args
        assert args[1] == self.registry.image_name
        assert kwargs["labels"]["klangk.managed"] == "true"
        # #2342: the creating daemon's PID (dead-owner reap liveness signal).
        assert kwargs["labels"]["klangk.pid"].isdigit()
        # #2286: shared label + role (supersedes klangk.workspace-id).
        assert kwargs["labels"]["klangk.workspace"] == workspace["id"]
        assert kwargs["labels"]["klangk.role"] == "workspace"
        assert kwargs["init"] is True
        assert kwargs["interactive"] is True

    async def test_create_container_with_extra_env(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_env={"MY_VAR": "hello", "FOO": "bar"},
                )
            )
        env_list = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["MY_VAR"] == "hello"
        assert env_dict["FOO"] == "bar"

    async def test_features_env_injected(
        self, workspace, monkeypatch, app_state
    ):
        # container_env() reads the build-emitted container_env_keys list and
        # resolves each from the server env (#1655). Feature-declared keys
        # must carry the KLANGKWS_FEATURE_ prefix (#1662). Patch the parsed
        # manifest so the test doesn't need a real features.json on disk.
        monkeypatch.setattr(
            self.registry.app.state.features,
            "_manifest",
            {"container_env_keys": ["KLANGKWS_FEATURE_TEST_VAR"]},
        )
        monkeypatch.setenv("KLANGKWS_FEATURE_TEST_VAR", "feature-val")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        env_list = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["KLANGKWS_FEATURE_TEST_VAR"] == "feature-val"


class TestStartContainerPortConflict:
    """Test retry logic when a stale container holds a port."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_port_conflict_removes_stale_and_retries(
        self, workspace, app_state
    ):
        # Pre-allocate ports so we know exactly which ones the workspace gets.
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(
                    500,
                    f"Bind for 0.0.0.0:{conflict_port} failed: "
                    "port is already allocated",
                )

        stale_info = {
            "HostConfig": {
                "PortBindings": {
                    "8000/tcp": [{"HostPort": str(conflict_port)}]
                }
            }
        }

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "stale-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(return_value=stale_info),
        ) as p:
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert status == "created"
        assert len(start_calls) == 2
        remove_calls = [c.args[0] for c in p.remove_container.call_args_list]
        assert "stale-cid" in remove_calls

    async def test_port_conflict_skips_own_container(
        self, workspace, app_state
    ):
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "new-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(
                return_value={
                    "HostConfig": {
                        "PortBindings": {
                            "8000/tcp": [{"HostPort": str(conflict_port)}]
                        }
                    }
                }
            ),
        ) as p:
            cid, _ = await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        # Should not have tried to remove its own container
        for call in p.remove_container.call_args_list:
            assert call.args[0] != "new-cid"

    async def test_port_conflict_skips_non_overlapping(self, workspace):
        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "other-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(
                return_value={
                    "HostConfig": {
                        "PortBindings": {"8000/tcp": [{"HostPort": "59999"}]}
                    }
                }
            ),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        # other-cid doesn't hold our ports — should not be removed

    async def test_port_conflict_stale_vanished(self, workspace, app_state):
        """Stale container gone by the time we inspect it."""
        await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "gone-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(return_value=None),
        ) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        # gone-cid vanished — no remove attempted
        assert not any(
            c.args[0] == "gone-cid" for c in p.remove_container.call_args_list
        )

    async def test_port_conflict_bad_port_bindings(self, workspace, app_state):
        """Malformed HostPort values don't crash the retry."""
        await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "bad-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(
                return_value={
                    "HostConfig": {
                        "PortBindings": {
                            "80/tcp": [{"HostPort": "not-a-number"}],
                            "81/tcp": [{}],
                            "82/tcp": None,
                        }
                    }
                }
            ),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )

    async def test_port_conflict_remove_error_logged(
        self, workspace, app_state
    ):
        """Error removing stale container is logged, not raised."""
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]
        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "stuck-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(
                return_value={
                    "HostConfig": {
                        "PortBindings": {
                            "8000/tcp": [{"HostPort": str(conflict_port)}]
                        }
                    }
                }
            ),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "removal in progress")
            ),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )

    async def test_non_port_conflict_error_raised(self, workspace):
        with (
            patch_podman(
                self.registry,
                start_container=AsyncMock(
                    side_effect=podman.PodmanError(500, "some other error")
                ),
            ),
            pytest.raises(podman.PodmanError, match="some other error"),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )

    async def test_port_conflict_pasta_bind_error(self, workspace, app_state):
        """Pasta-style 'Address already in use' errors trigger retry (#1810)."""
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(
                    409,
                    f"Failed to bind port {conflict_port} "
                    "(Address already in use)",
                )

        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(return_value=[]),
            inspect_container=AsyncMock(return_value=None),
        ):
            cid, status = await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )
        assert status == "created"
        assert len(start_calls) == 2

    async def test_port_conflict_retries_exhausted(self, workspace, app_state):
        """All retries exhausted re-raises the last PodmanError."""
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        async def always_fail(cid, **kwargs):
            raise podman.PodmanError(
                409,
                f"Failed to bind port {conflict_port} "
                "(Address already in use)",
            )

        with (
            patch_podman(
                self.registry,
                start_container=AsyncMock(side_effect=always_fail),
                list_containers=AsyncMock(return_value=[]),
                inspect_container=AsyncMock(return_value=None),
            ),
            pytest.raises(podman.PodmanError, match="Address already in use"),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )

    async def test_port_conflict_retry_non_conflict_error(
        self, workspace, app_state
    ):
        """Non-port-conflict error during retry is raised immediately."""
        allocated = await app_state.state.model.ports.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid, **kwargs):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(
                    409,
                    f"Failed to bind port {conflict_port} "
                    "(Address already in use)",
                )
            raise podman.PodmanError(500, "container vanished")

        with (
            patch_podman(
                self.registry,
                start_container=AsyncMock(side_effect=start_side_effect),
                list_containers=AsyncMock(return_value=[]),
                inspect_container=AsyncMock(return_value=None),
            ),
            pytest.raises(podman.PodmanError, match="container vanished"),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )


class TestValidateMountSpec:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_valid_bind_mount(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/host",
        )
        assert self.registry.validate_mount_spec("/host:/container") is None

    def test_valid_volume_mount(self):
        assert self.registry.validate_mount_spec("vol-name:/data") is None

    def test_valid_with_options(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/host",
        )
        assert self.registry.validate_mount_spec("/host:/container:ro") is None

    def test_valid_with_multiple_options(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/host",
        )
        assert (
            self.registry.validate_mount_spec("/host:/container:ro,nocopy")
            is None
        )

    def test_no_colon(self):
        err = self.registry.validate_mount_spec("nocolon")
        assert err is not None
        assert "expected" in err.lower()

    def test_too_many_colons(self):
        err = self.registry.validate_mount_spec("a:b:c:d")
        assert err is not None

    def test_empty_source(self):
        err = self.registry.validate_mount_spec(":/container")
        assert err is not None
        assert "source is empty" in err.lower()

    def test_relative_container_path(self):
        err = self.registry.validate_mount_spec("/host:relative")
        assert err is not None
        assert "absolute" in err.lower()

    def test_unknown_option(self):
        err = self.registry.validate_mount_spec("/host:/container:bogus")
        assert err is not None
        assert "unknown option" in err.lower()

    def test_named_volume_leading_dash_rejected(self):
        """#3018: a leading-dash source is parsed as a flag by the podman
        CLI (``podman volume create --opt=...``) — rejected at the mount
        gate, same rule as the volumes API (#2971)."""
        err = self.registry.validate_mount_spec("--opt=x:/data")
        assert err is not None
        assert "podman-safe" in err.lower()

    def test_named_volume_bad_charset_rejected(self):
        """#3018: sources outside [a-zA-Z0-9_.-] (after an alphanumeric
        first char) are rejected — including the trailing-newline case
        that a Python ``re`` ``$`` anchor would wrongly accept.
        (A "."-prefixed source is a bind source, not a volume — that
        case is covered by test_bind_sources_unaffected_by_volume_rule.)"""
        for source in ("has space", "ex!am", "_under", "a\nb"):
            err = self.registry.validate_mount_spec(f"{source}:/data")
            assert err is not None, source
            assert "podman-safe" in err.lower()

    def test_named_volume_length_cap(self):
        """#3018: the 64-char boundary passes, 65 chars rejects."""
        assert self.registry.validate_mount_spec(f"{'a' * 64}:/data") is None
        err = self.registry.validate_mount_spec(f"{'a' * 65}:/data")
        assert err is not None
        assert "podman-safe" in err.lower()

    def test_bind_sources_unaffected_by_volume_rule(self, monkeypatch):
        """#3018: absolute paths and '.'-prefixed (bind) sources never hit
        the volume-name rule — they keep the protected/allowed-root
        (or disabled, #3153) path."""
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/host",
        )
        assert self.registry.validate_mount_spec("/host:/container") is None
        # A relative bind source is NOT rejected by the volume-name
        # rule — it flows the bind path (allowed-root denial here).
        err = self.registry.validate_mount_spec("./cache:/data")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_validate_mounts_list(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/a"
        )
        assert self.registry.validate_mounts(["/a:/b", "vol:/c"]) is None

    def test_validate_mounts_list_with_error(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/a"
        )
        err = self.registry.validate_mounts(["/a:/b", "bad"])
        assert err is not None


class TestAllowedMountRoots:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_bind_mount_allowed(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/home,/data",
        )
        assert (
            self.registry.validate_mount_spec("/home/user/src:/work") is None
        )

    def test_bind_mount_exact_root(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/home"
        )
        assert self.registry.validate_mount_spec("/home:/work") is None

    def test_bind_mount_denied(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/home,/data",
        )
        err = self.registry.validate_mount_spec("/etc/passwd:/etc/passwd:ro")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_bind_mount_traversal_denied(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/home"
        )
        err = self.registry.validate_mount_spec("/home/../etc:/work")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_named_volume_always_allowed(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/home"
        )
        assert self.registry.validate_mount_spec("my-volume:/data") is None

    def test_bind_mounts_disabled_when_unset(self, monkeypatch):
        """#3153 deny-by-default: with no roots configured, ANY host-path
        bind source is rejected — only named volumes may be mounted."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", ""
        )
        err = self.registry.validate_mount_spec("/etc/shadow:/secrets")
        assert err is not None
        assert "bind mounts are disabled" in err
        err = self.registry.validate_mount_spec("/home/user/src:/work")
        assert err is not None
        assert "KLANGKD_ALLOWED_MOUNT_ROOTS" in err

    def test_named_volume_ok_when_unset(self, monkeypatch):
        """Named volumes are unaffected by the bind-mount gate."""
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", ""
        )
        assert self.registry.validate_mount_spec("my-volume:/data") is None

    def test_multiple_roots(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            "/home,/data,/opt",
        )
        assert self.registry.validate_mount_spec("/data/files:/work") is None
        assert self.registry.validate_mount_spec("/opt/app:/app") is None
        err = self.registry.validate_mount_spec("/var/log:/logs")
        assert err is not None


class TestProtectedPaths:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_docker_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/"
        )
        err = self.registry.validate_mount_spec(
            "/var/run/docker.sock:/var/run/docker.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_podman_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/"
        )
        err = self.registry.validate_mount_spec(
            "/run/podman/podman.sock:/run/podman/podman.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_data_dir_blocked(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/"
        )
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", "/srv/klangk/data"
        )
        err = self.registry.validate_mount_spec(
            "/srv/klangk/data/workspaces:/loot"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_protected_blocked_even_without_allowlist(self):
        err = self.registry.validate_mount_spec(
            "/var/run/docker.sock:/var/run/docker.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_symlink_to_protected_path_blocked(self, tmp_path):
        """Symlinks to protected paths are resolved and blocked."""
        link = tmp_path / "sneaky-sock"
        link.symlink_to("/var/run/docker.sock")
        err = self.registry.validate_mount_spec(f"{link}:/mnt/sock")
        assert err is not None
        assert "protected" in err.lower()

    def test_symlink_to_allowed_root_passes(self, monkeypatch):
        """Symlinks resolved to an allowed root pass validation."""
        import tempfile

        # Use a separate temp dir so it doesn't overlap with the
        # KLANGKD_DATA_DIR that conftest sets to tmp_path.
        with tempfile.TemporaryDirectory(prefix="mount-test-") as d:
            d = Path(d)
            allowed = d / "allowed"
            allowed.mkdir()
            target = allowed / "data"
            target.mkdir()
            link = d / "link-to-data"
            link.symlink_to(str(target))

            monkeypatch.setattr(
                self.registry.app.state.settings,
                "allowed_mount_roots",
                str(allowed),
            )
            err = self.registry.validate_mount_spec(f"{link}:/mnt/data")
            assert err is None

    def test_symlink_outside_allowed_root_blocked(self, monkeypatch):
        """Symlinks resolving outside allowed roots are blocked."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mount-test-") as d:
            d = Path(d)
            allowed = d / "allowed"
            allowed.mkdir()
            outside = d / "outside"
            outside.mkdir()
            link = d / "link-to-outside"
            link.symlink_to(str(outside))

            monkeypatch.setattr(
                self.registry.app.state.settings,
                "allowed_mount_roots",
                str(allowed),
            )
            err = self.registry.validate_mount_spec(f"{link}:/mnt/data")
            assert err is not None
            assert "allowed root" in err.lower()


class TestBindMountStartGate:
    """#3278: the start path re-runs the settings gate's bind-source
    containment (protected paths, allowed roots, realpath) immediately
    before the podman argv is built — the #3018 posture, bind side."""

    def setup_method(self):
        app_state = _make_app_state()
        self.app = app_state
        self.registry = app_state.state.container_registry

    def _allow(self, monkeypatch, roots):
        monkeypatch.setattr(
            self.app.state.settings, "allowed_mount_roots", roots
        )

    async def test_protected_path_refused_at_start(self, monkeypatch):
        """The issue's repro 1 (hardened deploy): /etc passes the old
        existence-only check; with roots configured that don't contain
        it, the start gate refuses it."""
        self._allow(monkeypatch, "/opt/allowed")
        with pytest.raises(ValueError, match="allowed root"):
            await ensure_volumes(self.app, ["/etc:/x"], "ws", None)

    async def test_bind_refused_when_roots_unset(self):
        """#3153 deny-by-default also holds at start: with no roots
        configured, every bind source is refused — a row that reached
        the DB without the API gate starts nothing."""
        with pytest.raises(ValueError, match="bind mounts are disabled"):
            await ensure_volumes(self.app, ["/etc:/x"], "ws", None)

    async def test_data_dir_refused_at_start(self, tmp_path):
        """The deploy's own data dir is protected at start too — the
        protected-path check runs before the roots question."""
        ws_dir = tmp_path / "workspaces"
        ws_dir.mkdir()
        with pytest.raises(ValueError, match="protected host path"):
            await ensure_volumes(self.app, [f"{ws_dir}:/loot"], "ws", None)

    async def test_symlink_swap_refused_at_start(self, tmp_path, monkeypatch):
        """The issue's repro 2: a source validated under an allowed root,
        then swapped for a symlink to /, is refused at start —
        realpath resolves through the swap before containment."""
        root = tmp_path / "allowed"
        share = root / "share"
        root.mkdir()
        share.mkdir()
        share.rmdir()
        share.symlink_to("/")
        self._allow(monkeypatch, str(root))
        with pytest.raises(ValueError, match="allowed root"):
            await ensure_volumes(self.app, [f"{share}:/x"], "ws", None)

    async def test_bind_under_root_passes_at_start(self, monkeypatch):
        """A real directory under a configured root still starts. The
        root lives outside the test's tmp_path — that IS the data dir,
        and mounting under it is refused as protected."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mount-start-") as d:
            share = Path(d) / "share"
            share.mkdir()
            self._allow(monkeypatch, d)
            await ensure_volumes(self.app, [f"{share}:/x"], "ws", None)

    async def test_start_container_refuses_symlink_escaped_mount(
        self, workspace, monkeypatch
    ):
        """Full start: the refusal fires before any podman argv is
        built — create_container is never reached."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="mount-start-") as d:
            share = Path(d) / "share"
            share.symlink_to("/")
            self._allow(monkeypatch, d)
            with patch_podman(self.registry) as p:
                with pytest.raises(ValueError, match="allowed root"):
                    await self.registry.start_container(
                        container.ContainerStartSpec(
                            workspace["id"],
                            "/tmp/home",
                            extra_mounts=[f"{share}:/x"],
                        )
                    )
            p.create_container.assert_not_awaited()


class TestExtraMountsVolumeCreation:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_auto_creates_named_volume(self, workspace, app_state):
        """Named volumes (no leading /) are auto-created with klangk labels."""
        # inspect_volume returns None (default) → volume is created.
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["nix-store:/nix"],
                    user_id="user-123",
                )
            )
        p.create_volume.assert_awaited_once()
        name, labels = p.create_volume.call_args.args
        assert name == "nix-store"
        assert labels["klangk.managed"] == "true"
        assert labels["klangk.workspace-id"] == workspace["id"]
        assert (
            labels["klangk.instance"]
            == self.registry.app.state.util.instance_id()
        )
        # #3153: workspace-owned — no user stamp at all.
        assert "klangk.user-id" not in labels

    async def test_unsafe_volume_name_rejected_at_start(self, workspace):
        """#3018 defense in depth: a row that slipped past the API gate
        (created before #3018, or via another writer) must not reach
        podman argv either — ValueError before any podman call."""
        mock_inspect = AsyncMock()
        mock_create = AsyncMock()
        with patch_podman(
            self.registry,
            inspect_volume=mock_inspect,
            create_volume=mock_create,
        ):
            with pytest.raises(ValueError, match="not podman-safe"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["--opt=x:/data"],
                    )
                )
        assert mock_inspect.await_count == 0
        assert mock_create.await_count == 0

    async def test_existing_volume_not_recreated(self, workspace, app_state):
        """This workspace's existing volume is used as-is, not recreated."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "existing",
                    "Labels": {
                        "klangk.instance": self.registry.app.state.util.instance_id(),
                        "klangk.workspace-id": workspace["id"],
                    },
                }
            ),
        ) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["existing:/data"],
                    user_id="user-123",
                )
            )
        p.create_volume.assert_not_awaited()

    async def test_foreign_volume_rejected(self, workspace):
        """A named volume owned by another instance is refused."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "stolen",
                    "Labels": {"klangk.instance": "someone-else"},
                }
            ),
        ):
            with pytest.raises(ValueError, match="not managed by this"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["stolen:/data"],
                    )
                )

    async def test_unlabelled_volume_rejected(self, workspace):
        """A named volume with no klangk labels is refused."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(return_value={"Name": "bare"}),
        ):
            with pytest.raises(ValueError, match="not managed by this"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["bare:/data"],
                    )
                )

    async def test_cross_workspace_volume_rejected(self, workspace, app_state):
        """A volume owned by another workspace is refused (#3153) —
        volumes cannot be shared between workspaces, whoever starts."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "private",
                    "Labels": {
                        "klangk.instance": self.registry.app.state.util.instance_id(),
                        "klangk.workspace-id": "ws-other",
                    },
                }
            ),
        ):
            with pytest.raises(
                ValueError, match="belongs to another workspace"
            ):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["private:/data"],
                        user_id="user-me",
                    )
                )

    async def test_workspace_volume_usable_by_any_starter(
        self, workspace, app_state
    ):
        """#3153: ownership has no user dimension — the same start
        succeeds with no user attributed at all (autonomous restart
        shape) and with any user attributed; only the workspace label
        is consulted."""
        for spec_user in (None, "user-a", "user-b"):
            with patch_podman(
                self.registry,
                inspect_volume=AsyncMock(
                    return_value={
                        "Name": "shared",
                        "Labels": {
                            "klangk.instance": self.registry.app.state.util.instance_id(),
                            "klangk.workspace-id": workspace["id"],
                        },
                    }
                ),
            ) as p:
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["shared:/data"],
                        user_id=spec_user,
                    )
                )
            p.create_volume.assert_not_awaited()

    async def test_auto_create_refused_at_quota(self, workspace, app_state):
        """The start-path auto-create door honors the per-workspace
        volume quota — a workspace at quota cannot mint volumes by
        adding mounts."""
        self.registry.app.state.settings.volume_quota_per_workspace = 1
        try:
            with patch_podman(
                self.registry,
                count_workspace_volumes=AsyncMock(return_value=1),
            ) as p:
                with pytest.raises(ValueError, match="volume quota reached"):
                    await self.registry.start_container(
                        container.ContainerStartSpec(
                            workspace["id"],
                            "/tmp/home",
                            extra_mounts=["v1:/data"],
                            user_id="user-123",
                        )
                    )
            p.create_volume.assert_not_awaited()
        finally:
            self.registry.app.state.settings.volume_quota_per_workspace = 0

    async def test_auto_create_under_quota(self, workspace, app_state):
        """A start-path create below the cap proceeds."""
        self.registry.app.state.settings.volume_quota_per_workspace = 2
        try:
            with patch_podman(
                self.registry,
                count_workspace_volumes=AsyncMock(return_value=1),
            ) as p:
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["v1:/data"],
                    )
                )
            p.create_volume.assert_awaited_once()
            # The count keys on the WORKSPACE, never a user (#3153).
            assert (
                p.count_workspace_volumes.await_args.args[1] == workspace["id"]
            )
        finally:
            self.registry.app.state.settings.volume_quota_per_workspace = 0

    async def test_volume_without_workspace_label_rejected(
        self, workspace, app_state
    ):
        """A managed volume with no workspace label cannot belong to
        this workspace — refused (#3153: no user fallback exists)."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "legacy",
                    "Labels": {
                        "klangk.instance": self.registry.app.state.util.instance_id(),
                        "klangk.user-id": "user-123",
                    },
                }
            ),
        ):
            with pytest.raises(
                ValueError, match="belongs to another workspace"
            ):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["legacy:/data"],
                        user_id="user-123",
                    )
                )

    async def test_bind_mount_not_treated_as_volume(
        self, workspace, monkeypatch
    ):
        """Bind mounts (starting with /) are not treated as volumes."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/home"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["/home/me/src:/work/src"],
                )
            )
        p.inspect_volume.assert_not_awaited()

    async def test_mount_with_multiple_colons(self, workspace, monkeypatch):
        """Mount spec with options (host:container:ro) — source starts with /."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        monkeypatch.setattr(
            self.registry.app.state.settings, "allowed_mount_roots", "/data"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["/data/shared:/mnt/data:ro"],
                )
            )
        # Bind mount, not a volume — inspect_volume should not be called
        p.inspect_volume.assert_not_awaited()

    async def test_volume_mount_with_options(self, workspace):
        """Named volume with options (vol:container:ro) — auto-creates."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["my-vol:/data:ro"],
                )
            )
        p.create_volume.assert_awaited_once()

    async def test_mount_source_with_slash_is_bind(
        self, workspace, monkeypatch
    ):
        """A mount source containing slashes is a bind mount, not a volume."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        # './relative/...' resolves against the cwd — allow that root.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "allowed_mount_roots",
            os.path.realpath("."),
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["./relative/path:/work/rel"],
                )
            )
        p.inspect_volume.assert_not_awaited()

    async def test_volume_create_error_propagates(self, workspace):
        """An error creating a named volume propagates to the caller."""
        with (
            patch_podman(
                self.registry,
                create_volume=AsyncMock(
                    side_effect=podman.PodmanError(500, "internal error")
                ),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(
                    workspace["id"],
                    "/tmp/home",
                    extra_mounts=["bad-vol:/data"],
                )
            )

    async def test_mount_source_with_special_characters(
        self, workspace, monkeypatch
    ):
        """A source with NUL bytes is not a usable host path — refused
        with a clean error (#3278): podman's argv is NUL-terminated, so
        it could never have carried this source anyway."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        with patch_podman(self.registry) as p:
            with pytest.raises(ValueError, match="not a valid host path"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=[
                            "/path/with spaces\x00and\x01binary:/work/bad"
                        ],
                    )
                )
        # Has leading /, so treated as bind mount — never as a volume
        p.inspect_volume.assert_not_awaited()

    async def test_missing_bind_mount_source_rejected(self, workspace):
        """A bind mount with a non-existent source path is refused."""
        with patch_podman(self.registry):
            with pytest.raises(ValueError, match="does not exist"):
                await self.registry.start_container(
                    container.ContainerStartSpec(
                        workspace["id"],
                        "/tmp/home",
                        extra_mounts=["/nonexistent/path:/work/src"],
                    )
                )

    async def test_browsers_revoked_on_creation_failure(self, workspace):
        """If container creation fails, the error propagates cleanly."""
        with (
            patch_podman(
                self.registry,
                create_container=AsyncMock(
                    side_effect=RuntimeError("podman broke")
                ),
            ),
            pytest.raises(RuntimeError, match="podman broke"),
        ):
            await self.registry.start_container(
                container.ContainerStartSpec(workspace["id"], "/tmp/home")
            )

        # No browser registrations should remain for this workspace
        for bid, (ws_id, _sock) in self.registry.browser_routes.items():
            assert ws_id != workspace["id"]


class TestStopContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_stop_running(self):
        self.registry.track_activity("cid", "ws")
        lock = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry) as p:
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks
        assert self.registry._workspace_locks["ws"] is lock

    async def test_stop_removes_network_sidecar_when_workspace_had_one(
        self, monkeypatch
    ):
        # #2254: a workspace that started a network sidecar tears it down on
        # stop (removed by label, #2286). A non-filtered workspace's stop calls
        # stop_network_sidecar too, but it's a no-op (no sidecar found).
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        self.registry.track_activity("cid", "ws1234567890")
        self.registry._ws_with_network_sidecar.add("ws1234567890")
        net_sidecar = {
            "Id": "net-cid",
            "Names": ["klangk-net-ws123456"],
            "Labels": {
                "klangk.workspace": "ws1234567890",
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[net_sidecar]),
        ) as p:
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        removes = [c.args[0] for c in p.remove_container.await_args_list]
        assert "cid" in removes  # the workspace container
        # #2286: the sidecar is removed by id (label-based), not by name.
        assert "net-cid" in removes
        assert "ws1234567890" not in self.registry._ws_with_network_sidecar

    async def test_stop_tears_down_sidecar_for_untracked_workspace(
        self, monkeypatch
    ):
        # #2286 follow-up: a workspace started by autostart or a prior klangkd
        # session isn't in the in-memory registry (_cid_to_wsid / states). When
        # /stop stops it (passing workspace_id), the sidecar must still be torn
        # down -- previously it leaked until the next start's clear-on-start or
        # the startup reaper. (Reproduces the TUI-stop-doesn't-stop-sidecar bug.)
        ws_id = "abcdef1234567890"
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        net_sidecar = {
            "Id": "net-cid",
            "Names": [f"klangk-net-{ws_id[:8]}"],
            "Labels": {
                "klangk.workspace": ws_id,
                "klangk.role": "network-sidecar",
            },
        }
        # NOTE: no track_activity -> not in _cid_to_wsid / states (untracked),
        # mirroring a workspace started outside this process.
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[net_sidecar]),
        ) as p:
            await self.registry.stop_and_remove_container(
                "cid-from-db", workspace_id=ws_id, cause=CAUSE_API
            )
        removes = [c.args[0] for c in p.remove_container.await_args_list]
        # The workspace container is removed ...
        assert "cid-from-db" in removes
        # ... and so is the sidecar, found by label despite no in-memory tracking.
        assert "net-cid" in removes

    async def test_stop_prunes_orphaned_service_session_locks(self):
        # stop_and_remove_container sweeps the per-container service-firing
        # lock dict so it does not grow unbounded with container churn (#1351).
        locks = self.registry._service_session_locks
        locks.clear()
        try:
            # Tracked container (being stopped) + two orphaned entries whose
            # containers are no longer in the registry.
            self.registry.track_activity("alive", "ws-alive")
            self.registry.get_service_session_lock("alive")
            self.registry.get_service_session_lock("orphan-a")
            self.registry.get_service_session_lock("orphan-b")
            assert len(locks) == 3

            with patch_podman(self.registry):
                await self.registry.stop_and_remove_container(
                    "alive", cause=CAUSE_API
                )

            # The stopped container's entry and the orphans are gone; the dict
            # is empty because no container remains tracked.
            assert locks == {}
        finally:
            locks.clear()

    async def test_stop_podman_error(self):
        self.registry.track_activity("cid", "ws")
        self.registry._get_workspace_lock("ws")

        with patch_podman(
            self.registry,
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(404, "gone")
            ),
        ):
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        # Should still remove from tracking
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks

    async def test_stop_reports_success_and_failure(self):
        """The bool return lets drains count only verifiable stops
        (#2527 review): True when gone via this call, False on a podman
        failure or a racing re-bind."""
        # Tracked + clean remove → True.
        self.registry.track_activity("cid-a", "ws-a")
        with patch_podman(self.registry):
            ok = await self.registry.stop_and_remove_container(
                "cid-a", cause=CAUSE_API
            )
        assert ok is True

        # Tracked + podman failure → False.
        self.registry.track_activity("cid-b", "ws-b")
        with patch_podman(
            self.registry,
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "boom")
            ),
        ):
            ok = await self.registry.stop_and_remove_container(
                "cid-b", cause=CAUSE_API
            )
        assert ok is False

        # Re-bound by a racing start → the fresh container is left alone;
        # the old cid's stop reports False.
        self.registry.track_activity("cid-old", "ws-c")
        self.registry.track_activity("cid-new", "ws-c")  # re-bind
        with patch_podman(self.registry):
            ok = await self.registry.stop_and_remove_container(
                "cid-old", workspace_id="ws-c", cause=CAUSE_API
            )
        assert ok is False
        # The fresh container's state survived.
        assert self.registry.states["ws-c"].container_id == "cid-new"

        # Untracked container (no workspace) + clean remove → True.
        with patch_podman(self.registry):
            ok = await self.registry.stop_and_remove_container(
                "cid-x", cause=CAUSE_API
            )
        assert ok is True

    async def test_stop_serializes_under_workspace_lock(self):
        # stop_and_remove_container must acquire the workspace lock before
        # mutating state, so it cannot tear down a registry entry while a
        # start_container holds the lock (#1258).
        self.registry.track_activity("cid", "ws")
        lock = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry):
            async with lock:
                # While the lock is held (as start_container would hold
                # it), stop must not be able to remove state.
                task = asyncio.create_task(
                    self.registry.stop_and_remove_container(
                        "cid", cause=CAUSE_API
                    )
                )
                # Yield repeatedly so the stop task has a chance to run;
                # it should be blocked on the lock.
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not task.done()
                assert "ws" in self.registry.states
                assert self.registry._cid_to_wsid.get("cid") == "ws"
            # Releasing the lock lets stop proceed and clean up.
            await task
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid

    async def test_stop_skips_teardown_when_container_rebound(
        self, monkeypatch
    ):
        # A racing start_container may re-bind the workspace to a new
        # container while stop waits for the lock. When stop finally
        # acquires the lock, container_id no longer maps to this ws, so it
        # must NOT tear down the fresh state or revoke its browsers.
        self.registry.track_activity("cid-old", "ws")
        lock = self.registry._get_workspace_lock("ws")
        revoked = []
        monkeypatch.setattr(
            self.registry,
            "revoke_workspace_browsers",
            lambda wid: revoked.append(wid),
        )

        with patch_podman(self.registry):
            async with lock:
                # Start stop; it runs podman (instant), peeks cid-old->ws,
                # then blocks on the lock we hold.
                task = asyncio.create_task(
                    self.registry.stop_and_remove_container(
                        "cid-old", cause=CAUSE_API
                    )
                )
                for _ in range(5):
                    await asyncio.sleep(0)
                # While stop is blocked, a racing start re-binds the
                # workspace to a new container (track_activity drops the
                # old cid reverse-mapping).
                self.registry.track_activity("cid-new", "ws")
            # Releasing the lock lets stop acquire it; under the lock the
            # re-check sees cid-old no longer maps to ws, so it bails.
            await task
        # The new container's state survives untouched.
        assert "ws" in self.registry.states
        assert self.registry.states["ws"].container_id == "cid-new"
        assert self.registry._cid_to_wsid.get("cid-new") == "ws"
        assert "cid-old" not in self.registry._cid_to_wsid
        # Browsers for the still-alive workspace were not revoked. (Without
        # the under-lock re-check, stop would have already torn the old
        # state down and revoked browsers before the rebind.)
        assert revoked == []

    async def test_stop_does_not_remove_sidecar_when_workspace_rebound(
        self, monkeypatch
    ):
        # #2265: a stop that loses the race to a rebound start must not tear
        # down the new generation's network sidecar -- doing so would leave
        # the new container joined (--network container:) to a removed netns.
        # The sidecar teardown is now under the workspace lock and gated by
        # the same re-verify that guards the registry teardown.
        monkeypatch.setattr(
            self.registry.app.state.settings,
            "network_sidecar_image",
            "test-net",
        )
        self.registry.track_activity("cid-old", "ws1234567890")
        self.registry._ws_with_network_sidecar.add("ws1234567890")
        lock = self.registry._get_workspace_lock("ws1234567890")

        with patch_podman(self.registry) as p:
            async with lock:
                # Start stop; it removes the old workspace container, peeks
                # cid-old->ws, then blocks on the lock we hold.
                task = asyncio.create_task(
                    self.registry.stop_and_remove_container(
                        "cid-old", cause=CAUSE_API
                    )
                )
                for _ in range(5):
                    await asyncio.sleep(0)
                # While stop is blocked, a racing start re-binds the
                # workspace to a new container (track_activity drops the
                # old cid reverse-mapping).
                self.registry.track_activity("cid-new", "ws1234567890")
            # Releasing the lock lets stop acquire it; under the lock the
            # re-check sees cid-old no longer maps to ws, so it bails --
            # including the sidecar teardown.
            await task
        # The new generation's sidecar was NOT removed.
        sidecar_name = self.registry.network_sidecar_name("ws1234567890")
        removes = [c.args[0] for c in p.remove_container.await_args_list]
        assert sidecar_name not in removes
        # Only the old workspace container itself was removed.
        assert "cid-old" in removes
        # And the new container's state + sidecar tracking survive.
        assert self.registry.states["ws1234567890"].container_id == "cid-new"
        assert "ws1234567890" in self.registry._ws_with_network_sidecar

    async def test_stop_does_not_replace_workspace_lock(self):
        # Regression for the lock-replacement race: even after stop tears
        # down state, _get_workspace_lock must return the SAME lock object,
        # so a subsequent start serializes against any in-flight acquirer.
        self.registry.track_activity("cid", "ws")
        lock_before = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry):
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        assert "ws" in self.registry._workspace_locks
        lock_after = self.registry._get_workspace_lock("ws")
        assert lock_after is lock_before


class TestRemoveContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_remove(self):
        self.registry.track_activity("cid", "ws")
        lock = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry) as p:
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks
        assert self.registry._workspace_locks["ws"] is lock

    async def test_remove_podman_error(self):
        self.registry.track_activity("cid", "ws")
        self.registry._get_workspace_lock("ws")

        with patch_podman(
            self.registry,
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(404, "gone")
            ),
        ):
            await self.registry.stop_and_remove_container(
                "cid", cause=CAUSE_API
            )
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks


class TestStopUserContainers:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_stop_user_containers(self, user, workspace, app_state):
        # Set container_id on the workspace
        await app_state.state.model.workspaces.update_workspace_container(
            workspace["id"], "cid"
        )
        self.registry.track_activity("cid", workspace["id"])

        with patch_podman(self.registry) as p:
            await self.registry.stop_user_containers(user["id"])
        p.remove_container.assert_awaited_once_with("cid")
        assert workspace["id"] not in self.registry.states

    async def test_stop_user_calls_workspace_killed(
        self, user, workspace, app_state
    ):
        await app_state.state.model.workspaces.update_workspace_container(
            workspace["id"], "cid"
        )
        self.registry.track_activity("cid", workspace["id"])

        killed_cb = AsyncMock()
        old_cb = self.registry.on_workspace_killed
        self.registry.on_workspace_killed = killed_cb

        with patch_podman(self.registry):
            await self.registry.stop_user_containers(user["id"])

        killed_cb.assert_awaited_once_with(workspace["id"], "cid")
        self.registry.on_workspace_killed = old_cb

    async def test_stop_user_no_containers(self, user):
        with patch_podman(self.registry) as p:
            await self.registry.stop_user_containers(user["id"])
        p.remove_container.assert_not_awaited()


class TestShutdown:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_shutdown_stops_tracked(self):
        # list_containers returns the tracked cid; it should be skipped in
        # the orphan loop (already tracked) but still removed via tracking.
        self.registry.track_activity("cid", "ws")

        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[{"Id": "cid"}]),
        ) as p:
            await self.registry.shutdown()
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in self.registry.states

    async def test_shutdown_stops_orphans(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[{"Id": "orphan-cid"}]),
        ) as p:
            await self.registry.shutdown()
        p.remove_container.assert_awaited_once_with("orphan-cid")

    async def test_shutdown_cancels_cleanup_task(self):
        # Create a real cancellable task so shutdown can await it.
        async def fake_cleanup():
            await asyncio.sleep(999)

        task = asyncio.create_task(fake_cleanup())
        self.registry.cleanup_task = task

        with patch_podman(self.registry):
            await self.registry.shutdown()
        assert task.cancelled()
        assert self.registry.cleanup_task is None

    async def test_shutdown_cancels_health_task(self):
        # A running health loop task is cancelled on shutdown.
        async def fake_health():
            await asyncio.sleep(999)

        task = asyncio.create_task(fake_health())
        reg = _health_registry()
        reg.health.health_task = task

        with patch_podman(self.registry):
            await reg.shutdown()
        assert task.cancelled()
        assert reg.health.health_task is None

    async def test_shutdown_handles_podman_error(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=OSError("podman connection refused")
            ),
        ):
            await self.registry.shutdown()
        # Should not raise

    async def test_shutdown_no_podman(self):
        with patch_podman(self.registry):
            await self.registry.shutdown()
        assert self.registry.cleanup_task is None

    async def test_shutdown_orphan_remove_error(self):
        """Orphan container that errors on removal is handled gracefully."""
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[{"Id": "orphan-cid"}]),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "remove failed")
            ),
        ) as p:
            await self.registry.shutdown()
        # Attempted removal and did not raise
        p.remove_container.assert_awaited_once_with("orphan-cid")


class TestCleanupIdleContainers:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_idle_container_stopped(self):
        # Set activity far in the past
        self.registry.track_activity("cid", "ws-1")
        self.registry.states["ws-1"].last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        with patch_podman(self.registry) as p:
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            # Let the task enter the Event wait, then wake it
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        p.remove_container.assert_awaited()
        assert "ws-1" not in self.registry.states

    async def test_idle_calls_workspace_killed_callback(self):
        self.registry.track_activity("cid", "ws-killed")
        self.registry.states["ws-killed"].last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        killed_cb = AsyncMock()
        old_cb = self.registry.on_workspace_killed
        self.registry.on_workspace_killed = killed_cb

        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        killed_cb.assert_awaited_once_with("ws-killed", "cid")
        self.registry.on_workspace_killed = old_cb

    async def test_idle_workspace_killed_callback_error(self):
        self.registry.track_activity("cid", "ws-err")
        self.registry.states["ws-err"].last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        killed_cb = AsyncMock(side_effect=RuntimeError("boom"))
        old_cb = self.registry.on_workspace_killed
        self.registry.on_workspace_killed = killed_cb

        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should not raise — error is logged
        killed_cb.assert_awaited_once()
        self.registry.on_workspace_killed = old_cb

    async def test_active_container_not_stopped(self):
        self.registry.track_activity("cid", "ws-1")

        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Container should still be tracked
        assert "ws-1" in self.registry.states

    async def test_idle_callback_invoked(self):
        self.registry.track_activity("cid", "ws-1")
        self.registry.states["ws-1"].last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        callback_called = []

        async def on_idle(ws_id):
            callback_called.append(ws_id)

        self.registry.on_idle_stop("ws-1", on_idle)

        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert callback_called == ["ws-1"]

    async def test_idle_callback_error_handled(self):
        self.registry.track_activity("cid", "ws-1")
        self.registry.states["ws-1"].last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        async def bad_callback(ws_id):
            raise RuntimeError("callback broke")

        self.registry.on_idle_stop("ws-1", bad_callback)

        with patch_podman(self.registry) as p:
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Container should still be stopped despite callback error
        p.remove_container.assert_awaited()

    async def test_per_workspace_timeout_uses_event_wait(self):
        """When per-workspace timeouts exist, cleanup uses Event-based wait."""
        self.registry.track_activity("cid", "ws-fast")
        self.registry.states["ws-fast"].last_activity = time.time() - 100
        self.registry.states["ws-fast"].idle_timeout = 5

        try:
            with patch_podman(self.registry) as p:
                # The Event-based wait will timeout after max(2, 5//2)=2s,
                # then check containers. We cancel after one iteration.
                task = asyncio.create_task(
                    self.registry.cleanup_idle_containers()
                )
                await asyncio.sleep(0.1)  # Let it start
                # Wake it immediately via the event
                self.registry.get_cleanup_wake().set()
                await asyncio.sleep(0.1)  # Let it process
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            p.remove_container.assert_awaited()
        finally:
            self.registry.states.clear()

    async def test_per_workspace_timeout_event_timeout(self):
        """Event-based wait times out when no wake signal is sent."""
        self.registry.track_activity("cid", "ws-fast")
        self.registry.states["ws-fast"].last_activity = time.time() - 100
        self.registry.states["ws-fast"].idle_timeout = 4

        try:
            with patch_podman(self.registry) as p:
                # Patch wait_for to immediately raise TimeoutError (simulates
                # the event not being set within the interval)
                async def fast_timeout(coro, timeout):
                    # Cancel the coroutine and raise TimeoutError
                    if hasattr(coro, "close"):
                        coro.close()
                    raise asyncio.TimeoutError

                call_count = 0

                async def patched_wait_for(coro, timeout):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return await fast_timeout(coro, timeout)
                    # Second call: cancel the loop
                    if hasattr(coro, "close"):
                        coro.close()
                    raise asyncio.CancelledError

                with patch("asyncio.wait_for", side_effect=patched_wait_for):
                    try:
                        await self.registry.cleanup_idle_containers()
                    except asyncio.CancelledError:
                        pass
            p.remove_container.assert_awaited()
        finally:
            self.registry.states.clear()

    async def test_sweep_removes_orphaned_sidecar_tokens(
        self, tmp_path, monkeypatch
    ):
        # Orphan ws-tokens/<id> files (workspace row gone) are unlinked;
        # tokens for live workspaces and transient .tmp writes are kept (#2309).
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        token_dir = tmp_path / "ws-tokens"
        token_dir.mkdir()
        (token_dir / "live-ws").write_text("tok-live")  # row exists
        (token_dir / "gone-ws").write_text("tok-gone")  # orphan
        (token_dir / "gone-ws.tmp").write_text("partial")  # transient write
        (token_dir / "subdir").mkdir()  # non-file entry: skipped, not unlinked
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            AsyncMock(return_value={"live-ws"}),
        )
        removed = await self.registry.sweep_orphaned_sidecar_tokens()
        assert removed == 1
        assert (token_dir / "live-ws").exists()
        assert not (token_dir / "gone-ws").exists()
        # .tmp is never unlinked (would race write_sidecar_token's os.replace)
        assert (token_dir / "gone-ws.tmp").exists()
        # non-file entries (subdirs) are left untouched
        assert (token_dir / "subdir").is_dir()

    async def test_sweep_orphaned_volumes_removes_only_orphans(
        self, app_state, monkeypatch
    ):
        """#3153: volumes whose klangk.workspace-id label names a
        workspace with no row are removed; live workspaces' volumes,
        label-less volumes, and foreign-instance volumes stay."""
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            AsyncMock(return_value={"live-ws"}),
        )
        volumes = [
            {
                "Name": "live-vol",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "live-ws",
                },
            },
            {
                "Name": "orphan-vol",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "gone-ws",
                },
            },
            # No workspace label: not ours to judge.
            {
                "Name": "bare",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                },
            },
            {"Name": "null-labels", "Labels": None},
        ]
        removed = []
        monkeypatch.setattr(
            self.registry.app.state.podman,
            "list_volumes",
            AsyncMock(return_value=volumes),
        )

        async def fake_rm(name):
            removed.append(name)

        monkeypatch.setattr(
            self.registry.app.state.podman, "remove_volume", fake_rm
        )
        assert await self.registry.sweep_orphaned_volumes() == 1
        assert removed == ["orphan-vol"]

    async def test_sweep_orphaned_volumes_skips_on_error(
        self, app_state, monkeypatch
    ):
        """A failing list or id lookup skips the sweep (returns 0)
        instead of raising — the idle loop treats it as best-effort."""
        monkeypatch.setattr(
            self.registry.app.state.podman,
            "list_volumes",
            AsyncMock(side_effect=OSError("podman down")),
        )
        assert await self.registry.sweep_orphaned_volumes() == 0

    async def test_sweep_orphaned_volumes_survives_rm_failure(
        self, app_state, monkeypatch
    ):
        """One failing removal is logged and skipped; the count reflects
        only successful removals."""
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            AsyncMock(return_value=set()),
        )
        volumes = [
            {
                "Name": "stuck",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "gone-ws",
                },
            },
            {
                "Name": "fine",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "gone-ws",
                },
            },
        ]
        monkeypatch.setattr(
            self.registry.app.state.podman,
            "list_volumes",
            AsyncMock(return_value=volumes),
        )
        calls = []

        async def fake_rm(name):
            calls.append(name)
            if name == "stuck":
                raise podman.PodmanError(409, "volume in use")

        monkeypatch.setattr(
            self.registry.app.state.podman, "remove_volume", fake_rm
        )
        assert await self.registry.sweep_orphaned_volumes() == 1
        assert sorted(calls) == ["fine", "stuck"]

    async def test_remove_workspace_volumes(self, app_state, monkeypatch):
        """The delete cascade removes exactly the workspace's labeled
        volumes; a refused removal is logged, not raised."""
        volumes = [
            {
                "Name": "mine",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "ws-1",
                },
            },
            {
                "Name": "other",
                "Labels": {
                    "klangk.instance": (
                        self.registry.app.state.util.instance_id()
                    ),
                    "klangk.workspace-id": "ws-2",
                },
            },
        ]
        monkeypatch.setattr(
            self.registry.app.state.podman,
            "list_volumes",
            AsyncMock(return_value=volumes),
        )

        async def fake_rm(name):
            if name == "mine":
                raise podman.PodmanError(409, "in use")

        monkeypatch.setattr(
            self.registry.app.state.podman, "remove_volume", fake_rm
        )
        assert await self.registry.remove_workspace_volumes("ws-1") == 0
        assert await self.registry.remove_workspace_volumes("ws-2") == 1

    async def test_remove_workspace_volumes_list_failure_is_noop(
        self, app_state, monkeypatch
    ):
        """A failing volume list logs and returns 0 — the delete
        cascade is best-effort; the orphan sweep retries later."""
        monkeypatch.setattr(
            self.registry.app.state.podman,
            "list_volumes",
            AsyncMock(side_effect=podman.PodmanError(500, "podman gone")),
        )
        rm = AsyncMock()
        monkeypatch.setattr(
            self.registry.app.state.podman, "remove_volume", rm
        )
        assert await self.registry.remove_workspace_volumes("ws-1") == 0
        rm.assert_not_awaited()

    async def test_sweep_noop_without_token_dir(self, tmp_path, monkeypatch):
        # No ws-tokens/ dir (no filtered workspace ever started) -> 0, and
        # the workspaces table is not even queried.
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        queried = AsyncMock()
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            queried,
        )
        assert await self.registry.sweep_orphaned_sidecar_tokens() == 0
        queried.assert_not_awaited()

    async def test_sweep_swallows_workspace_lookup_error(
        self, tmp_path, monkeypatch
    ):
        # A DB error mid-sweep must not propagate (resilience over crash); the
        # token file is left intact for the next sweep to retry.
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        (tmp_path / "ws-tokens").mkdir()
        (tmp_path / "ws-tokens" / "ws-x").write_text("t")
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        assert await self.registry.sweep_orphaned_sidecar_tokens() == 0
        assert (tmp_path / "ws-tokens" / "ws-x").exists()

    async def test_idle_loop_invokes_orphan_token_sweep(self):
        # The periodic idle loop piggybacks the sweep; last_token_sweep starts
        # at 0 so the first iteration always sweeps (#2309).
        sweep = AsyncMock()
        self.registry.sweep_orphaned_sidecar_tokens = sweep
        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        sweep.assert_awaited()

    async def test_sweep_swallows_listdir_error(self, tmp_path, monkeypatch):
        # An OSError scanning ws-tokens/ must not propagate; the workspaces
        # table is not queried (early return).
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        (tmp_path / "ws-tokens").mkdir()
        queried = AsyncMock()
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            queried,
        )
        monkeypatch.setattr(
            "klangk.container.sidecar.os.listdir",
            MagicMock(side_effect=OSError("io")),
        )
        assert await self.registry.sweep_orphaned_sidecar_tokens() == 0
        queried.assert_not_awaited()

    async def test_sweep_swallows_unlink_error(self, tmp_path, monkeypatch):
        # An OSError unlinking one orphan is logged and skipped (the file is
        # left for the next sweep); other orphans are still processed.
        monkeypatch.setattr(
            self.registry.app.state.settings, "data_dir", str(tmp_path)
        )
        token_dir = tmp_path / "ws-tokens"
        token_dir.mkdir()
        (token_dir / "gone-ws").write_text("tok")
        monkeypatch.setattr(
            self.registry.app.state.workspaces,
            "existing_workspace_ids",
            AsyncMock(return_value=set()),
        )
        monkeypatch.setattr(
            "klangk.container.sidecar.os.unlink",
            MagicMock(side_effect=OSError("busy")),
        )
        removed = await self.registry.sweep_orphaned_sidecar_tokens()
        assert removed == 0  # the unlink failed
        assert (token_dir / "gone-ws").exists()  # left intact

    async def test_idle_loop_swallows_sweep_error(self):
        # A sweep failure raised inside the loop is logged, not propagated --
        # the loop runs a full iteration and is only stopped by cancellation.
        self.registry.sweep_orphaned_sidecar_tokens = AsyncMock(
            side_effect=RuntimeError("sweep broke")
        )
        with patch_podman(self.registry):
            task = asyncio.create_task(self.registry.cleanup_idle_containers())
            await asyncio.sleep(0.05)
            self.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.registry.sweep_orphaned_sidecar_tokens.assert_awaited()


class TestStartCleanupLoop:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def teardown_method(self):
        if self.registry.cleanup_task:
            self.registry.cleanup_task.cancel()
            self.registry.cleanup_task = None

    async def test_start_creates_task(self):
        self.registry.start_cleanup_loop()
        assert self.registry.cleanup_task is not None
        self.registry.cleanup_task.cancel()

    async def test_start_idempotent(self):
        self.registry.start_cleanup_loop()
        task1 = self.registry.cleanup_task
        self.registry.start_cleanup_loop()
        assert self.registry.cleanup_task is task1
        self.registry.cleanup_task.cancel()


class TestPrewarmPodman:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_prewarm_creates_and_removes(self):
        with patch_podman(self.registry) as p:
            await self.registry.prewarm_podman()
        p.create_container.assert_awaited_once()
        p.remove_container.assert_awaited_once_with("new-cid")

    async def test_prewarm_handles_error(self):
        with patch_podman(
            self.registry,
            create_container=AsyncMock(
                side_effect=podman.PodmanError(500, "boom")
            ),
        ):
            await self.registry.prewarm_podman()
        # Should not raise


class TestReapInstanceContainers:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_removes_orphaned_containers(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "orphan-123",
                        "Labels": {"klangk.workspace": "ws-orphan"},
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_instance_containers()
        # Leftover containers are removed at startup.
        assert "ws-orphan" not in self.registry.states
        mocks.remove_container.assert_awaited_once_with("orphan-123")

    async def test_removes_container_without_labels(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[{"Id": "orphan-x", "Labels": None}]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_instance_containers()
        assert "unknown" not in self.registry.states
        mocks.remove_container.assert_awaited_once_with("orphan-x")

    async def test_removes_even_when_tracked(self):
        """At startup the registry is empty, so every leftover is reaped --
        including one whose ID happens to match a tracked container (which
        cannot happen at real startup, but the reap is unconditional)."""
        self.registry.track_activity("tracked-456", "ws-tracked")
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[{"Id": "tracked-456"}]),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_instance_containers()
        mocks.remove_container.assert_awaited_once_with("tracked-456")

    async def test_podman_error_handled(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "fail")
            ),
        ):
            await self.registry.reap_instance_containers()
        # Should not raise

    async def test_remove_podman_error_handled(self):
        """When remove_container raises PodmanError, it is logged but not raised."""
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "orphan-bad",
                        "Labels": {"klangk.workspace": "ws-bad"},
                    }
                ]
            ),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "remove failed")
            ),
        ) as mocks:
            await self.registry.reap_instance_containers()
        mocks.remove_container.assert_awaited_once_with("orphan-bad")
        # Container was not adopted -- just skipped after failed removal.
        assert "ws-bad" not in self.registry.states

    async def test_skips_empty_id(self):
        """A container dict with no Id/ID is skipped, not passed to remove."""
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[{"Id": "good-1"}, {"Labels": None}]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_instance_containers()
        mocks.remove_container.assert_awaited_once_with("good-1")

    async def test_removes_workspaces_before_their_sidecars(self):
        """Dependents (workspaces) are reaped before the sidecars whose netns
        they share, so no sidecar is skipped this pass (#2476).

        ``list_containers`` returns oldest-first and klangk creates the
        sidecar before the workspace, so the sidecar is listed first.
        Without the dependency-order sort that sidecar would be removed
        first and (against real podman) fail with "has dependent
        containers"; here we assert the awaited removal order directly.
        """
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "sidecar-1",
                        "Labels": {
                            "klangk.role": "network-sidecar",
                            "klangk.workspace": "ws-1",
                        },
                    },
                    {
                        "Id": "workspace-1",
                        "Labels": {
                            "klangk.role": "workspace",
                            "klangk.workspace": "ws-1",
                        },
                    },
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_instance_containers()
        order = [c.args[0] for c in mocks.remove_container.await_args_list]
        assert order == ["workspace-1", "sidecar-1"]


class TestPidAlive:
    """Unit tests for ``container.pid_alive`` — the liveness check the
    dead-owner reap keys on (#2342)."""

    def test_alive_when_process_exists(self):
        with patch("klangk.container.registry.os.kill", return_value=None):
            assert container.pid_alive(12345) is True

    def test_dead_when_no_such_process(self):
        with patch(
            "klangk.container.registry.os.kill", side_effect=ProcessLookupError
        ):
            assert container.pid_alive(12345) is False

    def test_alive_when_permission_denied(self):
        # EPERM: the process exists but belongs to another user (e.g. a
        # sibling klangkd under a different account) — assume alive so its
        # containers are left alone.
        with patch(
            "klangk.container.registry.os.kill", side_effect=PermissionError
        ):
            assert container.pid_alive(12345) is True

    def test_current_process_is_alive_without_mock(self):
        # The real liveness check (no os.kill mock) must see the running test
        # process as alive — the property the dead-owner reap relies on to
        # never self-reap (#1556).
        assert container.pid_alive(os.getpid()) is True


class TestReapDeadOwnerContainers:
    """Tests for ``ContainerRegistry.reap_dead_owner_containers`` (#2342).

    The companion to the per-instance reap: it culls ``klangk.managed=true``
    containers whose owning klangkd (recorded in ``klangk.pid``) is no longer
    running, while leaving label-less / live-owner / self-owned containers
    alone.
    """

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_reaps_container_whose_owner_pid_is_dead(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "dead-owner-1",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "ghost",
                            "klangk.pid": "99999",
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            with patch(
                "klangk.container.registry.pid_alive", return_value=False
            ):
                await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_awaited_once_with("dead-owner-1")

    async def test_removes_workspaces_before_their_sidecars(self):
        """Dependents (workspaces) are reaped before their sidecars (#2476).

        Same netns-dependency ordering as the per-instance reap
        (:class:`TestReapInstanceContainers`): the sidecar is listed first
        (created first) but must be removed after the workspace that joins
        its netns, or its removal is skipped this pass.
        """
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "sidecar-ghost",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "ghost",
                            "klangk.pid": "99999",
                            "klangk.role": "network-sidecar",
                            "klangk.workspace": "ws-ghost",
                        },
                    },
                    {
                        "Id": "workspace-ghost",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "ghost",
                            "klangk.pid": "99999",
                            "klangk.role": "workspace",
                            "klangk.workspace": "ws-ghost",
                        },
                    },
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            with patch(
                "klangk.container.registry.pid_alive", return_value=False
            ):
                await self.registry.reap_dead_owner_containers()
        order = [c.args[0] for c in mocks.remove_container.await_args_list]
        assert order == ["workspace-ghost", "sidecar-ghost"]

    async def test_skips_container_whose_owner_pid_is_alive(self):
        # A live owner (sibling klangkd, possibly mid-shutdown) always holds
        # its own PID, so its containers read alive and are left alone (#1556).
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "live-owner-1",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "sibling",
                            "klangk.pid": "4242",
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            with patch(
                "klangk.container.registry.pid_alive", return_value=True
            ):
                await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_not_awaited()

    async def test_never_reaps_current_instance_own_pid(self):
        # Security property (#1556): a container stamped with THIS daemon's
        # pid is skipped via the REAL liveness check — no pid_alive mock —
        # so a startup sweep can never self-reap. An inverted pid_alive or a
        # pid-label mis-parse would fail here.
        mine = str(os.getpid())
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "self-1",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "sibling",
                            "klangk.pid": mine,
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_not_awaited()

    async def test_skips_container_without_pid_label(self):
        # Tolerant: a label-less container (older klangkd that did not stamp
        # klangk.pid, possibly still running) is left alone — liveness cannot
        # be decided (#2342 backwards-compat).
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "no-pid-label",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.instance": "old-version",
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_not_awaited()

    async def test_skips_container_with_unparseable_pid(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "bad-pid",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.pid": "not-a-number",
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_not_awaited()

    async def test_skips_nonpositive_pid(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "zero-pid",
                        "Labels": {
                            "klangk.managed": "true",
                            "klangk.pid": "0",
                        },
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_not_awaited()

    async def test_skips_empty_id(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {"Labels": {"klangk.pid": "99999"}},
                    {"Id": "real-1", "Labels": {"klangk.pid": "99998"}},
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            with patch(
                "klangk.container.registry.pid_alive", return_value=False
            ):
                await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_awaited_once_with("real-1")

    async def test_lists_all_managed_containers(self):
        # The sweep spans all instances (not just this one), keyed on
        # klangk.managed=true so it covers workspace + network-sidecar.
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[]),
        ) as mocks:
            await self.registry.reap_dead_owner_containers()
        mocks.list_containers.assert_awaited_once_with("klangk.managed=true")

    async def test_podman_error_on_list_handled(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "fail")
            ),
        ):
            await self.registry.reap_dead_owner_containers()
        # Should not raise.

    async def test_podman_error_on_remove_handled(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "dead-owner-bad",
                        "Labels": {"klangk.pid": "99999"},
                    }
                ]
            ),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "remove failed")
            ),
        ) as mocks:
            with patch(
                "klangk.container.registry.pid_alive", return_value=False
            ):
                await self.registry.reap_dead_owner_containers()
        mocks.remove_container.assert_awaited_once_with("dead-owner-bad")


class TestBrowserRegistry:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_register_and_resolve(self):
        sock = object()
        self.registry.register_browser("bid-1", "ws-1", sock)
        assert self.registry.resolve_browser("bid-1") == ("ws-1", sock)

    def test_resolve_unknown(self):
        assert self.registry.resolve_browser("nonexistent") is None

    def test_register_idempotent(self):
        sock1 = object()
        sock2 = object()
        self.registry.register_browser("bid-1", "ws-1", sock1)
        self.registry.register_browser("bid-1", "ws-1", sock2)
        assert self.registry.resolve_browser("bid-1") == ("ws-1", sock2)

    def test_revoke_workspace_browsers(self):
        sock1 = object()
        sock2 = object()
        self.registry.register_browser("bid-1", "ws-1", sock1)
        self.registry.register_browser("bid-2", "ws-1", sock2)
        self.registry.revoke_workspace_browsers("ws-1")
        assert self.registry.resolve_browser("bid-1") is None
        assert self.registry.resolve_browser("bid-2") is None

    def test_revoke_browser_by_sock(self):
        sock1 = object()
        sock2 = object()
        self.registry.register_browser("bid-1", "ws-1", sock1)
        self.registry.register_browser("bid-2", "ws-1", sock2)
        self.registry.revoke_browser(sock1)
        assert self.registry.resolve_browser("bid-1") is None
        assert self.registry.resolve_browser("bid-2") == ("ws-1", sock2)

    def test_revoke_browser_no_match(self):
        sock = object()
        other_sock = object()
        self.registry.register_browser("bid-1", "ws-1", sock)
        self.registry.revoke_browser(other_sock)
        assert self.registry.resolve_browser("bid-1") == ("ws-1", sock)

    def test_multiple_browsers_same_workspace(self):
        sock1 = object()
        sock2 = object()
        self.registry.register_browser("bid-1", "ws-1", sock1)
        self.registry.register_browser("bid-2", "ws-1", sock2)
        assert self.registry.resolve_browser("bid-1") == ("ws-1", sock1)
        assert self.registry.resolve_browser("bid-2") == ("ws-1", sock2)


class TestTrackActivityContainerChanged:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_updates_reverse_mapping_on_container_change(self):
        self.registry.track_activity("old-cid", "ws-chg")
        assert self.registry._cid_to_wsid.get("old-cid") == "ws-chg"
        self.registry.track_activity("new-cid", "ws-chg")
        assert self.registry._cid_to_wsid.get("new-cid") == "ws-chg"
        assert "old-cid" not in self.registry._cid_to_wsid
        self.registry.states.pop("ws-chg", None)
        self.registry._cid_to_wsid.pop("new-cid", None)


def _mock_sock_for_health():
    """A minimal mock websocket for health broadcast fan-out tests."""
    from unittest.mock import MagicMock

    sock = MagicMock()
    sock.send_json = MagicMock()
    return sock


def _health_registry(ws_state=None):
    """A ContainerRegistry wired for health-monitor tests (#1464).

    Constructs a fresh registry via ``_make_app_state`` and wires its
    ``sockets`` to the given WebSocketState (or a fresh one by default).
    HealthMonitor reaches sockets via ``self.app_state.state.sockets``.
    """
    app_state = _make_app_state(sockets=ws_state)
    return app_state.state.container_registry


async def _grant_health_member(reg, user_id: str, workspace_id: str) -> None:
    """Seed a member ALLOW ``monitor`` ACE for a health fan-out test
    (#1714/#2783).

    The status fan-outs ACL-check each recipient for ``monitor`` on
    ``/workspaces/{id}``; the per-test DB starts with no ACEs (default
    deny), so tests that assert delivery must grant membership first.
    """
    from klangk import model

    await reg.app.state.model.init_db()
    # acl_entries.user_id has an FK to users(id): plant the principal row.
    async with reg.app.state.db.transaction() as tx:
        await tx.execute(
            "INSERT OR IGNORE INTO users (id, email, verified)"
            " VALUES (?, ?, 1)",
            (user_id, f"{user_id}@test.example"),
        )
    resource = f"/workspaces/{workspace_id}"
    entries = await reg.app.state.model.acl.get_acl_entries(resource)
    position = max((e["position"] for e in entries), default=-1) + 1
    await reg.app.state.model.acl.add_acl_entry(
        resource,
        position,
        model.ACTION_ALLOW,
        "monitor-workspace",
        model.PRINCIPAL_USER,
        user_id=user_id,
    )


def _health_state(
    *,
    workspace_id="ws-h",
    container_id="cid1234567890",
    health_check="curl -sf http://localhost:8080/health",
    owner_id="uid-owner",
    setup_state="complete",
    health_status=None,
    in_startup_grace=False,
    app_state=None,
):
    """Build a ContainerState wired up for health checks.

    *in_startup_grace* defaults to False so the core healthy/unhealthy
    tests exercise post-grace behavior; the startup-grace tests opt in.
    """
    if app_state is None:
        app_state = _health_registry().app
    st = container.ContainerState(workspace_id, container_id, app_state)
    st.health_check = health_check
    st.owner_id = owner_id
    # The per-handle probe path (owner handle → symlink home); the shared
    # tests flip it off explicitly (#3135 made shared the class default).
    st.per_handle_home = True
    st.setup_state = setup_state
    st.health_status = health_status
    # 0.0 = epoch, comfortably outside any real grace window.
    st.service_started_at = time.time() if in_startup_grace else 0.0
    return st


class _CountingBool:
    """A bool stand-in that counts evaluations — used to observe that
    the health loop actually iterated before the test cancels it (a
    fixed sleep window can expire under -n auto load before the first
    tick, silently losing the skip-branch coverage, #2944)."""

    def __init__(self, value: bool) -> None:
        self.value = value
        self.reads = 0

    def __bool__(self) -> bool:
        self.reads += 1
        return self.value


async def _wait_for_called(check, ticks: int = 500) -> None:
    """Deterministically wait for a mocked call instead of a fixed
    sleep: under -n auto load a 50ms window can elapse before the
    health loop's first tick, de-flaking both the assertion and the
    line coverage (#2944)."""
    for _ in range(ticks):
        if check.called:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("health loop never called _check_workspace")


class TestHealthMonitorRunOne:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    """_run_one: rc 0 → healthy, non-zero/error → unhealthy (with reason)."""

    async def test_exit_zero_is_healthy(self, app_state):
        monitor = _health_registry().health
        st = _health_state()
        exec_mock = AsyncMock(return_value=(0, "", ""))
        with (
            patch.object(
                monitor.app.state.podman, "exec_container", exec_mock
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value="owner"),
            ),
            patch.object(
                monitor.app.state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            assert await monitor._run_one(st) == ("healthy", "")
        # The check runs as the workspace user with HOME set, and is
        # logged with the container id (first 12 chars).
        call = exec_mock.call_args
        assert call.args[0] == "cid1234567890"
        assert call.kwargs["user"] == "klangk"
        assert call.kwargs["extra_env"] == {"HOME": "/home/klangk"}
        assert call.kwargs["timeout"] == self.registry.health_check_timeout
        # The health check runs as a NON-login bash shell (bash -c) on
        # purpose: it sources nothing, so the probe is deterministic and
        # decoupled from the user's interactive ~/.profile / ~/.bashrc.
        # The check command must therefore use absolute paths (or a
        # wrapper script with a shebang) -- it cannot rely on the user's
        # PATH. See docs/features/health-check.md.
        assert call.args[1][:2] == ["bash", "-c"]
        assert call.args[1][2] == st.health_check

    async def test_nonzero_exit_is_unhealthy_with_stderr_reason(
        self, app_state
    ):
        # The stderr that explains the non-zero exit is captured as the
        # reason instead of being thrown away (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.app.state.podman,
                "exec_container",
                AsyncMock(return_value=(1, "", "curl: connection refused")),
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value="owner"),
            ),
            patch.object(
                monitor.app.state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "connection refused" in message
        assert "exited 1" in message

    async def test_nonzero_exit_falls_back_to_stdout(self, app_state):
        # No stderr → the reason uses stdout instead.
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.app.state.podman,
                "exec_container",
                AsyncMock(return_value=(2, "all good on stdout", "")),
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value="owner"),
            ),
            patch.object(
                monitor.app.state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "all good on stdout" in message

    async def test_nonzero_exit_no_output_reports_exit_code(self, app_state):
        # Non-zero exit but no output at all → still surface the exit
        # code so it isn't a complete black box (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.app.state.podman,
                "exec_container",
                AsyncMock(return_value=(127, "", "")),
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value="owner"),
            ),
            patch.object(
                monitor.app.state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert message == "exited 127"

    async def test_message_truncated_to_bounded_tail(self):
        # A verbose check can't grow the retained reason unbounded; only
        # the last HEALTH_MESSAGE_MAX_BYTES bytes are kept (#1088).
        big = "x" * (container.HEALTH_MESSAGE_MAX_BYTES * 4)
        assert len(
            container.unhealthy_message(1, "", big)
        ) == container.HEALTH_MESSAGE_MAX_BYTES + len("...") + len(
            "exited 1: "
        )

    async def test_exec_error_is_unhealthy_with_reason(self, app_state):
        # The podman/timeout failure text is captured as the reason
        # instead of being discarded (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.app.state.podman,
                "exec_container",
                AsyncMock(side_effect=podman.PodmanError(500, "boom")),
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value="owner"),
            ),
            patch.object(
                monitor.app.state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "PodmanError" in message
        assert "boom" in message

    async def test_no_owner_is_unhealthy_with_reason(self, app_state):
        monitor = _health_registry().health
        st = _health_state(owner_id=None)
        with patch.object(
            monitor.app.state.podman, "exec_container"
        ) as exec_mock:
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "owner" in message
        exec_mock.assert_not_called()

    async def test_no_handle_is_unhealthy_with_reason(self, app_state):
        # Owner exists in the state but has no handle resolved — the
        # per-handle branch's failure (_health_state arms the layout).
        monitor = _health_registry().health
        st = _health_state(owner_id="uid-owner")
        with (
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(return_value=None),
            ),
            patch.object(
                monitor.app.state.podman, "exec_container"
            ) as exec_mock,
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "handle" in message
        exec_mock.assert_not_called()

    async def test_shared_layout_probes_shared_home(self, app_state):
        # per_handle_home=False (#2720): the check probes the workspace's
        # single shared /home/klangk — no owner handle lookup, no
        # per-user symlink — and runs with that HOME.
        monitor = _health_registry().health
        st = _health_state()
        st.per_handle_home = False
        exec_mock = AsyncMock(return_value=(0, "", ""))
        with (
            patch.object(
                monitor.app.state.podman, "exec_container", exec_mock
            ),
            patch.object(
                monitor.app.state.model.users,
                "get_user_handle",
                AsyncMock(),
            ) as handle_mock,
            patch.object(
                monitor.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
            ) as symlink_mock,
        ):
            assert await monitor._run_one(st) == ("healthy", "")
        handle_mock.assert_not_awaited()
        symlink_mock.assert_not_awaited()
        assert exec_mock.call_args.kwargs["extra_env"] == {
            "HOME": container.SHARED_HOME
        }


class TestHealthMonitorCheckWorkspace:
    """_check_workspace: records status + reason and broadcasts changes."""

    async def test_broadcasts_on_transition_to_unhealthy(self):
        monitor = _health_registry().health
        st = _health_state(health_status=None)  # unknown → unhealthy
        with (
            patch.object(
                monitor,
                "_run_one",
                AsyncMock(return_value=("unhealthy", "connection refused")),
            ),
            patch.object(monitor, "_broadcast", AsyncMock()) as bcast,
        ):
            await monitor._check_workspace(st)
        assert st.health_status == "unhealthy"
        assert st.health_message == "connection refused"
        assert st.health_checked_at is not None
        bcast.assert_called_once_with(st, "unhealthy", "connection refused")

    async def test_no_broadcast_when_status_unchanged(self):
        monitor = _health_registry().health
        st = _health_state(health_status="healthy")  # stays healthy
        with (
            patch.object(
                monitor, "_run_one", AsyncMock(return_value=("healthy", ""))
            ),
            patch.object(monitor, "_broadcast", AsyncMock()) as bcast,
        ):
            await monitor._check_workspace(st)
        assert st.health_status == "healthy"
        bcast.assert_not_called()

    async def test_clears_message_when_becomes_healthy(self):
        # A stale failure reason must not linger next to a "healthy"
        # status once the check starts passing again (#1088).
        monitor = _health_registry().health
        st = _health_state(health_status="unhealthy")
        st.health_message = "old reason"
        with patch.object(
            monitor, "_run_one", AsyncMock(return_value=("healthy", ""))
        ):
            await monitor._check_workspace(st)
        assert st.health_status == "healthy"
        assert st.health_message is None

    async def test_logs_reason_at_info_on_transition_to_unhealthy(
        self, caplog
    ):
        # Acceptance criterion: a failing check's reason appears in the
        # logs at least once per unhealthy transition, at info (#1088).
        import logging

        monitor = _health_registry().health
        st = _health_state(health_status=None)
        with patch.object(
            monitor,
            "_run_one",
            AsyncMock(return_value=("unhealthy", "curl: connection refused")),
        ):
            with caplog.at_level(logging.INFO, logger="klangk.container"):
                await monitor._check_workspace(st)
        assert any(
            "connection refused" in r.message and r.levelno == logging.INFO
            for r in caplog.records
        )

    async def test_logs_reason_at_debug_on_steady_unhealthy(self, caplog):
        # A persistently-failing check doesn't spam at info; steady-state
        # polls log the reason at debug (#1088).
        import logging

        monitor = _health_registry().health
        st = _health_state(health_status="unhealthy")
        with patch.object(
            monitor,
            "_run_one",
            AsyncMock(return_value=("unhealthy", "still down")),
        ):
            with caplog.at_level(logging.DEBUG, logger="klangk.container"):
                await monitor._check_workspace(st)
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert not any("still down" in r.message for r in info_records)
        assert any(
            "still down" in r.message and r.levelno == logging.DEBUG
            for r in caplog.records
        )


class TestHealthMonitorStartupGrace:
    """A failing check inside the startup grace window is not an outage.

    Mirrors Docker's HEALTHCHECK --start-period: while the service
    command is booting, unhealthy results are suppressed (no status
    change, no broadcast, no health_checked_at), but a *healthy* result
    is still recorded so a fast-booting service is marked up the moment
    it responds.  Prevents the boot-time false "unhealthy: Gateway not
    yet ready to accept connections" the very first poll produced.
    """

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_unhealthy_during_grace_is_suppressed(self):
        monitor = _health_registry().health
        st = _health_state(health_status=None, in_startup_grace=True)
        with (
            patch.object(
                monitor,
                "_run_one",
                AsyncMock(return_value=("unhealthy", "connection refused")),
            ),
            patch.object(monitor, "_broadcast", AsyncMock()) as bcast,
        ):
            await monitor._check_workspace(st)
        # Status, reason, and last-checked are all untouched: the grace
        # window swallowed the failure as an expected boot-time blip.
        assert st.health_status is None
        assert st.health_message is None
        assert st.health_checked_at is None
        bcast.assert_not_called()

    async def test_healthy_during_grace_recorded_immediately(self):
        # Even mid-grace, a passing check marks the service healthy
        # right away -- the grace only suppresses failures, not
        # successes, so a fast-booting service isn't hidden.
        monitor = _health_registry().health
        st = _health_state(health_status=None, in_startup_grace=True)
        with (
            patch.object(
                monitor, "_run_one", AsyncMock(return_value=("healthy", ""))
            ),
            patch.object(monitor, "_broadcast", AsyncMock()) as bcast,
        ):
            await monitor._check_workspace(st)
        assert st.health_status == "healthy"
        assert st.health_checked_at is not None
        bcast.assert_called_once_with(st, "healthy", None)

    async def test_unhealthy_after_grace_is_recorded(self):
        # Once the grace window has elapsed, a failing check is a real
        # outage again: status flips, reason is kept, and it broadcasts.
        monitor = _health_registry().health
        st = _health_state(health_status=None, in_startup_grace=False)
        with (
            patch.object(
                monitor,
                "_run_one",
                AsyncMock(return_value=("unhealthy", "connection refused")),
            ),
            patch.object(monitor, "_broadcast", AsyncMock()) as bcast,
        ):
            await monitor._check_workspace(st)
        assert st.health_status == "unhealthy"
        assert st.health_message == "connection refused"
        bcast.assert_called_once_with(st, "unhealthy", "connection refused")

    def test_in_startup_grace_uses_anchor_window(self):
        monitor = _health_registry().health
        # service_started_at = now -> within the default 30s window.
        st_in = _health_state(in_startup_grace=True)
        assert monitor._in_startup_grace(st_in) is True
        # service_started_at = epoch -> long past the window.
        st_out = _health_state(in_startup_grace=False)
        assert monitor._in_startup_grace(st_out) is False

    def test_mark_service_started_resets_anchor(self):
        # mark_service_started pushes the anchor forward, restarting the
        # grace window (e.g. the service command re-fires after a
        # container restart).
        st = _health_state(in_startup_grace=False)
        assert st.service_started_at == 0.0
        st.mark_service_started()
        assert time.time() - st.service_started_at < 1

    def test_registry_mark_service_started_looks_up_state(self):
        # The registry proxy resolves container_id -> workspace and
        # resets that workspace's anchor; unknown containers no-op.
        st = _health_state(in_startup_grace=False)
        self.registry.states[st.workspace_id] = st
        self.registry._cid_to_wsid[st.container_id] = st.workspace_id
        try:
            assert st.service_started_at == 0.0
            self.registry.mark_service_started(st.container_id)
            assert time.time() - st.service_started_at < 1
            # Unknown container is a safe no-op.
            self.registry.mark_service_started("no-such-cid")
        finally:
            self.registry.states.pop(st.workspace_id, None)
            self.registry._cid_to_wsid.pop(st.container_id, None)


class TestHealthMonitorBroadcast:
    """_broadcast fans out to workspace members, not just the session (#1714)."""

    async def test_fans_out_via_notify_service_health(self, app_state):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="unhealthy")
        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            # No WorkspaceSession registered for this workspace — yet
            # the event must still reach the member's connection.
            await monitor._broadcast(st, "unhealthy", "connection refused")
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
        sock.send_json.assert_called_once_with(
            {
                "type": "service_health",
                "workspace_id": "ws-h",
                "healthy": False,
                "health_message": "connection refused",
                "running": True,
                "health_checked_at": None,
                # _broadcast bumps the per-workspace seq on every emit.
                "seq": 1,
            }
        )

    async def test_non_member_never_receives_health_frames(self, app_state):
        """#1714: a connected user with no grant on the workspace is skipped."""
        reg = _health_registry()
        monitor = reg.health
        member = _mock_sock_for_health()
        stranger = _mock_sock_for_health()
        st = _health_state(health_status="unhealthy")
        try:
            reg.app.state.sockets.connections[member] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            reg.app.state.sockets.connections[stranger] = SimpleNamespace(
                user={"id": "u2", "email": "b@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            await monitor._broadcast(st, "unhealthy", "connection refused")
        finally:
            reg.app.state.sockets.connections.pop(member, None)
            reg.app.state.sockets.connections.pop(stranger, None)
        member.send_json.assert_called_once()
        stranger.send_json.assert_not_called()


class TestHealthMonitorLoopSkips:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    """run_health_loop skips setup-incomplete and checkless workspaces."""

    async def test_skips_setup_incomplete(self):
        reg = _health_registry()
        monitor = reg.health
        st = _health_state(setup_state="pending")
        reg.states[st.workspace_id] = st
        try:
            with (
                patch.object(
                    monitor, "_check_workspace", AsyncMock()
                ) as check,
                patch.object(
                    monitor,
                    "_setup_complete",
                    MagicMock(return_value=False),
                ) as setup,
                patch.object(
                    reg.app.state.settings, "health_check_interval", 0.01
                ),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                for _ in range(500):
                    if setup.call_count:
                        break
                    await asyncio.sleep(0.001)
                assert setup.call_count, "loop never iterated"
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            check.assert_not_called()
        finally:
            reg.states.pop(st.workspace_id, None)

    async def test_skips_when_no_health_check(self):
        reg = _health_registry()
        monitor = reg.health
        st = _health_state(health_check=None)
        gate = _CountingBool(False)
        st.health_check = gate
        reg.states[st.workspace_id] = st
        try:
            with (
                patch.object(
                    monitor, "_check_workspace", AsyncMock()
                ) as check,
                patch.object(
                    reg.app.state.settings, "health_check_interval", 0.01
                ),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                for _ in range(500):
                    if gate.reads:
                        break
                    await asyncio.sleep(0.001)
                assert gate.reads, "loop never iterated"
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            check.assert_not_called()
        finally:
            reg.states.pop(st.workspace_id, None)

    async def test_runs_when_setup_complete(self):
        reg = _health_registry()
        monitor = reg.health
        st = _health_state(setup_state="complete")
        reg.states[st.workspace_id] = st
        try:
            with (
                patch.object(
                    monitor, "_check_workspace", AsyncMock()
                ) as check,
                patch.object(
                    reg.app.state.settings, "health_check_interval", 0.01
                ),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await _wait_for_called(check)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            check.assert_called()
        finally:
            reg.states.pop(st.workspace_id, None)


class TestHealthMonitorBroadcastSeq:
    """_broadcast bumps per-workspace seq and forwards live fields."""

    async def test_bumps_seq_each_emit_and_forwards_fields(self, app_state):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="unhealthy")
        st.health_checked_at = 1_700_000_000.0
        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            await monitor._broadcast(st, "unhealthy", "connection refused")
            await monitor._broadcast(st, "unhealthy", "connection refused")
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
        frames = [c[0][0] for c in sock.send_json.call_args_list]
        assert len(frames) == 2
        # Monotonic seq across emits; live frames are running=True.
        assert frames[0]["seq"] == 1
        assert frames[1]["seq"] == 2
        assert st.health_seq == 2
        for f in frames:
            assert f["running"] is True
            assert f["health_checked_at"] == "2023-11-14T22:13:20+00:00"


class TestHealthMonitorDeath:
    """broadcast_death + notify_workspace_killed close the death hole.

    A dying container otherwise looks like "healthy, then silence" on
    the service_health stream (#1175 item 2)."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    async def test_broadcast_death_emits_terminal_frame(self, app_state):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="healthy")
        st.health_checked_at = 1_700_000_000.0
        st.health_seq = 4
        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            await monitor.broadcast_death(st)
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
        frame = sock.send_json.call_args[0][0]
        assert frame["type"] == "service_health"
        assert frame["healthy"] is False
        assert frame["running"] is False
        assert frame["health_checked_at"] == "2023-11-14T22:13:20+00:00"
        # seq bumped from 4 -> 5.
        assert frame["seq"] == 5
        assert st.health_seq == 5

    async def test_notify_workspace_killed_emits_death_for_health_checked(
        self,
    ):
        # A container death fans a terminal service_health frame to
        # subscribers BEFORE the on_workspace_killed callback drops state.
        reg = _health_registry()
        sock = _mock_sock_for_health()
        st = _health_state(health_status="healthy")
        reg.states[st.workspace_id] = st
        seen_state_present = []

        async def on_killed(wid, container_id=None):
            # The state must still be present when the callback runs --
            # death emission happens first, before removal.
            seen_state_present.append(wid in reg.states)

        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            reg.set_on_workspace_killed(on_killed)
            await reg.notify_workspace_killed(st.workspace_id)
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
            reg.states.pop(st.workspace_id, None)
            reg.set_on_workspace_killed(None)
        frame = sock.send_json.call_args[0][0]
        assert frame["healthy"] is False
        assert frame["running"] is False
        assert seen_state_present == [True]

    async def test_notify_workspace_killed_skips_non_health_checked(
        self, app_state
    ):
        # A workspace with no health_check never appeared on the stream,
        # so its death emits no terminal frame.
        sock = _mock_sock_for_health()
        st = _health_state(health_check=None)
        self.registry.states[st.workspace_id] = st
        try:
            self.registry.app.state.sockets.connections[sock] = (
                SimpleNamespace(user={"id": "u1", "email": "a@x"})
            )
            await self.registry.notify_workspace_killed(st.workspace_id)
        finally:
            self.registry.app.state.sockets.connections.pop(sock, None)
            self.registry.states.pop(st.workspace_id, None)
        sock.send_json.assert_not_called()

    async def test_notify_workspace_killed_no_state_no_emit(self, app_state):
        # If the state is already gone (double-kill), nothing to emit.
        sock = _mock_sock_for_health()
        try:
            self.registry.app.state.sockets.connections[sock] = (
                SimpleNamespace(user={"id": "u1", "email": "a@x"})
            )
            await self.registry.notify_workspace_killed("no-such-ws")
        finally:
            self.registry.app.state.sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_idle_cleanup_emits_death_frame(self, app_state):
        """Idle-timeout kills must emit the death frame before removing state.

        Regression test for #1343: cleanup_idle_containers called
        stop_and_remove_container (which pops state) before
        notify_workspace_killed (which reads state for the death frame),
        so the frame was silently skipped.
        """
        reg = _health_registry()
        sock = _mock_sock_for_health()
        st = _health_state(
            workspace_id="ws-idle-death",
            container_id="cid-idle-death",
            health_status="healthy",
        )
        reg.states[st.workspace_id] = st
        reg._cid_to_wsid[st.container_id] = st.workspace_id
        st.last_activity = time.time() - reg.idle_timeout_seconds - 100

        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            await _grant_health_member(reg, "u1", st.workspace_id)
            # Patch the registry that actually runs the cleanup loop
            # (``reg``), not ``self.registry`` -- otherwise the real
            # ``podman remove_container`` fires on a fake container id
            # and its subprocess transport leaks (#1493).
            with patch_podman(reg):
                task = asyncio.create_task(reg.cleanup_idle_containers())
                await asyncio.sleep(0.05)
                reg.get_cleanup_wake().set()
                # The notify chain between the wake and send_json has
                # several awaits (idle callbacks, the real ACL fetch in
                # _send_to_workspace_members) -- a fixed sleep races
                # task.cancel() against them under CI load (#2806).
                # Poll with a generous deadline instead; a genuine
                # regression still fails, just 5s later.
                deadline = time.monotonic() + 5.0
                while (
                    not sock.send_json.called and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
            reg.states.pop(st.workspace_id, None)
            reg._cid_to_wsid.pop(st.container_id, None)

        # The death frame must have been emitted.
        sock.send_json.assert_called()
        frame = sock.send_json.call_args[0][0]
        assert frame["type"] == "service_health"
        assert frame["healthy"] is False
        assert frame["running"] is False


class TestHealthLoopHeartbeat:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    """run_health_loop ticks a heartbeat each sweep (#1175 item 3b).

    Emitting from the loop (not a standalone task) ties heartbeat
    presence to the loop being alive."""

    async def test_heartbeats_sent_each_tick_to_opted_in(self, app_state):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        try:
            reg.app.state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"},
                wants_health_heartbeat=True,
            )
            with (
                patch.object(monitor, "_check_workspace", AsyncMock()),
                patch.object(
                    reg.app.state.settings, "health_check_interval", 0.01
                ),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            reg.app.state.sockets.connections.pop(sock, None)
        frames = [c[0][0] for c in sock.send_json.call_args_list]
        assert frames  # at least one heartbeat over ~5 ticks
        assert all(f["type"] == "service_health_heartbeat" for f in frames)


class TestRegistryServiceSessionLocks:
    """The registry owns the per-container service-firing lock dict
    (#1188, #1478). It used to live at module scope in terminal.py and
    the registry delegated; now the dict is ``self._service_session_locks``
    and terminal reaches it via app_state."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry
        self.registry._service_session_locks.clear()

    def teardown_method(self):
        self.registry._service_session_locks.clear()

    def test_get_lock_returns_same_lock_for_same_container(self):
        reg = self.registry
        lock_a = reg.get_service_session_lock("cid")
        lock_b = reg.get_service_session_lock("cid")
        assert lock_a is lock_b

    def test_get_lock_returns_distinct_locks_per_container(self):
        reg = self.registry
        lock_a = reg.get_service_session_lock("cid-a")
        lock_b = reg.get_service_session_lock("cid-b")
        assert lock_a is not lock_b

    def test_clear_lock_removes_entry(self):
        reg = self.registry
        reg.get_service_session_lock("cid")
        assert "cid" in reg._service_session_locks
        reg.clear_service_session_lock("cid")
        assert "cid" not in reg._service_session_locks

    def test_clear_lock_is_noop_for_unknown_container(self):
        # Must not raise for a container that never registered a lock.
        self.registry.clear_service_session_lock("never-seen")

    def test_prune_removes_entries_for_untracked_containers(self):
        reg = self.registry
        reg.get_service_session_lock("alive")
        reg.get_service_session_lock("dead-a")
        reg.get_service_session_lock("dead-b")
        assert len(reg._service_session_locks) == 3

        removed = reg.prune_service_session_locks({"alive"})
        assert removed == 2
        assert set(reg._service_session_locks) == {"alive"}

    async def test_prune_keeps_held_lock_even_if_untracked(self):
        reg = self.registry
        held = reg.get_service_session_lock("held-but-orphaned")
        await held.acquire()  # simulate an in-flight service-command fire
        try:
            removed = reg.prune_service_session_locks(set())
            # Not pruned: recreating its lock would not serialize against the
            # in-flight fire (#1188 duplicate-window race).
            assert removed == 0
            assert "held-but-orphaned" in reg._service_session_locks
        finally:
            held.release()


class TestRegistryServiceFirePending:
    """The registry's per-container pending-fire set (#2740): the marker
    that a service-command fire half-completed (window exists, command
    never typed) so the next ensure_service_session retries the send."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry
        self.registry._service_fire_pending.clear()

    def teardown_method(self):
        self.registry._service_fire_pending.clear()

    def test_mark_and_query_pending(self):
        reg = self.registry
        assert reg.service_fire_pending("cid") is False
        reg.mark_service_fire_pending("cid")
        assert reg.service_fire_pending("cid") is True

    def test_clear_pending(self):
        reg = self.registry
        reg.mark_service_fire_pending("cid")
        reg.clear_service_fire_pending("cid")
        assert reg.service_fire_pending("cid") is False

    def test_clear_pending_is_noop_for_unknown_container(self):
        # Must not raise for a container that never fired.
        self.registry.clear_service_fire_pending("never-seen")

    def test_prune_noop_when_all_tracked(self):
        reg = self.registry
        reg.get_service_session_lock("a")
        reg.get_service_session_lock("b")
        assert reg.prune_service_session_locks({"a", "b"}) == 0

    def test_registry_takes_settings(self):
        """ContainerRegistry.__init__ accepts app_state (#1426, #1487)."""
        import types as types_mod

        settings = make_settings({})
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        reg = container.ContainerRegistry(app_state)
        assert reg.app.state.settings is not None
        assert reg.app is app_state


class TestRegistryConnections:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    """HealthMonitor reaches WebSocketState via the registry, not a module global (#1464)."""

    def test_connections_property_reads_from_registry(self, app_state):
        """The connections property returns self.registry.app.state.sockets."""
        from klangk.wshandler.session import WebSocketState

        ws_state = WebSocketState()
        app_state = _make_app_state(sockets=ws_state)
        reg = app_state.state.container_registry
        assert reg.health.connections is ws_state


class TestRegistrySettingsDerived:
    """Settings-derived attrs on ContainerRegistry (#1487)."""

    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=settings)
        )
        return container.ContainerRegistry(app_state)

    def test_allowed_images_from_settings(self):
        reg = self._registry({"KLANGKD_ALLOWED_IMAGES": "foo,bar"})
        assert "foo" in reg.allowed_images
        assert "bar" in reg.allowed_images
        assert reg.image_name in reg.allowed_images  # default always allowed

    def test_allowed_mount_roots_from_settings(self):
        reg = self._registry({"KLANGKD_ALLOWED_MOUNT_ROOTS": "/home,/data"})
        assert any(r.endswith("/home") for r in reg.allowed_mount_roots)
        assert any(r.endswith("/data") for r in reg.allowed_mount_roots)

    def test_set_idle_timeout(self):
        reg = self._registry({})
        reg.set_idle_timeout(120)
        assert reg.idle_timeout_seconds == 120
        assert reg.check_interval_seconds == max(10, min(60, 120 // 3))


# --- nix /nix bind (#2201) --------------------------------------------------


async def test_nix_binds_empty_without_flag():
    """No /nix bind or env when the workspace hasn't enabled nix."""
    app_state = _make_app_state()
    assert await nix_binds(app_state, "ws1", None) == ([], [])
    assert await nix_binds(app_state, "ws1", {}) == ([], [])
    assert await nix_binds(app_state, "ws1", {"nix": False}) == ([], [])


async def test_nix_binds_empty_when_btrfs_not_configured():
    """Flag on but nix not configured -> ensure returns None -> no bind/env."""
    app_state = _make_app_state()  # no nix_seed -> not configured
    assert await nix_binds(app_state, "ws1", {"nix": True}) == ([], [])


async def test_nix_binds_mounts_snapshot_when_enabled():
    """nix on + configured: snapshot /nix + nix.conf binds AND KLANGKWS_NIX=1."""
    app_state = _make_app_state()
    app_state.state.nix.ensure_workspace_nix = AsyncMock(
        return_value="/mnt/nix-ws1"
    )
    binds, env = await nix_binds(app_state, "ws1", {"nix": True})
    assert binds == [
        "/mnt/nix-ws1/nix:/nix",
        "/mnt/nix-ws1/nix.conf:/etc/nix/nix.conf:ro",
    ]
    assert env == ["KLANGKWS_NIX=1"]
    app_state.state.nix.ensure_workspace_nix.assert_awaited_once_with("ws1")


class TestContainerBranchGaps2834:
    """#2834 branch gate: registry/health/idle/spec outcomes the mainline
    tests only take one side of."""

    def setup_method(self):
        app_state = _make_app_state()
        self.app_state = app_state
        self.registry = app_state.state.container_registry

    # --- registry ---

    def test_record_activity_mapped_container_without_state_is_noop(self):
        # The cid -> workspace mapping exists but the state is gone (a
        # racing teardown): nothing to bump, nothing raised.
        self.registry._cid_to_wsid["cid-gone"] = "ws-gone"
        self.registry.record_activity("cid-gone")

    def test_mark_service_started_mapped_container_without_state_noop(self):
        self.registry._cid_to_wsid["cid-gone"] = "ws-gone"
        self.registry.mark_service_started("cid-gone")

    async def test_stop_user_containers_skips_containerless_workspace(
        self, monkeypatch
    ):
        # A user's workspace without a running container contributes no
        # kill/stop (the row's container_id is None).
        workspaces = AsyncMock()
        workspaces.get_user_workspaces_with_containers = AsyncMock(
            return_value=[
                {"id": "ws-none", "container_id": None},
                {"id": "ws-live", "container_id": "cid-live"},
            ]
        )
        self.app_state.state.model = types.SimpleNamespace(
            workspaces=workspaces
        )
        killed = AsyncMock()
        monkeypatch.setattr(self.registry, "notify_workspace_killed", killed)
        stopped = AsyncMock(return_value=True)
        monkeypatch.setattr(
            self.registry, "stop_and_remove_container", stopped
        )
        await self.registry.stop_user_containers("u1")
        killed.assert_awaited_once_with("ws-live", container_id="cid-live")

    async def test_drain_unremovable_leftover_not_counted(self):
        # A racing-start leftover whose removal fails is not counted as
        # stopped (the caller treats the count as "all clear").
        leftover = {"Id": "cid-left", "Names": "klangk-managed-x"}
        stop = AsyncMock(return_value=False)  # removal failed
        with patch.object(self.registry, "stop_and_remove_container", stop):
            with patch.object(
                self.registry.app.state.podman,
                "list_containers",
                AsyncMock(return_value=[leftover]),
            ):
                assert await self.registry._sweep_drain_leftovers() == 0
        stop.assert_awaited_once_with(
            "cid-left", workspace_id=None, cause=CAUSE_DRAIN
        )

    # --- health ---

    async def test_start_health_loop_twice_keeps_single_task(self):
        health = self.registry.health
        health.start_health_loop()
        first = health.health_task
        assert first is not None
        health.start_health_loop()
        assert health.health_task is first
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass
        health.health_task = None

    # --- idle ---

    def test_on_idle_stop_unknown_workspace_is_noop(self):
        idle = self.registry.idle
        idle.on_idle_stop("ws-gone", lambda wid: None)  # no state, no raise

    def test_set_workspace_idle_timeout_unknown_workspace_is_noop(self):
        idle = self.registry.idle
        # No state -> no timeout write, no wake (must not raise trying to
        # touch the wake event either).
        idle.set_workspace_idle_timeout("ws-gone", 300)

    async def test_idle_reap_skips_callbacks_for_vanished_state(self):
        # Two overdue workspaces; stopping the first also removes the
        # second's state (a racing delete): the loop must skip the second
        # workspace's idle callbacks but still notify + stop it.
        self.registry.track_activity("cid-a", "ws-a")
        self.registry.track_activity("cid-b", "ws-b")
        for ws in ("ws-a", "ws-b"):
            self.registry.states[ws].last_activity = (
                time.time() - self.registry.idle_timeout_seconds - 100
            )
        callbacks = []

        async def _cb(wid):
            callbacks.append(wid)

        self.registry.on_idle_stop("ws-b", _cb)

        real_stop = self.registry.stop_and_remove_container

        async def _stop_and_pop(cid, **kw):
            # The first reap also deletes the second workspace's state.
            self.registry.states.pop("ws-b", None)
            return await real_stop(cid, **kw)

        with patch_podman(self.registry) as p:
            with patch.object(
                self.registry, "stop_and_remove_container", _stop_and_pop
            ):
                with patch.object(
                    self.registry,
                    "notify_workspace_killed",
                    AsyncMock(),
                ):
                    task = asyncio.create_task(
                        self.registry.cleanup_idle_containers()
                    )
                    await asyncio.sleep(0.05)
                    self.registry.get_cleanup_wake().set()
                    # Both reaps must land before the cancel; each now
                    # carries a container_events audit write (#2915), so
                    # poll for the second stop instead of a fixed sleep.
                    for _ in range(200):
                        if p.remove_container.await_count >= 2:
                            break
                        await asyncio.sleep(0.01)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        # ws-b's state vanished before its turn: no idle callbacks ran for
        # it, but its container was still stopped.
        assert callbacks == []
        assert p.remove_container.await_count >= 2

    async def test_idle_loop_survives_volume_sweep_error(self, monkeypatch):
        # A raising orphan-volume sweep is swallowed by the loop's
        # due-wrapper (best-effort, #3153) — the cleanup task itself
        # stays alive.
        async def boom():
            raise RuntimeError("podman gone")

        monkeypatch.setattr(self.registry, "sweep_orphaned_volumes", boom)
        monkeypatch.setattr(
            type(self.registry.idle),
            "_cleanup_interval",
            lambda self: 0.01,
            raising=True,
        )
        task = asyncio.create_task(self.registry.cleanup_idle_containers())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # survived: a RuntimeError would propagate instead

    async def test_orphan_token_sweep_throttles_to_interval(self, monkeypatch):
        # Two loop passes within ORPHAN_TOKEN_SWEEP_INTERVAL: the sweep
        # runs once (the throttle), not per pass.
        sweeps = AsyncMock()
        monkeypatch.setattr(
            self.registry, "sweep_orphaned_sidecar_tokens", sweeps
        )
        monkeypatch.setattr(
            type(self.registry.idle),
            "_cleanup_interval",
            lambda self: 0.01,
            raising=True,
        )
        task = asyncio.create_task(self.registry.cleanup_idle_containers())
        await asyncio.sleep(0.1)  # several passes at 10ms
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        sweeps.assert_awaited_once()

    async def test_sweep_due_helpers_advance_only_when_due(self):
        # The throttle contract of the loop's due-wrappers, asserted
        # directly: a pass inside ORPHAN_TOKEN_SWEEP_INTERVAL neither
        # re-runs the sweep nor advances the timestamp (the loop-based
        # tests above exercise the same arm, but their coverage rides
        # on task scheduling under xdist).
        idle = self.registry.idle
        sweeps = AsyncMock()
        vols = AsyncMock()
        self.registry.sweep_orphaned_sidecar_tokens = sweeps
        self.registry.sweep_orphaned_volumes = vols
        now = 1000.0
        assert await idle._sweep_tokens_if_due(self.registry, 0.0, now) == now
        assert await idle._sweep_volumes_if_due(self.registry, 0.0, now) == now
        soon = now + 1.0
        assert await idle._sweep_tokens_if_due(self.registry, now, soon) == now
        assert (
            await idle._sweep_volumes_if_due(self.registry, now, soon) == now
        )
        sweeps.assert_awaited_once()
        vols.assert_awaited_once()

    # --- spec ---

    def test_hosting_floor_partial_values_get_floors(self):
        # Only the omitted hosting values take the resolver floor; each
        # explicit one survives (including an empty base_path, #2722).
        from klangk.container.spec import hosting_floor

        app = types.SimpleNamespace(
            state=types.SimpleNamespace(util=MagicMock())
        )
        app.state.util.derive_hosting_info = MagicMock(
            return_value=("floor.example", "https", "/floor")
        )
        # Hostname given, proto + base omitted.
        assert hosting_floor(app, "ext.example", None, None) == (
            "ext.example",
            "https",
            "/floor",
        )
        app.state.util.derive_hosting_info = MagicMock(
            return_value=("floor.example", "https", "/floor")
        )
        # Proto given, hostname + base omitted.
        assert hosting_floor(app, None, "http", None) == (
            "floor.example",
            "http",
            "/floor",
        )
        app.state.util.derive_hosting_info = MagicMock(
            return_value=("floor.example", "https", "/floor")
        )
        # Base given (empty string is legitimate), hostname + proto
        # omitted.
        assert hosting_floor(app, None, None, "") == (
            "floor.example",
            "https",
            "",
        )


class TestRegistryIdleDelegation2910:
    def test_cleanup_wake_property_roundtrip(self, app_state):
        """The container registry delegates cleanup_wake to the idle
        monitor (property getter + setter)."""
        registry = app_state.state.container_registry
        wake = asyncio.Event()
        registry.cleanup_wake = wake
        assert registry.cleanup_wake is wake
        registry.cleanup_wake = None
        assert registry.cleanup_wake is None


class TestRegistryWorkspaceEntryPrune2912:
    """#2912: workspace *delete* is the only release path for the
    per-workspace lock and stop-epoch entries -- stops must retain them
    (#1258: a racing start has to serialize against the in-flight stop's
    lock object), but a deleted id can never be started again."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.state.container_registry

    def test_prune_drops_lock_and_epoch_entries(self):
        reg = self.registry
        lock = reg._get_workspace_lock("ws-prune")
        reg.stop_epoch["ws-prune"] = 3
        reg.prune_workspace_registry_entries("ws-prune")
        assert "ws-prune" not in reg._workspace_locks
        assert "ws-prune" not in reg.stop_epoch
        # The popped lock object itself is untouched -- an in-flight
        # holder keeps a working lock; only the dict entry is dropped.
        assert not lock.locked()

    def test_prune_is_noop_for_never_started_workspace(self):
        reg = self.registry
        reg.prune_workspace_registry_entries("ws-never")
        assert "ws-never" not in reg._workspace_locks
        assert "ws-never" not in reg.stop_epoch

    async def test_prune_while_held_replaces_lock_identity(self):
        # A start that already passed its DB check may still hold the lock
        # when a delete prunes it (#2912 review): the holder keeps a
        # working lock object, but a later _get_workspace_lock builds a
        # fresh one that does not serialize against it, and
        # workspace_operation_in_flight stops reporting the workspace.
        # Pin those semantics for the (orphaned) raced container.
        reg = self.registry
        held = reg._get_workspace_lock("ws-held")
        await held.acquire()
        try:
            reg.prune_workspace_registry_entries("ws-held")
            assert reg.workspace_operation_in_flight("ws-held") is False
            fresh = reg._get_workspace_lock("ws-held")
            assert fresh is not held
            assert not fresh.locked()
        finally:
            held.release()
