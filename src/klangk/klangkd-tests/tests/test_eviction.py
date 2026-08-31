"""Tests for host memory-pressure eviction (#2526).

Covers the ``/proc/meminfo`` reader, the sustain/hysteresis state
machine, the connections-aware least-recently-active victim choice, the
graceful stop path, the ``workspace_evicted`` broadcast, settings
validation, and the ``run`` loop's disabled/unreadable branches.
"""

import asyncio
import logging
import time
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk import container
from klangk.model.container_events import CAUSE_EVICTION
from klangk.container.eviction import (
    MemoryPressureEvictor,
    available_fraction,
    cgroup_memory_headroom,
    macos_available_fraction,
    measure_available_fraction,
    parse_vm_stat,
    read_meminfo,
    vm_stat_page_size,
)
from klangk.wshandler.safe_websocket import WS_ERRORS
from _helpers import make_settings

# A realistic /proc/meminfo excerpt: ~4.7% of 32 GiB available.
_MEMINFO = """\
MemTotal:        33554432 kB
MemFree:          1048576 kB
MemAvailable:      1572864 kB
Cached:           4194304 kB
SwapTotal:        8388608 kB
"""


async def _boom(evictor) -> None:
    """Pretend to be a dead run task: raise immediately."""
    raise OSError(11, "fork failed")


def _make_app_state(env=None):
    """Minimal app_state for eviction tests (settings + sockets + registry)."""
    settings = make_settings(env)
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    from klangk.podman import Podman
    from klangk.wshandler.session import WebSocketState
    from _helpers import wire_db_and_model

    app.state.podman = Podman(app)
    app.state.sockets = WebSocketState(app)
    app.state.container_registry = container.ContainerRegistry(app)
    # #1714: the evicted broadcast ACL-checks recipients, so the eviction
    # paths need the DB/model/acl wiring the real app has.
    wire_db_and_model(app)
    return app


class TestMeminfo:
    def test_read_meminfo_parses_kb_to_bytes(self, tmp_path):
        path = tmp_path / "meminfo"
        path.write_text(_MEMINFO)
        info = read_meminfo(str(path))
        assert info["MemTotal"] == 33554432 * 1024
        assert info["MemAvailable"] == 1572864 * 1024
        # Non-kB / malformed lines are skipped, not fatal.
        assert "SwapTotal" in info

    def test_read_meminfo_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            read_meminfo(str(tmp_path / "nope"))

    def test_available_fraction_prefers_memavailable(self):
        info = {
            "MemTotal": 100 * 1024,
            "MemAvailable": 8 * 1024,
            "MemFree": 2 * 1024,
            "Cached": 3 * 1024,
        }
        assert available_fraction(info) == pytest.approx(0.08)

    def test_available_fraction_falls_back_without_memavailable(self):
        # Old kernels: MemFree + Cached as the approximation.
        info = {
            "MemTotal": 100 * 1024,
            "MemFree": 2 * 1024,
            "Cached": 3 * 1024,
        }
        assert available_fraction(info) == pytest.approx(0.05)

    def test_available_fraction_zero_total_raises(self):
        with pytest.raises(ValueError):
            available_fraction({"MemTotal": 0, "MemAvailable": 1})


