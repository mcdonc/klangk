"""Cordon/drain operator controls (#2527) — model + registry level.

Cordon: a persisted flag (``server_state`` table) that makes the
container-start choke point refuse new starts everywhere — API start /
restart, WS connect/restart, create's eager start, boot auto-start, and
crash-recovery restart. Existing workspaces keep running.

Drain: stop every running workspace via the graceful logout/idle path,
with terminal frames and a container_stopped reason broadcast.

HTTP-surface tests (routes, auth, 503 mapping) live in test_api.py's
TestCordonDrainApi.
"""

import asyncio
import types
from unittest.mock import AsyncMock, patch

import pytest

from klangk.exceptions import NodeCordonedError


class TestServerStateModel:
    async def test_flag_roundtrip_and_default(self, app_state, db):
        """Default uncordoned; set/clear round-trips through the DB."""
        m = app_state.state.model.server_state
        assert await m.is_cordoned() is False
        await m.set_cordoned(True)
        assert await m.is_cordoned() is True
        await m.set_cordoned(False)
        assert await m.is_cordoned() is False

    async def test_generic_kv_roundtrip(self, app_state, db):
        """The table is a generic KV store; upsert replaces."""
        m = app_state.state.model.server_state
        await m.set("k", "v1")
        await m.set("k", "v2")
        assert await m.get("k") == "v2"
        assert await m.get("missing") is None
        assert await m.get("missing", "d") == "d"


class TestCordonGate:
    async def test_registry_choke_point_raises(self, app_state, db):
        """The single start choke point raises NodeCordonedError — the
        error every start path (WS, auto-start, crash restart) funnels
        through."""
        from klangk.container import ContainerStartSpec

        registry = app_state.state.container_registry
        await app_state.state.model.server_state.set_cordoned(True)
        with pytest.raises(NodeCordonedError):
            await registry.start_container(
                ContainerStartSpec(
                    workspace_id="ws-x",
                    host_path="/tmp/x",
                    home_path="/tmp/x/home",
                )
            )

    async def test_gate_blocked_by_draining_flag(self, app_state, db):
        """The in-memory SIGHUP drain flag refuses starts through the same
        choke point — without any DB cordon set."""
        from klangk.container import ContainerStartSpec

        registry = app_state.state.container_registry
        registry.draining = True
        with pytest.raises(NodeCordonedError, match="draining"):
            await registry.start_container(
                ContainerStartSpec(
                    workspace_id="ws-x",
                    host_path="/tmp/x",
                    home_path="/tmp/x/home",
                )
            )

    async def test_gate_opens_after_uncordon(self, app_state, db):
        """After uncordon the same start proceeds past the gate (it may
        still fail later on podman — that is not the gate's business)."""
        from klangk.container import ContainerStartSpec

        registry = app_state.state.container_registry
        await app_state.state.model.server_state.set_cordoned(False)
        try:
            await registry.start_container(
                ContainerStartSpec(
                    workspace_id="ws-x",
                    host_path="/tmp/x",
                    home_path="/tmp/x/home",
                )
            )
        except NodeCordonedError:  # pragma: no cover — gate must be open
            pytest.fail("gate still closed after uncordon")
        except Exception:
            pass  # later failure (podman etc.) is fine — gate let it pass

    async def test_existing_workspace_untouched(self, app_state, db):
        """Cordon does not stop running workspaces: state present in the
        registry survives cordon (drain is the stopping half)."""
        from klangk.container.state import ContainerState

        registry = app_state.state.container_registry
        state = ContainerState("ws-live", "cid-live", app_state)
        registry.states["ws-live"] = state
        await app_state.state.model.server_state.set_cordoned(True)
        assert registry.states.get("ws-live") is state


