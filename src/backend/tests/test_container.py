"""Tests for container: idle timeout parsing, activity tracking, callbacks, port allocation."""

import asyncio
import time
import types
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import (
    auth as auth_mod,
    bringup,
    container,
    model,
    podman,
    util as util_mod,
)
from _helpers import make_settings


def _make_app_state(registry=None, sockets=None):
    """Build a minimal app_state for tests."""
    from klangk_backend.podman import Podman
    from klangk_backend.wshandler.session import WebSocketState

    settings = make_settings({})
    podman_inst = Podman(settings)
    # Two-phase: build the namespace shell first so the owned instances
    # (sockets, registry, terminal, plugins) can take app_state at
    # construction and reach collaborators via self.app_state (#1426).
    app_state = types.SimpleNamespace(settings=settings)
    app_state.podman = podman_inst
    if sockets is None:
        sockets = WebSocketState(app_state)
    app_state.sockets = sockets
    if registry is None:
        registry = container.ContainerRegistry(app_state)
    app_state.container_registry = registry
    # #1480: container.py reaches set_workspace_token via app_state.terminal.
    from klangk_backend.terminal import Terminal
    from klangk_backend import plugins as plugins_mod

    app_state.terminal = Terminal(app_state)
    app_state.plugins = plugins_mod.Plugins(app_state)
    from klangk_backend.workspaces import Workspaces

    app_state.workspaces = Workspaces(app_state)
    # #1503: container.py reaches derive_hosting_info via app_state.util.
    app_state.util = util_mod.Util(app_state)

    app_state.auth = auth_mod.Auth(app_state)
    return app_state


@pytest.fixture(autouse=True)
def _stub_bringup(monkeypatch):
    """Block every container-mechanics test from spawning real processes.

    ``start_container`` calls ``bringup.bringup`` at the create choke
    point (#1244), which otherwise reaches ``agent.ensure_agent_home``
    and spawns a real ``podman exec`` subprocess. These tests exercise
    port/sudo/reuse mechanics against a fake ``new-cid`` — they must
    never touch real podman. Bring-up has its own dedicated coverage
    (test_bringup.py).
    """
    monkeypatch.setattr(bringup, "bringup", AsyncMock())