class TestCgroupHeadroom:
    """The Docker case: a cgroup limit tighter than host memory (#2526)."""

    def _write_cgroup(self, root, *, v2=None, v1=None):
        """v2: dict of cgroup-v2 files under root; v1: under root/memory."""
        if v2 is not None:
            for name, text in v2.items():
                path = root / name
                path.write_text(text)
        if v1 is not None:
            sub = root / "memory"
            sub.mkdir(exist_ok=True)
            for name, text in v1.items():
                (sub / name).write_text(text)

    def test_v2_finite_limit_and_working_set(self, tmp_path):
        self._write_cgroup(
            tmp_path,
            v2={
                "memory.max": "2147483648",  # 2g
                "memory.current": "1610612736",  # 1.5g
                "memory.stat": "anon 100\ninactive_file 268435456\n",  # 256m
            },
        )
        limit, working_set = cgroup_memory_headroom(str(tmp_path))
        assert limit == 2147483648
        assert working_set == 1610612736 - 268435456

    def test_v2_max_means_no_limit(self, tmp_path):
        self._write_cgroup(
            tmp_path, v2={"memory.max": "max", "memory.current": "123"}
        )
        assert cgroup_memory_headroom(str(tmp_path)) is None

    def test_v1_finite_limit_and_working_set(self, tmp_path):
        self._write_cgroup(
            tmp_path,
            v1={
                "memory.limit_in_bytes": "1073741824",  # 1g
                "memory.usage_in_bytes": "805306368",  # 768m
                "memory.stat": "total_inactive_file 134217728\n",  # 128m
            },
        )
        limit, working_set = cgroup_memory_headroom(str(tmp_path))
        assert limit == 1073741824
        assert working_set == 805306368 - 134217728

    def test_v1_sentinel_limit_means_no_limit(self, tmp_path):
        self._write_cgroup(
            tmp_path,
            v1={
                "memory.limit_in_bytes": "9223372036854771712",
                "memory.usage_in_bytes": "123",
            },
        )
        assert cgroup_memory_headroom(str(tmp_path)) is None

    def test_v1_without_stat_file_treats_cache_as_zero(self, tmp_path):
        # Unreadable memory.stat degrades to working set == usage.
        self._write_cgroup(
            tmp_path,
            v1={
                "memory.limit_in_bytes": "1073741824",
                "memory.usage_in_bytes": "536870912",
            },
        )
        limit, working_set = cgroup_memory_headroom(str(tmp_path))
        assert working_set == 536870912

    def test_v2_zero_limit_is_ignored(self, tmp_path):
        self._write_cgroup(
            tmp_path, v2={"memory.max": "0", "memory.current": "0"}
        )
        assert cgroup_memory_headroom(str(tmp_path)) is None

    def test_missing_files_mean_no_limit(self, tmp_path):
        assert cgroup_memory_headroom(str(tmp_path / "absent")) is None

    def test_working_set_over_limit_pins_headroom_to_zero(self, tmp_path):
        # Over the ceiling (accounted with lag): no headroom left.
        self._write_cgroup(
            tmp_path,
            v2={
                "memory.max": "1000000",
                "memory.current": "1500000",
                "memory.stat": "inactive_file 0\n",
            },
        )
        limit, working_set = cgroup_memory_headroom(str(tmp_path))
        assert working_set == limit

    def test_v2_wins_over_v1_when_both_readable(self, tmp_path):
        self._write_cgroup(
            tmp_path,
            v2={"memory.max": "1000000", "memory.current": "0"},
            v1={
                "memory.limit_in_bytes": "9223372036854771712",
                "memory.usage_in_bytes": "0",
            },
        )
        limit, _ = cgroup_memory_headroom(str(tmp_path))
        assert limit == 1000000


class TestMacOsMeasurement:
    _VM_STAT = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                               1000.\n"
        "Pages inactive:                           2000.\n"
        "Pages speculative:                         500.\n"
        "no colon garbage line 99999\n"
    )

    def test_parse_vm_stat_basic(self):
        total = 16384 * 10000
        # (1000 + 2000 + 500) pages of 16384 bytes over 10000 pages;
        # the colon-less garbage line is skipped.
        assert parse_vm_stat(self._VM_STAT, 16384, total) == pytest.approx(
            0.35
        )

    def test_parse_vm_stat_capped_at_one(self):
        out = (
            "Pages free: 100000.\n"
            "Pages inactive: 200000.\n"
            "Pages speculative: 50000.\n"
        )
        assert parse_vm_stat(out, 4096, 4096 * 1000) == 1.0

    def test_parse_vm_stat_missing_counters_raises(self):
        with pytest.raises(ValueError):
            parse_vm_stat("Pages free: 1.\n", 4096, 4096)

    def test_parse_vm_stat_nonpositive_size_raises(self):
        with pytest.raises(ValueError):
            parse_vm_stat(self._VM_STAT, 0, 16384 * 10000)

    def test_vm_stat_page_size_from_header(self):
        assert vm_stat_page_size(self._VM_STAT) == 16384

    def test_vm_stat_page_size_raises_without_header(self):
        # Unmeasurable is the safe failure: a 4096 guess on a 16 KiB-page
        # host would mis-state availability 4× in the unsafe direction.
        with pytest.raises(ValueError):
            vm_stat_page_size("")
        with pytest.raises(ValueError):
            vm_stat_page_size("no header at all\n")

    def test_vm_stat_page_size_malformed_header_raises(self):
        out = (
            "Mach Virtual Memory Statistics: (page size of zzz bytes)\n"
            "Pages free: 1.\n"
        )
        with pytest.raises(ValueError):
            vm_stat_page_size(out)

    async def test_macos_fraction_with_injected_runner(self):
        total = 16384 * 10000

        async def fake_runner(*cmd: str) -> str:
            assert cmd == ("sysctl", "-n", "hw.memsize") or cmd == ("vm_stat",)
            if cmd[0] == "sysctl":
                return str(total)
            return self._VM_STAT

        assert await macos_available_fraction(
            runner=fake_runner
        ) == pytest.approx(0.35)

    async def test_macos_fraction_default_runner_is_run_command(
        self, monkeypatch
    ):
        total = 16384 * 10000

        async def fake_run(*cmd: str) -> str:
            return str(total) if cmd[0] == "sysctl" else self._VM_STAT

        monkeypatch.setattr("klangk.container.eviction.run_command", fake_run)
        assert await macos_available_fraction() == pytest.approx(0.35)

    async def test_run_command_success_and_failure(self):
        from klangk.container.eviction import run_command

        assert (await run_command("true")).strip() == ""
        with pytest.raises(OSError):
            await run_command("false")


