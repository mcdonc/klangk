"""Crash recovery: death classification, backoff restarts, crash-loop (#2524)."""

import asyncio
import time
import types
from contextlib import contextmanager
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk import container, podman
from klangk.container.crash import (
    RESTART_BACKOFF_CAP,
    RESTART_RESET_WINDOW,
    CrashRecoveryMonitor,
    RestartTracker,
    classify_death,
)
from klangk.wshandler.session import WebSocketState
from _helpers import make_settings, wire_db_and_model


def make_app_state(env=None):
    """Minimal app_state with a real registry + wired model (#2524 tests)."""
    settings = make_settings(env or {})
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    app_state.state.podman = podman.Podman(app_state)
    app_state.state.sockets = WebSocketState(app_state)
    registry = container.ContainerRegistry(app_state)
    app_state.state.container_registry = registry
    from klangk.workspaces import Workspaces

    app_state.state.workspaces = Workspaces(app_state)
    from klangk import util as util_mod

    app_state.state.util = util_mod.Util(app_state)
    wire_db_and_model(app_state)
    return app_state


@pytest.fixture
def crash_env():
    """Restart enabled with a near-zero backoff so tests never really sleep."""
    return {
        "KLANGKD_CONTAINER_RESTART_ENABLED": "true",
        "KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS": "0.01",
        "KLANGKD_CONTAINER_RESTART_MAX_RETRIES": "3",
    }


def dead_state(registry, ws_id="ws-crash", cid="cid-crash", health_check=None):
    """Track a workspace whose container will be reported dead."""
    state = container.state.ContainerState(ws_id, cid, registry.app)
    state.health_check = health_check
    registry.states[ws_id] = state
    registry._cid_to_wsid[cid] = ws_id
    return state


def inspect_dead(oom=False, exit_code=137):
    return {
        "State": {"Running": False, "OOMKilled": oom, "ExitCode": exit_code}
    }


INSPECT_RUNNING = {"State": {"Running": True, "OOMKilled": False}}


@contextmanager
def patch_podman_methods(
    app_state,
    inspect_value,
    *,
    listed=None,
    list_containers=None,
    inspect_container=None,
):
    """Patch the podman surface the sweep/stop paths touch.

    Yields a namespace of the AsyncMocks so tests can assert on them
    (unlike patch.multiple, whose mocks are torn down with the block).
    *listed* builds the batched liveness ``ps`` result (default: the
    container is gone); *list_containers* / *inspect_container* override
    the mocks outright for error-injection tests.
    """
    mocks = {
        "inspect_container": inspect_container
        or AsyncMock(return_value=inspect_value),
        "remove_container": AsyncMock(),
        "list_containers": list_containers
        or AsyncMock(return_value=list(listed or [])),
    }
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(
                patch.object(app_state.state.podman, name, mock)
            )
        yield SimpleNamespace(**mocks)


class TestClassifyDeath:
    def test_oom_with_limit(self):
        cause, msg = classify_death(inspect_dead(oom=True), "8g")
        assert cause == "oom"
        assert msg == "OOM-killed at 8g memory limit (exit code 137)"

    def test_oom_without_limit(self):
        cause, msg = classify_death(inspect_dead(oom=True), None)
        assert cause == "oom"
        assert msg == "OOM-killed (exit code 137)"

    def test_clean_exit(self):
        cause, msg = classify_death(inspect_dead(exit_code=0))
        assert (cause, msg) == (
            "exited",
            "main process exited cleanly (code 0)",
        )

    def test_nonzero_exit(self):
        cause, msg = classify_death(inspect_dead(exit_code=1))
        assert (cause, msg) == ("exited", "main process exited with code 1")

    def test_signal_death(self):
        cause, msg = classify_death(inspect_dead(exit_code=139))
        assert (cause, msg) == ("exited", "killed by SIGSEGV (exit code 139)")

    def test_missing_exit_code(self):
        cause, msg = classify_death({"State": {"OOMKilled": False}})
        assert (cause, msg) == (
            "exited",
            "main process exited (no exit code recorded)",
        )

    def test_external_removal(self):
        cause, msg = classify_death(None)
        assert (cause, msg) == (
            "removed",
            "container removed externally (not found)",
        )


class TestBackoff:
    def test_progression_capped(self):
        app_state = make_app_state()
        monitor = CrashRecoveryMonitor(app_state)
        # 5s -> 10s -> 20s -> 40s -> 60s (cap) -> 60s ...
        delays = [monitor.backoff_delay(n) for n in range(1, 7)]
        assert delays == [5, 10, 20, 40, 60, 60]
        assert RESTART_BACKOFF_CAP == 60

    def test_custom_base(self):
        app_state = make_app_state(
            {"KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS": "2"}
        )
        monitor = CrashRecoveryMonitor(app_state)
        assert [monitor.backoff_delay(n) for n in (1, 2, 3, 4, 5)] == [
            2,
            4,
            8,
            16,
            32,
        ]


