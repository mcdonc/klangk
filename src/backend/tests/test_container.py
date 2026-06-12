"""Tests for container: idle timeout parsing, activity tracking, callbacks, port allocation."""

import asyncio
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import container, model, podman


class TestParseIdleTimeout:
    def test_default_values(self, monkeypatch):
        monkeypatch.delenv("KLANGK_IDLE_TIMEOUT_SECONDS", raising=False)
        timeout, interval = container.parse_idle_timeout()
        assert timeout == 30 * 60
        assert interval == max(10, min(60, timeout // 3))

    def test_custom_value(self, monkeypatch):
        monkeypatch.setenv("KLANGK_IDLE_TIMEOUT_SECONDS", "120")
        timeout, interval = container.parse_idle_timeout()
        assert timeout == 120
        assert interval == max(10, min(60, 120 // 3))

    def test_invalid_value_uses_default(self, monkeypatch):
        monkeypatch.setenv("KLANGK_IDLE_TIMEOUT_SECONDS", "not_a_number")
        timeout, interval = container.parse_idle_timeout()
        assert timeout == 30 * 60

    def test_small_value_clamps_interval(self, monkeypatch):
        monkeypatch.setenv("KLANGK_IDLE_TIMEOUT_SECONDS", "15")
        timeout, interval = container.parse_idle_timeout()
        assert timeout == 15
        assert interval == 10  # clamped to min 10

    def test_large_value_clamps_interval(self, monkeypatch):
        monkeypatch.setenv("KLANGK_IDLE_TIMEOUT_SECONDS", "3600")
        timeout, interval = container.parse_idle_timeout()
        assert timeout == 3600
        assert interval == 60  # clamped to max 60


class TestImagePullPolicy:
    def test_default_is_never(self, monkeypatch):
        monkeypatch.delenv("KLANGK_IMAGE_PULL_POLICY", raising=False)
        assert container.image_pull_policy() == "never"

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("KLANGK_IMAGE_PULL_POLICY", "missing")
        assert container.image_pull_policy() == "missing"

    def test_invalid_falls_back_to_never(self, monkeypatch, caplog):
        monkeypatch.setenv("KLANGK_IMAGE_PULL_POLICY", "sometimes")
        with caplog.at_level("WARNING"):
            assert container.image_pull_policy() == "never"
        assert "Invalid KLANGK_IMAGE_PULL_POLICY" in caplog.text


class TestActivityTracking:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    def testtrack_activity(self):
        container.registry.track_activity("cid-1", "ws-1")
        assert "ws-1" in container.registry.states
        state = container.registry.states["ws-1"]
        assert state.container_id == "cid-1"
        assert state.last_activity <= time.time()

    def test_record_activity_updates_time(self):
        container.registry.track_activity("cid-1", "ws-1")
        old_time = container.registry.states["ws-1"].last_activity
        time.sleep(0.01)
        container.registry.record_activity("cid-1")
        new_time = container.registry.states["ws-1"].last_activity
        assert new_time > old_time

    def test_record_activity_unknown_container(self):
        # Should not raise
        container.registry.record_activity("nonexistent")
        assert "nonexistent" not in container.registry.states

    def testtrack_activity_overwrites(self):
        container.registry.track_activity("cid-1", "ws-1")
        container.registry.track_activity("cid-1", "ws-2")
        assert container.registry.states["ws-2"].container_id == "cid-1"

    def test_track_activity_same_workspace_updates_container(self):
        container.registry.track_activity("cid-1", "ws-1")
        container.registry.track_activity("cid-1", "ws-1")
        assert container.registry.states["ws-1"].container_id == "cid-1"

    def test_remove_state_cleans_up_reverse_mapping(self):
        container.registry.track_activity("cid-rm", "ws-rm")
        assert "cid-rm" in container.registry._cid_to_wsid
        container.registry.remove_state("ws-rm")
        assert "ws-rm" not in container.registry.states
        assert "cid-rm" not in container.registry._cid_to_wsid

    def test_get_state_returns_state(self):
        container.registry.track_activity("cid-1", "ws-1")
        state = container.registry.get_state("ws-1")
        assert state is not None
        assert state.container_id == "cid-1"

    def test_get_state_returns_none_for_unknown(self):
        assert container.registry.get_state("nonexistent") is None


def _noop_callback(ws):
    pass


class TestIdleCallbacks:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    def test_on_idle_stop_registers(self):
        container.registry.track_activity("cid-1", "ws-1")
        container.registry.on_idle_stop("ws-1", _noop_callback)
        assert (
            _noop_callback in container.registry.states["ws-1"].idle_callbacks
        )

    def test_multiple_callbacks(self):
        def cb2(ws):
            pass

        container.registry.track_activity("cid-1", "ws-1")
        container.registry.on_idle_stop("ws-1", _noop_callback)
        container.registry.on_idle_stop("ws-1", cb2)
        assert len(container.registry.states["ws-1"].idle_callbacks) == 2

    def test_remove_idle_callback(self):
        container.registry.track_activity("cid-1", "ws-1")
        container.registry.on_idle_stop("ws-1", _noop_callback)
        container.registry.remove_idle_callback("ws-1", _noop_callback)
        assert (
            _noop_callback
            not in container.registry.states["ws-1"].idle_callbacks
        )

    def test_remove_idle_callback_not_registered(self):
        container.registry.track_activity("cid-1", "ws-1")
        container.registry.remove_idle_callback("ws-1", _noop_callback)
        assert (
            _noop_callback
            not in container.registry.states["ws-1"].idle_callbacks
        )

    def test_remove_idle_callback_unknown_workspace(self):
        container.registry.remove_idle_callback("nonexistent", _noop_callback)
        assert "nonexistent" not in container.registry.states

    def test_callbacks_per_workspace(self):
        def cb2(ws):
            pass

        container.registry.track_activity("cid-1", "ws-1")
        container.registry.track_activity("cid-2", "ws-2")
        container.registry.on_idle_stop("ws-1", _noop_callback)
        container.registry.on_idle_stop("ws-2", cb2)
        assert (
            _noop_callback in container.registry.states["ws-1"].idle_callbacks
        )
        assert cb2 in container.registry.states["ws-2"].idle_callbacks
        assert (
            _noop_callback
            not in container.registry.states["ws-2"].idle_callbacks
        )


class TestPortAllocation:
    async def test_allocate_ports(self, workspace):
        ports = await model.find_and_allocate_ports(
            workspace["id"], 3, container.PORT_RANGE_START
        )
        assert len(ports) == 3
        assert all(p >= container.PORT_RANGE_START for p in ports)

    async def test_allocate_ports_avoids_used(self, workspace, user):
        # Allocate some ports for workspace 1
        ports1 = await model.find_and_allocate_ports(
            workspace["id"], 3, container.PORT_RANGE_START
        )
        # Create second workspace and allocate
        ws2 = await model.create_workspace(user["id"], "ws2")
        ports2 = await model.find_and_allocate_ports(
            ws2["id"], 3, container.PORT_RANGE_START
        )
        # No overlap
        assert set(ports1).isdisjoint(set(ports2))

    async def test_get_workspace_ports(self, workspace):
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 2, container.PORT_RANGE_START
        )
        retrieved = await container.registry.get_workspace_ports(
            workspace["id"]
        )
        assert retrieved == sorted(allocated)

    async def test_get_workspace_ports_empty(self, workspace):
        ports = await container.registry.get_workspace_ports(workspace["id"])
        assert ports == []


class TestDnsConfig:
    def test_no_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("KLANGK_DNS_SERVERS", raising=False)
        assert container._container_dns_config() == []

    def test_single_server(self, monkeypatch):
        monkeypatch.setenv("KLANGK_DNS_SERVERS", "100.100.100.100")
        assert container._container_dns_config() == ["100.100.100.100"]

    def test_multiple_servers(self, monkeypatch):
        monkeypatch.setenv("KLANGK_DNS_SERVERS", "100.100.100.100, 8.8.8.8")
        assert container._container_dns_config() == [
            "100.100.100.100",
            "8.8.8.8",
        ]

    def test_empty_string(self, monkeypatch):
        monkeypatch.setenv("KLANGK_DNS_SERVERS", "")
        assert container._container_dns_config() == []


class TestConstants:
    def test_port_range_start(self):
        assert container.PORT_RANGE_START == 9000

    def test_container_port_start(self):
        assert container.CONTAINER_PORT_START == 8000

    def test_default_ports_per_workspace(self):
        assert container.DEFAULT_PORTS_PER_WORKSPACE == 5


# --- Container lifecycle tests (mocked) ---


@contextmanager
def patch_podman(**overrides):
    """Patch the podman.* calls container.py makes.

    Yields a namespace of the AsyncMocks so tests can assert on them.
    Override any default by passing ``name=AsyncMock(...)``.
    """
    defaults = {
        "inspect_container": AsyncMock(return_value=None),
        "create_container": AsyncMock(return_value="new-cid"),
        "start_container": AsyncMock(),
        "remove_container": AsyncMock(),
        "list_containers": AsyncMock(return_value=[]),
        "inspect_volume": AsyncMock(return_value=None),
        "create_volume": AsyncMock(
            return_value={"Name": "v", "CreatedAt": ""}
        ),
    }
    mocks = {**defaults, **overrides}
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch.object(podman, name, mock))
        yield SimpleNamespace(**mocks)


def _running(value=True):
    """An inspect_container mock returning a container in the given state."""
    return AsyncMock(return_value={"State": {"Running": value}})


class TestStartContainer:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_create_new_container(self, workspace):
        with patch_podman() as p:
            cid, status = await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        assert cid == "new-cid"
        assert status == "created"
        p.start_container.assert_awaited_once_with("new-cid")
        assert workspace["id"] in container.registry.states

    async def test_container_id_persisted_before_start(self, workspace, user):
        # If `start` fails, the id created just before it must already be on
        # record so the next connect can inspect/recreate it rather than
        # orphaning a created-but-unrecorded container.
        with patch_podman(
            start_container=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await container.registry.start_container(
                    workspace["id"], "/tmp/ws", "/tmp/home"
                )
        ws = await model.get_workspace(workspace["id"], user["id"])
        assert ws["container_id"] == "new-cid"
        assert workspace["id"] in container.registry.states

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
            start_container=AsyncMock(side_effect=slow_start)
        ) as p:
            task = asyncio.create_task(
                container.registry.start_container(
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
        assert workspace["id"] in container.registry.states

    async def test_reuse_running_container(self, workspace):
        with patch_podman(inspect_container=_running(True)) as p:
            cid, status = await container.registry.start_container(
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
        with patch_podman(inspect_container=_running(False)) as p:
            cid, status = await container.registry.start_container(
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
        with patch_podman() as p:
            cid, status = await container.registry.start_container(
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
            await container.registry.start_container(
                workspace["id"], "/work", "/home", image="evil:latest"
            )

    async def test_llm_proxy_env_vars(self, workspace, monkeypatch):
        """Container gets proxy URL, not real API keys."""
        monkeypatch.setenv("KLANGK_LLM_MODEL", "gemma4:31b")
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")

        with patch_podman() as p:
            await container.registry.start_container(
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
        assert (
            env_dict["KLANGK_BRIDGE_URL"]
            == "http://host.containers.internal:8995"
        )
        # API keys should NOT be in the container env
        assert not any(e.startswith("KLANGK_LLM_API_KEY=") for e in env)
        assert not any(e.startswith("ANTHROPIC_API_KEY=") for e in env)
        # host.containers.internal must be resolvable
        assert "host.containers.internal:host-gateway" in kwargs["add_hosts"]

    async def test_workspace_token_injected(self, workspace):
        """Container env includes a valid KLANGK_WORKSPACE_TOKEN JWT."""
        from klangk_backend import auth

        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        kwargs = p.create_container.call_args.kwargs
        env_dict = dict(e.split("=", 1) for e in kwargs["env"])
        assert "KLANGK_WORKSPACE_TOKEN" in env_dict
        decoded_ws = auth.decode_workspace_token(
            env_dict["KLANGK_WORKSPACE_TOKEN"]
        )
        assert decoded_ws == workspace["id"]

    async def test_pull_policy_default_never(self, workspace):
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert p.create_container.call_args.kwargs["pull"] == "never"

    async def test_pull_policy_from_env(self, workspace, monkeypatch):
        monkeypatch.setenv("KLANGK_IMAGE_PULL_POLICY", "missing")
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert p.create_container.call_args.kwargs["pull"] == "missing"

    async def test_config_mount_added(self, workspace):
        """Container gets read-only config mount when config_path is set."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                config_path="/tmp/config",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/config:/opt/klangk/config:ro" in binds

    async def test_no_config_mount_without_config_path(self, workspace):
        """Container has no config mount when config_path is not set."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert not any("config" in b for b in binds)

    async def test_home_mounted_at_slash_home(self, workspace):
        """Home path is mounted at /home (not /home/klangk)."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        binds = p.create_container.call_args.kwargs["binds"]
        assert "/tmp/home:/home" in binds

    async def test_hosting_env_vars(self, workspace):
        with patch_podman() as p:
            await container.registry.start_container(
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

    async def test_port_allocation_on_create(self, workspace):
        with patch_podman():
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=3,
            )
        # Ports should have been allocated
        ports = await container.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_excess_ports_trimmed(self, workspace):
        # Pre-allocate more ports than needed
        await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
        )
        with patch_podman():
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=2,
            )
        ports = await container.registry.get_workspace_ports(workspace["id"])
        assert len(ports) == 2

    async def test_container_config_structure(self, workspace):
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        args, kwargs = p.create_container.call_args
        assert args[1] == container.IMAGE_NAME
        assert kwargs["labels"]["klangk.managed"] == "true"
        assert kwargs["labels"]["klangk.workspace-id"] == workspace["id"]
        assert kwargs["init"] is True
        assert kwargs["interactive"] is True

    async def test_create_container_with_extra_env(self, workspace):
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_env={"KLANGK_SKILLS": "stats,rdkit", "FOO": "bar"},
            )
        env_list = p.create_container.call_args.kwargs["env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["KLANGK_SKILLS"] == "stats,rdkit"
        assert env_dict["FOO"] == "bar"


class TestStartContainerPortConflict:
    """Test retry logic when a stale container holds a port."""

    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_port_conflict_removes_stale_and_retries(self, workspace):
        # Pre-allocate ports so we know exactly which ones the workspace gets.
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
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
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "stale-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(return_value=stale_info),
        ) as p:
            cid, status = await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        assert status == "created"
        assert len(start_calls) == 2
        remove_calls = [c.args[0] for c in p.remove_container.call_args_list]
        assert "stale-cid" in remove_calls

    async def test_port_conflict_skips_own_container(self, workspace):
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
        )
        conflict_port = allocated[0]

        start_calls = []

        async def start_side_effect(cid):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
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
            cid, _ = await container.registry.start_container(
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
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        # other-cid doesn't hold our ports — should not be removed

    async def test_port_conflict_stale_vanished(self, workspace):
        """Stale container gone by the time we inspect it."""
        await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
        )
        start_calls = []

        async def start_side_effect(cid):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
            start_container=AsyncMock(side_effect=start_side_effect),
            list_containers=AsyncMock(
                return_value=[{"Id": "gone-cid", "Labels": {}}]
            ),
            inspect_container=AsyncMock(return_value=None),
        ) as p:
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )
        # gone-cid vanished — no remove attempted
        assert not any(
            c.args[0] == "gone-cid" for c in p.remove_container.call_args_list
        )

    async def test_port_conflict_bad_port_bindings(self, workspace):
        """Malformed HostPort values don't crash the retry."""
        await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
        )
        start_calls = []

        async def start_side_effect(cid):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
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
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )

    async def test_port_conflict_remove_error_logged(self, workspace):
        """Error removing stale container is logged, not raised."""
        allocated = await model.find_and_allocate_ports(
            workspace["id"], 5, container.PORT_RANGE_START
        )
        conflict_port = allocated[0]
        start_calls = []

        async def start_side_effect(cid):
            start_calls.append(cid)
            if len(start_calls) == 1:
                raise podman.PodmanError(500, "port is already allocated")

        with patch_podman(
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
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )

    async def test_non_port_conflict_error_raised(self, workspace):
        with (
            patch_podman(
                start_container=AsyncMock(
                    side_effect=podman.PodmanError(500, "some other error")
                ),
            ),
            pytest.raises(podman.PodmanError, match="some other error"),
        ):
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )


class TestValidateMountSpec:
    def test_valid_bind_mount(self):
        assert container.validate_mount_spec("/host:/container") is None

    def test_valid_volume_mount(self):
        assert container.validate_mount_spec("vol-name:/data") is None

    def test_valid_with_options(self):
        assert container.validate_mount_spec("/host:/container:ro") is None

    def test_valid_with_multiple_options(self):
        assert (
            container.validate_mount_spec("/host:/container:ro,nocopy") is None
        )

    def test_no_colon(self):
        err = container.validate_mount_spec("nocolon")
        assert err is not None
        assert "expected" in err.lower()

    def test_too_many_colons(self):
        err = container.validate_mount_spec("a:b:c:d")
        assert err is not None

    def test_empty_source(self):
        err = container.validate_mount_spec(":/container")
        assert err is not None
        assert "source is empty" in err.lower()

    def test_relative_container_path(self):
        err = container.validate_mount_spec("/host:relative")
        assert err is not None
        assert "absolute" in err.lower()

    def test_unknown_option(self):
        err = container.validate_mount_spec("/host:/container:bogus")
        assert err is not None
        assert "unknown option" in err.lower()

    def test_validate_mounts_list(self):
        assert container.validate_mounts(["/a:/b", "vol:/c"]) is None

    def test_validate_mounts_list_with_error(self):
        err = container.validate_mounts(["/a:/b", "bad"])
        assert err is not None


class TestAllowedMountRoots:
    def test_bind_mount_allowed(self, monkeypatch):
        monkeypatch.setattr(
            container, "ALLOWED_MOUNT_ROOTS", ["/home", "/data"]
        )
        assert container.validate_mount_spec("/home/user/src:/work") is None

    def test_bind_mount_exact_root(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/home"])
        assert container.validate_mount_spec("/home:/work") is None

    def test_bind_mount_denied(self, monkeypatch):
        monkeypatch.setattr(
            container, "ALLOWED_MOUNT_ROOTS", ["/home", "/data"]
        )
        err = container.validate_mount_spec("/etc/passwd:/etc/passwd:ro")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_bind_mount_traversal_denied(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/home"])
        err = container.validate_mount_spec("/home/../etc:/work")
        assert err is not None
        assert "allowed root" in err.lower()

    def test_named_volume_always_allowed(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/home"])
        assert container.validate_mount_spec("my-volume:/data") is None

    def test_no_restriction_when_empty(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", [])
        assert container.validate_mount_spec("/etc/shadow:/secrets") is None

    def test_multiple_roots(self, monkeypatch):
        monkeypatch.setattr(
            container, "ALLOWED_MOUNT_ROOTS", ["/home", "/data", "/opt"]
        )
        assert container.validate_mount_spec("/data/files:/work") is None
        assert container.validate_mount_spec("/opt/app:/app") is None
        err = container.validate_mount_spec("/var/log:/logs")
        assert err is not None


class TestProtectedPaths:
    def test_docker_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/"])
        err = container.validate_mount_spec(
            "/var/run/docker.sock:/var/run/docker.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_podman_socket_blocked(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/"])
        err = container.validate_mount_spec(
            "/run/podman/podman.sock:/run/podman/podman.sock"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_data_dir_blocked(self, monkeypatch):
        monkeypatch.setattr(container, "ALLOWED_MOUNT_ROOTS", ["/"])
        monkeypatch.setenv("KLANGK_DATA_DIR", "/srv/klangk/data")
        err = container.validate_mount_spec(
            "/srv/klangk/data/workspaces:/loot"
        )
        assert err is not None
        assert "protected" in err.lower()

    def test_protected_blocked_even_without_allowlist(self):
        err = container.validate_mount_spec(
            "/var/run/docker.sock:/var/run/docker.sock"
        )
        assert err is not None
        assert "protected" in err.lower()


class TestExtraMountsVolumeCreation:
    async def test_auto_creates_named_volume(self, workspace):
        """Named volumes (no leading /) are auto-created with klangk labels."""
        # inspect_volume returns None (default) → volume is created.
        with patch_podman() as p:
            await container.registry.start_container(
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
        assert labels["klangk.instance"] == container.INSTANCE_ID
        assert labels["klangk.user-id"] == "user-123"

    async def test_existing_volume_not_recreated(self, workspace):
        """Existing volumes owned by this instance and user are used as-is."""
        with patch_podman(
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "existing",
                    "Labels": {
                        "klangk.instance": container.INSTANCE_ID,
                        "klangk.user-id": "user-123",
                    },
                }
            )
        ) as p:
            await container.registry.start_container(
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
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "stolen",
                    "Labels": {"klangk.instance": "someone-else"},
                }
            )
        ):
            with pytest.raises(ValueError, match="not managed by this"):
                await container.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["stolen:/data"],
                )

    async def test_unlabelled_volume_rejected(self, workspace):
        """A named volume with no klangk labels is refused."""
        with patch_podman(
            inspect_volume=AsyncMock(return_value={"Name": "bare"})
        ):
            with pytest.raises(ValueError, match="not managed by this"):
                await container.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["bare:/data"],
                )

    async def test_cross_user_volume_rejected(self, workspace):
        """A volume owned by another user is refused."""
        with patch_podman(
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "private",
                    "Labels": {
                        "klangk.instance": container.INSTANCE_ID,
                        "klangk.user-id": "user-other",
                    },
                }
            )
        ):
            with pytest.raises(ValueError, match="belongs to another user"):
                await container.registry.start_container(
                    workspace["id"],
                    "/tmp/ws",
                    "/tmp/home",
                    extra_mounts=["private:/data"],
                    user_id="user-me",
                )

    async def test_volume_without_user_label_allowed(self, workspace):
        """A volume with no user-id label (pre-existing) is allowed."""
        with patch_podman(
            inspect_volume=AsyncMock(
                return_value={
                    "Name": "legacy",
                    "Labels": {
                        "klangk.instance": container.INSTANCE_ID,
                    },
                }
            )
        ) as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["legacy:/data"],
                user_id="user-123",
            )
        p.create_volume.assert_not_awaited()

    async def test_bind_mount_not_treated_as_volume(self, workspace):
        """Bind mounts (starting with /) are not treated as volumes."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/home/me/src:/work/src"],
            )
        p.inspect_volume.assert_not_awaited()

    async def test_mount_with_multiple_colons(self, workspace):
        """Mount spec with options (host:container:ro) — source starts with /."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/data/shared:/mnt/data:ro"],
            )
        # Bind mount, not a volume — inspect_volume should not be called
        p.inspect_volume.assert_not_awaited()

    async def test_volume_mount_with_options(self, workspace):
        """Named volume with options (vol:container:ro) — auto-creates."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["my-vol:/data:ro"],
            )
        p.create_volume.assert_awaited_once()

    async def test_mount_source_with_slash_is_bind(self, workspace):
        """A mount source containing slashes is a bind mount, not a volume."""
        with patch_podman() as p:
            await container.registry.start_container(
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
                create_volume=AsyncMock(
                    side_effect=podman.PodmanError(500, "internal error")
                )
            ),
            pytest.raises(podman.PodmanError),
        ):
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["bad-vol:/data"],
            )

    async def test_mount_source_with_special_characters(self, workspace):
        """Mount source with special/binary-like chars is a bind mount."""
        with patch_podman() as p:
            await container.registry.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                extra_mounts=["/path/with spaces\x00and\x01binary:/work/bad"],
            )
        # Has leading /, so treated as bind mount
        p.inspect_volume.assert_not_awaited()

    async def test_bridge_token_revoked_on_creation_failure(self, workspace):
        """If container creation fails, the error propagates cleanly."""
        with (
            patch_podman(
                create_container=AsyncMock(
                    side_effect=RuntimeError("podman broke")
                )
            ),
            pytest.raises(RuntimeError, match="podman broke"),
        ):
            await container.registry.start_container(
                workspace["id"], "/tmp/ws", "/tmp/home"
            )

        # No bridge tokens should remain for this workspace
        for token, (ws_id, _sock) in container.registry._bridge_tokens.items():
            assert ws_id != workspace["id"]


class TestStopContainer:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_stop_running(self):
        container.registry.track_activity("cid", "ws")

        with patch_podman() as p:
            await container.registry.stop_and_remove_container("cid")
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in container.registry.states

    async def test_stop_podman_error(self):
        container.registry.track_activity("cid", "ws")

        with patch_podman(
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(404, "gone")
            )
        ):
            await container.registry.stop_and_remove_container("cid")
        # Should still remove from tracking
        assert "ws" not in container.registry.states


class TestRemoveContainer:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_remove(self):
        container.registry.track_activity("cid", "ws")

        with patch_podman() as p:
            await container.registry.stop_and_remove_container("cid")
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in container.registry.states

    async def test_remove_podman_error(self):
        container.registry.track_activity("cid", "ws")

        with patch_podman(
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(404, "gone")
            )
        ):
            await container.registry.stop_and_remove_container("cid")
        assert "ws" not in container.registry.states


class TestStopUserContainers:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_stop_user_containers(self, user, workspace):
        # Set container_id on the workspace
        await model.update_workspace_container(workspace["id"], "cid")
        container.registry.track_activity("cid", workspace["id"])

        with patch_podman() as p:
            await container.registry.stop_user_containers(user["id"])
        p.remove_container.assert_awaited_once_with("cid")
        assert workspace["id"] not in container.registry.states

    async def test_stop_user_calls_workspace_killed(self, user, workspace):
        await model.update_workspace_container(workspace["id"], "cid")
        container.registry.track_activity("cid", workspace["id"])

        killed_cb = AsyncMock()
        old_cb = container.registry.on_workspace_killed
        container.registry.on_workspace_killed = killed_cb

        with patch_podman():
            await container.registry.stop_user_containers(user["id"])

        killed_cb.assert_awaited_once_with(workspace["id"])
        container.registry.on_workspace_killed = old_cb

    async def test_stop_user_no_containers(self, user):
        with patch_podman() as p:
            await container.registry.stop_user_containers(user["id"])
        p.remove_container.assert_not_awaited()


class TestShutdown:
    def setup_method(self):
        container.registry.states.clear()
        container.registry._cid_to_wsid.clear()
        container.registry.cleanup_task = None

    def teardown_method(self):
        container.registry.states.clear()
        container.registry._cid_to_wsid.clear()
        container.registry.cleanup_task = None

    async def test_shutdown_skips_in_container(self):
        """When running inside a container, shutdown skips cleanup."""
        container.registry.track_activity("cid", "ws")
        with patch("os.path.exists", return_value=True):
            await container.registry.shutdown()
        # Container should still be tracked (not cleaned up)
        assert "ws" in container.registry.states

    async def test_shutdown_stops_tracked(self):
        # list_containers returns the tracked cid; it should be skipped in
        # the orphan loop (already tracked) but still removed via tracking.
        container.registry.track_activity("cid", "ws")

        with patch_podman(
            list_containers=AsyncMock(return_value=[{"Id": "cid"}])
        ) as p:
            await container.registry.shutdown()
        p.remove_container.assert_awaited_once_with("cid")
        assert "ws" not in container.registry.states

    async def test_shutdown_stops_orphans(self):
        with patch_podman(
            list_containers=AsyncMock(return_value=[{"Id": "orphan-cid"}])
        ) as p:
            await container.registry.shutdown()
        p.remove_container.assert_awaited_once_with("orphan-cid")

    async def test_shutdown_cancels_cleanup_task(self):
        # Create a real cancellable task so shutdown can await it.
        async def fake_cleanup():
            await asyncio.sleep(999)

        task = asyncio.create_task(fake_cleanup())
        container.registry.cleanup_task = task

        with patch_podman():
            await container.registry.shutdown()
        assert task.cancelled()
        assert container.registry.cleanup_task is None

    async def test_shutdown_handles_podman_error(self):
        with patch_podman(
            list_containers=AsyncMock(
                side_effect=OSError("podman connection refused")
            )
        ):
            await container.registry.shutdown()
        # Should not raise

    async def test_shutdown_no_podman(self):
        with patch_podman():
            await container.registry.shutdown()
        assert container.registry.cleanup_task is None

    async def test_shutdown_orphan_remove_error(self):
        """Orphan container that errors on removal is handled gracefully."""
        with patch_podman(
            list_containers=AsyncMock(return_value=[{"Id": "orphan-cid"}]),
            remove_container=AsyncMock(
                side_effect=podman.PodmanError(500, "remove failed")
            ),
        ) as p:
            await container.registry.shutdown()
        # Attempted removal and did not raise
        p.remove_container.assert_awaited_once_with("orphan-cid")


class TestCleanupIdleContainers:
    def setup_method(self):
        container.registry.states.clear()
        container.registry._cleanup_wake = None

    def teardown_method(self):
        container.registry.states.clear()
        container.registry._cleanup_wake = None

    async def test_idle_container_stopped(self):
        # Set activity far in the past
        container.registry.track_activity("cid", "ws-1")
        container.registry.states["ws-1"].last_activity = (
            time.time() - container.IDLE_TIMEOUT_SECONDS - 100
        )

        with patch_podman() as p:
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            # Let the task enter the Event wait, then wake it
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        p.remove_container.assert_awaited()
        assert "ws-1" not in container.registry.states

    async def test_idle_calls_workspace_killed_callback(self):
        container.registry.track_activity("cid", "ws-killed")
        container.registry.states["ws-killed"].last_activity = (
            time.time() - container.IDLE_TIMEOUT_SECONDS - 100
        )

        killed_cb = AsyncMock()
        old_cb = container.registry.on_workspace_killed
        container.registry.on_workspace_killed = killed_cb

        with patch_podman():
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        killed_cb.assert_awaited_once_with("ws-killed")
        container.registry.on_workspace_killed = old_cb

    async def test_idle_workspace_killed_callback_error(self):
        container.registry.track_activity("cid", "ws-err")
        container.registry.states["ws-err"].last_activity = (
            time.time() - container.IDLE_TIMEOUT_SECONDS - 100
        )

        killed_cb = AsyncMock(side_effect=RuntimeError("boom"))
        old_cb = container.registry.on_workspace_killed
        container.registry.on_workspace_killed = killed_cb

        with patch_podman():
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should not raise — error is logged
        killed_cb.assert_awaited_once()
        container.registry.on_workspace_killed = old_cb

    async def test_active_container_not_stopped(self):
        container.registry.track_activity("cid", "ws-1")

        with patch_podman():
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Container should still be tracked
        assert "ws-1" in container.registry.states

    async def test_idle_callback_invoked(self):
        container.registry.track_activity("cid", "ws-1")
        container.registry.states["ws-1"].last_activity = (
            time.time() - container.IDLE_TIMEOUT_SECONDS - 100
        )

        callback_called = []

        async def on_idle(ws_id):
            callback_called.append(ws_id)

        container.registry.on_idle_stop("ws-1", on_idle)

        with patch_podman():
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert callback_called == ["ws-1"]

    async def test_idle_callback_error_handled(self):
        container.registry.track_activity("cid", "ws-1")
        container.registry.states["ws-1"].last_activity = (
            time.time() - container.IDLE_TIMEOUT_SECONDS - 100
        )

        async def bad_callback(ws_id):
            raise RuntimeError("callback broke")

        container.registry.on_idle_stop("ws-1", bad_callback)

        with patch_podman() as p:
            task = asyncio.create_task(
                container.registry.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container.registry.get_cleanup_wake().set()
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
        container.registry.track_activity("cid", "ws-fast")
        container.registry.states["ws-fast"].last_activity = time.time() - 100
        container.registry.states["ws-fast"].idle_timeout = 5

        try:
            with patch_podman() as p:
                # The Event-based wait will timeout after max(2, 5//2)=2s,
                # then check containers. We cancel after one iteration.
                task = asyncio.create_task(
                    container.registry.cleanup_idle_containers()
                )
                await asyncio.sleep(0.1)  # Let it start
                # Wake it immediately via the event
                container.registry.get_cleanup_wake().set()
                await asyncio.sleep(0.1)  # Let it process
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            p.remove_container.assert_awaited()
        finally:
            container.registry.states.clear()

    async def test_per_workspace_timeout_event_timeout(self):
        """Event-based wait times out when no wake signal is sent."""
        container.registry.track_activity("cid", "ws-fast")
        container.registry.states["ws-fast"].last_activity = time.time() - 100
        container.registry.states["ws-fast"].idle_timeout = 4

        try:
            with patch_podman() as p:
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
                        await container.registry.cleanup_idle_containers()
                    except asyncio.CancelledError:
                        pass
            p.remove_container.assert_awaited()
        finally:
            container.registry.states.clear()


class TestStartCleanupLoop:
    def setup_method(self):
        container.registry.cleanup_task = None

    def teardown_method(self):
        if container.registry.cleanup_task:
            container.registry.cleanup_task.cancel()
            container.registry.cleanup_task = None

    async def test_start_creates_task(self):
        container.registry.start_cleanup_loop()
        assert container.registry.cleanup_task is not None
        container.registry.cleanup_task.cancel()

    async def test_start_idempotent(self):
        container.registry.start_cleanup_loop()
        task1 = container.registry.cleanup_task
        container.registry.start_cleanup_loop()
        assert container.registry.cleanup_task is task1
        container.registry.cleanup_task.cancel()


class TestPrewarmPodman:
    async def test_prewarm_creates_and_removes(self):
        with patch_podman() as p:
            await container.registry.prewarm_podman()
        p.create_container.assert_awaited_once()
        p.remove_container.assert_awaited_once_with("new-cid")

    async def test_prewarm_handles_error(self):
        with patch_podman(
            create_container=AsyncMock(
                side_effect=podman.PodmanError(500, "boom")
            )
        ):
            await container.registry.prewarm_podman()
        # Should not raise


class TestAdoptOrphanedContainers:
    def setup_method(self):
        container.registry.states.clear()

    def teardown_method(self):
        container.registry.states.clear()

    async def test_adopts_running_containers(self):
        with patch_podman(
            list_containers=AsyncMock(
                return_value=[
                    {
                        "Id": "orphan-123",
                        "Labels": {"klangk.workspace-id": "ws-orphan"},
                    }
                ]
            )
        ):
            await container.registry.adopt_orphaned_containers()
        assert "ws-orphan" in container.registry.states
        assert (
            container.registry.states["ws-orphan"].container_id == "orphan-123"
        )

    async def test_adopts_orphan_without_labels(self):
        # A container with no labels gets the "unknown" workspace id.
        with patch_podman(
            list_containers=AsyncMock(
                return_value=[{"Id": "orphan-x", "Labels": None}]
            )
        ):
            await container.registry.adopt_orphaned_containers()
        assert container.registry.states["unknown"].container_id == "orphan-x"

    async def test_skips_already_tracked(self):
        container.registry.track_activity("tracked-456", "ws-tracked")
        with patch_podman(
            list_containers=AsyncMock(return_value=[{"Id": "tracked-456"}])
        ):
            await container.registry.adopt_orphaned_containers()
        # Already tracked → not re-adopted, no "unknown" entry created.
        assert (
            container.registry.states["ws-tracked"].container_id
            == "tracked-456"
        )
        assert "unknown" not in container.registry.states

    async def test_podman_error_handled(self):
        with patch_podman(
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "fail")
            )
        ):
            await container.registry.adopt_orphaned_containers()
        # Should not raise


class TestBridgeTokens:
    def setup_method(self):
        container.registry._bridge_tokens.clear()

    def teardown_method(self):
        container.registry._bridge_tokens.clear()

    def test_create_and_resolve(self):
        sock = object()
        token = container.registry.create_bridge_token("ws-1", sock)
        result = container.registry.resolve_bridge_token(token)
        assert result == ("ws-1", sock)

    def test_resolve_unknown_token(self):
        assert container.registry.resolve_bridge_token("nonexistent") is None

    def test_revoke_bridge_token_removes_all(self):
        sock1 = object()
        sock2 = object()
        t1 = container.registry.create_bridge_token("ws-1", sock1)
        t2 = container.registry.create_bridge_token("ws-1", sock2)
        container.registry.revoke_bridge_token("ws-1")
        assert container.registry.resolve_bridge_token(t1) is None
        assert container.registry.resolve_bridge_token(t2) is None

    def test_revoke_connection_token(self):
        sock1 = object()
        sock2 = object()
        t1 = container.registry.create_bridge_token("ws-1", sock1)
        t2 = container.registry.create_bridge_token("ws-1", sock2)
        container.registry.revoke_connection_token(sock1)
        assert container.registry.resolve_bridge_token(t1) is None
        assert container.registry.resolve_bridge_token(t2) == ("ws-1", sock2)

    def test_revoke_connection_token_no_match(self):
        sock = object()
        other_sock = object()
        token = container.registry.create_bridge_token("ws-1", sock)
        container.registry.revoke_connection_token(other_sock)
        assert container.registry.resolve_bridge_token(token) == ("ws-1", sock)

    def test_multiple_connections_same_workspace(self):
        sock1 = object()
        sock2 = object()
        t1 = container.registry.create_bridge_token("ws-1", sock1)
        t2 = container.registry.create_bridge_token("ws-1", sock2)
        assert container.registry.resolve_bridge_token(t1) == ("ws-1", sock1)
        assert container.registry.resolve_bridge_token(t2) == ("ws-1", sock2)