class TestMeasureAvailableFraction:
    """The platform dispatcher combining meminfo + cgroup headroom.

    Every test here pins ``platform.system`` to "Linux": the dispatcher
    routes Darwin hosts to vm_stat/sysctl regardless of the patched
    meminfo/cgroup functions, so unpinned tests would measure the real
    host on macOS runners (#2627 CI).
    """

    @pytest.fixture(autouse=True)
    def _pin_linux(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.container.eviction.platform.system", lambda: "Linux"
        )

    async def test_no_cgroup_uses_meminfo(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(_MEMINFO)
        monkeypatch.setattr(
            "klangk.container.eviction.read_meminfo",
            lambda path="/proc/meminfo": read_meminfo(str(meminfo)),
        )
        monkeypatch.setattr(
            "klangk.container.eviction.cgroup_memory_headroom", lambda: None
        )
        assert await measure_available_fraction() == pytest.approx(
            1572864 / 33554432
        )

    async def test_cgroup_more_pressured_wins(self, tmp_path, monkeypatch):
        # Host healthy (50%), container at its 2g ceiling (~2.5% free).
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 100000 kB\nMemAvailable: 50000 kB\n")
        monkeypatch.setattr(
            "klangk.container.eviction.read_meminfo",
            lambda path="/proc/meminfo": read_meminfo(str(meminfo)),
        )
        monkeypatch.setattr(
            "klangk.container.eviction.cgroup_memory_headroom",
            lambda: (2 * 1024**3, int(1.95 * 1024**3)),
        )
        assert await measure_available_fraction() == pytest.approx(
            (2 * 1024**3 - int(1.95 * 1024**3)) / (2 * 1024**3)
        )

    async def test_meminfo_more_pressured_wins(self, tmp_path, monkeypatch):
        # Host at 3%, container roomy (40% free).
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 100000 kB\nMemAvailable: 3000 kB\n")
        monkeypatch.setattr(
            "klangk.container.eviction.read_meminfo",
            lambda path="/proc/meminfo": read_meminfo(str(meminfo)),
        )
        monkeypatch.setattr(
            "klangk.container.eviction.cgroup_memory_headroom",
            lambda: (1024, 614),
        )
        assert await measure_available_fraction() == pytest.approx(0.03)

    async def test_darwin_dispatches_to_macos(self, monkeypatch):
        async def fake_macos() -> float:
            return 0.42

        monkeypatch.setattr(
            "klangk.container.eviction.platform.system",
            lambda: "Darwin",
        )
        monkeypatch.setattr(
            "klangk.container.eviction.macos_available_fraction",
            fake_macos,
        )
        assert await measure_available_fraction() == pytest.approx(0.42)


class TestEvictOne:
    def _evictor(self, env=None):
        app = _make_app_state(env)
        return app, MemoryPressureEvictor(app)

    def _tracked(self, registry, ws_id, cid, idle_for):
        registry.track_activity(cid, ws_id)
        registry.states[ws_id].last_activity = time.time() - idle_for
        return registry.states[ws_id]

    async def test_evicts_least_recently_active_first(self):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-old", "cid-old", 500)
        self._tracked(registry, "ws-new", "cid-new", 10)
        killed = AsyncMock()
        stopped = AsyncMock()
        registry.notify_workspace_killed = killed
        registry.stop_and_remove_container = stopped

        assert await evictor.evict_one(0.03) is True
        # Victim is the idle-longest workspace; kill notification precedes
        # the stop (the death frame needs live registry state). The
        # notification names the dead container (#331 re-bind guard).
        killed.assert_awaited_once_with("ws-old", container_id="cid-old")
        stopped.assert_awaited_once_with(
            "cid-old", workspace_id="ws-old", cause=CAUSE_EVICTION
        )
        assert killed.await_count == stopped.await_count == 1

    async def test_broadcasts_workspace_evicted_event(self):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-1", "cid-1", 100)
        registry.notify_workspace_killed = AsyncMock()
        registry.stop_and_remove_container = AsyncMock()
        broadcast = AsyncMock()
        app.state.sockets.notify_workspace_evicted = broadcast

        await evictor.evict_one(0.03)
        broadcast.assert_awaited_once_with(
            "ws-1", reason="host memory pressure"
        )

    async def test_never_evicts_workspace_with_subscribers(self):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-busy", "cid-busy", 500)
        session = app.state.sockets.get_or_create_session("ws-busy")
        session.subscribers.add(MagicMock())

        assert await evictor.evict_one(0.03) is False
        assert "ws-busy" in registry.states

    async def test_browser_subscribers_block_eviction(self):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-browse", "cid-b", 500)
        session = app.state.sockets.get_or_create_session("ws-browse")
        session.browser_subscribers.add(MagicMock())

        assert await evictor.evict_one(0.03) is False

    async def test_subscriber_workspace_skipped_idle_one_chosen(self):
        """Busy workspace is passed over while an idle one exists."""
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-busy", "cid-busy", 5000)
        self._tracked(registry, "ws-idle", "cid-idle", 50)
        session = app.state.sockets.get_or_create_session("ws-busy")
        session.subscribers.add(MagicMock())
        stopped = AsyncMock()
        registry.notify_workspace_killed = AsyncMock()
        registry.stop_and_remove_container = stopped

        assert await evictor.evict_one(0.03) is True
        stopped.assert_awaited_once_with(
            "cid-idle", workspace_id="ws-idle", cause=CAUSE_EVICTION
        )

    async def test_stopping_workspace_skipped(self):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-stopping", "cid-s", 500)
        registry.stopping.add("ws-stopping")

        assert await evictor.evict_one(0.03) is False

    async def test_workspace_with_operation_in_flight_skipped(self):
        """A mid-(re)connect workspace — per-workspace lock held — is
        skipped: its container is tracked from podman create but has no
        subscriber until container_ready, so an armed evictor would stop
        the fresh container under the connecting client (#2527 e2e
        flake)."""
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-connecting", "cid-c", 500)
        async with registry._get_workspace_lock("ws-connecting"):
            assert await evictor.evict_one(0.03) is False
        assert "ws-connecting" in registry.states
        # Lock released → eligible again.
        assert await evictor.evict_one(0.03) is True

    async def test_never_stop_pin_skipped(self):
        """idle_timeout=0 means "never stop" (auto-start boot services,
        #1244) — eviction must respect the pin too (#2627 review B2):
        pinned services have zero subscribers and stale last_activity,
        so without this they would be evicted first.
        """
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-pinned", "cid-p", 5000)
        registry.states["ws-pinned"].idle_timeout = 0

        assert await evictor.evict_one(0.03) is False
        assert "ws-pinned" in registry.states

    async def test_deploy_wide_zero_idle_timeout_disables_eviction(self):
        """KLANGKD_IDLE_TIMEOUT_SECONDS=0 = idle stopping disabled — the
        conservative reading also disables eviction."""
        app, evictor = self._evictor({"KLANGKD_IDLE_TIMEOUT_SECONDS": "0"})
        registry = app.state.container_registry
        self._tracked(registry, "ws-any", "cid-a", 500)
        registry.notify_workspace_killed = AsyncMock()
        registry.stop_and_remove_container = AsyncMock()

        assert await evictor.evict_one(0.03) is False
        registry.stop_and_remove_container.assert_not_awaited()

    async def test_no_candidate_warning_once_then_debug(self, caplog):
        app, evictor = self._evictor()
        with caplog.at_level(
            logging.DEBUG, logger="klangk.container.eviction"
        ):
            assert await evictor.evict_one(0.03) is False
            assert await evictor.evict_one(0.03) is False
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "no idle workspace" in r.message
        ]
        debugs = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "still no evictable" in r.message
        ]
        assert len(warnings) == 1
        assert len(debugs) == 1

    async def test_warning_rearms_after_successful_eviction(self, caplog):
        app, evictor = self._evictor()
        registry = app.state.container_registry
        registry.notify_workspace_killed = AsyncMock()
        registry.stop_and_remove_container = AsyncMock()
        with caplog.at_level(
            logging.WARNING, logger="klangk.container.eviction"
        ):
            assert await evictor.evict_one(0.03) is False  # arms the flag
            self._tracked(registry, "ws-1", "cid-1", 10)
            assert await evictor.evict_one(0.03) is True  # resets it
            registry.states.pop("ws-1", None)
            assert await evictor.evict_one(0.03) is False  # warns again
        warnings = [
            r for r in caplog.records if "no idle workspace" in r.message
        ]
        assert len(warnings) == 2

    async def test_exhausted_after_evictions_warns_with_count(self, caplog):
        """Evicted-N-without-recovery names the cgroup misattribution
        possibility (#2627 review: systemd slice MemoryMax on klangkd
        itself — evictions cannot relieve it)."""
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-1", "cid-1", 10)
        registry.notify_workspace_killed = AsyncMock()
        registry.stop_and_remove_container = AsyncMock()
        # True paths only count real evictions.
        assert await evictor.evict_one(0.03) is True
        assert evictor._evicted_this_episode == 1
        # The mocked stop doesn't tear registry state down; drop it so
        # the next poll finds the registry exhausted.
        registry.states.pop("ws-1", None)
        with caplog.at_level(
            logging.WARNING, logger="klangk.container.eviction"
        ):
            assert await evictor.evict_one(0.03) is False
        assert any(
            "evicted 1 workspace(s) this episode without recovery" in r.message
            for r in caplog.records
        )

    async def test_eviction_counter_resets_on_recovery(self):
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        evictor._evicted_this_episode = 3
        await evictor._handle_measurement(0.20, 0, True)  # recovery
        assert evictor._evicted_this_episode == 0

    async def test_empty_registry_returns_false(self):
        app, evictor = self._evictor()
        assert await evictor.evict_one(0.03) is False

    async def test_graceful_stop_path_with_real_registry(self):
        """evict_one drives the real stop path end to end (podman mocked)."""
        app, evictor = self._evictor()
        registry = app.state.container_registry
        self._tracked(registry, "ws-real", "cid-real", 100)
        with patch.object(
            app.state.podman, "remove_container", new=AsyncMock()
        ) as rm:
            assert await evictor.evict_one(0.03) is True
            rm.assert_awaited_once_with("cid-real")
        # Idle-stop semantics: state torn down, next connect restarts.
        assert "ws-real" not in registry.states