class TestSettings:
    def test_defaults(self):
        s = make_settings({})
        assert s.container_restart_enabled is False
        assert s.container_restart_max_retries == 5
        assert s.container_restart_backoff_seconds == 5.0

    def test_env_values(self):
        s = make_settings(
            {
                "KLANGKD_CONTAINER_RESTART_ENABLED": "true",
                "KLANGKD_CONTAINER_RESTART_MAX_RETRIES": "9",
                "KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS": "1.5",
            }
        )
        assert s.container_restart_enabled is True
        assert s.container_restart_max_retries == 9
        assert s.container_restart_backoff_seconds == 1.5

    def test_empty_uses_defaults(self):
        s = make_settings(
            {
                "KLANGKD_CONTAINER_RESTART_MAX_RETRIES": "",
                "KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS": "",
            }
        )
        assert s.container_restart_max_retries == 5
        assert s.container_restart_backoff_seconds == 5.0

    def test_enabled_empty_string_is_false(self):
        """Env ``""`` means an explicit False — same convention as the
        sibling ``KLANGKD_CONTAINER_*`` unset-vars, not a boot-aborting
        bool parse error."""
        s = make_settings({"KLANGKD_CONTAINER_RESTART_ENABLED": ""})
        assert s.container_restart_enabled is False

    def test_enabled_spellings(self):
        for raw, want in [
            ("true", True),
            ("1", True),
            ("YES", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
        ]:
            s = make_settings({"KLANGKD_CONTAINER_RESTART_ENABLED": raw})
            assert s.container_restart_enabled is want, raw

    def test_native_bool_from_config_file(self, tmp_path):
        """A YAML ``true`` (native bool) validates too — the env path is
        the string branch, this covers the native branch."""
        cfg = tmp_path / "klangkd.yaml"
        cfg.write_text("container_restart_enabled: true\n")
        s = make_settings({}, config_file=str(cfg))
        assert s.container_restart_enabled is True

    @pytest.mark.parametrize(
        "value",
        ["0", "-1", "two", "1.5"],
    )
    def test_bad_max_retries_raises(self, value):
        with pytest.raises(ValueError):
            make_settings({"KLANGKD_CONTAINER_RESTART_MAX_RETRIES": value})

    @pytest.mark.parametrize("value", ["0", "-2", "fast", "nan"])
    def test_bad_backoff_raises(self, value):
        with pytest.raises(ValueError):
            make_settings({"KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS": value})


def pod_entry(cid: str, state: str) -> dict:
    """A ``podman ps --format json`` entry."""
    return {"Id": cid, "State": state}


class TestSweep:
    def _monitor(self, env=None):
        app_state = make_app_state(env)
        return app_state, app_state.state.container_registry.crash

    async def test_running_container_noop(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "running")],
        ):
            await monitor.sweep_once()
        assert "ws-crash" in reg.states  # untouched
        assert monitor.trackers == {}

    async def test_alive_container_never_inspected(self):
        """Liveness is the batched ps; inspect is classification-only."""
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "created")],
        ) as pm:
            await monitor.sweep_once()
        # "created" (between create and start) is alive, and no
        # per-container inspect subprocess was spawned for it.
        pm.inspect_container.assert_not_awaited()
        assert "ws-crash" in reg.states

    async def test_stopping_workspace_skipped(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        reg.stopping.add("ws-crash")
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "exited")],
        ) as pm:
            await monitor.sweep_once()
        pm.list_containers.assert_not_awaited()  # excluded from the snapshot

    async def test_pending_restart_skipped(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        monitor.pending["ws-crash"] = asyncio.create_task(asyncio.sleep(0))
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "exited")],
        ) as pm:
            await monitor.sweep_once()
        pm.list_containers.assert_not_awaited()
        await asyncio.sleep(0)

    async def test_liveness_list_error_skipped(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            list_containers=AsyncMock(
                side_effect=podman.PodmanError(500, "boom")
            ),
        ):
            await monitor.sweep_once()
        assert "ws-crash" in reg.states  # left alone

    async def test_one_bad_death_does_not_abort_sweep(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg, "ws-a", "cid-a")
        dead_state(reg, "ws-b", "cid-b")
        handled = []

        async def fake_handle(ws_id, cid, info, *, epoch=None):
            handled.append(ws_id)
            if ws_id == "ws-a":
                raise RuntimeError("boom")

        with (
            patch_podman_methods(
                app_state,
                inspect_dead(),
                listed=[
                    pod_entry("cid-a", "exited"),
                    pod_entry("cid-b", "dead"),
                ],
            ),
            patch.object(monitor, "handle_death", fake_handle),
        ):
            await monitor.sweep_once()
        assert handled == ["ws-a", "ws-b"]

    async def test_classify_inspect_error_skipped(self, crash_env):
        """A classification inspect failure leaves the workspace alone."""
        app_state, monitor = self._monitor(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg)
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "exited")],
            inspect_container=AsyncMock(
                side_effect=podman.PodmanError(500, "boom")
            ),
        ):
            await monitor.sweep_once()
        assert "ws-crash" in reg.states  # left alone

    async def test_sweep_skips_stop_in_flight_during_listing(self, crash_env):
        """A stop that BEGAN (marker set) but hasn't completed when the
        listing returns: the sweep skips the workspace."""
        app_state, monitor = self._monitor(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg)
        parked = asyncio.Event()
        list_calls = 0

        async def slow_list(label):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                await parked.wait()
            return [pod_entry("cid-crash", "exited")]

        with patch_podman_methods(
            app_state, inspect_dead(), list_containers=slow_list
        ) as pm:
            sweep = asyncio.create_task(monitor.sweep_once())
            await asyncio.sleep(0)
            reg.stopping.add("ws-crash")  # a stop is now in flight
            parked.set()
            await sweep
            pm.inspect_container.assert_not_awaited()
        assert "ws-crash" in reg.states

    async def test_sweep_skips_stop_completed_during_listing(self, crash_env):
        """A stop that began AND completed during the listing — with the
        registry state surviving because the stop targeted a stale
        container id — is caught by the epoch guard."""
        app_state, monitor = self._monitor(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg)
        parked = asyncio.Event()
        list_calls = 0

        async def slow_list(label):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                await parked.wait()
            return [pod_entry("cid-crash", "exited")]

        with patch_podman_methods(
            app_state, inspect_dead(), list_containers=slow_list
        ) as pm:
            sweep = asyncio.create_task(monitor.sweep_once())
            await asyncio.sleep(0)
            # A /stop racing a stale DB container id: bumps the epoch and
            # cancels crash state, but the rebind check leaves the live
            # state (bound to cid-crash) untouched.
            await reg.stop_and_remove_container(
                "stale-cid", workspace_id="ws-crash"
            )
            parked.set()
            await sweep
            pm.inspect_container.assert_not_awaited()
        assert "ws-crash" in reg.states  # untouched by both parties
        assert monitor.pending == {}

    async def test_stable_container_resets_tracker(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        tracker = RestartTracker()
        tracker.attempts = 2
        tracker.last_started_at = time.time() - (RESTART_RESET_WINDOW + 60)
        monitor.trackers["ws-crash"] = tracker
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "running")],
        ):
            await monitor.sweep_once()
        assert "ws-crash" not in monitor.trackers

    async def test_unstable_container_keeps_tracker(self):
        app_state, monitor = self._monitor()
        reg = app_state.state.container_registry
        dead_state(reg)
        tracker = RestartTracker()
        tracker.attempts = 2
        tracker.last_started_at = time.time() - 5
        monitor.trackers["ws-crash"] = tracker
        with patch_podman_methods(
            app_state,
            inspect_dead(),
            listed=[pod_entry("cid-crash", "running")],
        ):
            await monitor.sweep_once()
        assert monitor.trackers["ws-crash"] is tracker


