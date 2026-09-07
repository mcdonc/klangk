"""Operational resource detection (#3206).

Covers the disk-capacity state machine (threshold entry, hysteresis
recovery, transition-only event emission), filesystem discovery
(device dedup, unmeasurable paths, the cached podman storage root),
the audit-degradation edge detection over both write-failure counters,
settings validation/coercion, the loop's guarded/disabled branches,
and the lifecycle wiring.
"""

import asyncio
import logging
import time
import types
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from _helpers import make_settings
from klangk import lifecycle
from klangk.notifier import DEFAULT_NOTIFY_EVENTS, THROTTLE_SECONDS
from klangk.resource_watchdog import (
    CPU_KEY,
    CRITICAL,
    MEMORY_KEY,
    OK,
    RECOVERY_GAP_PERCENT,
    REFRESH_SECONDS,
    WARN,
    ResourceWatchdog,
    classify,
    parse_cpu_psi,
    psi_field,
    read_local_text,
    usage_percent,
)


def make_wd(env=None, **state_extras):
    """A watchdog over a minimal app state (the eviction-test shape).

    Extra ``state_extras`` attach arbitrary state objects (a notifier
    spy, a podman stub, a registry stub) for the surface under test.
    """
    settings = make_settings(env)
    state = types.SimpleNamespace(settings=settings)
    for name, value in state_extras.items():
        setattr(state, name, value)
    app = types.SimpleNamespace(state=state)
    return ResourceWatchdog(app), app


def notifier_spy(app):
    """A Mock notifier wired onto the app state; returns the spy."""
    spy = Mock()
    app.state.notifier = spy
    return spy


class FakeVfs:
    """A statvfs result with the fields usage_percent reads."""

    def __init__(self, used_percent, frsize=4096, blocks=1000):
        self.f_frsize = frsize
        self.f_blocks = blocks
        self.f_bavail = int(blocks * (100 - used_percent) / 100)


class FakeStat:
    """A stat result carrying only st_dev."""

    def __init__(self, dev):
        self.st_dev = dev


def patch_stat(monkeypatch, devs):
    """Patch os.stat/os.statvfs: *devs* maps path -> device id (paths
    absent from the map get device 99); every filesystem reads 80%."""
    monkeypatch.setattr("os.stat", lambda path: FakeStat(devs.get(path, 99)))
    monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(80.0))


async def run_briefly(wd, seconds=0.1):
    """Start the loop, let it cycle, stop it (the eviction-test shape)."""
    wd.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await wd.stop()


# --- pure threshold state machine ---


class TestClassify:
    @pytest.mark.parametrize(
        "usage,state,expected",
        [
            (50.0, OK, OK),  # healthy stays healthy
            (76.0, OK, WARN),  # OK -> warn on crossing warn
            (91.0, OK, CRITICAL),  # deep crossing goes straight critical
            (91.0, WARN, CRITICAL),  # warn -> critical
            (76.0, WARN, WARN),  # sustained warn is no transition
            (73.0, WARN, WARN),  # warn hysteresis band keeps warn
            (69.0, WARN, OK),  # at/below floor recovers
            (73.0, CRITICAL, CRITICAL),  # warn band keeps critical
            (69.0, CRITICAL, OK),  # critical recovers only fully
            (50.0, CRITICAL, OK),
            # Critical-boundary hysteresis: readings between the
            # critical floor and the critical threshold hold CRITICAL
            # (an 89.9/90.1 oscillation cannot flap warn/critical).
            (89.9, CRITICAL, CRITICAL),
            (86.0, CRITICAL, CRITICAL),
            (84.0, CRITICAL, WARN),  # at/below the critical floor eases
            (88.0, OK, WARN),  # never-critical reading between thresholds
        ],
    )
    def test_transitions(self, usage, state, expected):
        # Defaults 75/90; floors 70/85 = thresholds - gap.
        assert classify(usage, state, 75.0, 90.0, 70.0, 85.0) == expected

    def test_recovery_gap_value(self):
        assert RECOVERY_GAP_PERCENT == 5.0


class TestUsagePercent:
    def test_measures_a_real_filesystem(self, tmp_path):
        usage = usage_percent(str(tmp_path))
        assert 0.0 <= usage <= 100.0

    def test_zero_capacity_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(50.0, blocks=0))
        with pytest.raises(ValueError):
            usage_percent(str(tmp_path))

    def test_computes_percent_used(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(80.0))
        assert usage_percent(str(tmp_path)) == pytest.approx(80.0)


# --- disk surface ---