class TestStateMachine:
    """_handle_measurement: sustain, hysteresis, and flap-prevention."""

    def _evictor(self, env=None):
        app = _make_app_state(env)
        return app, MemoryPressureEvictor(app)

    async def test_transient_spike_never_opens_episode(self):
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        # threshold 10 / recovery 15 / sustain 3: two low polls then
        # recovery-high — below the sustain count, no eviction.
        below, pressured = await evictor._handle_measurement(0.05, 0, False)
        assert (below, pressured) == (1, False)
        below, pressured = await evictor._handle_measurement(0.05, 1, False)
        assert (below, pressured) == (2, False)
        below, pressured = await evictor._handle_measurement(0.50, 2, False)
        assert (below, pressured) == (0, False)
        evictor.evict_one.assert_not_awaited()

    async def test_sustained_pressure_opens_episode_and_evicts(self):
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        below, pressured = await evictor._handle_measurement(0.05, 0, False)
        below, pressured = await evictor._handle_measurement(0.05, 1, False)
        below, pressured = await evictor._handle_measurement(0.05, 2, False)
        assert (below, pressured) == (0, True)
        evictor.evict_one.assert_awaited_once_with(0.05)

    async def test_pressured_below_threshold_evicts_each_poll(self):
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        below, pressured = await evictor._handle_measurement(0.05, 0, True)
        assert (below, pressured) == (0, True)
        below, pressured = await evictor._handle_measurement(0.04, 0, True)
        assert (below, pressured) == (0, True)
        assert evictor.evict_one.await_count == 2

    async def test_pressured_between_thresholds_holds(self):
        """The threshold..recovery gap is the anti-flap hysteresis band."""
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        # 12% is above the 10% pressure threshold but below the 15%
        # recovery threshold: hold, no eviction, episode stays open.
        below, pressured = await evictor._handle_measurement(0.12, 0, True)
        assert (below, pressured) == (0, True)
        evictor.evict_one.assert_not_awaited()

    async def test_recovery_closes_episode(self):
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        below, pressured = await evictor._handle_measurement(0.20, 0, True)
        assert (below, pressured) == (0, False)
        evictor.evict_one.assert_not_awaited()

    async def test_renewed_pressure_after_recovery_needs_full_sustain(self):
        """Ending an episode resets the sustain counter — no instant re-arm."""
        app, evictor = self._evictor()
        evictor.evict_one = AsyncMock()
        _, pressured = await evictor._handle_measurement(0.20, 2, True)
        assert pressured is False
        below, pressured = await evictor._handle_measurement(0.05, 0, False)
        assert (below, pressured) == (1, False)
        evictor.evict_one.assert_not_awaited()