class TestAutostartSuppressed:
    async def test_boot_autostart_skipped_when_cordoned(self, app_state, db):
        """auto_start_workspaces returns 0 and starts nothing while
        cordoned, even with allow_autostart on."""
        settings = app_state.state.settings
        with (
            patch.object(settings, "allow_autostart", "1"),
            patch.object(
                app_state.state.model.workspaces,
                "list_auto_start_workspaces",
                AsyncMock(return_value=[{"id": "ws-a", "name": "a"}]),
            ),
            patch.object(
                app_state.state.workspaces,
                "start_workspace",
                AsyncMock(side_effect=AssertionError("must not start")),
            ),
        ):
            await app_state.state.model.server_state.set_cordoned(True)
            n = await app_state.state.workspaces.auto_start_workspaces()
            assert n == 0

    async def test_boot_autostart_skipped_when_draining(self, app_state, db):
        """The in-memory SIGHUP drain flag alone suppresses boot
        auto-start (same shared check as cordon)."""
        settings = app_state.state.settings
        with (
            patch.object(settings, "allow_autostart", "1"),
            patch.object(
                app_state.state.model.workspaces,
                "list_auto_start_workspaces",
                AsyncMock(return_value=[{"id": "ws-a", "name": "a"}]),
            ),
            patch.object(
                app_state.state.workspaces,
                "start_workspace",
                AsyncMock(side_effect=AssertionError("must not start")),
            ),
        ):
            app_state.state.container_registry.draining = True
            n = await app_state.state.workspaces.auto_start_workspaces()
            assert n == 0

    async def test_boot_autostart_runs_when_uncordoned(self, app_state, db):
        settings = app_state.state.settings
        with (
            patch.object(settings, "allow_autostart", "1"),
            patch.object(
                app_state.state.model.workspaces,
                "list_auto_start_workspaces",
                AsyncMock(return_value=[{"id": "ws-a", "name": "a"}]),
            ),
            patch.object(
                app_state.state.workspaces,
                "start_workspace",
                AsyncMock(return_value=("cid-a", "created")),
            ),
        ):
            n = await app_state.state.workspaces.auto_start_workspaces()
            assert n == 1


class TestCrashRestartSuppressed:
    async def test_restart_loop_abandons_when_cordoned(self, app_state, db):
        """A pending crash-restart does not fire while cordoned — a
        drain's stopped state must stick (#2527). Restart must be
        ENABLED so the cordon check (not the enabled check) is what
        stops the attempt."""
        from klangk.container.crash import RestartTracker

        monitor = app_state.state.container_registry.crash
        tracker = RestartTracker()
        started = []

        async def fake_start(ws):
            started.append(ws["id"])
            return "new-cid", "created"

        async def fake_ws(ws_id):
            return {"id": ws_id, "name": "ws", "settings": {}}

        with (
            patch.object(
                app_state.state.settings, "container_restart_enabled", True
            ),
            patch.object(
                app_state.state.model.server_state,
                "is_cordoned",
                AsyncMock(return_value=True),
            ),
            patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(side_effect=fake_ws),
            ),
            patch.object(
                app_state.state.workspaces,
                "start_workspace",
                fake_start,
            ),
        ):
            monitor.trackers["ws-c"] = tracker
            await monitor.delayed_restart("ws-c", tracker)
        assert started == []
        assert tracker.next_attempt_at is None

    async def test_restart_loop_abandons_when_draining(self, app_state, db):
        """A pending crash-restart also abandons on the in-memory SIGHUP
        drain flag (no DB cordon set) — a mid-restart container death
        must not re-start under the recycling runtime."""
        from klangk.container.crash import RestartTracker

        monitor = app_state.state.container_registry.crash
        tracker = RestartTracker()
        started = []

        async def fake_start(ws):
            started.append(ws["id"])
            return "new-cid", "created"

        async def fake_ws(ws_id):
            return {"id": ws_id, "name": "ws", "settings": {}}

        with (
            patch.object(
                app_state.state.settings, "container_restart_enabled", True
            ),
            patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(side_effect=fake_ws),
            ),
            patch.object(
                app_state.state.workspaces,
                "start_workspace",
                fake_start,
            ),
        ):
            app_state.state.container_registry.draining = True
            monitor.trackers["ws-c"] = tracker
            await monitor.delayed_restart("ws-c", tracker)
        assert started == []
        assert tracker.next_attempt_at is None