class TestStepFilesystem:
    def _wd(self, env=None):
        wd, app = make_wd(env)
        return wd, notifier_spy(app)

    def test_ok_to_warn_emits_warn(self, caplog):
        wd, spy = self._wd()
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            wd.step_filesystem(7, "/data", 76.0)
        assert spy.notify_admins.call_count == 1
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "resource.disk.warn"
        assert kwargs["detail"]["path"] == "/data"
        assert kwargs["detail"]["usage_percent"] == 76.0
        assert kwargs["detail"]["state"] == WARN
        assert "Disk usage 76.0% on /data: warn" in caplog.text

    def test_warn_to_critical_emits_critical(self, caplog):
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 76.0)
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            wd.step_filesystem(7, "/data", 91.0)
        args, _ = spy.notify_admins.call_args
        assert args[0] == "resource.disk.critical"
        assert "critical" in caplog.text

    def test_no_transition_no_event(self):
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 91.0)  # -> critical (event)
        spy.notify_admins.reset_mock()
        wd.step_filesystem(7, "/data", 92.0)  # deeper, same state
        wd.step_filesystem(7, "/data", 95.0)  # deeper still
        wd.step_filesystem(7, "/data", 73.0)  # hysteresis band keeps critical
        spy.notify_admins.assert_not_called()
        # The band keeps a warn state too: 73 is above the floor (70).
        wd.step_filesystem(8, "/other", 76.0)  # -> warn (event)
        spy.notify_admins.reset_mock()
        wd.step_filesystem(8, "/other", 73.0)
        wd.step_filesystem(8, "/other", 80.0)
        spy.notify_admins.assert_not_called()
        # A persisting healthy state never refreshes.
        wd.step_filesystem(9, "/healthy", 50.0)
        wd.step_filesystem(9, "/healthy", 51.0)
        spy.notify_admins.assert_not_called()

    def test_critical_easing_to_warn_emits_warn(self):
        """Usage between the thresholds after critical is a real
        improvement — a warn event reports it (recovered fires only
        from below the floor)."""
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 91.0)
        wd.step_filesystem(7, "/data", 85.0)
        assert spy.notify_admins.call_count == 2
        args, _ = spy.notify_admins.call_args
        assert args[0] == "resource.disk.warn"
        assert kwargs_of(spy, 1)["detail"]["state"] == WARN

    def test_recovery_emits_recovered_at_info(self, caplog):
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 91.0)
        with caplog.at_level(logging.INFO, logger="klangk.resource_watchdog"):
            wd.step_filesystem(7, "/data", 65.0)
        args, _ = spy.notify_admins.call_args
        assert args[0] == "resource.disk.recovered"
        assert kwargs_of(spy, 1)["detail"]["state"] == OK
        assert "recovered" in caplog.text

    def test_independent_devices_transition_independently(self):
        wd, spy = self._wd()
        wd.step_filesystem(1, "/a", 91.0)
        wd.step_filesystem(2, "/b", 91.0)
        assert spy.notify_admins.call_count == 2
        paths = [kwargs_of(spy, i)["detail"]["path"] for i in range(2)]
        assert paths == ["/a", "/b"]

    def test_live_thresholds_from_settings(self):
        wd, spy = self._wd({"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "50"})
        wd.step_filesystem(7, "/data", 55.0)
        assert spy.notify_admins.call_count == 1
        assert kwargs_of(spy)["detail"]["warn_percent"] == 50.0

    def test_persisting_state_refreshes_once_per_window(self):
        """A still-degraded filesystem re-notifies once per refresh
        window, so a transition whose dispatch the notifier throttled
        away is late, never lost (the #3206 review swallow case)."""
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 91.0)  # transition -> critical
        spy.notify_admins.reset_mock()
        # Same state, inside the window: quiet.
        wd.step_filesystem(7, "/data", 92.0)
        spy.notify_admins.assert_not_called()
        # Same state, past the window: refresh.
        wd._emitted_at[7] = time.monotonic() - REFRESH_SECONDS - 1
        wd.step_filesystem(7, "/data", 92.0)
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "resource.disk.critical"
        assert kwargs["detail"]["usage_percent"] == 92.0

    def test_refill_inside_window_alerts_via_refresh(self):
        """warn -> recovered -> refill inside the throttle window: the
        transition dispatch may be throttled, but the persisting state
        refreshes past the window — no permanently lost alert."""
        wd, spy = self._wd()
        wd.step_filesystem(7, "/data", 76.0)  # warn (stamps the bucket)
        wd.step_filesystem(7, "/data", 65.0)  # recovered
        wd.step_filesystem(7, "/data", 80.0)  # refill: transition fires
        spy.notify_admins.reset_mock()
        assert wd._states[7] == WARN
        # Within the refresh window nothing new fires...
        wd.step_filesystem(7, "/data", 81.0)
        spy.notify_admins.assert_not_called()
        # ...past it, the still-warn state re-alerts.
        wd._emitted_at[7] = time.monotonic() - REFRESH_SECONDS - 1
        wd.step_filesystem(7, "/data", 81.0)
        assert spy.notify_admins.call_count == 1


def kwargs_of(spy, call=0):
    return spy.notify_admins.call_args_list[call].kwargs


def patch_dispatch(monkeypatch, results):
    """Patch the watchdog's notify_event with a scripted dispatcher.

    *results* is consumed one bool per dispatch (True = the notifier
    created a delivery task — the #3206 retry signal); the event names
    are returned for assertions.
    """
    calls = []

    def fake(app, event, **fields):
        calls.append(event)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr("klangk.resource_watchdog.notify_event", fake)
    return calls


def dispatchable_notifier(
    *, allowlist=frozenset(DEFAULT_NOTIFY_EVENTS), channels=True
):
    """A notifier stub whose allowlist/channels admit the disk events
    — the structural dispatchability the retry machinery consults
    (:meth:`ResourceWatchdog._event_dispatchable`)."""
    return types.SimpleNamespace(
        notify_events=lambda: allowlist,
        channels_configured=lambda: channels,
    )


class TestMonitoredFilesystems:
    async def test_deduplicates_paths_sharing_a_device(self, monkeypatch):
        wd, app = make_wd()
        data = app.state.settings.data_dir
        state = app.state.settings.state_dir
        extra = "/srv/elsewhere"
        app.state.settings.disk_watchdog_paths = [extra]
        patch_stat(monkeypatch, {data: 1, state: 1, extra: 1})
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        result = await wd.monitored_filesystems()
        assert result == [(1, data, pytest.approx(80.0))]

    async def test_distinct_devices_are_separate(self, monkeypatch):
        wd, app = make_wd()
        data = app.state.settings.data_dir
        state = app.state.settings.state_dir
        extra = "/srv/other"
        app.state.settings.disk_watchdog_paths = [extra]
        patch_stat(monkeypatch, {data: 1, state: 1, extra: 2})
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        result = await wd.monitored_filesystems()
        assert [entry[:2] for entry in result] == [(1, data), (2, extra)]

    async def test_split_mount_state_dir_monitored_separately(
        self, monkeypatch
    ):
        """#3310: a state_dir on its own filesystem is measured even
        when data_dir lives on another device (split-mount
        deployment)."""
        wd, app = make_wd(
            {
                "KLANGKD_STATE_DIR": "/srv/state",
                "KLANGKD_DATA_DIR": "/mnt/bigdata",
            }
        )
        state = app.state.settings.state_dir
        data = app.state.settings.data_dir
        patch_stat(monkeypatch, {data: 1, state: 2})
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        result = await wd.monitored_filesystems()
        assert [entry[:2] for entry in result] == [(1, data), (2, state)]

    async def test_same_mount_state_dir_deduplicates_under_data_dir(
        self, monkeypatch
    ):
        """#3310: when state_dir shares data_dir's filesystem (the
        default layout), the alert set is exactly as before — one
        filesystem, reported under the data directory."""
        wd, app = make_wd()
        state = app.state.settings.state_dir
        data = app.state.settings.data_dir
        patch_stat(monkeypatch, {data: 1, state: 1})
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        result = await wd.monitored_filesystems()
        assert result == [(1, data, pytest.approx(80.0))]

    async def test_split_mount_state_dir_alerts_under_its_own_path(
        self, monkeypatch, caplog
    ):
        """#3310 acceptance: a split-mount state_dir crossing the warn
        threshold emits ``resource.disk.warn`` reported under the
        state_dir path — not silently unmonitored."""
        wd, app = make_wd(
            {
                "KLANGKD_STATE_DIR": "/srv/state",
                "KLANGKD_DATA_DIR": "/mnt/bigdata",
            }
        )
        spy = notifier_spy(app)
        state = app.state.settings.state_dir
        data = app.state.settings.data_dir
        patch_stat(monkeypatch, {data: 1, state: 2})

        def vfs_by_path(path):
            return FakeVfs(76.0 if path == state else 50.0)

        monkeypatch.setattr("os.statvfs", vfs_by_path)
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_disk()
        assert spy.notify_admins.call_count == 1
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "resource.disk.warn"
        assert kwargs["detail"]["path"] == state
        assert f"Disk usage 76.0% on {state}" in caplog.text

    async def test_unmeasurable_path_warned_once_then_skipped(
        self, monkeypatch, caplog, tmp_path
    ):
        wd, app = make_wd(
            {
                # A state_dir that exists on disk (the derived default
                # is never created); the autouse fixture points
                # KLANGKD_DATA_DIR at tmp_path itself, so state_dir and
                # data_dir are the same path here.
                "KLANGKD_STATE_DIR": str(tmp_path),
                "KLANGKD_DISK_WATCHDOG_PATHS": str(tmp_path / "missing"),
            }
        )
        monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(10.0))
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            first = await wd.monitored_filesystems()
            second = await wd.monitored_filesystems()
        # The data dir alone survived; the warning fired once.
        assert len(first) == 1 and len(second) == 1
        warnings = [r for r in caplog.records if "cannot measure" in r.message]
        assert len(warnings) == 1

    async def test_all_unmeasurable_yields_nothing(self, monkeypatch, caplog):
        wd, app = make_wd()

        def boom(path):
            raise OSError(13, "permission denied")

        monkeypatch.setattr("os.statvfs", boom)
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            assert await wd.monitored_filesystems() == []
        assert "cannot measure" in caplog.text

    async def test_recovered_measurement_rearms_warning(
        self, monkeypatch, caplog, tmp_path
    ):
        state = tmp_path / "state"
        state.mkdir()
        wd, app = make_wd({"KLANGKD_STATE_DIR": str(state)})
        data = app.state.settings.data_dir

        def flaky(path, attempts=[0]):
            if path != data:
                return FakeVfs(10.0)
            attempts[0] += 1
            if attempts[0] % 2:  # fail, succeed, fail, ...
                raise OSError(5, "I/O error")
            return FakeVfs(10.0)

        monkeypatch.setattr("os.statvfs", flaky)
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            for _ in range(3):
                await wd.monitored_filesystems()
        warnings = [r for r in caplog.records if "cannot measure" in r.message]
        assert len(warnings) == 2  # 1st and 3rd polls