class TestRunLoop:
    def _evictor(self, env=None):
        app = _make_app_state(env)
        return app, MemoryPressureEvictor(app)

    async def _run_briefly(self, evictor, seconds=0.3):
        evictor.start()
        try:
            await asyncio.sleep(seconds)
        finally:
            await evictor.stop()

    async def test_loop_evicts_under_sustained_low_memory(
        self, monkeypatch, tmp_path
    ):
        app, evictor = self._evictor(
            {
                "KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001",
                "KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS": "2",
            }
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        monkeypatch.setattr(
            "klangk.container.eviction.platform.system", lambda: "Linux"
        )
        meminfo = tmp_path / "meminfo"
        meminfo.write_text(_MEMINFO)  # ~4.7% available — below 10%
        monkeypatch.setattr(
            "klangk.container.eviction.read_meminfo",
            lambda path="/proc/meminfo": read_meminfo(str(meminfo)),
        )
        monkeypatch.setattr(
            "klangk.container.eviction.cgroup_memory_headroom", lambda: None
        )
        evictor.evict_one = AsyncMock(return_value=True)
        await self._run_briefly(evictor)
        assert evictor.evict_one.await_count >= 1

    async def test_loop_does_not_evict_when_disabled(
        self, monkeypatch, tmp_path
    ):
        app, evictor = self._evictor(
            {
                "KLANGKD_MEMORY_EVICTION_ENABLED": "false",
                "KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001",
            }
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        measure = AsyncMock(return_value=0.01)
        monkeypatch.setattr(
            "klangk.container.eviction.measure_available_fraction", measure
        )
        evictor.evict_one = AsyncMock()
        await self._run_briefly(evictor, seconds=0.1)
        evictor.evict_one.assert_not_awaited()

    async def test_loop_survives_evict_one_raise(self, monkeypatch, caplog):
        """A failing eviction cycle must not kill the loop (#2627 review B1).

        The realistic raise: under <10% availability, fork fails — podman
        raises raw OSError out of create_subprocess_exec. The loop skips
        the cycle and keeps running; eviction is not silently disabled.
        """
        app, evictor = self._evictor(
            {
                "KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001",
                "KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS": "1",
            }
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        measure = AsyncMock(return_value=0.01)
        monkeypatch.setattr(
            "klangk.container.eviction.measure_available_fraction", measure
        )
        evictor.evict_one = AsyncMock(side_effect=OSError(11, "fork failed"))
        with caplog.at_level(
            logging.WARNING, logger="klangk.container.eviction"
        ):
            await self._run_briefly(evictor, seconds=0.1)
        # Many cycles ran; the loop is still alive and each failure was
        # logged, not fatal.
        assert evictor.evict_one.await_count > 1
        assert any(
            "eviction cycle failed" in r.message for r in caplog.records
        )

    async def test_stop_suppresses_dead_task_exception(self, monkeypatch):
        """stop() must not break the shutdown cascade if the loop already
        died with an exception (#2627 review B1)."""
        app, evictor = self._evictor()
        evictor._task = asyncio.get_running_loop().create_task(_boom(evictor))
        await asyncio.sleep(0.01)  # let it die
        await evictor.stop()  # must not raise

    async def test_cancel_during_eviction_still_stops(self, monkeypatch):
        """Cancellation landing mid-eviction propagates (not swallowed by
        the cycle guard) and stop() stays clean (#2627 review B1)."""
        app, evictor = self._evictor(
            {
                "KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001",
                "KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS": "1",
            }
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        measure = AsyncMock(return_value=0.01)
        monkeypatch.setattr(
            "klangk.container.eviction.measure_available_fraction", measure
        )
        entered = asyncio.Event()

        async def slow_evict(fraction):
            entered.set()
            await asyncio.sleep(60)  # cancellation lands here

        evictor.evict_one = slow_evict
        evictor.start()
        await asyncio.wait_for(entered.wait(), timeout=5)
        await evictor.stop()  # cancels inside _handle_measurement
        assert evictor._task is None

    async def test_start_logs_armed_configuration(self, caplog):
        """Operators can see from the log that eviction is armed (I2)."""
        app, evictor = self._evictor()
        with caplog.at_level(logging.INFO, logger="klangk.container.eviction"):
            evictor.start()
            await evictor.stop()
        armed = [r for r in caplog.records if "armed" in r.message]
        assert armed, "expected an 'armed' log line at start"
        assert "threshold 10.0%" in armed[0].getMessage()
        assert "recovery 15.0%" in armed[0].getMessage()
        assert "enabled=True" in armed[0].getMessage()

    async def test_loop_survives_unreadable_meminfo(self, monkeypatch, caplog):
        app, evictor = self._evictor(
            {"KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001"}
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        evictor.evict_one = AsyncMock()
        with patch(
            "klangk.container.eviction.measure_available_fraction",
            side_effect=OSError("no procfs"),
        ):
            with caplog.at_level(
                logging.WARNING, logger="klangk.container.eviction"
            ):
                await self._run_briefly(evictor, seconds=0.1)
        evictor.evict_one.assert_not_awaited()
        assert any(
            "cannot measure memory availability" in r.message
            for r in caplog.records
        )

    async def test_unreadable_warning_rearms_after_recovery(
        self, monkeypatch, caplog
    ):
        """fail → warn; recover → re-arm; fail again → warn again (#2627
        review nit: the once-only flag must not mask a later permanent
        break)."""
        app, evictor = self._evictor(
            {"KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001"}
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        evictor.evict_one = AsyncMock()
        seq = iter(
            [
                OSError("no procfs"),  # transient failure
                0.50,  # recovery — re-arms the warning
                OSError("still broken"),  # later permanent failure
            ]
        )

        async def flaky_measure():
            nxt = next(seq)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(
            "klangk.container.eviction.measure_available_fraction",
            flaky_measure,
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.container.eviction"
        ):
            await self._run_briefly(evictor, seconds=0.1)
        warnings = [
            r
            for r in caplog.records
            if "cannot measure memory availability" in r.message
        ]
        assert len(warnings) == 2

    async def test_loop_survives_empty_meminfo(self, monkeypatch):
        """A readable but empty measurement (ValueError) is skipped."""
        app, evictor = self._evictor(
            {"KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "0.001"}
        )
        monkeypatch.setattr(
            "klangk.container.eviction.MIN_POLL_INTERVAL_SECONDS", 0.001
        )
        evictor.evict_one = AsyncMock()
        with patch(
            "klangk.container.eviction.measure_available_fraction",
            side_effect=ValueError("no MemTotal"),
        ):
            await self._run_briefly(evictor, seconds=0.1)
        evictor.evict_one.assert_not_awaited()

    async def test_start_is_idempotent_stop_cancels(self):
        app, evictor = self._evictor()
        evictor.start()
        task = evictor._task
        evictor.start()
        assert evictor._task is task
        await evictor.stop()
        assert evictor._task is None
        assert task.cancelled()

    def test_reconfigure_swaps_app(self):
        app, evictor = self._evictor()
        new_app = _make_app_state()
        evictor.reconfigure(new_app)
        assert evictor.app is new_app


class TestNotifyWorkspaceEvicted:
    """The WebSocketState broadcast distinct from idle stops."""

    def _sockets(self):
        app = _make_app_state()
        from klangk.wshandler.session import WebSocketState
        from _helpers import wire_db_and_model

        wire_db_and_model(app)
        return WebSocketState(app), app

    def _conn(self, user_id="u1"):
        conn = MagicMock()
        conn.user = {"id": user_id}
        return conn

    async def _grant(self, app, user_id, workspace_id):
        """Seed a member ALLOW ``monitor`` ACE (#1714/#2783)."""
        from klangk import model

        await app.state.model.init_db()
        # acl_entries.user_id has an FK to users(id): plant the row.
        async with app.state.db.transaction() as tx:
            await tx.execute(
                "INSERT OR IGNORE INTO users (id, email, verified)"
                " VALUES (?, ?, 1)",
                (user_id, f"{user_id}@test.example"),
            )
        resource = f"/workspaces/{workspace_id}"
        entries = await app.state.model.acl.get_acl_entries(resource)
        position = max((e["position"] for e in entries), default=-1) + 1
        await app.state.model.acl.add_acl_entry(
            resource,
            position,
            model.ACTION_ALLOW,
            "monitor",
            model.PRINCIPAL_USER,
            user_id=user_id,
        )

    async def test_fans_out_to_members(self):
        sockets, app = self._sockets()
        sock1, sock2 = MagicMock(), MagicMock()
        sockets.connections[sock1] = self._conn()
        sockets.connections[sock2] = self._conn("u2")
        await self._grant(app, "u1", "ws-1")
        await self._grant(app, "u2", "ws-1")

        await sockets.notify_workspace_evicted("ws-1")

        for sock in (sock1, sock2):
            sock.send_json.assert_called_once_with(
                {
                    "type": "workspace_evicted",
                    "workspace_id": "ws-1",
                    "reason": "host memory pressure",
                }
            )

    async def test_non_member_receives_nothing(self):
        """#1714: a connected user with no grant on the workspace is skipped."""
        sockets, app = self._sockets()
        member, stranger = MagicMock(), MagicMock()
        sockets.connections[member] = self._conn()
        sockets.connections[stranger] = self._conn("u2")
        await self._grant(app, "u1", "ws-1")

        await sockets.notify_workspace_evicted("ws-1")

        member.send_json.assert_called_once()
        stranger.send_json.assert_not_called()

    async def test_dead_socket_popped_unauthenticated_skipped(self):
        sockets, app = self._sockets()
        dead, alive, anon = MagicMock(), MagicMock(), MagicMock()
        dead.send_json.side_effect = WS_ERRORS[0](
            "sender stopped — cannot enqueue"
        )
        sockets.connections[dead] = self._conn()
        sockets.connections[alive] = self._conn()
        sockets.connections[anon] = self._conn(user_id=None)
        await self._grant(app, "u1", "ws-1")

        await sockets.notify_workspace_evicted("ws-1", reason="other")

        alive.send_json.assert_called_once()
        anon.send_json.assert_not_called()
        assert dead not in sockets.connections
        assert alive in sockets.connections


class TestEvictionSettings:
    def test_defaults(self):
        s = make_settings({})
        assert s.memory_eviction_enabled is True
        assert s.memory_eviction_threshold_percent == 10.0
        assert s.memory_eviction_recovery_percent == 15.0
        assert s.memory_eviction_sustain_polls == 3
        assert s.memory_eviction_poll_interval == 10.0

    def test_string_env_coercion(self):
        s = make_settings(
            {
                "KLANGKD_MEMORY_EVICTION_ENABLED": "false",
                "KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT": "5.5",
                "KLANGKD_MEMORY_EVICTION_RECOVERY_PERCENT": "6",
                "KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS": "2",
                "KLANGKD_MEMORY_EVICTION_POLL_INTERVAL": "30",
            }
        )
        assert s.memory_eviction_enabled is False
        assert s.memory_eviction_threshold_percent == 5.5
        assert s.memory_eviction_recovery_percent == 6.0
        assert s.memory_eviction_sustain_polls == 2
        assert s.memory_eviction_poll_interval == 30.0

    def test_inverted_thresholds_rejected(self):
        with pytest.raises(ValueError):
            make_settings(
                {
                    "KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT": "20",
                    "KLANGKD_MEMORY_EVICTION_RECOVERY_PERCENT": "10",
                }
            )

    def test_equal_thresholds_allowed(self):
        """Zero hysteresis gap is the operator's explicit choice."""
        s = make_settings(
            {
                "KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT": "10",
                "KLANGKD_MEMORY_EVICTION_RECOVERY_PERCENT": "10",
            }
        )
        assert s.memory_eviction_recovery_percent == 10.0

    def test_bad_sustain_polls_rejected(self):
        with pytest.raises(ValueError):
            make_settings({"KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS": "0"})


class TestEvictionBranchGaps2834:
    """#2834 branch gate: malformed meminfo lines, a stat file without a
    matching counter, and stop-before-start."""

    def test_read_meminfo_skips_malformed_lines(self, tmp_path):
        # Lines without the 3-part "Name: N kB" shape are skipped, valid
        # ones still parse.
        path = tmp_path / "meminfo"
        path.write_text(
            "MemTotal:       16384000 kB\n"
            "Weird: 5\n"
            "Other:       6 MB\n"
            "MemFree:        1000 kB\n"
        )
        info = read_meminfo(str(path))
        assert info["MemTotal"] == 16384000 * 1024
        assert info["MemFree"] == 1000 * 1024
        assert "Weird" not in info
        assert "Other" not in info

    def test_stat_value_without_match_returns_zero(self, tmp_path):
        from klangk.container.eviction import stat_value

        path = tmp_path / "memory.stat"
        path.write_text("anon 5\nkernel 7\n")
        assert stat_value(str(path), ("file", "inactive_file")) == 0

    async def test_stop_when_never_started_is_noop(self):
        from klangk.container.eviction import MemoryPressureEvictor

        app = _make_app_state()
        evictor = MemoryPressureEvictor(app)
        await evictor.stop()  # no task -> no raise