class TestParseIdleTimeout:
    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(settings=settings)
        return container.ContainerRegistry(app_state)

    def test_default_values(self):
        reg = self._registry({})
        assert reg.idle_timeout_seconds == 30 * 60
        assert reg.check_interval_seconds == max(10, min(60, 30 * 60 // 3))

    def test_custom_value(self):
        reg = self._registry({"KLANGK_IDLE_TIMEOUT_SECONDS": "120"})
        assert reg.idle_timeout_seconds == 120
        assert reg.check_interval_seconds == max(10, min(60, 120 // 3))

    def test_invalid_value_uses_default(self):
        reg = self._registry({"KLANGK_IDLE_TIMEOUT_SECONDS": "not_a_number"})
        assert reg.idle_timeout_seconds == 30 * 60

    def test_small_value_clamps_interval(self):
        reg = self._registry({"KLANGK_IDLE_TIMEOUT_SECONDS": "15"})
        assert reg.idle_timeout_seconds == 15
        assert reg.check_interval_seconds == 10  # clamped to min 10

    def test_large_value_clamps_interval(self):
        reg = self._registry({"KLANGK_IDLE_TIMEOUT_SECONDS": "3600"})
        assert reg.idle_timeout_seconds == 3600
        assert reg.check_interval_seconds == 60  # clamped to max 60


class TestSslCertDir:
    """Runtime SSL/CA certificate injection (#1181): ssl_cert_dir() resolver."""

    def test_unset_returns_none(self):
        assert container.ssl_cert_dir(make_settings({})) is None

    def test_missing_dir_returns_none(self, tmp_path):
        gone = tmp_path / "does-not-exist"
        s = make_settings({"KLANGK_SSL_CERT_DIR": str(gone)})
        assert container.ssl_cert_dir(s) is None

    def test_empty_dir_returns_none(self, tmp_path):
        # Dir exists but contains no .pem/.crt.
        (tmp_path / "readme.txt").write_text("no certs here")
        s = make_settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert container.ssl_cert_dir(s) is None

    def test_dir_with_pem_returns_path(self, tmp_path):
        (tmp_path / "ca.pem").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert container.ssl_cert_dir(s) == str(tmp_path.resolve())

    def test_dir_with_crt_returns_path(self, tmp_path):
        (tmp_path / "my-ca.crt").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert container.ssl_cert_dir(s) == str(tmp_path.resolve())

    def test_extension_case_insensitive(self, tmp_path):
        (tmp_path / "CA.PEM").write_text("-----BEGIN CERTIFICATE-----")
        s = make_settings({"KLANGK_SSL_CERT_DIR": str(tmp_path)})
        assert container.ssl_cert_dir(s) == str(tmp_path.resolve())

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


class TestImagePullPolicy:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_default_is_never(self):
        assert self.registry.image_pull_policy() == "never"

    def test_valid_override(self):
        # Rebuild registry with env override
        import types as types_mod

        settings = make_settings({"KLANGK_IMAGE_PULL_POLICY": "missing"})
        app_state = types_mod.SimpleNamespace(settings=settings)
        reg = container.ContainerRegistry(app_state)
        assert reg.image_pull_policy() == "missing"

    def test_invalid_falls_back_to_never(self, caplog):
        import types as types_mod

        settings = make_settings({"KLANGK_IMAGE_PULL_POLICY": "sometimes"})
        app_state = types_mod.SimpleNamespace(settings=settings)
        reg = container.ContainerRegistry(app_state)
        with caplog.at_level("WARNING"):
            assert reg.image_pull_policy() == "never"
        assert "Invalid KLANGK_IMAGE_PULL_POLICY" in caplog.text


class TestActivityTracking:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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

    def test_track_activity_fires_status_changed_on_new(self):
        calls = []
        self.registry.set_on_container_status_changed(
            lambda ws_id, running: calls.append((ws_id, running))
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


def _noop_callback(ws):
    pass


class TestIdleCallbacks:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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
        self.registry = app_state.container_registry

    async def test_allocate_ports(self, workspace):
        ports = await model.find_and_allocate_ports(
            workspace["id"], 3, self.registry.port_range_start
        )
        assert len(ports) == 3
        assert all(p >= self.registry.port_range_start for p in ports)

    async def test_allocate_ports_avoids_used(self, workspace, user):
        # Allocate some ports for workspace 1
        ports1 = await model.find_and_allocate_ports(
            workspace["id"], 3, self.registry.port_range_start
        )
        # Create second workspace and allocate
        ws2 = await model.create_workspace(user["id"], "ws2")
        ports2 = await model.find_and_allocate_ports(
            ws2["id"], 3, self.registry.port_range_start
        )
        # No overlap
        assert set(ports1).isdisjoint(set(ports2))

    async def test_get_workspace_ports(self, workspace):
        app_state = _make_app_state()
        registry = app_state.container_registry
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 2, self.registry.port_range_start
        )
        retrieved = await registry.get_workspace_ports(workspace["id"])
        assert retrieved == sorted(allocated)

    async def test_get_workspace_ports_empty(self, workspace):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ports = await registry.get_workspace_ports(workspace["id"])
        assert ports == []


class TestDnsConfig:
    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(settings=settings)
        return container.ContainerRegistry(app_state)

    def test_no_env_returns_empty(self):
        assert self._registry({}).container_dns_config() == []

    def test_single_server(self):
        assert self._registry(
            {"KLANGK_DNS_SERVERS": "100.100.100.100"}
        ).container_dns_config() == ["100.100.100.100"]

    def test_multiple_servers(self):
        result = self._registry(
            {"KLANGK_DNS_SERVERS": "100.100.100.100, 8.8.8.8"}
        ).container_dns_config()
        assert result == ["100.100.100.100", "8.8.8.8"]

    def test_empty_string(self):
        assert (
            self._registry({"KLANGK_DNS_SERVERS": ""}).container_dns_config()
            == []
        )


class TestConstants:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_port_range_start(self):
        assert self.registry.port_range_start == 9000

    def test_container_port_start(self):
        assert container.CONTAINER_PORT_START == 8000

    def test_default_ports_per_workspace(self):
        assert container.DEFAULT_PORTS_PER_WORKSPACE == 5


class TestPortsPerWorkspaceCap:
    """KLANGK_HOSTED_PORTS_PER_WORKSPACE resolver (#1237)."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(settings=settings)
        return container.ContainerRegistry(app_state)

    def test_default_when_unset(self):
        assert self._registry({}).ports_per_workspace_cap() == 5

    def test_override(self):
        assert (
            self._registry(
                {"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "3"}
            ).ports_per_workspace_cap()
            == 3
        )

    def test_zero_disables(self):
        assert (
            self._registry(
                {"KLANGK_HOSTED_PORTS_PER_WORKSPACE": "0"}
            ).ports_per_workspace_cap()
            == 0
        )

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.settings, "hosted_ports_per_workspace", "abc"
        )
        assert self.registry.ports_per_workspace_cap() == 5

    def test_negative_clamped_to_zero(self, monkeypatch):
        monkeypatch.setattr(
            self.registry.settings, "hosted_ports_per_workspace", "-2"
        )
        assert self.registry.ports_per_workspace_cap() == 0


# --- Container lifecycle tests (mocked) ---


@contextmanager
def patch_podman(registry=None, **overrides):
    """Patch the podman.* calls container.py makes.

    container.py reaches the CLI wrappers via ``self.registry.app_state.podman.X``
    (#1468); this patches the methods on that instance. Yields a namespace
    of the AsyncMocks so tests can assert on them. Override any default by
    passing ``name=AsyncMock(...)``.
    """
    defaults = {
        "inspect_container": AsyncMock(return_value=None),
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
    target = registry.app_state.podman if registry is not None else podman
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


class TestStartContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_create_new_container(self, workspace):
        with patch_podman(self.registry) as p:
            cid, status = await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        assert cid == "new-cid"
        assert status == "created"
        p.start_container.assert_awaited_once_with("new-cid")
        assert workspace["id"] in self.registry.states

    async def test_sudo_disabled_by_default(self, workspace, monkeypatch):
        monkeypatch.delenv("KLANGK_ALLOW_SUDO", raising=False)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        call = _sudo_call(p)
        assert call.kwargs.get("user") == "root"
        assert "!ALL" in str(call.args[1])

    async def test_sudo_enabled(self, workspace, monkeypatch):
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "true")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        call = _sudo_call(p)
        assert call.kwargs.get("user") == "root"
        assert "NOPASSWD:ALL" in str(call.args[1])

    async def test_sudo_disabled(self, workspace, monkeypatch):
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "0")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_disabled_false(self, workspace, monkeypatch):
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "false")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_toggled_off_to_on(self, workspace, monkeypatch):
        """Start with sudo disabled, restart with sudo enabled."""
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "false")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

        # "Restart" — remove container state so start_container creates a new one
        self.registry.states.clear()
        await model.update_workspace_container(workspace["id"], None)
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "true")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "NOPASSWD:ALL" in str(_sudo_call(p).args[1])

    async def test_sudo_toggled_on_to_off(self, workspace, monkeypatch):
        """Start with sudo enabled, restart with sudo disabled."""
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "true")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "NOPASSWD:ALL" in str(_sudo_call(p).args[1])

        self.registry.states.clear()
        await model.update_workspace_container(workspace["id"], None)
        monkeypatch.setattr(self.registry.settings, "allow_sudo", "false")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert "!ALL" in str(_sudo_call(p).args[1])

    async def test_container_id_persisted_before_start(self, workspace, user):
        # If `start` fails, the id created just before it must already be on
        # record so the next connect can inspect/recreate it rather than
        # orphaning a created-but-unrecorded container.
        with patch_podman(
            self.registry,
            start_container=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await self.registry.start_container(
                    workspace["id"], "/tmp/ws", "/tmp/home"
                )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] == "new-cid"
        assert workspace["id"] in self.registry.states

    async def test_cancel_during_start_still_persists(self, workspace, user):
        # The connecting client can disconnect mid-startup, cancelling this
        # coroutine. The shield must let create+persist+start finish so a
        # running container is never orphaned with a NULL container_id.
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_start(_cid):
            started.set()
            await release.wait()

        with patch_podman(
            self.registry, start_container=AsyncMock(side_effect=slow_start)
        ) as p:
            task = asyncio.create_task(
                self.registry.start_container(
                    workspace["id"], "/tmp/ws", "/tmp/home"
                )
            )
            await started.wait()
            task.cancel()  # client disconnects mid-startup
            release.set()  # let the shielded inner run to completion
            with pytest.raises(asyncio.CancelledError):
                await task

        # Despite the cancel, the container was started and recorded.
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] == "new-cid"
        p.start_container.assert_awaited_once_with("new-cid")
        assert workspace["id"] in self.registry.states

    async def test_reuse_running_container(self, workspace):
        with patch_podman(
            self.registry, inspect_container=_running(True)
        ) as p:
            cid, status = await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="existing-cid",
            )
        assert cid == "existing-cid"
        assert status == "connected"
        p.start_container.assert_not_awaited()
        p.create_container.assert_not_awaited()

    async def test_recreate_stopped_container(self, workspace):
        with patch_podman(
            self.registry, inspect_container=_running(False)
        ) as p:
            cid, status = await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="old-cid",
            )
        assert cid == "new-cid"
        assert status == "created"
        p.remove_container.assert_awaited_once_with("old-cid")

    async def test_missing_container_creates_new(self, workspace):
        # inspect_container returns None (default) → treated as gone.
        with patch_podman(self.registry) as p:
            cid, status = await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="gone-cid",
            )
        assert cid == "new-cid"
        assert status == "created"
        p.remove_container.assert_not_awaited()

    async def test_disallowed_image_raises(self, workspace):
        with pytest.raises(ValueError, match="not in the allowed list"):
            await self.registry.start_container(
                workspace["id"], "/work", "/home", image="evil:latest"
            )

    async def test_llm_proxy_env_vars(self, workspace, monkeypatch):
        """Container gets proxy URL, not real API keys."""
        monkeypatch.setattr(self.registry.settings, "llm_model", "gemma4:31b")
        monkeypatch.setattr(self.registry.settings, "nginx_port", "8995")

        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        kwargs = p.create_container.call_args.kwargs
        env = kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["KLANGK_LLM_PROXY_URL"] == (
            "http://host.containers.internal:8995/llm-proxy"
        )
        assert env_dict["KLANGK_LLM_MODEL"] == "gemma4:31b"
        # The agent's home is injected at container start so every exec
        # process (terminals, service command, health check) inherits it.
        assert env_dict["KLANGK_AGENT_HOME"] == "/home/clanker"
        assert (
            env_dict["KLANGK_BRIDGE_URL"]
            == "http://host.containers.internal:8995"
        )
        # API keys should NOT be in the container env
        assert not any(e.startswith("KLANGK_LLM_API_KEY=") for e in env)
        assert not any(e.startswith("ANTHROPIC_API_KEY=") for e in env)
        # host.containers.internal must be resolvable
        assert "host.containers.internal:host-gateway" in kwargs["add_hosts"]

    async def test_workspace_token_written_to_container(self, workspace):
        """Workspace token is written to the container via set_workspace_token."""
        with (
            patch_podman(self.registry),
            patch.object(
                self.registry.app_state.terminal,
                "set_workspace_token",
                new_callable=AsyncMock,
            ) as mock_set,
        ):
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        mock_set.assert_called_once()
        cid, token = mock_set.call_args.args
        assert cid == "new-cid"
        assert cid == "new-cid"
        decoded_ws = self.registry.app_state.auth.decode_workspace_token(token)
        assert decoded_ws == workspace["id"]

    async def test_pull_policy_default_never(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert p.create_container.call_args.kwargs["pull"] == "never"

    async def test_pull_policy_from_env(self, workspace, monkeypatch):
        monkeypatch.setattr(
            self.registry.settings, "image_pull_policy", "missing"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert p.create_container.call_args.kwargs["pull"] == "missing"

    async def test_config_mount_added(self, workspace):
        """Container gets read-only config mount when config_path is set."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                config_path="/tmp/config",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/config:/opt/klangk/config:ro" in binds

    async def test_no_config_mount_without_config_path(self, workspace):
        """Container has no config mount when config_path is not set."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("config" in b for b in binds)

    async def test_home_mounted_at_slash_home(self, workspace):
        """Home path is mounted at /home (not /home/klangk)."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/home:/home" in binds

    async def test_hosting_env_vars(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                hosting_hostname="example.com",
                hosting_proto="https",
                hosting_base_path="/klangk",
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGK_HOSTING_HOSTNAME=example.com" in env
        assert "KLANGK_HOSTING_PROTO=https" in env
        assert "KLANGK_HOSTING_BASE_PATH=/klangk" in env

    async def test_hosting_env_vars_default_is_bare_localhost(
        self, workspace, monkeypatch
    ):
        """Omitted hosting_* resolves to bare localhost (#1240).

        This is the path ``eager_start_workspace`` takes (autostart /
        workspace create have no request to derive from). Before the fix
        the defaults were a bare ``localhost`` with no port anyway, but
        *bypassed* ``derive_hosting_info`` entirely — so a deployer who set
        ``KLANGK_HOSTING_HOSTNAME`` saw it ignored on every eager start.
        Now the choke point resolves it, and no port is synthesized from
        ``KLANGK_NGINX_PORT`` (the port must live in HOSTING_HOSTNAME).
        """
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8996")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGK_HOSTING_HOSTNAME=localhost" in env
        assert "KLANGK_HOSTING_PROTO=http" in env
        assert "KLANGK_HOSTING_BASE_PATH=" in env

    async def test_terminal_banner_default_empty(self, workspace):
        """Default terminal banner is empty, so env var is not passed."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("KLANGK_TERMINAL_BANNER=") for e in env)

    async def test_terminal_banner_custom(self, workspace, monkeypatch):
        """Deployer can set a terminal banner via env var."""
        monkeypatch.setattr(self.registry, "terminal_banner", "Custom warning")
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        env = p.create_container.call_args.kwargs["env"]
        assert "KLANGK_TERMINAL_BANNER=Custom warning" in env

    async def test_ssl_trust_mounted_when_cert_dir_configured(
        self, workspace, monkeypatch, tmp_path
    ):
        """A configured KLANGK_SSL_CERT_DIR is bind-mounted ro and env set (#1181)."""
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        (ssl_dir / "corp-ca.pem").write_text("-----BEGIN CERTIFICATE-----")
        monkeypatch.setattr(
            self.registry.app_state.settings, "ssl_cert_dir", str(ssl_dir)
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert f"{ssl_dir.resolve()}:/opt/klangk/ssl:ro" in binds
        env = p.create_container.call_args.kwargs["env"]
        assert "SSL_CERT_FILE=/tmp/klangk/ca-bundle.crt" in env
        assert "REQUESTS_CA_BUNDLE=/tmp/klangk/ca-bundle.crt" in env
        assert "CURL_CA_BUNDLE=/tmp/klangk/ca-bundle.crt" in env
        assert "NODE_EXTRA_CA_CERTS=/tmp/klangk/ca-bundle.crt" in env

    async def test_no_ssl_trust_when_cert_dir_unset(
        self, workspace, monkeypatch
    ):
        """Without KLANGK_SSL_CERT_DIR there is no mount and no trust env."""
        monkeypatch.delenv("KLANGK_SSL_CERT_DIR", raising=False)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("/opt/klangk/ssl" in b for b in binds)
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("SSL_CERT_FILE=") for e in env)

    async def test_no_ssl_trust_when_dir_has_no_certs(
        self, workspace, monkeypatch, tmp_path
    ):
        """A cert dir with no .pem/.crt is not mounted (#1181)."""
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        (ssl_dir / "notes.txt").write_text("not a cert")
        monkeypatch.setenv("KLANGK_SSL_CERT_DIR", str(ssl_dir))
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("/opt/klangk/ssl" in b for b in binds)
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("SSL_CERT_FILE=") for e in env)

    async def test_port_allocation_on_create(self, workspace):
        with patch_podman(self.registry):
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=3,
            )
        # Ports should have been allocated
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_excess_ports_trimmed(self, workspace):
        # Pre-allocate more ports than needed
        await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=2,
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 2

    async def test_cap_clamps_allocation_down(self, workspace, monkeypatch):
        """KLANGK_HOSTED_PORTS_PER_WORKSPACE clamps num_ports down (#1237)."""
        monkeypatch.setattr(
            self.registry.settings, "hosted_ports_per_workspace", "3"
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=5,  # DB default; cap is 3
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_cap_zero_releases_existing_ports(
        self, workspace, monkeypatch
    ):
        """cap=0 trims an existing workspace's allocations on next start."""
        monkeypatch.setattr(
            self.registry.settings, "hosted_ports_per_workspace", "0"
        )
        await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        with patch_podman(self.registry):
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=5,
            )
        ports = await self.registry.get_workspace_ports(workspace["id"])
        assert ports == []

    async def test_cap_zero_omits_hosting_env(self, workspace, monkeypatch):
        """cap=0 suppresses KLANGK_PORT_MAPPINGS / KLANGK_HOSTING_* (#1237)."""
        monkeypatch.setattr(
            self.registry.settings, "hosted_ports_per_workspace", "0"
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=5,
            )
        env = p.create_container.call_args.kwargs["env"]
        assert not any(e.startswith("KLANGK_PORT_MAPPINGS=") for e in env)
        assert not any(e.startswith("KLANGK_HOSTING_") for e in env)
        # Non-hosting env is still present.
        assert any(e.startswith("KLANGK_WORKSPACE_ID=") for e in env)
        assert any(e.startswith("KLANGK_LLM_PROXY_URL=") for e in env)

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
            self.registry.settings, "hosted_ports_per_workspace", "0"
        )
        await self.registry.allocate_ports(workspace["id"], 5)
        assert await self.registry.get_workspace_ports(workspace["id"]) == []

    async def test_hosting_env_present_when_enabled(
        self, workspace, monkeypatch
    ):
        """Sanity: with the default cap, hosting env is injected as before."""
        monkeypatch.delenv("KLANGK_HOSTED_PORTS_PER_WORKSPACE", raising=False)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=5,
            )
        env = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["KLANGK_PORT_MAPPINGS"].count(",") == 4  # 5 mappings
        assert "KLANGK_HOSTING_HOSTNAME" in env_dict
        assert "KLANGK_HOSTING_PROTO" in env_dict
        assert "KLANGK_HOSTING_BASE_PATH" in env_dict

    async def test_container_config_structure(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        args, kwargs = p.create_container.call_args
        assert args[1] == self.registry.image_name
        assert kwargs["labels"]["klangk.managed"] == "true"
        assert kwargs["labels"]["klangk.workspace-id"] == workspace["id"]
        assert kwargs["init"] is True
        assert kwargs["interactive"] is True

    async def test_create_container_with_extra_env(self, workspace):
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_env={"MY_VAR": "hello", "FOO": "bar"},
            )
        env_list = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["MY_VAR"] == "hello"
        assert env_dict["FOO"] == "bar"

    async def test_plugins_env_injected(self, workspace, monkeypatch):
        monkeypatch.setattr(
            self.registry.app_state.plugins,
            "declarations",
            {
                "PLUGIN_VAR": {
                    "plugin": "test",
                    "description": "",
                    "default": "",
                    "scope": "container",
                }
            },
        )
        monkeypatch.setattr(
            self.registry.app_state.plugins,
            "values",
            {"PLUGIN_VAR": "plugin-val"},
        )
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        env_list = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["PLUGIN_VAR"] == "plugin-val"


class TestStartContainerPortConflict:
    """Test retry logic when a stale container holds a port."""

    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_port_conflict_removes_stale_and_retries(self, workspace):
        # Pre-allocate ports so we know exactly which ones the workspace gets.
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert status == "created"
        assert len(start_calls) == 2
        remove_calls = [c.args[0] for c in p.remove_container.call_args_list]
        assert "stale-cid" in remove_calls

    async def test_port_conflict_skips_own_container(self, workspace):
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        # Should not have tried to remove its own container
        for call in p.remove_container.call_args_list:
            assert call.args[0] != "new-cid"

    async def test_port_conflict_skips_non_overlapping(self, workspace):
        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        # other-cid doesn't hold our ports — should not be removed

    async def test_port_conflict_stale_vanished(self, workspace):
        """Stale container gone by the time we inspect it."""
        await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        # gone-cid vanished — no remove attempted
        assert not any(
            c.args[0] == "gone-cid" for c in p.remove_container.call_args_list
        )

    async def test_port_conflict_bad_port_bindings(self, workspace):
        """Malformed HostPort values don't crash the retry."""
        await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )

    async def test_port_conflict_remove_error_logged(self, workspace):
        """Error removing stale container is logged, not raised."""
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, self.registry.port_range_start
        )
        conflict_port = allocated[0]
        start_calls = []

        async def start_side_effect(cid):
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
                workspace["id"], "/tmp/ws", "/tmp/home"
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )


class TestValidateMountSpec:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_valid_bind_mount(self):
        assert self.registry.validate_mount_spec("/host:/container") is None

    def test_valid_volume_mount(self):
        assert self.registry.validate_mount_spec("vol-name:/data") is None

    def test_valid_with_options(self):
        assert self.registry.validate_mount_spec("/host:/container:ro") is None

    def test_valid_with_multiple_options(self):
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

    def test_validate_mounts_list(self):
        assert self.registry.validate_mounts(["/a:/b", "vol:/c"]) is None

    def test_validate_mounts_list_with_error(self):
        err = self.registry.validate_mounts(["/a:/b", "bad"])
        assert err is not None


class TestAllowedMountRoots:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_bind_mount_allowed(self, monkeypatch):
        monkeypatch.setattr(
            self.registry, "allowed_mount_roots", ["/home", "/data"]
        )
        assert (
            self.registry.validate_mount_spec("/home/user/src:/work") is None
        )

    def test_bind_mount_exact_root(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/home"])
        assert self.registry.validate_mount_spec("/home:/work") is None

    def test_bind_mount_denied(self, monkeypatch):
        monkeypatch.setattr(
            self.registry, "allowed_mount_roots", ["/home", "/data"]
        )
        err = self.registry.validate_mount_spec("/etc/passwd:/etc/passwd:ro")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_bind_mount_traversal_denied(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/home"])
        err = self.registry.validate_mount_spec("/home/../etc:/work")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_named_volume_always_allowed(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/home"])
        assert self.registry.validate_mount_spec("my-volume:/data") is None

    def test_no_restriction_when_empty(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", [])
        assert (
            self.registry.validate_mount_spec("/etc/shadow:/secrets") is None
        )

    def test_multiple_roots(self, monkeypatch):
        monkeypatch.setattr(
            self.registry, "allowed_mount_roots", ["/home", "/data", "/opt"]
        )
        assert self.registry.validate_mount_spec("/data/files:/work") is None
        assert self.registry.validate_mount_spec("/opt/app:/app") is None
        err = self.registry.validate_mount_spec("/var/log:/logs")
        assert err is not None


class TestProtectedPaths:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_docker_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/"])
        err = self.registry.validate_mount_spec(
            "/var/run/docker.sock:/var/run/docker.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_podman_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/"])
        err = self.registry.validate_mount_spec(
            "/run/podman/podman.sock:/run/podman/podman.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_data_dir_blocked(self, monkeypatch):
        monkeypatch.setattr(self.registry, "allowed_mount_roots", ["/"])
        monkeypatch.setattr(
            self.registry.settings, "data_dir", "/srv/klangk/data"
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
        # KLANGK_DATA_DIR that conftest sets to tmp_path.
        with tempfile.TemporaryDirectory(prefix="mount-test-") as d:
            d = Path(d)
            allowed = d / "allowed"
            allowed.mkdir()
            target = allowed / "data"
            target.mkdir()
            link = d / "link-to-data"
            link.symlink_to(str(target))

            monkeypatch.setattr(
                self.registry,
                "allowed_mount_roots",
                [str(allowed)],
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
                self.registry,
                "allowed_mount_roots",
                [str(allowed)],
            )
            err = self.registry.validate_mount_spec(f"{link}:/mnt/data")
            assert err is not None
            assert "allowed root" in err.lower()


class TestExtraMountsVolumeCreation:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_auto_creates_named_volume(self, workspace):
        """Named volumes (no leading /) are auto-created with klangk labels."""
        # inspect_volume returns None (default) → volume is created.
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["nix-store:/nix"],
                user_id="user-123",
            )
        p.create_volume.assert_awaited_once()
        name, labels = p.create_volume.call_args.args
        assert name == "nix-store"
        assert labels["klangk.managed"] == "true"
        assert labels["klangk.instance"] == model.get_instance_id()
        assert labels["klangk.user-id"] == "user-123"

    async def test_existing_volume_not_recreated(self, workspace):
        """Existing volumes owned by this instance and user are used as-is."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "existing",
                    "Labels": {
                        "klangk.instance": model.get_instance_id(),
                        "klangk.user-id": "user-123",
                    },
                }
            ),
        ) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["existing:/data"],
                user_id="user-123",
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
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["stolen:/data"],
                )

    async def test_unlabelled_volume_rejected(self, workspace):
        """A named volume with no klangk labels is refused."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(return_value={"Name": "bare"}),
        ):
            with pytest.raises(ValueError, match="not managed by this"):
                await self.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["bare:/data"],
                )

    async def test_cross_user_volume_rejected(self, workspace):
        """A volume owned by another user is refused."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "private",
                    "Labels": {
                        "klangk.instance": model.get_instance_id(),
                        "klangk.user-id": "user-other",
                    },
                }
            ),
        ):
            with pytest.raises(ValueError, match="belongs to another user"):
                await self.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["private:/data"],
                    user_id="user-me",
                )

    async def test_volume_without_user_label_allowed(self, workspace):
        """A volume with no user-id label (pre-existing) is allowed."""
        with patch_podman(
            self.registry,
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "legacy",
                    "Labels": {
                        "klangk.instance": model.get_instance_id(),
                    },
                }
            ),
        ) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["legacy:/data"],
                user_id="user-123",
            )
        p.create_volume.assert_not_awaited()

    async def test_bind_mount_not_treated_as_volume(
        self, workspace, monkeypatch
    ):
        """Bind mounts (starting with /) are not treated as volumes."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/home/me/src:/work/src"],
            )
        p.inspect_volume.assert_not_awaited()

    async def test_mount_with_multiple_colons(self, workspace, monkeypatch):
        """Mount spec with options (host:container:ro) — source starts with /."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/data/shared:/mnt/data:ro"],
            )
        # Bind mount, not a volume — inspect_volume should not be called
        p.inspect_volume.assert_not_awaited()

    async def test_volume_mount_with_options(self, workspace):
        """Named volume with options (vol:container:ro) — auto-creates."""
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["my-vol:/data:ro"],
            )
        p.create_volume.assert_awaited_once()

    async def test_mount_source_with_slash_is_bind(
        self, workspace, monkeypatch
    ):
        """A mount source containing slashes is a bind mount, not a volume."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["./relative/path:/work/rel"],
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
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["bad-vol:/data"],
            )

    async def test_mount_source_with_special_characters(
        self, workspace, monkeypatch
    ):
        """Mount source with special/binary-like chars is a bind mount."""
        monkeypatch.setattr("os.path.exists", lambda p: True)
        with patch_podman(self.registry) as p:
            await self.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/path/with spaces\x00and\x01binary:/work/bad"],
            )
        # Has leading /, so treated as bind mount
        p.inspect_volume.assert_not_awaited()

    async def test_missing_bind_mount_source_rejected(self, workspace):
        """A bind mount with a non-existent source path is refused."""
        with patch_podman(self.registry):
            with pytest.raises(ValueError, match="does not exist"):
                await self.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["/nonexistent/path:/work/src"],
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
                workspace["id"], "/tmp/ws", "/tmp/home"
            )

        # No browser registrations should remain for this workspace
        for bid, (ws_id, _sock) in self.registry._browsers.items():
            assert ws_id != workspace["id"]


class TestStopContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_stop_running(self):
        self.registry.track_activity("cid", "ws")
        lock = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry) as p:
            await self.registry.stop_and_remove_container("cid")
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks
        assert self.registry._workspace_locks["ws"] is lock

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
                await self.registry.stop_and_remove_container("alive")

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
            await self.registry.stop_and_remove_container("cid")
        # Should still remove from tracking
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks

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
                    self.registry.stop_and_remove_container("cid")
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
                    self.registry.stop_and_remove_container("cid-old")
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

    async def test_stop_does_not_replace_workspace_lock(self):
        # Regression for the lock-replacement race: even after stop tears
        # down state, _get_workspace_lock must return the SAME lock object,
        # so a subsequent start serializes against any in-flight acquirer.
        self.registry.track_activity("cid", "ws")
        lock_before = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry):
            await self.registry.stop_and_remove_container("cid")
        assert "ws" in self.registry._workspace_locks
        lock_after = self.registry._get_workspace_lock("ws")
        assert lock_after is lock_before


class TestRemoveContainer:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_remove(self):
        self.registry.track_activity("cid", "ws")
        lock = self.registry._get_workspace_lock("ws")

        with patch_podman(self.registry) as p:
            await self.registry.stop_and_remove_container("cid")
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
            await self.registry.stop_and_remove_container("cid")
        assert "ws" not in self.registry.states
        assert "cid" not in self.registry._cid_to_wsid
        # The workspace lock entry is deliberately retained (#1258).
        assert "ws" in self.registry._workspace_locks