class TestGraphRoot:
    def _wd_with_podman(self, run_result=None, run_raises=False):
        podman = types.SimpleNamespace()
        if run_raises:
            podman.run = AsyncMock(side_effect=RuntimeError("spawn failed"))
        else:
            podman.run = AsyncMock(return_value=run_result)
        wd, app = make_wd(podman=podman)
        return wd, app, podman

    async def test_resolved_and_cached(self, monkeypatch):
        wd, app, podman = self._wd_with_podman(
            (0, "/var/lib/containers/storage\n", "")
        )
        patch_stat(
            monkeypatch,
            {
                app.state.settings.data_dir: 1,
                "/var/lib/containers/storage": 42,
            },
        )
        for _ in range(3):
            result = await wd.monitored_filesystems()
        assert (
            42,
            "/var/lib/containers/storage",
            pytest.approx(80.0),
        ) in result
        assert podman.run.await_count == 1

    async def test_resolved_root_unmeasurable_is_skipped(
        self, monkeypatch, caplog
    ):
        """A remote podman machine's storage path (resolved but absent
        on this host) is skipped — warned once, configured paths
        still monitored."""
        wd, app, podman = self._wd_with_podman(
            (0, "/var/lib/containers/storage\n", "")
        )
        data = app.state.settings.data_dir
        monkeypatch.setattr("os.stat", lambda path: FakeStat(1))

        def statvfs_by_path(path):
            if path == data:
                return FakeVfs(10.0)
            raise OSError(2, "no such file")

        monkeypatch.setattr("os.statvfs", statvfs_by_path)
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            result = await wd.monitored_filesystems()
        assert [entry[:2] for entry in result] == [(1, data)]
        assert "cannot measure" in caplog.text

    async def test_query_failure_retries_after_cooldown(self, caplog):
        """A failed query is not a permanent degradation: one retry
        per cooldown window (the _measure re-arm posture)."""
        wd, app, podman = self._wd_with_podman((125, "", "boom"))
        with caplog.at_level(logging.INFO, logger="klangk.resource_watchdog"):
            assert await wd.resolve_graph_root() is None
            # Inside the cooldown: no new subprocess.
            assert await wd.resolve_graph_root() is None
            assert podman.run.await_count == 1
            assert "storage root unavailable" in caplog.text
            # Past the cooldown: retried.
            wd._graph_root_retry_at = time.monotonic() - 1
            assert await wd.resolve_graph_root() is None
        assert podman.run.await_count == 2

    async def test_transient_failure_recovers_on_retry(self):
        """The boot race (podman socket not up yet) heals: the retry
        after the cooldown caches the root."""
        wd, app, podman = self._wd_with_podman((125, "", "boom"))
        assert await wd.resolve_graph_root() is None
        podman.run = AsyncMock(
            return_value=(0, "/var/lib/containers/storage\n", "")
        )
        wd._graph_root_retry_at = time.monotonic() - 1
        assert await wd.resolve_graph_root() == "/var/lib/containers/storage"
        assert wd._graph_root == "/var/lib/containers/storage"

    async def test_empty_output_means_unavailable(self):
        wd, app, podman = self._wd_with_podman((0, "\n", ""))
        assert await wd.resolve_graph_root() is None
        assert wd._graph_root_retry_at > time.monotonic()

    async def test_spawn_error_means_unavailable(self):
        wd, app, podman = self._wd_with_podman(run_raises=True)
        assert await wd.resolve_graph_root() is None
        assert wd._graph_root_retry_at > time.monotonic()

    async def test_absent_podman_state_is_unavailable(self):
        wd, _ = make_wd()  # no podman on the state
        assert await wd.resolve_graph_root() is None
        assert wd._graph_root_retry_at > time.monotonic()

    async def test_reconfigure_clears_the_cache_and_cooldown(self):
        wd, app, podman = self._wd_with_podman(
            (0, "/var/lib/containers/storage\n", "")
        )
        await wd.resolve_graph_root()
        wd.reconfigure(app)
        await wd.resolve_graph_root()
        assert podman.run.await_count == 2

    async def test_reconfigure_retries_past_failure_immediately(self):
        """A reload (SIGHUP) clears the cooldown too, so an operator
        can force a retry without waiting out the window."""
        wd, app, podman = self._wd_with_podman((125, "", "boom"))
        assert await wd.resolve_graph_root() is None
        podman.run = AsyncMock(
            return_value=(0, "/var/lib/containers/storage\n", "")
        )
        wd.reconfigure(app)  # cooldown cleared
        assert await wd.resolve_graph_root() == "/var/lib/containers/storage"


# --- audit surface ---


class TestAuditSurface:
    def _wd(self, container_failures=0, audit_failures=0):
        registry = types.SimpleNamespace(
            audit_write_failures=container_failures
        )
        events = types.SimpleNamespace(write_failures=audit_failures)
        model = types.SimpleNamespace(audit_events=events)
        wd, app = make_wd(container_registry=registry, model=model)
        return wd, notifier_spy(app), registry, events

    def test_first_poll_is_a_baseline_no_event(self):
        wd, spy, _, _ = self._wd(container_failures=3)
        wd.check_audit()
        spy.notify_admins.assert_not_called()

    def test_growth_emits_audit_failure(self, caplog):
        wd, spy, registry, _ = self._wd(container_failures=1)
        wd.check_audit()
        registry.audit_write_failures = 4
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            wd.check_audit()
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "audit.failure"
        assert kwargs["detail"]["table"] == "container_events"
        assert kwargs["detail"]["failures"] == 3
        assert kwargs["detail"]["total"] == 4
        assert "degradation" in caplog.text

    def test_continued_growth_within_episode_stays_quiet(self):
        wd, spy, registry, _ = self._wd(container_failures=1)
        wd.check_audit()
        registry.audit_write_failures = 2
        wd.check_audit()
        registry.audit_write_failures = 9
        wd.check_audit()
        assert spy.notify_admins.call_count == 1

    def test_clean_window_rearms(self):
        wd, spy, registry, _ = self._wd(container_failures=1)
        wd.check_audit()
        registry.audit_write_failures = 2
        wd.check_audit()  # episode opens
        wd.check_audit()  # clean window (count stable) closes it
        registry.audit_write_failures = 3
        wd.check_audit()  # new episode alerts again
        assert spy.notify_admins.call_count == 2

    def test_identity_table_watched_independently(self):
        wd, spy, _, events = self._wd(audit_failures=0)
        wd.check_audit()
        events.write_failures = 5
        wd.check_audit()
        assert spy.notify_admins.call_count == 1
        assert kwargs_of(spy)["detail"]["table"] == "audit_events"

    def test_minimal_state_is_skipped(self):
        wd, _ = make_wd()  # no registry, no model
        wd.check_audit()  # must not raise