class TestDrain:
    def _track(self, app_state, registry, n):
        from klangk.container.state import ContainerState

        for i in range(n):
            ws_id = f"ws-{i}"
            registry.states[ws_id] = ContainerState(
                ws_id, f"cid-{i}", app_state
            )

    def _stub_sweep(self, app_state, containers=()):
        """Give the registry's app a podman stub so the drain's
        instance-label sweep lists predictable containers without a CLI
        call (the conftest app_state has no podman wired)."""
        app_state.state.podman = types.SimpleNamespace(
            list_containers=AsyncMock(return_value=list(containers))
        )

    async def test_drain_stops_everything(self, app_state, db):
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 3)
        self._stub_sweep(app_state)
        stopped = []

        async def fake_stop(cid, workspace_id=None):
            stopped.append(workspace_id)
            return True

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 3
        assert sorted(stopped) == ["ws-0", "ws-1", "ws-2"]

    async def test_drain_runs_concurrently(self, app_state, db):
        """Per-workspace stops overlap (a node with many workspaces must
        not pay N sequential 5s stops) — a stop that is still in flight
        while another begins proves the gather (#2527 review)."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 2)
        self._stub_sweep(app_state)
        in_flight: list[str] = []
        overlapped: list[str] = []

        async def fake_stop(cid, workspace_id=None):
            in_flight.append(workspace_id)
            await asyncio.sleep(0.02)
            if len(in_flight) > 1:
                overlapped.append(workspace_id)
            in_flight.remove(workspace_id)
            return True

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 2
        assert overlapped, "drain stops did not overlap"

    async def test_drain_does_not_count_failed_stops(self, app_state, db):
        """A stop that reports failure (or a racing re-bind) is logged,
        not counted — the reported count never overstates (#2527
        review)."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 2)
        self._stub_sweep(app_state)

        async def fake_stop(cid, workspace_id=None):
            return workspace_id == "ws-0"

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 1

    async def test_drain_exception_is_contained(self, app_state, db, caplog):
        """One workspace's stop raising does not abort the drain."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 2)
        self._stub_sweep(app_state)

        async def fake_stop(cid, workspace_id=None):
            if workspace_id == "ws-1":
                raise RuntimeError("podman exploded")
            return True

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            n = await registry.drain_all_containers()
        assert n == 1
        assert any("podman exploded" in r.message for r in caplog.records)

    async def test_drain_sweeps_racing_start_containers(
        self, app_state, db, caplog
    ):
        """The instance-label sweep catches a container whose start
        passed the gate just before it closed (not yet in the states
        snapshot) — both drain paths stop it (#2527 review)."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        # Nothing tracked; the "racing start" container exists only in
        # podman's view of this instance's containers.
        self._stub_sweep(
            app_state, [{"Id": "cid-race", "Names": ["klangk-ws-race"]}]
        )
        swept = []

        async def fake_stop(cid, workspace_id=None):
            swept.append(cid)
            return True

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            caplog.at_level("INFO"),
        ):
            n = await registry.drain_all_containers()
        assert n == 1
        assert swept == ["cid-race"]
        assert any("racing-start" in r.message for r in caplog.records)

    async def test_drain_sweep_tolerates_podman_errors(
        self, app_state, db, caplog
    ):
        """A failing sweep listing (podman down) is logged and skipped —
        the tracked-workspace drain result stands."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 1)
        # Only PodmanError/OSError are tolerated by the sweep.
        from klangk.podman import PodmanError

        app_state.state.podman = types.SimpleNamespace(
            list_containers=AsyncMock(
                side_effect=PodmanError(500, "podman down")
            )
        )

        async def fake_stop(cid, workspace_id=None):
            return True

        with (
            patch.object(registry, "stop_and_remove_container", fake_stop),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            n = await registry.drain_all_containers()
        assert n == 1
        assert any("sweeping leftover" in r.message for r in caplog.records)

    async def test_drain_sweep_skips_identless_containers(self, app_state, db):
        """A sweep result with no usable identifier is skipped, not
        stopped."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._stub_sweep(app_state, [{"Id": "", "Names": []}])
        with patch.object(
            registry, "stop_and_remove_container", AsyncMock()
        ) as mock_stop:
            n = await registry.drain_all_containers()
        assert n == 0
        mock_stop.assert_not_awaited()

    async def test_drain_broadcasts_reason(self, app_state):
        """Connected clients get a container_stopped frame carrying the
        drain reason (clean 'stopped', not a dropped WebSocket)."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 1)
        self._stub_sweep(app_state)
        broadcast = []

        class FakeSession:
            def broadcast(self, message):
                broadcast.append(message)

        with (
            patch.object(
                app_state.state.sockets,
                "get_session",
                return_value=FakeSession(),
            ),
            patch.object(
                registry,
                "stop_and_remove_container",
                AsyncMock(return_value=True),
            ),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 1
        assert broadcast, "expected a container_stopped broadcast"
        event = broadcast[0]["event"]
        assert event["name"] == "container_stopped"
        assert "drain" in event["value"]["reason"]

    async def test_drain_skips_broadcast_without_session(self, app_state):
        """No connected clients (get_session -> None) — drain still
        completes and simply skips the broadcast."""
        from klangk.wshandler.session import WebSocketState

        registry = app_state.state.container_registry
        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 1)
        self._stub_sweep(app_state)
        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                AsyncMock(return_value=True),
            ),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 1

    async def test_drain_skips_cidless_state(self, app_state, db):
        """A tracked state with no container id is skipped, not stopped."""
        from klangk.container.state import ContainerState

        registry = app_state.state.container_registry
        from klangk.wshandler.session import WebSocketState

        app_state.state.sockets = WebSocketState(app_state)
        state = ContainerState("ws-nocid", None, app_state)
        registry.states["ws-nocid"] = state
        self._stub_sweep(app_state)
        with (
            patch.object(
                registry, "stop_and_remove_container", AsyncMock()
            ) as mock_stop,
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 0
        mock_stop.assert_not_awaited()

    async def test_drain_fires_kill_callback_per_workspace(
        self, app_state, db
    ):
        """Drain's session/agent reset rides the on_workspace_killed
        callback (wired in main.py) — assert it fires per workspace, so
        a wiring change cannot silently leave stale sessions (#2527
        review: the /stop endpoint calls reset_workspace_state itself,
        drain relies on this callback)."""
        registry = app_state.state.container_registry
        from klangk.wshandler.session import WebSocketState

        app_state.state.sockets = WebSocketState(app_state)
        self._track(app_state, registry, 2)
        self._stub_sweep(app_state)
        killed = []

        async def on_killed(ws_id):
            killed.append(ws_id)

        registry.on_workspace_killed = on_killed

        async def fake_stop(cid, workspace_id=None):
            return True

        # No patch on notify_workspace_killed — the real wrapper is the
        # code under test (it invokes the callback above).
        with patch.object(registry, "stop_and_remove_container", fake_stop):
            n = await registry.drain_all_containers()
        assert n == 2
        assert sorted(killed) == ["ws-0", "ws-1"]

    async def test_drain_idempotent(self, app_state):
        registry = app_state.state.container_registry
        self._stub_sweep(app_state)
        with (
            patch.object(registry, "stop_and_remove_container", AsyncMock()),
            patch.object(registry, "notify_workspace_killed", AsyncMock()),
        ):
            n = await registry.drain_all_containers()
        assert n == 0