class TestStopUserContainers:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_stop_user_containers(self, user, workspace):
        # Set container_id on the workspace
        await model.update_workspace_container(workspace["id"], "cid")
        self.registry.track_activity("cid", workspace["id"])

        with patch_podman(self.registry) as p:
            await self.registry.stop_user_containers(user["id"])
        p.remove_container.assert_awaited_once_with("cid")
        assert workspace["id"] not in self.registry.states

    async def test_stop_user_calls_workspace_killed(self, user, workspace):
        await model.update_workspace_container(workspace["id"], "cid")
        self.registry.track_activity("cid", workspace["id"])

        killed_cb = AsyncMock()
        old_cb = self.registry.on_workspace_killed
        self.registry.on_workspace_killed = killed_cb

        with patch_podman(self.registry):
            await self.registry.stop_user_containers(user["id"])

        killed_cb.assert_awaited_once_with(workspace["id"])
        self.registry.on_workspace_killed = old_cb

    async def test_stop_user_no_containers(self, user):
        with patch_podman(self.registry) as p:
            await self.registry.stop_user_containers(user["id"])
        p.remove_container.assert_not_awaited()


class TestShutdown:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_shutdown_skips_in_container(self):
        """When running inside a container, shutdown skips cleanup."""
        self.registry.track_activity("cid", "ws")
        with patch("os.path.exists", return_value=True):
            await self.registry.shutdown()
        # Container should still be tracked (not cleaned up)
        assert "ws" in self.registry.states

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
        self.registry = app_state.container_registry

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

        killed_cb.assert_awaited_once_with("ws-killed")
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