# --- loop + guards ---


class TestLoop:
    async def test_start_is_idempotent_stop_cancels(self):
        wd, _ = make_wd()
        wd.start()
        task = wd._task
        wd.start()
        assert wd._task is task
        await wd.stop()
        assert wd._task is None
        assert task.cancelled()

    async def test_stop_when_never_started_is_noop(self):
        wd, _ = make_wd()
        await wd.stop()

    async def test_stop_suppresses_dead_task_exception(self):
        wd, _ = make_wd()

        async def boom():
            raise OSError(11, "fork failed")

        wd._task = asyncio.get_running_loop().create_task(boom())
        await asyncio.sleep(0.01)  # let it die
        await wd.stop()  # must not raise

    async def test_disabled_resets_remembered_states(self, monkeypatch):
        wd, app = make_wd({"KLANGKD_RESOURCE_WATCHDOG_ENABLED": "false"})
        wd._states[1] = CRITICAL
        wd._emitted_at[1] = 123.0
        wd._audit_alerted["container_events"] = True
        monkeypatch.setattr(
            "klangk.resource_watchdog.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        await run_briefly(wd, seconds=0.05)
        assert wd._states == {}
        assert wd._emitted_at == {}
        assert wd._audit_alerted == {}

    async def test_enabled_loop_checks_disk(self, monkeypatch):
        wd, app = make_wd({"KLANGKD_RESOURCE_WATCHDOG_POLL_INTERVAL": "0.001"})
        monkeypatch.setattr(
            "klangk.resource_watchdog.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        spy = notifier_spy(app)
        monkeypatch.setattr("os.stat", lambda path: FakeStat(1))
        monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(95.0))
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        # Hermetic: the host's real memory/CPU state must not add
        # events to the disk assertion.
        monkeypatch.setattr(wd, "check_memory", AsyncMock(), raising=True)
        monkeypatch.setattr(wd, "check_cpu", AsyncMock())
        await run_briefly(wd, seconds=0.05)
        assert spy.notify_admins.call_count == 1  # one transition, then quiet
        assert kwargs_of(spy)["detail"]["state"] == CRITICAL

    async def test_sweep_survives_a_surface_failure(self, caplog):
        wd, app = make_wd()
        spy = notifier_spy(app)
        wd.check_disk = AsyncMock(side_effect=RuntimeError("statvfs blew up"))
        wd.check_memory = AsyncMock(
            side_effect=RuntimeError("meminfo blew up")
        )
        wd.check_cpu = AsyncMock(side_effect=RuntimeError("psi blew up"))
        wd.check_audit = Mock(side_effect=RuntimeError("getattr blew up"))
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.sweep()  # must not raise
        for label in ("disk", "memory", "cpu", "audit"):
            assert f"{label} check failed" in caplog.text
        spy.notify_admins.assert_not_called()

    async def test_guarded_cycle_propagates_cancellation(self):
        wd, _ = make_wd()
        wd.sweep = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await wd.guarded_cycle()

    async def test_sweep_propagates_cancellation(self):
        wd, _ = make_wd()
        wd.check_disk = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await wd.sweep()

    async def test_loop_survives_cycle_raise(self, monkeypatch, caplog):
        """A sweep that escapes its guards must not silently kill the
        loop — the next cycle still runs (the #2627 posture)."""
        wd, app = make_wd({"KLANGKD_RESOURCE_WATCHDOG_POLL_INTERVAL": "0.001"})
        monkeypatch.setattr(
            "klangk.resource_watchdog.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        wd.sweep = AsyncMock(side_effect=RuntimeError("bug in sweep"))
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await run_briefly(wd, seconds=0.05)
        assert wd.sweep.await_count > 1
        assert "cycle failed" in caplog.text


class TestDispatchRetry:
    """Undelivered transitions (the notifier throttled the dispatch)
    are retried on later polls until they land — but only while the
    event could ever dispatch (#3206 second and third reviews)."""

    def _wd(self, **notifier_kwargs):
        wd, app = make_wd(notifier=dispatchable_notifier(**notifier_kwargs))
        return wd

    def test_undelivered_transition_is_retried(self, monkeypatch):
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, True])
        wd.step_filesystem(7, "/data", 91.0)  # critical, swallowed
        assert 7 in wd._pending
        wd.step_filesystem(7, "/data", 92.0)  # no transition -> retry lands
        assert calls == ["resource.disk.critical", "resource.disk.critical"]
        assert 7 not in wd._pending

    def test_retry_keeps_pending_until_delivered(self, monkeypatch):
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, False, True])
        wd.step_filesystem(7, "/data", 76.0)  # warn, swallowed
        wd.step_filesystem(7, "/data", 77.0)  # retry, still swallowed
        wd.step_filesystem(7, "/data", 78.0)  # retry lands
        assert len(calls) == 3
        assert 7 not in wd._pending

    def test_no_notifier_means_no_retry(self, monkeypatch):
        """Channels can never dispatch without a notifier: the
        swallowed dispatch is treated as done, not retried every poll
        forever (the third review's noise defect)."""
        wd, _ = make_wd()  # no notifier on the state
        calls = patch_dispatch(monkeypatch, [False, False])
        wd.step_filesystem(7, "/data", 91.0)
        assert 7 not in wd._pending
        wd.step_filesystem(7, "/data", 92.0)
        assert len(calls) == 1  # no retry fired

    def test_channels_off_means_no_retry(self, monkeypatch):
        """The default deployment: a notifier with no channels. The
        transition logs once and is never retried."""
        wd = self._wd(channels=False)
        calls = patch_dispatch(monkeypatch, [False, False])
        wd.step_filesystem(7, "/data", 91.0)
        assert 7 not in wd._pending
        wd.step_filesystem(7, "/data", 92.0)
        assert len(calls) == 1

    def test_allowlist_exclusion_means_no_retry(self, monkeypatch):
        """An operator who removed the disk events from the allowlist
        made an explicit choice; the retry loop must not re-log the
        dispatch attempt every poll against it."""
        wd = self._wd(allowlist=frozenset({"user.create"}))
        calls = patch_dispatch(monkeypatch, [False, False])
        wd.step_filesystem(7, "/data", 91.0)
        assert 7 not in wd._pending
        wd.step_filesystem(7, "/data", 92.0)
        assert len(calls) == 1

    def test_retry_stops_when_allowlist_shrinks_mid_episode(self, monkeypatch):
        """A reload that removes the event mid-episode also ends the
        retry loop (checked at retry time, not just entry time)."""
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, False])
        wd.step_filesystem(7, "/data", 91.0)  # swallowed, retryable
        assert 7 in wd._pending
        wd.app.state.notifier = dispatchable_notifier(
            allowlist=frozenset({"user.create"})
        )
        wd.step_filesystem(7, "/data", 92.0)  # retry, then drop
        assert 7 not in wd._pending
        assert len(calls) == 2

    def test_broken_notifier_probe_means_no_retry(self, monkeypatch):
        """A notifier whose allowlist probe raises is treated as
        undispatchable (the guarded-helper posture — never a crash out
        of the poll loop)."""

        def broken():
            raise RuntimeError("settings gone")

        notifier = types.SimpleNamespace(
            notify_events=broken, channels_configured=lambda: True
        )
        wd, _ = make_wd(notifier=notifier)
        calls = patch_dispatch(monkeypatch, [False, False])
        wd.step_filesystem(7, "/data", 91.0)
        assert 7 not in wd._pending
        wd.step_filesystem(7, "/data", 92.0)
        assert len(calls) == 1

    def test_retry_attempts_log_at_debug(self, monkeypatch, caplog):
        """A retry re-attempt logs at DEBUG, not WARNING — the
        transition already said it, and an edge retried at the poll
        floor inside one throttle window must not repeat the WARNING
        line hundreds of times."""
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, True])
        wd.step_filesystem(7, "/data", 91.0)
        with caplog.at_level(logging.DEBUG, logger="klangk.resource_watchdog"):
            wd.step_filesystem(7, "/data", 92.0)  # the retry
        assert len(calls) == 2
        retries = [r for r in caplog.records if "(retry" in r.message]
        assert len(retries) == 1
        assert retries[0].levelno == logging.DEBUG

    def test_second_recovery_inside_window_lands_via_retry(self, monkeypatch):
        """Two episode ends inside one throttle window: the second
        recovery dispatch is swallowed by the notifier and retried
        until it lands, so the operator's last word is never a stale
        warn."""
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [True, True, True, False, True])
        wd.step_filesystem(7, "/data", 76.0)  # warn delivered
        wd.step_filesystem(7, "/data", 65.0)  # recovered delivered
        wd.step_filesystem(7, "/data", 80.0)  # refill: warn delivered
        wd.step_filesystem(7, "/data", 64.0)  # recovery swallowed
        assert wd._states[7] == OK
        assert 7 in wd._pending
        wd.step_filesystem(7, "/data", 63.0)  # healthy poll -> retry lands
        assert calls.count("resource.disk.recovered") == 3
        assert 7 not in wd._pending

    def test_newer_transition_replaces_pending(self, monkeypatch):
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, True])
        wd.step_filesystem(7, "/data", 76.0)  # warn swallowed -> pending
        wd.step_filesystem(7, "/data", 91.0)  # critical transition delivers
        assert 7 not in wd._pending
        wd.step_filesystem(7, "/data", 92.0)  # no stale retry fires
        assert len(calls) == 2

    async def test_pending_survives_unmeasurable_polls(self, monkeypatch):
        """A swallowed transition whose path goes unmeasurable waits:
        sweeps skip the device entirely (no transition, no retry), and
        the retry resumes when the path measures again."""
        wd, app = make_wd(notifier=dispatchable_notifier())
        calls = patch_dispatch(monkeypatch, [False, True])
        wd.step_filesystem(7, "/data", 91.0)  # critical, swallowed

        def boom(path):
            raise OSError(5, "I/O error")

        monkeypatch.setattr("os.statvfs", boom)
        monkeypatch.setattr(
            wd, "resolve_graph_root", AsyncMock(return_value=None)
        )
        await wd.check_disk()  # the path is unmeasurable this poll
        assert 7 in wd._pending
        assert len(calls) == 1
        monkeypatch.setattr("os.stat", lambda path: FakeStat(7))
        monkeypatch.setattr("os.statvfs", lambda path: FakeVfs(95.0))
        await wd.check_disk()  # measurable again: the retry lands
        assert 7 not in wd._pending
        assert len(calls) == 2

    def test_refresh_failure_requeues_pending(self, monkeypatch):
        """A due refresh that is itself swallowed must re-set the
        pending entry (not lose it) — the next poll's retry still
        owns the episode edge."""
        wd = self._wd()
        calls = patch_dispatch(monkeypatch, [False, False, True])
        wd.step_filesystem(7, "/data", 76.0)  # warn swallowed -> pending
        wd._emitted_at[7] = time.monotonic() - REFRESH_SECONDS - 1
        wd.step_filesystem(7, "/data", 77.0)  # refresh fires, swallowed
        assert 7 in wd._pending
        wd.step_filesystem(7, "/data", 78.0)  # retry lands
        assert 7 not in wd._pending
        assert len(calls) == 3


