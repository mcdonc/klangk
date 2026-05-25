"""Tests for container_manager: idle timeout parsing, activity tracking, callbacks, port allocation."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions

from bark_backend import container_manager, user_store


class TestParseIdleTimeout:
    def test_default_values(self, monkeypatch):
        monkeypatch.delenv("BARK_IDLE_TIMEOUT_SECONDS", raising=False)
        timeout, interval = container_manager.parse_idle_timeout()
        assert timeout == 30 * 60
        assert interval == max(10, min(60, timeout // 3))

    def test_custom_value(self, monkeypatch):
        monkeypatch.setenv("BARK_IDLE_TIMEOUT_SECONDS", "120")
        timeout, interval = container_manager.parse_idle_timeout()
        assert timeout == 120
        assert interval == max(10, min(60, 120 // 3))

    def test_invalid_value_uses_default(self, monkeypatch):
        monkeypatch.setenv("BARK_IDLE_TIMEOUT_SECONDS", "not_a_number")
        timeout, interval = container_manager.parse_idle_timeout()
        assert timeout == 30 * 60

    def test_small_value_clamps_interval(self, monkeypatch):
        monkeypatch.setenv("BARK_IDLE_TIMEOUT_SECONDS", "15")
        timeout, interval = container_manager.parse_idle_timeout()
        assert timeout == 15
        assert interval == 10  # clamped to min 10

    def test_large_value_clamps_interval(self, monkeypatch):
        monkeypatch.setenv("BARK_IDLE_TIMEOUT_SECONDS", "3600")
        timeout, interval = container_manager.parse_idle_timeout()
        assert timeout == 3600
        assert interval == 60  # clamped to max 60


class TestActivityTracking:
    def setup_method(self):
        container_manager._containers.clear()

    def teardown_method(self):
        container_manager._containers.clear()

    def testtrack_activity(self):
        container_manager.track_activity("cid-1", "ws-1")
        assert "cid-1" in container_manager._containers
        info = container_manager._containers["cid-1"]
        assert info["workspace_id"] == "ws-1"
        assert info["last_activity"] <= time.time()

    def test_record_activity_updates_time(self):
        container_manager.track_activity("cid-1", "ws-1")
        old_time = container_manager._containers["cid-1"]["last_activity"]
        time.sleep(0.01)
        container_manager.record_activity("cid-1")
        new_time = container_manager._containers["cid-1"]["last_activity"]
        assert new_time > old_time

    def test_record_activity_unknown_container(self):
        # Should not raise
        container_manager.record_activity("nonexistent")
        assert "nonexistent" not in container_manager._containers

    def testtrack_activity_overwrites(self):
        container_manager.track_activity("cid-1", "ws-1")
        container_manager.track_activity("cid-1", "ws-2")
        assert container_manager._containers["cid-1"]["workspace_id"] == "ws-2"


def _noop_callback(ws):
    pass


class TestIdleCallbacks:
    def setup_method(self):
        container_manager._idle_callbacks.clear()

    def teardown_method(self):
        container_manager._idle_callbacks.clear()

    def test_on_idle_stop_registers(self):
        container_manager.on_idle_stop("ws-1", _noop_callback)
        assert _noop_callback in container_manager._idle_callbacks["ws-1"]

    def test_multiple_callbacks(self):
        def cb2(ws):
            pass

        container_manager.on_idle_stop("ws-1", _noop_callback)
        container_manager.on_idle_stop("ws-1", cb2)
        assert len(container_manager._idle_callbacks["ws-1"]) == 2

    def test_remove_idle_callback(self):
        container_manager.on_idle_stop("ws-1", _noop_callback)
        container_manager.remove_idle_callback("ws-1", _noop_callback)
        assert _noop_callback not in container_manager._idle_callbacks["ws-1"]

    def test_remove_idle_callback_not_registered(self):
        container_manager.remove_idle_callback("ws-1", _noop_callback)
        assert _noop_callback not in container_manager._idle_callbacks.get(
            "ws-1", []
        )

    def test_remove_idle_callback_unknown_workspace(self):
        container_manager.remove_idle_callback("nonexistent", _noop_callback)
        assert "nonexistent" not in container_manager._idle_callbacks

    def test_callbacks_per_workspace(self):
        def cb2(ws):
            pass

        container_manager.on_idle_stop("ws-1", _noop_callback)
        container_manager.on_idle_stop("ws-2", cb2)
        assert _noop_callback in container_manager._idle_callbacks["ws-1"]
        assert cb2 in container_manager._idle_callbacks["ws-2"]
        assert _noop_callback not in container_manager._idle_callbacks.get(
            "ws-2", []
        )


class TestPortAllocation:
    async def test_allocate_ports(self, workspace):
        ports = await container_manager.allocate_ports(workspace["id"], 3)
        assert len(ports) == 3
        assert all(p >= container_manager.PORT_RANGE_START for p in ports)

    async def test_allocate_ports_avoids_used(self, workspace, user):
        # Allocate some ports for workspace 1
        ports1 = await container_manager.allocate_ports(workspace["id"], 3)
        # Create second workspace and allocate
        ws2 = await user_store.create_workspace(user["id"], "ws2")
        ports2 = await container_manager.allocate_ports(ws2["id"], 3)
        # No overlap
        assert set(ports1).isdisjoint(set(ports2))

    async def test_get_workspace_ports(self, workspace):
        allocated = await container_manager.allocate_ports(workspace["id"], 2)
        retrieved = await container_manager.get_workspace_ports(
            workspace["id"]
        )
        assert retrieved == sorted(allocated)

    async def test_get_workspace_ports_empty(self, workspace):
        ports = await container_manager.get_workspace_ports(workspace["id"])
        assert ports == []


class TestConstants:
    def test_port_range_start(self):
        assert container_manager.PORT_RANGE_START == 9000

    def test_container_port_start(self):
        assert container_manager.CONTAINER_PORT_START == 8000

    def test_default_ports_per_workspace(self):
        assert container_manager.DEFAULT_PORTS_PER_WORKSPACE == 5


# --- Docker-dependent tests (mocked) ---


def _mock_docker():
    """Create a mock Docker client with containers namespace."""
    docker = AsyncMock()
    docker.containers = AsyncMock()
    docker.close = AsyncMock()
    return docker


def _mock_container(container_id="fake-cid", running=True):
    """Create a mock container object."""
    c = AsyncMock()
    c.id = container_id
    c.show = AsyncMock(return_value={"State": {"Running": running}})
    c.start = AsyncMock()
    c.stop = AsyncMock()
    c.delete = AsyncMock()
    c.attach = MagicMock(return_value=AsyncMock())
    return c


class TestStartContainer:
    def setup_method(self):
        container_manager._containers.clear()

    def teardown_method(self):
        container_manager._containers.clear()

    async def test_create_new_container(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            cid, status = await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        assert cid == "new-cid"
        assert status == "created"
        mock_c.start.assert_awaited_once()
        assert "new-cid" in container_manager._containers

    async def test_create_container_with_logfire(self, workspace, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.delenv("LOGFIRE_BASE_URL", raising=False)
        mock_docker = _mock_docker()
        mock_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env_list = call_kwargs[1]["config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "https://logfire-api.pydantic.dev"
        )
        assert (
            "Authorization=Bearer test-token"
            in (env_dict["OTEL_EXPORTER_OTLP_HEADERS"])
        )
        assert env_dict["OTEL_SERVICE_NAME"] == "bark-pi-agent"

    async def test_create_container_logfire_custom_base_url(
        self, workspace, monkeypatch
    ):
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://custom.logfire.dev")
        mock_docker = _mock_docker()
        mock_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env_list = call_kwargs[1]["config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert env_dict["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
            "https://custom.logfire.dev"
        )

    async def test_create_container_logfire_environment(
        self, workspace, monkeypatch
    ):
        monkeypatch.setenv("LOGFIRE_TOKEN", "tok")
        monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
        monkeypatch.delenv("LOGFIRE_BASE_URL", raising=False)
        mock_docker = _mock_docker()
        mock_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env_list = call_kwargs[1]["config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env_list)
        assert (
            env_dict["OTEL_RESOURCE_ATTRIBUTES"]
            == "deployment.environment=staging"
        )

    async def test_reuse_running_container(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("existing-cid", running=True)
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            cid, status = await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="existing-cid",
            )
        assert cid == "existing-cid"
        assert status == "connected"
        mock_c.start.assert_not_awaited()

    async def test_recreate_stopped_container(self, workspace):
        mock_docker = _mock_docker()
        stopped_c = _mock_container("old-cid", running=False)
        mock_docker.containers.get = AsyncMock(return_value=stopped_c)
        new_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=new_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            cid, status = await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="old-cid",
            )
        assert cid == "new-cid"
        assert status == "created"
        stopped_c.delete.assert_awaited_once_with(force=True)

    async def test_missing_container_creates_new(self, workspace):
        mock_docker = _mock_docker()
        mock_docker.containers.get = AsyncMock(
            side_effect=aiodocker.exceptions.DockerError(404, "not found")
        )
        new_c = _mock_container("new-cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=new_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            cid, status = await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                existing_container_id="gone-cid",
            )
        assert cid == "new-cid"
        assert status == "created"

    async def test_resume_session_env_var(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                resume_session="/path/to/session.jsonl",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env = call_kwargs[1]["config"]["Env"]
        assert any(
            "BARK_RESUME_SESSION=/path/to/session.jsonl" in e for e in env
        )

    async def test_llm_proxy_env_vars(self, workspace, monkeypatch):
        """Container gets proxy URL, not real API keys."""
        monkeypatch.setenv("LLM_MODEL", "gemma4:31b")
        monkeypatch.setenv("BARK_NGINX_PORT", "8995")
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env = call_kwargs[1]["config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env)
        assert env_dict["LLM_PROXY_URL"] == (
            "http://host.docker.internal:8995/llm-proxy"
        )
        assert env_dict["LLM_MODEL"] == "gemma4:31b"
        # API keys should NOT be in the container env
        assert not any(e.startswith("LLM_API_KEY=") for e in env)
        assert not any(e.startswith("ANTHROPIC_API_KEY=") for e in env)
        # host.docker.internal must be resolvable
        host_config = call_kwargs[1]["config"]["HostConfig"]
        assert "host.docker.internal:host-gateway" in host_config["ExtraHosts"]

    async def test_hosting_env_vars(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                hosting_hostname="example.com",
                hosting_proto="https",
                hosting_base_path="/bark",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        env = call_kwargs[1]["config"]["Env"]
        assert "BARK_HOSTING_HOSTNAME=example.com" in env
        assert "BARK_HOSTING_PROTO=https" in env
        assert "BARK_HOSTING_BASE_PATH=/bark" in env

    async def test_port_allocation_on_create(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=3,
            )
        # Ports should have been allocated
        ports = await container_manager.get_workspace_ports(workspace["id"])
        assert len(ports) == 3

    async def test_excess_ports_trimmed(self, workspace):
        # Pre-allocate more ports than needed
        await container_manager.allocate_ports(workspace["id"], 5)
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
                num_ports=2,
            )
        ports = await container_manager.get_workspace_ports(workspace["id"])
        assert len(ports) == 2

    async def test_container_config_structure(self, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.create_or_replace = AsyncMock(
            return_value=mock_c
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.start_container(
                workspace["id"],
                "/tmp/ws",
                "/tmp/home",
            )
        call_kwargs = mock_docker.containers.create_or_replace.call_args
        config = call_kwargs[1]["config"]
        assert config["Image"] == container_manager.IMAGE_NAME
        assert config["Labels"]["bark.managed"] == "true"
        assert config["Labels"]["bark.workspace-id"] == workspace["id"]
        assert config["HostConfig"]["ReadonlyRootfs"] is True
        assert config["HostConfig"]["Init"] is True
        assert config["OpenStdin"] is True


class TestAttachContainer:
    async def test_attach(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            stream = await container_manager.attach_container("cid")
        mock_c.attach.assert_called_once_with(
            stdin=True, stdout=True, stderr=True, stream=True
        )
        assert stream is not None


class TestStopContainer:
    def setup_method(self):
        container_manager._containers.clear()

    def teardown_method(self):
        container_manager._containers.clear()

    async def test_stop_running(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_and_remove_container("cid")
        mock_c.stop.assert_awaited_once()
        assert "cid" not in container_manager._containers

    async def test_stop_docker_error(self):
        mock_docker = _mock_docker()
        mock_docker.containers.get = AsyncMock(
            side_effect=aiodocker.exceptions.DockerError(404, "gone")
        )
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_and_remove_container("cid")
        # Should still remove from tracking
        assert "cid" not in container_manager._containers


class TestRemoveContainer:
    def setup_method(self):
        container_manager._containers.clear()

    def teardown_method(self):
        container_manager._containers.clear()

    async def test_remove(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_and_remove_container("cid")
        mock_c.stop.assert_awaited_once()
        mock_c.delete.assert_awaited_once_with(force=True)
        assert "cid" not in container_manager._containers

    async def test_remove_docker_error(self):
        mock_docker = _mock_docker()
        mock_docker.containers.get = AsyncMock(
            side_effect=aiodocker.exceptions.DockerError(404, "gone")
        )
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_and_remove_container("cid")
        assert "cid" not in container_manager._containers


class TestStopUserContainers:
    def setup_method(self):
        container_manager._containers.clear()

    def teardown_method(self):
        container_manager._containers.clear()

    async def test_stop_user_containers(self, user, workspace):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        # Set container_id on the workspace
        await user_store.update_workspace_container(workspace["id"], "cid")
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": workspace["id"],
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_user_containers(user["id"])
        mock_c.stop.assert_awaited_once()
        assert "cid" not in container_manager._containers

    async def test_stop_user_no_containers(self, user):
        mock_docker = _mock_docker()
        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.stop_user_containers(user["id"])
        mock_docker.containers.get.assert_not_awaited()


class TestShutdown:
    def setup_method(self):
        container_manager._containers.clear()
        container_manager._cleanup_task = None
        container_manager._docker = None

    def teardown_method(self):
        container_manager._containers.clear()
        container_manager._cleanup_task = None
        container_manager._docker = None

    async def test_shutdown_stops_tracked(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)
        mock_docker.containers.list = AsyncMock(return_value=[])
        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            container_manager._docker = mock_docker
            await container_manager.shutdown()
        mock_c.stop.assert_awaited()
        assert "cid" not in container_manager._containers
        mock_docker.close.assert_awaited_once()

    async def test_shutdown_stops_orphans(self):
        mock_docker = _mock_docker()
        orphan = _mock_container("orphan-cid")
        mock_docker.containers.list = AsyncMock(return_value=[orphan])
        mock_docker.containers.get = AsyncMock(
            return_value=_mock_container("x")
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            container_manager._docker = mock_docker
            await container_manager.shutdown()
        orphan.delete.assert_awaited_once()

    async def test_shutdown_cancels_cleanup_task(self):
        mock_docker = _mock_docker()
        mock_docker.containers.list = AsyncMock(return_value=[])
        mock_task = MagicMock()
        container_manager._cleanup_task = mock_task

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            container_manager._docker = mock_docker
            await container_manager.shutdown()
        mock_task.cancel.assert_called_once()
        assert container_manager._cleanup_task is None

    async def test_shutdown_handles_docker_error(self):
        mock_docker = _mock_docker()
        mock_docker.containers.list = AsyncMock(
            side_effect=OSError("Docker connection refused")
        )

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            container_manager._docker = mock_docker
            await container_manager.shutdown()
        # Should not raise
        mock_docker.close.assert_awaited_once()

    async def test_shutdown_no_docker(self):
        container_manager._docker = None
        mock_docker = _mock_docker()
        mock_docker.containers.list = AsyncMock(return_value=[])
        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.shutdown()
        assert container_manager._cleanup_task is None

    async def test_shutdown_orphan_delete_error(self):
        """Orphan container that raises on delete is handled gracefully."""
        mock_docker = _mock_docker()
        orphan = _mock_container("orphan-cid")
        orphan.delete = AsyncMock(
            side_effect=aiodocker.exceptions.DockerError(500, "delete failed")
        )
        mock_docker.containers.list = AsyncMock(return_value=[orphan])

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            container_manager._docker = mock_docker
            await container_manager.shutdown()
        # Should have attempted to delete and not raised
        orphan.delete.assert_awaited_once()
        mock_docker.close.assert_awaited_once()


class TestCleanupIdleContainers:
    def setup_method(self):
        container_manager._containers.clear()
        container_manager._idle_callbacks.clear()
        container_manager._cleanup_wake = None

    def teardown_method(self):
        container_manager._containers.clear()
        container_manager._idle_callbacks.clear()
        container_manager._cleanup_wake = None

    async def test_idle_container_stopped(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        # Set activity far in the past
        container_manager._containers["cid"] = {
            "last_activity": time.time()
            - container_manager.IDLE_TIMEOUT_SECONDS
            - 100,
            "workspace_id": "ws-1",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            # Let the task enter the Event wait, then wake it
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        mock_c.stop.assert_awaited()
        assert "cid" not in container_manager._containers

    async def test_idle_calls_workspace_killed_callback(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time()
            - container_manager.IDLE_TIMEOUT_SECONDS
            - 100,
            "workspace_id": "ws-killed",
        }

        killed_cb = AsyncMock()
        old_cb = container_manager._on_workspace_killed
        container_manager._on_workspace_killed = killed_cb

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        killed_cb.assert_awaited_once_with("ws-killed")
        container_manager._on_workspace_killed = old_cb

    async def test_idle_workspace_killed_callback_error(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time()
            - container_manager.IDLE_TIMEOUT_SECONDS
            - 100,
            "workspace_id": "ws-err",
        }

        killed_cb = AsyncMock(side_effect=RuntimeError("boom"))
        old_cb = container_manager._on_workspace_killed
        container_manager._on_workspace_killed = killed_cb

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should not raise — error is logged
        killed_cb.assert_awaited_once()
        container_manager._on_workspace_killed = old_cb

    async def test_active_container_not_stopped(self):
        mock_docker = _mock_docker()

        container_manager._containers["cid"] = {
            "last_activity": time.time(),
            "workspace_id": "ws-1",
        }

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Container should still be tracked
        assert "cid" in container_manager._containers

    async def test_idle_callback_invoked(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time()
            - container_manager.IDLE_TIMEOUT_SECONDS
            - 100,
            "workspace_id": "ws-1",
        }

        callback_called = []

        async def on_idle(ws_id):
            callback_called.append(ws_id)

        container_manager.on_idle_stop("ws-1", on_idle)

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert callback_called == ["ws-1"]

    async def test_idle_callback_error_handled(self):
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time()
            - container_manager.IDLE_TIMEOUT_SECONDS
            - 100,
            "workspace_id": "ws-1",
        }

        async def bad_callback(ws_id):
            raise RuntimeError("callback broke")

        container_manager.on_idle_stop("ws-1", bad_callback)

        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            task = asyncio.create_task(
                container_manager.cleanup_idle_containers()
            )
            await asyncio.sleep(0.05)
            container_manager.get_cleanup_wake().set()
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Container should still be stopped despite callback error
        mock_c.stop.assert_awaited()

    async def test_per_workspace_timeout_uses_event_wait(self):
        """When per-workspace timeouts exist, cleanup uses Event-based wait."""
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time() - 100,
            "workspace_id": "ws-fast",
        }
        container_manager._workspace_idle_timeouts["ws-fast"] = 5

        try:
            with patch.object(
                container_manager, "get_docker", return_value=mock_docker
            ):
                # The Event-based wait will timeout after max(2, 5//2)=2s,
                # then check containers. We cancel after one iteration.
                task = asyncio.create_task(
                    container_manager.cleanup_idle_containers()
                )
                await asyncio.sleep(0.1)  # Let it start
                # Wake it immediately via the event
                container_manager.get_cleanup_wake().set()
                await asyncio.sleep(0.1)  # Let it process
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            mock_c.stop.assert_awaited()
        finally:
            container_manager._workspace_idle_timeouts.clear()
            container_manager._containers.clear()

    async def test_per_workspace_timeout_event_timeout(self):
        """Event-based wait times out when no wake signal is sent."""
        mock_docker = _mock_docker()
        mock_c = _mock_container("cid")
        mock_docker.containers.get = AsyncMock(return_value=mock_c)

        container_manager._containers["cid"] = {
            "last_activity": time.time() - 100,
            "workspace_id": "ws-fast",
        }
        container_manager._workspace_idle_timeouts["ws-fast"] = 4

        try:
            with patch.object(
                container_manager, "get_docker", return_value=mock_docker
            ):
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
                        await container_manager.cleanup_idle_containers()
                    except asyncio.CancelledError:
                        pass
            mock_c.stop.assert_awaited()
        finally:
            container_manager._workspace_idle_timeouts.clear()
            container_manager._containers.clear()


class TestStartCleanupLoop:
    def setup_method(self):
        container_manager._cleanup_task = None

    def teardown_method(self):
        if container_manager._cleanup_task:
            container_manager._cleanup_task.cancel()
            container_manager._cleanup_task = None

    async def test_start_creates_task(self):
        container_manager.start_cleanup_loop()
        assert container_manager._cleanup_task is not None
        container_manager._cleanup_task.cancel()

    async def test_start_idempotent(self):
        container_manager.start_cleanup_loop()
        task1 = container_manager._cleanup_task
        container_manager.start_cleanup_loop()
        assert container_manager._cleanup_task is task1
        container_manager._cleanup_task.cancel()


class TestConnectionRefcount:
    def test_add_connection(self):
        ws_id = "refcount-test-1"
        container_manager._workspace_connections.pop(ws_id, None)
        assert container_manager.add_connection(ws_id) == 1
        assert container_manager.add_connection(ws_id) == 2
        assert container_manager.connection_count(ws_id) == 2
        container_manager._workspace_connections.pop(ws_id, None)

    def test_remove_connection_to_zero(self):
        ws_id = "refcount-test-2"
        container_manager._workspace_connections.pop(ws_id, None)
        container_manager.add_connection(ws_id)
        assert container_manager.remove_connection(ws_id) == 0
        assert container_manager.connection_count(ws_id) == 0

    def test_remove_connection_decrement(self):
        ws_id = "refcount-test-3"
        container_manager._workspace_connections.pop(ws_id, None)
        container_manager.add_connection(ws_id)
        container_manager.add_connection(ws_id)
        assert container_manager.remove_connection(ws_id) == 1
        assert container_manager.connection_count(ws_id) == 1
        container_manager._workspace_connections.pop(ws_id, None)

    def test_remove_connection_already_zero(self):
        ws_id = "refcount-test-4"
        container_manager._workspace_connections.pop(ws_id, None)
        assert container_manager.remove_connection(ws_id) == 0

    def test_connection_count_unknown(self):
        assert container_manager.connection_count("nonexistent") == 0


class TestAdoptOrphanedContainers:
    async def test_adopts_running_containers(self):
        mock_container = MagicMock()
        mock_container.id = "orphan-123"
        mock_container.show = AsyncMock(
            return_value={
                "Config": {"Labels": {"bark.workspace-id": "ws-orphan"}}
            }
        )
        mock_docker = AsyncMock()
        mock_docker.containers.list = AsyncMock(return_value=[mock_container])
        container_manager._containers.pop("orphan-123", None)
        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.adopt_orphaned_containers()
        assert "orphan-123" in container_manager._containers
        assert (
            container_manager._containers["orphan-123"]["workspace_id"]
            == "ws-orphan"
        )
        container_manager._containers.pop("orphan-123", None)

    async def test_skips_already_tracked(self):
        container_manager._containers["tracked-456"] = {
            "last_activity": 0,
            "workspace_id": "ws-tracked",
        }
        mock_container = MagicMock()
        mock_container.id = "tracked-456"
        mock_docker = AsyncMock()
        mock_docker.containers.list = AsyncMock(return_value=[mock_container])
        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.adopt_orphaned_containers()
        # Should not have called show() since it's already tracked
        mock_container.show.assert_not_called()
        container_manager._containers.pop("tracked-456", None)

    async def test_docker_error_handled(self):
        mock_docker = AsyncMock()
        mock_docker.containers.list = AsyncMock(
            side_effect=aiodocker.exceptions.DockerError(
                "err", {"message": "fail"}
            )
        )
        with patch.object(
            container_manager, "get_docker", return_value=mock_docker
        ):
            await container_manager.adopt_orphaned_containers()
        # Should not raise