class TestStartCleanupLoop:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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
        self.registry = app_state.container_registry

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


class TestAdoptOrphanedContainers:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    async def test_removes_orphaned_containers(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "orphan-123",
                        "Labels": {"klangk.workspace-id": "ws-orphan"},
                    }
                ]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.adopt_orphaned_containers()
        # Orphaned containers are removed, not adopted.
        assert "ws-orphan" not in self.registry.states
        mocks.remove_container.assert_awaited_once_with("orphan-123")

    async def test_removes_orphan_without_labels(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[{"Id": "orphan-x", "Labels": None}]
            ),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.adopt_orphaned_containers()
        assert "unknown" not in self.registry.states
        mocks.remove_container.assert_awaited_once_with("orphan-x")

    async def test_skips_already_tracked(self):
        self.registry.track_activity("tracked-456", "ws-tracked")
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(return_value=[{"Id": "tracked-456"}]),
            remove_container=AsyncMock(),
        ) as mocks:
            await self.registry.adopt_orphaned_containers()
        # Already tracked → not removed.
        assert self.registry.states["ws-tracked"].container_id == "tracked-456"
        mocks.remove_container.assert_not_awaited()

    async def test_podman_error_handled(self):
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "fail")
            ),
        ):
            await self.registry.adopt_orphaned_containers()
        # Should not raise

    async def test_remove_podman_error_handled(self):
        """When remove_container raises PodmanError, it is logged but not raised."""
        with patch_podman(
            self.registry,
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "orphan-bad",
                        "Labels": {"klangk.workspace-id": "ws-bad"},
                    }
                ]
            ),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "remove failed")
            ),
        ) as mocks:
            await self.registry.adopt_orphaned_containers()
        mocks.remove_container.assert_awaited_once_with("orphan-bad")
        # Container was not adopted — just skipped after failed removal.
        assert "ws-bad" not in self.registry.states