class TestReconfigure:
    def _wd_with_state(self):
        wd, _ = make_wd()
        wd._states[1] = CRITICAL
        wd._emitted_at[1] = 123.0
        wd._unmeasurable.add("unmeasurable:disk usage of /gone")
        wd._audit_counts["container_events"] = 5
        return wd

    def test_unrelated_reload_keeps_disk_states(self):
        """A reload that did not move a threshold must not re-alert
        already-degraded filesystems: states, emission clocks, and
        pending retries survive; audit baselines always survive (the
        third review's SIGHUP re-alert defect)."""
        wd = self._wd_with_state()
        wd._pending[1] = CRITICAL
        new_app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wd.reconfigure(new_app)
        assert wd.app is new_app
        assert wd._states == {1: CRITICAL}
        assert wd._emitted_at == {1: 123.0}
        assert wd._pending == {1: CRITICAL}
        assert wd._unmeasurable == set()
        assert wd._audit_counts == {"container_events": 5}

    def test_production_shape_reload_same_app(self):
        """apply_reloaded_settings swaps app.state.settings in place
        BEFORE reconfigure(app) runs on the same app — the comparison
        must work in that shape (an instance snapshot, not old-vs-new
        off the app, which is already new-vs-new by then)."""
        wd, app = make_wd()
        wd._states[1] = CRITICAL
        # Same app, thresholds swapped in place (the production shape).
        app.state.settings = make_settings(
            {"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "80"}
        )
        wd.reconfigure(app)
        assert wd._states == {}
        # An unrelated in-place swap keeps the states (warn stays 80
        # — only the interval moves).
        wd._states[2] = WARN
        app.state.settings = make_settings(
            {
                "KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "80",
                "KLANGKD_RESOURCE_WATCHDOG_POLL_INTERVAL": "30",
            }
        )
        wd.reconfigure(app)
        assert wd._states == {2: WARN}

    def test_threshold_change_re_evaluates_fresh(self):
        """A reload that moved a threshold resets the disk states —
        every filesystem is re-classified from ``ok`` against the new
        thresholds."""
        wd = self._wd_with_state()
        new_app = types.SimpleNamespace(
            state=types.SimpleNamespace(
                settings=make_settings(
                    {"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "80"}
                )
            )
        )
        wd.reconfigure(new_app)
        assert wd.app is new_app
        assert wd._states == {}
        assert wd._emitted_at == {}
        assert wd._pending == {}
        assert wd._audit_counts == {"container_events": 5}


# --- lifecycle wiring ---


class TestLifecycleWiring:
    def _workers_app(self, with_watchdog=True):
        state = types.SimpleNamespace()
        for name in (
            "consent_sweeper",
            "inactivity_sweeper",
            "session_idle_monitor",
            "consent_coordinator",
            "consent_deciders",
            "sidecar_connections",
            "memory_evictor",
            "proxy_watchdog",
            "server_scheduler",
        ):
            setattr(
                state,
                name,
                types.SimpleNamespace(start=Mock(), stop=AsyncMock()),
            )
        if with_watchdog:
            state.resource_watchdog = types.SimpleNamespace(
                start=Mock(), stop=AsyncMock()
            )
        return types.SimpleNamespace(state=state)

    async def test_workers_start_and_stop_the_watchdog(self):
        app = self._workers_app()
        lifecycle.start_background_workers(app)
        app.state.resource_watchdog.start.assert_called_once()
        await lifecycle.stop_background_workers(app)
        app.state.resource_watchdog.stop.assert_awaited_once()

    async def test_absent_watchdog_is_skipped(self):
        app = self._workers_app(with_watchdog=False)
        lifecycle.start_background_workers(app)  # no AttributeError
        await lifecycle.stop_background_workers(app)

    def test_subsystems_list_includes_the_watchdog(self):
        assert (
            "resource_watchdog" in lifecycle.Lifecycle._RECONFIGURE_SUBSYSTEMS
        )


# --- settings ---


class TestSettings:
    def test_defaults(self):
        settings = make_settings({})
        assert settings.resource_watchdog_enabled is True
        assert settings.resource_watchdog_poll_interval == 60.0
        assert settings.disk_watchdog_warn_percent == 75.0
        assert settings.disk_watchdog_critical_percent == 90.0
        assert settings.disk_watchdog_paths is None
        assert settings.memory_watchdog_enabled is True
        assert settings.memory_watchdog_warn_percent == 80.0
        assert settings.memory_watchdog_critical_percent == 90.0
        assert settings.cpu_watchdog_enabled is True
        assert settings.cpu_watchdog_warn_percent == 30.0
        assert settings.cpu_watchdog_critical_percent == 60.0

    def test_paths_comma_separated_env(self):
        settings = make_settings(
            {"KLANGKD_DISK_WATCHDOG_PATHS": "/srv/a, /srv/b"}
        )
        assert settings.disk_watchdog_paths == ["/srv/a", "/srv/b"]

    def test_paths_empty_string_is_none(self):
        settings = make_settings({"KLANGKD_DISK_WATCHDOG_PATHS": " "})
        assert settings.disk_watchdog_paths is None

    def test_float_coercion_from_string(self):
        settings = make_settings(
            {
                "KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "80",
                "KLANGKD_DISK_WATCHDOG_CRITICAL_PERCENT": "95.5",
                "KLANGKD_RESOURCE_WATCHDOG_POLL_INTERVAL": "30",
            }
        )
        assert settings.disk_watchdog_warn_percent == 80.0
        assert settings.disk_watchdog_critical_percent == 95.5
        assert settings.resource_watchdog_poll_interval == 30.0

    def test_equal_thresholds_are_tolerated(self):
        settings = make_settings(
            {
                "KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "90",
                "KLANGKD_DISK_WATCHDOG_CRITICAL_PERCENT": "90",
            }
        )
        assert settings.disk_watchdog_critical_percent == 90.0

    def test_warn_at_the_gap_floor_is_accepted(self):
        """The smallest legal warn — exactly the hysteresis gap — puts
        the recovery floor at 0 (emptied filesystem recovers)."""
        settings = make_settings({"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "5"})
        assert settings.disk_watchdog_warn_percent == 5.0

    @pytest.mark.parametrize(
        "env",
        [
            # critical below warn
            {
                "KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "90",
                "KLANGKD_DISK_WATCHDOG_CRITICAL_PERCENT": "75",
            },
            # warn below the hysteresis gap — recovery would be
            # unreachable (the floor would sit below 0% usage)
            {"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "4"},
            {"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "0"},
            {"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "101"},
            {"KLANGKD_DISK_WATCHDOG_CRITICAL_PERCENT": "101"},
            # The same rules hold for the memory and CPU thresholds.
            {
                "KLANGKD_MEMORY_WATCHDOG_WARN_PERCENT": "90",
                "KLANGKD_MEMORY_WATCHDOG_CRITICAL_PERCENT": "80",
            },
            {"KLANGKD_MEMORY_WATCHDOG_WARN_PERCENT": "3"},
            {
                "KLANGKD_CPU_WATCHDOG_WARN_PERCENT": "70",
                "KLANGKD_CPU_WATCHDOG_CRITICAL_PERCENT": "50",
            },
            {"KLANGKD_CPU_WATCHDOG_CRITICAL_PERCENT": "101"},
        ],
    )
    def test_bad_thresholds_abort_startup(self, env):
        with pytest.raises(ValidationError):
            make_settings(env)

    def test_memory_and_cpu_float_coercion_from_string(self):
        settings = make_settings(
            {
                "KLANGKD_MEMORY_WATCHDOG_WARN_PERCENT": "85.5",
                "KLANGKD_MEMORY_WATCHDOG_CRITICAL_PERCENT": "95",
                "KLANGKD_CPU_WATCHDOG_WARN_PERCENT": "40",
                "KLANGKD_CPU_WATCHDOG_CRITICAL_PERCENT": "75.5",
                "KLANGKD_CPU_WATCHDOG_ENABLED": "false",
            }
        )
        assert settings.memory_watchdog_warn_percent == 85.5
        assert settings.memory_watchdog_critical_percent == 95.0
        assert settings.cpu_watchdog_warn_percent == 40.0
        assert settings.cpu_watchdog_critical_percent == 75.5
        assert settings.cpu_watchdog_enabled is False


class TestNotifierRegistration:
    def test_disk_events_are_in_the_default_allowlist(self):
        assert "resource.disk.warn" in DEFAULT_NOTIFY_EVENTS
        assert "resource.disk.critical" in DEFAULT_NOTIFY_EVENTS
        assert "resource.disk.recovered" in DEFAULT_NOTIFY_EVENTS

    def test_memory_and_cpu_events_are_in_the_default_allowlist(self):
        for name in (
            "resource.memory.warn",
            "resource.memory.critical",
            "resource.memory.recovered",
            "resource.cpu.warn",
            "resource.cpu.critical",
            "resource.cpu.recovered",
        ):
            assert name in DEFAULT_NOTIFY_EVENTS
            assert THROTTLE_SECONDS[name] == 300

    def test_disk_events_are_throttled(self):
        for name in (
            "resource.disk.warn",
            "resource.disk.critical",
            "resource.disk.recovered",
        ):
            assert THROTTLE_SECONDS[name] == 300

    def test_audit_failure_event_name_registered(self):
        assert "audit.failure" in DEFAULT_NOTIFY_EVENTS


# --- PSI parsing (#3309) ---


class TestPsiParsing:
    PSI_TEXT = (
        "some avg10=0.00 avg60=1.25 avg300=0.10 total=123456789\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )

    def test_reads_some_avg60(self):
        assert parse_cpu_psi(self.PSI_TEXT) == pytest.approx(1.25)

    def test_missing_some_line_raises(self):
        with pytest.raises(ValueError):
            parse_cpu_psi("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")

    def test_unparseable_avg60_raises(self):
        """A truncated or non-finite file must not read as zero
        pressure (a nan would poison every comparison)."""
        with pytest.raises(ValueError):
            parse_cpu_psi("some avg10=0.00 avg60=nan avg300=0.00\n")
        with pytest.raises(ValueError):
            parse_cpu_psi("some avg10=0.00 avg60=inf avg300=0.00\n")

    def test_field_found_and_missing(self):
        assert psi_field("some avg60=42.5", "avg60") == pytest.approx(42.5)
        assert psi_field("some avg60=42.5", "avg300") is None
        assert psi_field("some avg60=bogus", "avg60") is None


# --- memory surface (#3309) ---


def patch_memory_fraction(monkeypatch, fraction_or_error):
    """Script ResourceWatchdog._measure_memory_fraction: a float is
    returned availability, an exception instance is raised."""
    if isinstance(fraction_or_error, BaseException):
        mock = AsyncMock(side_effect=fraction_or_error)
    else:
        mock = AsyncMock(return_value=fraction_or_error)
    monkeypatch.setattr(
        "klangk.resource_watchdog.ResourceWatchdog._measure_memory_fraction",
        mock,
    )
    return mock


class TestMemorySurface:
    def _wd(self, env=None):
        wd, app = make_wd(env)
        return wd, notifier_spy(app)

    async def test_ok_to_warn_emits_warn(self, monkeypatch, caplog):
        patch_memory_fraction(monkeypatch, 0.15)  # 85% used; warn at 80
        wd, spy = self._wd()
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_memory()
        assert spy.notify_admins.call_count == 1
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "resource.memory.warn"
        assert kwargs["detail"]["metric"] == "memory"
        assert kwargs["detail"]["usage_percent"] == 85.0
        assert kwargs["detail"]["state"] == WARN
        assert "Memory usage 85.0%: warn" in caplog.text

    async def test_warn_to_critical_to_recovered(self, monkeypatch):
        patch_memory_fraction(monkeypatch, 0.15)
        wd, spy = self._wd()
        await wd.check_memory()  # -> warn
        patch_memory_fraction(monkeypatch, 0.05)  # 95% used -> critical
        await wd.check_memory()
        patch_memory_fraction(monkeypatch, 0.30)  # 70% -> recovered
        await wd.check_memory()
        events = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert events == [
            "resource.memory.warn",
            "resource.memory.critical",
            "resource.memory.recovered",
        ]

    async def test_hovering_inside_the_band_is_quiet(self, monkeypatch):
        patch_memory_fraction(monkeypatch, 0.15)
        wd, spy = self._wd()
        await wd.check_memory()  # -> warn
        spy.notify_admins.reset_mock()
        patch_memory_fraction(monkeypatch, 0.22)  # 78%: inside 75-80 band
        await wd.check_memory()
        spy.notify_admins.assert_not_called()
        assert wd._states[MEMORY_KEY] == WARN

    async def test_unmeasurable_warns_once_rearms_on_recovery(
        self, monkeypatch, caplog
    ):
        wd, spy = self._wd()
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            patch_memory_fraction(monkeypatch, OSError("no meminfo"))
            await wd.check_memory()
            await wd.check_memory()  # same condition: warned once
        warnings = [
            r
            for r in caplog.records
            if "cannot measure host memory" in r.message
        ]
        assert len(warnings) == 1
        spy.notify_admins.assert_not_called()
        # Measurable again: the warning re-arms (a later failure is a
        # new episode).
        patch_memory_fraction(monkeypatch, 0.05)
        await wd.check_memory()
        assert spy.notify_admins.call_count == 1  # critical transition
        patch_memory_fraction(monkeypatch, OSError("gone again"))
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_memory()
        assert (
            len(
                [
                    r
                    for r in caplog.records
                    if "cannot measure host memory" in r.message
                ]
            )
            == 2
        )

    async def test_disabled_forgets_state_and_skips_measurement(
        self, monkeypatch
    ):
        measure = patch_memory_fraction(monkeypatch, 0.05)
        wd, spy = self._wd({"KLANGKD_MEMORY_WATCHDOG_ENABLED": "false"})
        wd._states[MEMORY_KEY] = CRITICAL
        await wd.check_memory()
        measure.assert_not_called()
        spy.notify_admins.assert_not_called()
        assert MEMORY_KEY not in wd._states

    async def test_live_thresholds_from_settings(self, monkeypatch):
        patch_memory_fraction(monkeypatch, 0.15)
        wd, spy = self._wd(
            {
                "KLANGKD_MEMORY_WATCHDOG_WARN_PERCENT": "95",
                "KLANGKD_MEMORY_WATCHDOG_CRITICAL_PERCENT": "96",
            }
        )
        await wd.check_memory()
        spy.notify_admins.assert_not_called()  # 85% under a 95% warn


# --- CPU surface (#3309) ---


def patch_cpu_psi(monkeypatch, psi_or_error):
    """Script ResourceWatchdog._read_cpu_psi: a str is the PSI file
    content, an exception instance is raised."""
    if isinstance(psi_or_error, BaseException):
        mock = AsyncMock(side_effect=psi_or_error)
    else:
        mock = AsyncMock(return_value=psi_or_error)
    monkeypatch.setattr(
        "klangk.resource_watchdog.ResourceWatchdog._read_cpu_psi", mock
    )
    return mock


class TestCpuSurface:
    def _wd(self, env=None):
        wd, app = make_wd(env)
        return wd, notifier_spy(app)

    async def test_warn_critical_recovered(self, monkeypatch, caplog):
        wd, spy = self._wd()
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            patch_cpu_psi(monkeypatch, "some avg60=35.0\n")
            await wd.check_cpu()  # warn at 30
            patch_cpu_psi(monkeypatch, "some avg60=65.0\n")
            await wd.check_cpu()  # critical at 60
            patch_cpu_psi(monkeypatch, "some avg60=10.0\n")
            await wd.check_cpu()  # recovered (below 25)
        events = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert events == [
            "resource.cpu.warn",
            "resource.cpu.critical",
            "resource.cpu.recovered",
        ]
        assert "CPU pressure 35.0% (PSI some avg60): warn" in caplog.text
        assert kwargs_of(spy, 1)["detail"]["psi_avg60_percent"] == 65.0

    async def test_missing_psi_disables_loudly_once(self, monkeypatch, caplog):
        """A kernel/VM without PSI disables the CPU check with one
        logged warning — never a bogus zero-pressure read, never a
        dead loop."""
        wd, spy = self._wd()
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            patch_cpu_psi(monkeypatch, OSError(2, "No such file"))
            await wd.check_cpu()
            await wd.check_cpu()
        warnings = [
            r
            for r in caplog.records
            if "cannot measure CPU pressure (PSI)" in r.message
        ]
        assert len(warnings) == 1
        spy.notify_admins.assert_not_called()
        assert CPU_KEY not in wd._states
        # PSI appearing re-arms the check.
        patch_cpu_psi(monkeypatch, "some avg60=70.0\n")
        await wd.check_cpu()
        assert wd._states[CPU_KEY] == CRITICAL

    async def test_disabled_forgets_state_and_skips_read(self, monkeypatch):
        read = patch_cpu_psi(monkeypatch, "some avg60=70.0\n")
        wd, spy = self._wd({"KLANGKD_CPU_WATCHDOG_ENABLED": "false"})
        wd._states[CPU_KEY] = WARN
        await wd.check_cpu()
        read.assert_not_called()
        spy.notify_admins.assert_not_called()
        assert CPU_KEY not in wd._states


# --- macOS: the podman machine is where containers run (#3309) ---


class FakePodman:
    """A podman stub whose ``machine ssh -- cat /proc/<name>`` answers
    from a scripted {name: (rc, stdout)} table."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def run(self, cmd, check=False, timeout=None):
        self.calls.append(cmd)
        name = cmd[cmd.index("cat") + 1].removeprefix("/proc/")
        rc, out = self.answers.get(name, (125, ""))
        return rc, out, ""


MEMINFO_VM = "MemTotal:       2048000 kB\nMemAvailable:    204800 kB\n"
PSI_VM = "some avg10=0.00 avg60=45.0 avg300=0.00 total=0\n"


class TestPodmanMachinePath:
    def _wd(self, answers):
        podman = FakePodman(answers)
        wd, app = make_wd(podman=podman)
        notifier_spy(app)
        return wd, podman

    def _on_macos(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.resource_watchdog.platform.system",
            lambda: "Darwin",
        )

    async def test_memory_measures_the_vm_meminfo(self, monkeypatch, caplog):
        self._on_macos(monkeypatch)
        wd, podman = self._wd({"meminfo": (0, MEMINFO_VM)})
        spy = wd.app.state.notifier
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_memory()
        # 10% available = 90% used: critical transition.
        assert spy.notify_admins.call_count == 1
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "resource.memory.critical"
        assert kwargs["detail"]["usage_percent"] == pytest.approx(90.0)
        assert podman.calls == [
            ["machine", "ssh", "--", "cat", "/proc/meminfo"]
        ]
        assert "Memory usage 90.0%: critical" in caplog.text

    async def test_cpu_measures_the_vm_psi(self, monkeypatch):
        self._on_macos(monkeypatch)
        wd, podman = self._wd({"pressure/cpu": (0, PSI_VM)})
        spy = wd.app.state.notifier
        await wd.check_cpu()
        assert spy.notify_admins.call_args.args[0] == "resource.cpu.warn"
        assert podman.calls == [
            ["machine", "ssh", "--", "cat", "/proc/pressure/cpu"]
        ]

    async def test_stopped_machine_warns_once_per_metric(
        self, monkeypatch, caplog
    ):
        """A stopped podman machine (ssh exits non-zero) is
        unmeasurable: one warning per metric, no events, and the loop
        keeps running."""
        self._on_macos(monkeypatch)
        wd, podman = self._wd({})  # every cat fails
        spy = wd.app.state.notifier
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_memory()
            await wd.check_cpu()
            await wd.check_memory()
        warnings = sorted(
            r.message.split("cannot measure ")[1].split(" (")[0]
            for r in caplog.records
            if "cannot measure" in r.message
        )
        assert warnings == ["CPU pressure", "host memory"]
        spy.notify_admins.assert_not_called()

    async def test_no_podman_state_is_unmeasurable(self, monkeypatch, caplog):
        self._on_macos(monkeypatch)
        wd, _ = make_wd()  # no podman on the state
        with caplog.at_level(
            logging.WARNING, logger="klangk.resource_watchdog"
        ):
            await wd.check_memory()
        assert "cannot measure host memory" in caplog.text

    async def test_linux_memory_uses_the_local_measurement(self, monkeypatch):
        """On a Linux host the local /proc measurement runs (the
        eviction subsystem's function, imported at call time) — no
        podman subprocess."""
        monkeypatch.setattr(
            "klangk.resource_watchdog.platform.system", lambda: "Linux"
        )
        monkeypatch.setattr(
            "klangk.container.eviction.measure_available_fraction",
            AsyncMock(return_value=0.05),
        )
        wd, app = make_wd(podman=FakePodman({}))
        spy = notifier_spy(app)
        await wd.check_memory()
        assert spy.notify_admins.call_args.args[0] == (
            "resource.memory.critical"
        )
        assert app.state.podman.calls == []

    async def test_linux_cpu_reads_the_local_psi_file(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "klangk.resource_watchdog.platform.system", lambda: "Linux"
        )
        psi = tmp_path / "psi"
        psi.write_text(PSI_VM)
        monkeypatch.setattr(
            "klangk.resource_watchdog.read_local_text",
            lambda path: (
                PSI_VM
                if path == "/proc/pressure/cpu"
                else read_local_text(path)
            ),
        )
        wd, app = make_wd(podman=FakePodman({}))
        spy = notifier_spy(app)
        await wd.check_cpu()
        assert spy.notify_admins.call_args.args[0] == "resource.cpu.warn"
        assert app.state.podman.calls == []

    def test_read_local_text_reads_a_file(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("hello")
        assert read_local_text(str(f)) == "hello"


class TestReconfigureThresholdReset:
    """Only the metric family whose thresholds changed resets on a
    SIGHUP reload — another family's degraded state survives without
    re-alerting (the notifier's throttle clocks reset on reload too,
    so a reset would re-deliver a duplicate)."""

    def test_cpu_threshold_change_leaves_disk_and_memory(self):
        wd, _ = make_wd()
        wd._states[7] = CRITICAL
        wd._states[MEMORY_KEY] = WARN
        _, other = make_wd({"KLANGKD_CPU_WATCHDOG_WARN_PERCENT": "50"})
        wd.reconfigure(other)
        assert wd._states[7] == CRITICAL
        assert wd._states[MEMORY_KEY] == WARN
        assert CPU_KEY not in wd._states

    def test_disk_threshold_change_resets_disk_only(self):
        wd, _ = make_wd()
        wd._states[7] = CRITICAL
        wd._states[MEMORY_KEY] = WARN
        _, other = make_wd({"KLANGKD_DISK_WATCHDOG_WARN_PERCENT": "50"})
        wd.reconfigure(other)
        assert 7 not in wd._states
        assert wd._states[MEMORY_KEY] == WARN

    def test_memory_threshold_change_resets_memory_only(self):
        wd, _ = make_wd()
        wd._states[7] = CRITICAL
        wd._states[MEMORY_KEY] = WARN
        _, other = make_wd({"KLANGKD_MEMORY_WATCHDOG_WARN_PERCENT": "70"})
        wd.reconfigure(other)
        assert wd._states[7] == CRITICAL
        assert MEMORY_KEY not in wd._states

    def test_unrelated_reload_resets_nothing(self):
        wd, _ = make_wd()
        wd._states[7] = CRITICAL
        wd._states[MEMORY_KEY] = WARN
        wd._states[CPU_KEY] = WARN
        _, other = make_wd()  # identical thresholds
        wd.reconfigure(other)
        assert wd._states[7] == CRITICAL
        assert wd._states[MEMORY_KEY] == WARN
        assert wd._states[CPU_KEY] == WARN