class TestHandleDeathEntryGuards:
    """A death whose world moved before handling starts is not handled."""

    async def test_state_gone_returns(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        reg.states.pop("ws-crash")  # a user action already cleaned it
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            await monitor.handle_death("ws-crash", "cid-crash", inspect_dead())
        pm.remove_container.assert_not_awaited()
        assert monitor.pending == {}

    async def test_rebound_container_returns(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        reg.track_activity("cid-new", "ws-crash")  # user start rebound
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            await monitor.handle_death("ws-crash", "cid-crash", inspect_dead())
        pm.remove_container.assert_not_awaited()
        assert reg.states["ws-crash"].container_id == "cid-new"

    async def test_stop_in_flight_returns(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        reg.stopping.add("ws-crash")
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            await monitor.handle_death("ws-crash", "cid-crash", inspect_dead())
        pm.remove_container.assert_not_awaited()

    async def test_epoch_mismatch_returns(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        stale_epoch = reg.stop_epoch.get("ws-crash", 0) - 1
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            await monitor.handle_death(
                "ws-crash", "cid-crash", inspect_dead(), epoch=stale_epoch
            )
        pm.remove_container.assert_not_awaited()
        assert monitor.pending == {}

    @pytest.mark.parametrize(
        "interleave",
        ["rebind_in_place", "state_replaced", "stop_in_flight", "epoch"],
    )
    async def test_race_reconnect_during_memory_limit_read(
        self, crash_env, interleave
    ):
        """#331: a user-driven reconnect completing between death
        handling's entry guards and its memory-limit read must win.

        Choreography: the container was stopped externally; the sweep's
        handle_death passes its entry guards and parks inside
        ``_effective_memory_limit`` (its first await). Meanwhile the
        user's ``klangk exec`` reconnect runs ``start_container`` —
        ``_handle_existing_container`` removes the dead container with a
        direct podman rm (never marking ``stopping`` nor bumping the
        epoch) and ``track_activity`` re-binds the SAME state object to a
        fresh container (in-place mutation). Death handling resumes: the
        re-validation must bail before any teardown — the old code tore
        down the fresh container's registry state, killed its network
        sidecar via ``stop_and_remove``'s untracked branch, and scheduled
        a spurious restart.
        """
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        epoch0 = reg.stop_epoch.get("ws-crash", 0)
        limit_read_started = asyncio.Event()
        reconnect_done = asyncio.Event()

        async def parked_limit_read(ws_id):
            limit_read_started.set()
            await reconnect_done.wait()
            return None

        killed_cb = AsyncMock()
        reg.set_on_workspace_killed(killed_cb)
        start_mock = AsyncMock(return_value=("new-cid", "created"))
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            with patch.object(
                monitor, "_effective_memory_limit", parked_limit_read
            ):
                with patch.object(
                    app_state.state.workspaces,
                    "start_workspace",
                    start_mock,
                ):
                    death = asyncio.create_task(
                        monitor.handle_death(
                            "ws-crash",
                            "cid-crash",
                            inspect_dead(),
                            epoch=epoch0,
                        )
                    )
                    await limit_read_started.wait()
                    # The user action completes while death handling is
                    # parked (all four interleave shapes the re-validation
                    # guards must catch):
                    if interleave == "rebind_in_place":
                        # The reconnect's _handle_existing_container rm +
                        # track_activity re-bind (state mutated in place).
                        await pm.remove_container("cid-crash")
                        reg.track_activity("cid-fresh", "ws-crash")
                    elif interleave == "state_replaced":
                        # The state object was swapped outright (state
                        # removal followed by a fresh track).
                        reg.states.pop("ws-crash", None)
                        reg._cid_to_wsid.pop("cid-crash", None)
                        dead_state(reg, cid="cid-fresh")
                    elif interleave == "stop_in_flight":
                        # An expected /stop began and is still running.
                        reg.stopping.add("ws-crash")
                    else:  # epoch
                        # An expected stop began AND completed (epoch bump
                        # without the marker lingering).
                        reg.stop_epoch["ws-crash"] = epoch0 + 1
                    reconnect_done.set()
                    await death
        # Death handling bailed at the re-validation: no killed callback,
        # no teardown remove, no restart.
        killed_cb.assert_not_awaited()
        # No post-resume teardown remove (the rebind_in_place shape made
        # one explicit rm for the dead container; others made none).
        assert pm.remove_container.await_count <= 1
        if interleave == "rebind_in_place":
            assert reg.states["ws-crash"].container_id == "cid-fresh"
        elif interleave == "state_replaced":
            assert reg.states["ws-crash"].container_id == "cid-fresh"
        elif interleave == "stop_in_flight":
            reg.stopping.discard("ws-crash")
        start_mock.assert_not_awaited()
        assert monitor.pending == {}
        assert monitor.status("ws-crash") is None

    async def test_notify_killed_skips_rebound_workspace(self, crash_env):
        """#331: notify_workspace_killed names the dead container; a
        workspace re-bound to a fresh one gets no death teardown."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg, cid="cid-dead")
        reg.track_activity("cid-fresh", "ws-crash")  # re-bind in place
        killed_cb = AsyncMock()
        reg.set_on_workspace_killed(killed_cb)
        with patch_podman_methods(app_state, inspect_dead()) as pm:
            await reg.notify_workspace_killed(
                "ws-crash", container_id="cid-dead"
            )
        killed_cb.assert_not_awaited()
        pm.remove_container.assert_not_awaited()
        assert reg.states["ws-crash"].container_id == "cid-fresh"

    async def test_notify_killed_matching_container_proceeds(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg, cid="cid-dead")
        killed_cb = AsyncMock()
        reg.set_on_workspace_killed(killed_cb)
        await reg.notify_workspace_killed("ws-crash", container_id="cid-dead")
        killed_cb.assert_awaited_once_with("ws-crash", "cid-dead")

    async def test_remove_state_expect_container_guard(self, crash_env):
        """#331: remove_state pops only the named container's state."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        dead_state(reg, cid="cid-a")
        await reg.remove_state("ws-crash", expect_container_id="cid-b")
        assert "ws-crash" in reg.states  # re-bound: fresh state survives
        await reg.remove_state("ws-crash", expect_container_id="cid-a")
        assert "ws-crash" not in reg.states
        # No-kwarg (legacy) form still pops unconditionally.
        reg.track_activity("cid-c", "ws-crash")
        await reg.remove_state("ws-crash")
        assert "ws-crash" not in reg.states


class TestHandleDeathDisabled:
    """With restart off: classify + events + teardown, no restart task."""

    async def test_oom_death_classified_and_surfaced(self):
        app_state = make_app_state()
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg, health_check="true")
        notified = []

        async def fake_notify(ws_id, *, cause=None, container_id=None):
            notified.append((ws_id, cause))

        session = MagicMock()
        with patch_podman_methods(app_state, inspect_dead(oom=True)) as pm:
            with patch.object(reg, "notify_workspace_killed", fake_notify):
                with patch.object(
                    app_state.state.sockets,
                    "get_session",
                    return_value=session,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead(oom=True)
                    )
        # The cause rode the death notification (the service_health
        # death frame's message field).
        assert notified == [
            ("ws-crash", "OOM-killed at 8g memory limit (exit code 137)")
        ]
        # Dead container + state torn down.
        pm.remove_container.assert_awaited_once_with("cid-crash")
        assert "ws-crash" not in reg.states
        # No restart scheduled; the tracker still records the cause for
        # the status API.
        assert monitor.pending == {}
        assert monitor.trackers["ws-crash"].last_cause.startswith("OOM-killed")
        assert monitor.status("ws-crash")["state"] == "dead"
        # A container_died event reached the workspace session.
        event = session.broadcast.call_args[0][0]
        assert event["event"]["name"] == "container_died"
        assert event["event"]["value"]["cause"] == "oom"
        assert event["event"]["value"]["restart_scheduled"] is False

    async def test_external_removal_classified(self):
        app_state = make_app_state()
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        with patch_podman_methods(app_state, None):
            await monitor.handle_death("ws-crash", "cid-crash", None)
        assert monitor.trackers["ws-crash"].last_cause == (
            "container removed externally (not found)"
        )


class TestRestartEnabled:
    async def test_restart_after_backoff(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        ws_row = {"id": "ws-crash", "name": "ws", "settings": {}}
        started = []

        async def fake_start(ws):
            started.append(ws["id"])
            return "new-cid", "created"

        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value=ws_row),
            ):
                with patch.object(
                    app_state.state.workspaces,
                    "start_workspace",
                    fake_start,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead(exit_code=1)
                    )
                    assert "ws-crash" in monitor.pending
                    await monitor.pending["ws-crash"]
        assert started == ["ws-crash"]
        tracker = monitor.trackers["ws-crash"]
        assert tracker.attempts == 1
        assert tracker.last_started_at is not None
        assert monitor.status("ws-crash")["state"] == "recovering"
        assert monitor.pending == {}

    async def test_memory_limit_resolved_from_workspace_bag(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        ws_row = {"id": "ws-crash", "settings": {"memory_limit": "512m"}}
        with patch_podman_methods(app_state, inspect_dead(oom=True)):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value=ws_row),
            ):
                with patch.object(reg, "notify_workspace_killed", AsyncMock()):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead(oom=True)
                    )
        assert "512m" in monitor.trackers["ws-crash"].last_cause, (
            "per-workspace override beats deploy default"
        )

    async def test_workspace_deleted_during_backoff(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        start_mock = AsyncMock(return_value=("new-cid", "created"))
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value=None),
            ):
                with patch.object(
                    app_state.state.workspaces, "start_workspace", start_mock
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    await monitor.pending["ws-crash"]
        start_mock.assert_not_awaited()
        assert "ws-crash" not in monitor.trackers

    async def test_disabled_mid_flight_aborts(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        start_mock = AsyncMock(return_value=("new-cid", "created"))
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value={"id": "ws-crash"}),
            ):
                with patch.object(
                    app_state.state.workspaces, "start_workspace", start_mock
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    # SIGHUP-disabled while backing off.
                    app_state.state.settings.container_restart_enabled = False
                    await monitor.pending["ws-crash"]
        start_mock.assert_not_awaited()

    async def test_repeated_start_failures_reach_crash_loop(self, crash_env):
        app_state = make_app_state(
            {**crash_env, "KLANGKD_CONTAINER_RESTART_MAX_RETRIES": "2"}
        )
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        calls = []

        async def failing_start(ws):
            calls.append(ws["id"])
            raise RuntimeError("mount source missing")

        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value={"id": "ws-crash"}),
            ):
                with patch.object(
                    app_state.state.workspaces,
                    "start_workspace",
                    failing_start,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    await monitor.pending["ws-crash"]
        # 2 bounded attempts, then the terminal state — no infinite loop.
        assert len(calls) == 2
        status = monitor.status("ws-crash")
        assert status["state"] == "crash-loop"
        assert status["attempts"] == 2
        assert status["gave_up_at"] is not None

    async def test_death_after_exhausted_retries_gives_up(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        tracker = RestartTracker()
        tracker.attempts = monitor.max_retries
        tracker.last_cause = "earlier death"
        monitor.trackers["ws-crash"] = tracker
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(reg, "notify_workspace_killed", AsyncMock()):
                await monitor.handle_death(
                    "ws-crash", "cid-crash", inspect_dead()
                )
        assert monitor.pending == {}  # no further restart scheduled
        assert monitor.status("ws-crash")["state"] == "crash-loop"


class TestExpectedStopsNeverRestart:
    async def test_stop_and_remove_cancels_pending_restart(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        # Simulate a scheduled restart mid-backoff.
        await asyncio.sleep(0)
        monitor.trackers["ws-crash"] = RestartTracker()
        task = asyncio.create_task(asyncio.sleep(3600))
        monitor.pending["ws-crash"] = task
        # The user hits /stop: expected death path.
        with patch_podman_methods(app_state, INSPECT_RUNNING):
            await reg.stop_and_remove_container(
                "cid-crash", workspace_id="ws-crash"
            )
        await asyncio.sleep(0)  # let the cancellation propagate
        assert task.cancelled()
        assert "ws-crash" not in monitor.trackers
        assert monitor.pending == {}
        assert reg.stopping == set()

    async def test_stopping_marker_during_slow_remove(self, crash_env):
        """The sweep must not misread an in-flight expected stop as a death."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        seen_markers = []

        async def slow_remove(cid):
            seen_markers.append("ws-crash" in reg.stopping)
            await asyncio.sleep(0.01)

        inspect_mock = AsyncMock(return_value=inspect_dead())
        with (
            patch.object(
                app_state.state.podman, "remove_container", slow_remove
            ),
            patch.object(
                app_state.state.podman,
                "list_containers",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                app_state.state.podman, "inspect_container", inspect_mock
            ),
        ):
            stop_task = asyncio.create_task(
                reg.stop_and_remove_container(
                    "cid-crash", workspace_id="ws-crash"
                )
            )
            await asyncio.sleep(0.005)
            # While the remove is in flight the sweep sees the container
            # dead but must skip it (marked stopping).
            await monitor.sweep_once()
            await stop_task
        assert seen_markers == [True]
        inspect_mock.assert_not_called()
        assert reg.stopping == set()

    async def test_race_user_stop_during_death_handling(self, crash_env):
        """A user stop interleaved with death handling still wins (#2524).

        Choreography: death handling passes its entry guards and parks
        mid-teardown (slow podman remove); a user /stop then begins and
        COMPLETES (bumping the stop epoch); death handling resumes. The
        pre-schedule epoch guard must refuse to schedule a restart — an
        expected death never restarts, regardless of interleaving.
        """
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        epoch0 = reg.stop_epoch.get("ws-crash", 0)
        teardown_parked = asyncio.Event()
        remove_calls = []
        plain_remove = AsyncMock()

        async def slow_remove(cid):
            remove_calls.append(cid)
            if len(remove_calls) == 1:
                # The first remover is death handling's teardown: park it
                # so a user stop can fully complete underneath it.
                await teardown_parked.wait()
            return await plain_remove(cid)

        start_mock = AsyncMock(return_value=("new-cid", "created"))
        with patch_podman_methods(app_state, inspect_dead()):
            app_state.state.podman.remove_container = slow_remove
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value={"id": "ws-crash"}),
            ):
                with patch.object(
                    app_state.state.workspaces, "start_workspace", start_mock
                ):
                    death = asyncio.create_task(
                        monitor.handle_death(
                            "ws-crash",
                            "cid-crash",
                            inspect_dead(),
                            epoch=epoch0,
                        )
                    )
                    await asyncio.sleep(0)  # let the teardown park
                    # The user /stop runs to completion while death
                    # handling is parked (bumps the stop epoch).
                    await reg.stop_and_remove_container(
                        "cid-crash", workspace_id="ws-crash"
                    )
                    teardown_parked.set()
                    await death
        # No restart was scheduled, so nothing to cancel — but verify the
        # outcome directly: no restart attempt, no tracker.
        start_mock.assert_not_awaited()
        assert monitor.pending == {}
        assert monitor.status("ws-crash") is None

    async def test_race_stop_completing_during_liveness_listing(
        self, crash_env
    ):
        """Review #2625 race 1: a user /stop that fully completes while the
        sweep's batched liveness call is in flight must not produce a
        death-handling pass at all (no restart, no second teardown)."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        parked = asyncio.Event()
        list_calls = 0

        async def slow_list(label):
            # Park only the sweep's liveness call; later calls (the user
            # stop's network-sidecar teardown lists containers too) must
            # proceed or the test deadlocks.
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                await parked.wait()
            return [pod_entry("cid-crash", "exited")]

        with patch_podman_methods(
            app_state,
            inspect_dead(),
            list_containers=slow_list,
        ) as pm:
            sweep = asyncio.create_task(monitor.sweep_once())
            await asyncio.sleep(0)  # sweep parks in the liveness listing
            # The user /stop completes fully underneath the listing.
            await reg.stop_and_remove_container(
                "cid-crash", workspace_id="ws-crash"
            )
            parked.set()
            await sweep
            # Post-await revalidation: the workspace's registry state is
            # gone and its stop epoch moved — the sweep skips it entirely.
            pm.inspect_container.assert_not_awaited()  # no classification
        assert monitor.pending == {}
        assert monitor.status("ws-crash") is None

    async def test_race_rebind_during_liveness_listing(self, crash_env):
        """Review #2625 race 2: a user start rebinding the workspace to a
        NEW container while the liveness listing is in flight must not kill
        the new container."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg, "ws-crash", "cid-old")
        parked = asyncio.Event()
        list_calls = 0

        async def slow_list(label):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                await parked.wait()
            return [
                pod_entry("cid-old", "exited"),
                pod_entry("cid-new", "running"),
            ]

        with patch_podman_methods(
            app_state,
            inspect_dead(),
            list_containers=slow_list,
        ) as pm:
            sweep = asyncio.create_task(monitor.sweep_once())
            await asyncio.sleep(0)  # sweep parks in the liveness listing
            # A user start rebinds the workspace: track_activity mutates
            # the SAME ContainerState in place, in the real start path.
            reg.track_activity("cid-new", "ws-crash")
            parked.set()
            await sweep
            # The captured cid (cid-old) no longer matches the state's
            # rebound cid — the sweep must not act on either container.
            pm.inspect_container.assert_not_awaited()
            pm.remove_container.assert_not_awaited()
        assert reg.states["ws-crash"].container_id == "cid-new"
        assert monitor.pending == {}

    async def test_user_start_resets_tracker(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        monitor.trackers["ws-crash"] = RestartTracker()

        # on_start runs in a task that is NOT the pending restart task
        # (e.g. the API /start request handler).
        async def user_start():
            monitor.on_start("ws-crash")

        await asyncio.create_task(user_start())
        assert "ws-crash" not in monitor.trackers

    async def test_monitor_restart_does_not_reset_its_own_tracker(
        self, crash_env
    ):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        tracker = RestartTracker()
        tracker.attempts = 2
        monitor.trackers["ws-crash"] = tracker

        async def restart_task_body():
            # Inside the pending restart task, on_start must not clear.
            monitor.pending["ws-crash"] = asyncio.current_task()
            monitor.on_start("ws-crash")
            del monitor.pending["ws-crash"]

        await asyncio.create_task(restart_task_body())
        assert monitor.trackers["ws-crash"] is tracker


class TestRunLoop:
    async def test_loop_sweeps_and_survives_errors(self, monkeypatch):
        from klangk.container import crash as crash_mod

        monkeypatch.setattr(crash_mod, "LIVENESS_SWEEP_INTERVAL", 0.01)
        app_state = make_app_state()
        monitor = app_state.state.container_registry.crash
        calls = []

        async def flaky_sweep():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient")

        monitor.sweep_once = flaky_sweep
        monitor.start()
        await asyncio.sleep(0.05)
        await monitor.stop()
        assert len(calls) >= 2  # survived the first sweep's error

    async def test_stop_clears_stale_backing_off(self, crash_env):
        """Cancelling pending restarts must not leave a stale
        ``backing-off`` status behind (#2524 review). After a SIGHUP
        runtime restart, /status must not promise a restart that is
        never coming."""
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        tracker = RestartTracker()
        tracker.attempts = 1
        tracker.next_attempt_at = time.time() + 10  # mid-backoff
        monitor.trackers["ws-crash"] = tracker
        task = asyncio.create_task(asyncio.sleep(3600))
        monitor.pending["ws-crash"] = task
        await monitor.stop()
        await asyncio.sleep(0)  # let the cancellation propagate
        assert task.cancelled()
        status = monitor.status("ws-crash")
        assert status["state"] == "dead"  # not "backing-off"
        assert "next_attempt_at" not in status

    async def test_stop_cancels_pending_restarts(self, crash_env):
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        task = asyncio.create_task(asyncio.sleep(3600))
        monitor.pending["ws-crash"] = task
        await monitor.stop()
        await asyncio.sleep(0)  # let the cancellation propagate
        assert task.cancelled()
        assert monitor.crash_task is None


class TestStatusShape:
    def test_status_none_when_clean(self):
        app_state = make_app_state()
        monitor = app_state.state.container_registry.crash
        assert monitor.status("ws") is None

    def test_backing_off_shape(self, crash_env):
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        tracker = RestartTracker()
        tracker.attempts = 1
        tracker.next_attempt_at = time.time() + 10
        monitor.trackers["ws"] = tracker
        status = monitor.status("ws")
        assert status["state"] == "backing-off"
        assert status["next_attempt_at"] is not None
        assert "gave_up_at" not in status  # only present once given up


class TestDeathFrameCarriesCause:
    async def test_notify_workspace_killed_forwards_cause(self):
        app_state = make_app_state()
        reg = app_state.state.container_registry
        dead_state(reg, health_check="true")
        frames = []

        async def fake_broadcast(state, *, message=None):
            frames.append(message)

        with patch.object(reg.health, "broadcast_death", fake_broadcast):
            await reg.notify_workspace_killed(
                "ws-crash", cause="OOM-killed at 8g memory limit"
            )
        assert frames == ["OOM-killed at 8g memory limit"]

    async def test_notify_workspace_killed_without_cause(self):
        app_state = make_app_state()
        reg = app_state.state.container_registry
        dead_state(reg, health_check="true")
        frames = []

        async def fake_broadcast(state, *, message=None):
            frames.append(message)

        with patch.object(reg.health, "broadcast_death", fake_broadcast):
            await reg.notify_workspace_killed("ws-crash")
        assert frames == [None]

    async def test_superseded_tracker_aborts_restart(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        start_mock = AsyncMock(return_value=("new-cid", "created"))
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value={"id": "ws-crash"}),
            ):
                with patch.object(
                    app_state.state.workspaces, "start_workspace", start_mock
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    # While backing off, a user action replaces the tracker
                    # (identity check fails) — the pending restart aborts.
                    monitor.trackers["ws-crash"] = RestartTracker()
                    await monitor.pending["ws-crash"]
        start_mock.assert_not_awaited()

    async def test_restart_cancelled_mid_start(self, crash_env):
        """A cancellation during the restart start propagates (not retried)."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)

        async def cancellable_start(ws):
            raise asyncio.CancelledError()

        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(
                app_state.state.model.workspaces,
                "get_workspace_by_id",
                AsyncMock(return_value={"id": "ws-crash"}),
            ):
                with patch.object(
                    app_state.state.workspaces,
                    "start_workspace",
                    cancellable_start,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    with pytest.raises(asyncio.CancelledError):
                        await monitor.pending["ws-crash"]
        # The cancellation did not burn a retry / schedule another task.
        assert monitor.pending == {}
        assert monitor.trackers["ws-crash"].gave_up_at is None


class TestRegistryShutdownCancelsCrashLoop:
    async def test_give_up_death_event_reaches_session(self, crash_env):
        """The crash-loop terminal death event carries gave_up + attempts."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        tracker = RestartTracker()
        tracker.attempts = monitor.max_retries
        monitor.trackers["ws-crash"] = tracker
        session = MagicMock()
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(reg, "notify_workspace_killed", AsyncMock()):
                with patch.object(
                    app_state.state.sockets,
                    "get_session",
                    return_value=session,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
        event = session.broadcast.call_args[0][0]
        value = event["event"]["value"]
        assert value["gave_up"] is True
        assert value["restart_scheduled"] is False
        assert value["restart_attempts"] == monitor.max_retries
        assert "restart_in_seconds" not in value

    async def test_scheduled_death_event_carries_backoff(self, crash_env):
        """A scheduled restart's death event carries the backoff window."""
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        monitor = reg.crash
        dead_state(reg)
        session = MagicMock()
        with patch_podman_methods(app_state, inspect_dead()):
            with patch.object(reg, "notify_workspace_killed", AsyncMock()):
                with patch.object(
                    app_state.state.sockets,
                    "get_session",
                    return_value=session,
                ):
                    await monitor.handle_death(
                        "ws-crash", "cid-crash", inspect_dead()
                    )
                    await monitor.pending["ws-crash"]
        event = session.broadcast.call_args[0][0]
        assert "restart_in_seconds" in event["event"]["value"]

    async def test_shutdown_stops_monitor(self, crash_env):
        app_state = make_app_state(crash_env)
        reg = app_state.state.container_registry
        task = asyncio.create_task(asyncio.sleep(3600))
        reg.crash.pending["ws-crash"] = task
        with patch_podman_methods(app_state, INSPECT_RUNNING):
            await reg.shutdown()
        await asyncio.sleep(0)  # let the cancellation propagate
        assert task.cancelled()


class TestCrashBranchGaps2834:
    """#2834 branch gate: the unknown-signal exit, idempotent start, a
    superseded restart's done-callback, and the tracker-less death event."""

    def test_unknown_signal_code_falls_back_to_plain_exit(self):
        # Exit code 200 = signal 72, which no known table entry matches:
        # the generic "exited with code" message.
        cause, msg = classify_death(inspect_dead(exit_code=200))
        assert (cause, msg) == (
            "exited",
            "main process exited with code 200",
        )

    async def test_start_twice_keeps_single_task(self, crash_env):
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        monitor.start()
        first = monitor.crash_task
        assert first is not None
        monitor.start()
        assert monitor.crash_task is first
        await monitor.stop()

    async def test_superseded_restart_task_pop_is_skipped(self, crash_env):
        # A second restart scheduled for the same workspace replaces the
        # pending entry; the FIRST task's done-callback then sees a
        # different task and must not pop it (the second survives).
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        with patch_podman_methods(app_state, inspect_dead()):
            monitor.schedule_restart("ws-race", RestartTracker())
            first = monitor.pending["ws-race"]
            # The successor fires far later (pre-bumped attempts -> a
            # near-cap backoff), so "first done, second in flight" is a
            # stable window, not a race with the successor completing.
            late = RestartTracker()
            late.attempts = 12
            monitor.schedule_restart("ws-race", late)
            second = monitor.pending["ws-race"]
            assert second is not first
            for _ in range(200):
                if first.done():
                    break
                await asyncio.sleep(0.01)
            assert first.done()
            # The discriminating state: after the superseded task's
            # done-callback ran, the successor's entry SURVIVES. (An
            # unconditional pop in the callback would have cleared it —
            # the end state alone can't tell those apart.)
            assert monitor.pending["ws-race"] is second
            # The successor's own callback does the popping: cancel it
            # (done-callbacks fire on cancellation too) and observe.
            second.cancel()
            await asyncio.sleep(0.01)
            assert "ws-race" not in monitor.pending

    async def test_broadcast_death_event_without_tracker(self, crash_env):
        # A death with no restart tracker (restarts disabled / gave up
        # unknown) still broadcasts, minus the restart_* fields.
        app_state = make_app_state(crash_env)
        monitor = app_state.state.container_registry.crash
        session = MagicMock()
        with patch.object(
            app_state.state.sockets, "get_session", return_value=session
        ):
            monitor.broadcast_death_event(
                "ws-crash", "exited", "main process exited", None
            )
        value = session.broadcast.call_args[0][0]["event"]["value"]
        assert value["cause"] == "exited"
        assert "restart_attempts" not in value
        assert "gave_up" not in value