class TestBrowserRegistry:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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


class TestWorkspaceIdFor:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    def test_returns_workspace_id(self):
        self.registry.track_activity("cid-lookup", "ws-lookup")
        try:
            assert self.registry.workspace_id_for("cid-lookup") == "ws-lookup"
        finally:
            self.registry.states.pop("ws-lookup", None)
            self.registry._cid_to_wsid.pop("cid-lookup", None)

    def test_returns_none_for_unknown(self):
        assert self.registry.workspace_id_for("nonexistent") is None


class TestTrackActivityContainerChanged:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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
    HealthMonitor reaches sockets via ``self.registry.app_state.sockets``.
    """
    app_state = _make_app_state(sockets=ws_state)
    return app_state.container_registry


def _health_state(
    *,
    workspace_id="ws-h",
    container_id="cid1234567890",
    health_check="curl -sf http://localhost:8080/health",
    owner_id="uid-owner",
    setup_state="complete",
    health_status=None,
    in_startup_grace=False,
    registry=None,
):
    """Build a ContainerState wired up for health checks.

    *in_startup_grace* defaults to False so the core healthy/unhealthy
    tests exercise post-grace behavior; the startup-grace tests opt in.
    """
    if registry is None:
        registry = _health_registry()
    st = container.ContainerState(workspace_id, container_id, registry)
    st.health_check = health_check
    st.owner_id = owner_id
    st.setup_state = setup_state
    st.health_status = health_status
    # 0.0 = epoch, comfortably outside any real grace window.
    st.service_started_at = time.time() if in_startup_grace else 0.0
    return st


class TestHealthMonitorRunOne:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    """_run_one: rc 0 → healthy, non-zero/error → unhealthy (with reason)."""

    async def test_exit_zero_is_healthy(self):
        monitor = _health_registry().health
        st = _health_state()
        exec_mock = AsyncMock(return_value=(0, "", ""))
        with (
            patch.object(
                monitor.registry.app_state.podman, "exec_container", exec_mock
            ),
            patch.object(
                model, "get_user_handle", AsyncMock(return_value="owner")
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
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

    async def test_nonzero_exit_is_unhealthy_with_stderr_reason(self):
        # The stderr that explains the non-zero exit is captured as the
        # reason instead of being thrown away (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.registry.app_state.podman,
                "exec_container",
                AsyncMock(return_value=(1, "", "curl: connection refused")),
            ),
            patch.object(
                model, "get_user_handle", AsyncMock(return_value="owner")
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "connection refused" in message
        assert "exited 1" in message

    async def test_nonzero_exit_falls_back_to_stdout(self):
        # No stderr → the reason uses stdout instead.
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.registry.app_state.podman,
                "exec_container",
                AsyncMock(return_value=(2, "all good on stdout", "")),
            ),
            patch.object(
                model, "get_user_handle", AsyncMock(return_value="owner")
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "all good on stdout" in message

    async def test_nonzero_exit_no_output_reports_exit_code(self):
        # Non-zero exit but no output at all → still surface the exit
        # code so it isn't a complete black box (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.registry.app_state.podman,
                "exec_container",
                AsyncMock(return_value=(127, "", "")),
            ),
            patch.object(
                model, "get_user_handle", AsyncMock(return_value="owner")
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
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

    async def test_exec_error_is_unhealthy_with_reason(self):
        # The podman/timeout failure text is captured as the reason
        # instead of being discarded (#1088).
        monitor = _health_registry().health
        st = _health_state()
        with (
            patch.object(
                monitor.registry.app_state.podman,
                "exec_container",
                AsyncMock(side_effect=podman.PodmanError(500, "boom")),
            ),
            patch.object(
                model, "get_user_handle", AsyncMock(return_value="owner")
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "home_path",
                return_value="/h/p",
            ),
            patch.object(
                monitor.registry.app_state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
                return_value=("/home/klangk", False),
            ),
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "PodmanError" in message
        assert "boom" in message

    async def test_no_owner_is_unhealthy_with_reason(self):
        monitor = _health_registry().health
        st = _health_state(owner_id=None)
        with patch.object(
            monitor.registry.app_state.podman, "exec_container"
        ) as exec_mock:
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "owner" in message
        exec_mock.assert_not_called()

    async def test_no_handle_is_unhealthy_with_reason(self):
        # Owner exists in the state but has no handle resolved.
        monitor = _health_registry().health
        st = _health_state(owner_id="uid-owner")
        with (
            patch.object(
                model, "get_user_handle", AsyncMock(return_value=None)
            ),
            patch.object(
                monitor.registry.app_state.podman, "exec_container"
            ) as exec_mock,
        ):
            status, message = await monitor._run_one(st)
        assert status == "unhealthy"
        assert "handle" in message
        exec_mock.assert_not_called()


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
            patch.object(monitor, "_broadcast") as bcast,
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
            patch.object(monitor, "_broadcast") as bcast,
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
            with caplog.at_level(
                logging.INFO, logger="klangk_backend.container"
            ):
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
            with caplog.at_level(
                logging.DEBUG, logger="klangk_backend.container"
            ):
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
        self.registry = app_state.container_registry

    async def test_unhealthy_during_grace_is_suppressed(self):
        monitor = _health_registry().health
        st = _health_state(health_status=None, in_startup_grace=True)
        with (
            patch.object(
                monitor,
                "_run_one",
                AsyncMock(return_value=("unhealthy", "connection refused")),
            ),
            patch.object(monitor, "_broadcast") as bcast,
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
            patch.object(monitor, "_broadcast") as bcast,
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
            patch.object(monitor, "_broadcast") as bcast,
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

    """_broadcast fans out to ALL connections, not just the session."""

    def test_fans_out_via_notify_service_health(self):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="unhealthy")
        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            # No WorkspaceSession registered for this workspace — yet
            # the event must still reach the connection.
            monitor._broadcast(st, "unhealthy", "connection refused")
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
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


class TestHealthMonitorLoopSkips:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

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
                patch.object(reg, "health_check_interval", 0.01),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await asyncio.sleep(0.05)
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
        reg.states[st.workspace_id] = st
        try:
            with (
                patch.object(
                    monitor, "_check_workspace", AsyncMock()
                ) as check,
                patch.object(reg, "health_check_interval", 0.01),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await asyncio.sleep(0.05)
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
                patch.object(reg, "health_check_interval", 0.01),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await asyncio.sleep(0.05)
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

    def test_bumps_seq_each_emit_and_forwards_fields(self):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="unhealthy")
        st.health_checked_at = 1_700_000_000.0
        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            monitor._broadcast(st, "unhealthy", "connection refused")
            monitor._broadcast(st, "unhealthy", "connection refused")
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
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
        self.registry = app_state.container_registry

    def test_broadcast_death_emits_terminal_frame(self):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        st = _health_state(health_status="healthy")
        st.health_checked_at = 1_700_000_000.0
        st.health_seq = 4
        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            monitor.broadcast_death(st)
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
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

        async def on_killed(wid):
            # The state must still be present when the callback runs --
            # death emission happens first, before removal.
            seen_state_present.append(wid in reg.states)

        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            reg.set_on_workspace_killed(on_killed)
            await reg.notify_workspace_killed(st.workspace_id)
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
            reg.states.pop(st.workspace_id, None)
            reg.set_on_workspace_killed(None)
        frame = sock.send_json.call_args[0][0]
        assert frame["healthy"] is False
        assert frame["running"] is False
        assert seen_state_present == [True]

    async def test_notify_workspace_killed_skips_non_health_checked(self):
        # A workspace with no health_check never appeared on the stream,
        # so its death emits no terminal frame.
        sock = _mock_sock_for_health()
        st = _health_state(health_check=None)
        self.registry.states[st.workspace_id] = st
        try:
            self.registry.app_state.sockets.connections[sock] = (
                SimpleNamespace(user={"id": "u1", "email": "a@x"})
            )
            await self.registry.notify_workspace_killed(st.workspace_id)
        finally:
            self.registry.app_state.sockets.connections.pop(sock, None)
            self.registry.states.pop(st.workspace_id, None)
        sock.send_json.assert_not_called()

    async def test_notify_workspace_killed_no_state_no_emit(self):
        # If the state is already gone (double-kill), nothing to emit.
        sock = _mock_sock_for_health()
        try:
            self.registry.app_state.sockets.connections[sock] = (
                SimpleNamespace(user={"id": "u1", "email": "a@x"})
            )
            await self.registry.notify_workspace_killed("no-such-ws")
        finally:
            self.registry.app_state.sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_idle_cleanup_emits_death_frame(self):
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
        st.last_activity = (
            time.time() - self.registry.idle_timeout_seconds - 100
        )

        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"}
            )
            with patch_podman(self.registry):
                task = asyncio.create_task(reg.cleanup_idle_containers())
                await asyncio.sleep(0.05)
                reg.get_cleanup_wake().set()
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
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
        self.registry = app_state.container_registry

    """run_health_loop ticks a heartbeat each sweep (#1175 item 3b).

    Emitting from the loop (not a standalone task) ties heartbeat
    presence to the loop being alive."""

    async def test_heartbeats_sent_each_tick_to_opted_in(self):
        reg = _health_registry()
        monitor = reg.health
        sock = _mock_sock_for_health()
        try:
            reg.app_state.sockets.connections[sock] = SimpleNamespace(
                user={"id": "u1", "email": "a@x"},
                wants_health_heartbeat=True,
            )
            with (
                patch.object(monitor, "_check_workspace", AsyncMock()),
                patch.object(reg, "health_check_interval", 0.01),
            ):
                task = asyncio.create_task(monitor.run_health_loop())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            reg.app_state.sockets.connections.pop(sock, None)
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
        self.registry = app_state.container_registry
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

    def test_prune_noop_when_all_tracked(self):
        reg = self.registry
        reg.get_service_session_lock("a")
        reg.get_service_session_lock("b")
        assert reg.prune_service_session_locks({"a", "b"}) == 0

    def test_registry_takes_settings(self):
        """ContainerRegistry.__init__ accepts app_state (#1426, #1487)."""
        import types as types_mod

        settings = make_settings({})
        app_state = types_mod.SimpleNamespace(settings=settings)
        reg = container.ContainerRegistry(app_state)
        assert reg.settings is not None
        assert reg.app_state is app_state


class TestRegistryConnections:
    def setup_method(self):
        app_state = _make_app_state()
        self.registry = app_state.container_registry

    """HealthMonitor reaches WebSocketState via the registry, not a module global (#1464)."""

    def test_connections_property_reads_from_registry(self):
        """The connections property returns self.registry.app_state.sockets."""
        from klangk_backend.wshandler.session import WebSocketState

        ws_state = WebSocketState()
        app_state = _make_app_state(sockets=ws_state)
        reg = app_state.container_registry
        assert reg.health.connections is ws_state


class TestRegistrySettingsDerived:
    """Settings-derived attrs on ContainerRegistry (#1487)."""

    def _registry(self, env):
        import types as types_mod

        settings = make_settings(env)
        app_state = types_mod.SimpleNamespace(settings=settings)
        return container.ContainerRegistry(app_state)

    def test_allowed_images_from_settings(self):
        reg = self._registry({"KLANGK_ALLOWED_IMAGES": "foo,bar"})
        assert "foo" in reg.allowed_images
        assert "bar" in reg.allowed_images
        assert reg.image_name in reg.allowed_images  # default always allowed

    def test_allowed_mount_roots_from_settings(self):
        reg = self._registry({"KLANGK_ALLOWED_MOUNT_ROOTS": "/home,/data"})
        assert any(r.endswith("/home") for r in reg.allowed_mount_roots)
        assert any(r.endswith("/data") for r in reg.allowed_mount_roots)

    def test_set_idle_timeout(self):
        reg = self._registry({})
        reg.set_idle_timeout(120)
        assert reg.idle_timeout_seconds == 120
        assert reg.check_interval_seconds == max(10, min(60, 120 // 3))
