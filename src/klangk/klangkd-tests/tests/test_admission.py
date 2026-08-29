"""Admission control for workspace starts (#2525) — registry level.

Two gates at the container-start choke point (right after the #2527
drain gate, after the running-container adoption check, so every start
path — API start/restart, WS connect, create eager start, boot
auto-start, crash-recovery restart — is covered and an already-running
workspace reconnecting is never re-admitted):

1. Host-memory fit: available host memory vs. the workspace's resolved
   ``container_memory_limit`` + ``admission_memory_margin``.
2. Per-user quota: ``max_running_workspaces_per_user`` concurrently
   running (or mid-start/stop) workspaces per owner.

Capacity refusals raise :class:`WorkspaceCapacityError` — a
deterministic, operator-actionable failure surfaced as a 503 / WS
error frame upstream, distinguishable from config errors (400) and
runtime failures (500).
"""

import types
from unittest.mock import AsyncMock, patch

import pytest

from klangk.container.admission import (
    AdmissionControl,
    available_memory_bytes,
    format_size,
    parse_size_bytes,
)
from klangk.container import ContainerStartSpec
from klangk.exceptions import WorkspaceCapacityError

GIB = 1024**3
MIB = 1024**2


def _spec(workspace_id, workspace_settings=None, existing_container_id=None):
    return ContainerStartSpec(
        workspace_id=workspace_id,
        home_path="/tmp/x/home",
        workspace_settings=workspace_settings,
        existing_container_id=existing_container_id,
    )


# --- size parsing / formatting ---


class TestParseSizeBytes:
    def test_units(self):
        assert parse_size_bytes("1024") == 1024
        assert parse_size_bytes("512m") == 512 * MIB
        assert parse_size_bytes("512mb") == 512 * MIB
        assert parse_size_bytes("2g") == 2 * GIB
        assert parse_size_bytes("2G") == 2 * GIB
        assert parse_size_bytes("2gb") == 2 * GIB
        assert parse_size_bytes("1.5g") == int(1.5 * GIB)
        assert parse_size_bytes("1t") == 1024**4
        assert parse_size_bytes("1p") == 1024**5
        assert parse_size_bytes("64k") == 64 * 1024

    def test_malformed_raises(self):
        for bad in ("", "abc", "-1g", "g", "1x", "1 gib", "1ki"):
            with pytest.raises(ValueError):
                parse_size_bytes(bad)


class TestFormatSize:
    def test_gib_and_mib(self):
        assert format_size(2 * GIB) == "2.0 GB"
        assert format_size(int(1.2 * GIB)) == "1.2 GB"
        assert format_size(512 * MIB) == "512 MB"
        assert format_size(0) == "0 MB"


# --- availability measurement ---


def _meminfo(total, available=None, free=None, cached=None):
    info = {"MemTotal": total}
    if available is not None:
        info["MemAvailable"] = available
    if free is not None:
        info["MemFree"] = free
    if cached is not None:
        info["Cached"] = cached
    return info


class TestAvailableMemoryBytes:
    async def test_linux_meminfo(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Linux",
        )
        monkeypatch.setattr(
            "klangk.container.admission.read_meminfo",
            lambda: _meminfo(16 * GIB, available=6 * GIB),
        )
        monkeypatch.setattr(
            "klangk.container.admission.cgroup_memory_headroom",
            lambda: None,
        )
        assert await available_memory_bytes() == 6 * GIB

    async def test_linux_memfree_fallback(self, monkeypatch):
        """Old kernels without MemAvailable fall back to MemFree+Cached."""
        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Linux",
        )
        monkeypatch.setattr(
            "klangk.container.admission.read_meminfo",
            lambda: _meminfo(8 * GIB, free=1 * GIB, cached=2 * GIB),
        )
        monkeypatch.setattr(
            "klangk.container.admission.cgroup_memory_headroom",
            lambda: None,
        )
        assert await available_memory_bytes() == 3 * GIB

    async def test_linux_cgroup_headroom_wins_when_smaller(self, monkeypatch):
        """Inside a memory-limited container the cgroup's own headroom
        (limit - working set) governs when smaller than meminfo."""
        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Linux",
        )
        monkeypatch.setattr(
            "klangk.container.admission.read_meminfo",
            lambda: _meminfo(16 * GIB, available=6 * GIB),
        )
        monkeypatch.setattr(
            "klangk.container.admission.cgroup_memory_headroom",
            lambda: (4 * GIB, 3 * GIB),
        )
        assert await available_memory_bytes() == 1 * GIB

    async def test_linux_cgroup_headroom_ignored_when_larger(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Linux",
        )
        monkeypatch.setattr(
            "klangk.container.admission.read_meminfo",
            lambda: _meminfo(16 * GIB, available=6 * GIB),
        )
        monkeypatch.setattr(
            "klangk.container.admission.cgroup_memory_headroom",
            lambda: (32 * GIB, 2 * GIB),
        )
        assert await available_memory_bytes() == 6 * GIB

    async def test_linux_no_memtotal_raises(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Linux",
        )
        monkeypatch.setattr(
            "klangk.container.admission.read_meminfo",
            lambda: {},
        )
        with pytest.raises(ValueError):
            await available_memory_bytes()

    async def test_macos(self, monkeypatch):
        async def fake_measure(runner=None):
            return 8 * GIB, 2 * GIB

        monkeypatch.setattr(
            "klangk.container.admission.platform.system",
            lambda: "Darwin",
        )
        monkeypatch.setattr(
            "klangk.container.admission.macos_measure", fake_measure
        )
        assert await available_memory_bytes() == 2 * GIB


# --- host-memory fit gate ---


class TestHostMemoryGate:
    async def test_disabled_by_default(self, app_state, db, user):
        """The check ships off: even a host with no memory admits (the
        default 8g limit would otherwise refuse every start on small
        dev/CI hosts — see the settings comment)."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-off"
        )
        with patch(
            "klangk.container.admission.available_memory_bytes",
            side_effect=OSError("no meminfo"),
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_fits(self, app_state, db, user):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-fit"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=100 * GIB,
            ),
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_refused_with_clear_message(self, app_state, db, user):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-refuse"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=int(1.2 * GIB),
            ),
        ):
            with pytest.raises(WorkspaceCapacityError) as excinfo:
                await app_state.state.container_registry.admission.admit(
                    _spec(ws["id"])
                )
        msg = str(excinfo.value)
        assert "host at capacity" in msg
        assert "1.2 GB available" in msg
        assert "workspace wants 9.0 GB" in msg  # 8g limit + 1g reserve
        assert "Stop an idle workspace" in msg

    async def test_margin_unset_fits_bare_limit(self, app_state, db, user):
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-bare"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch.object(
                app_state.state.settings, "admission_memory_margin", None
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=8 * GIB,
            ),
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_no_limit_skips_check(self, app_state, db, user):
        """Unbounded memory limit: nothing to admit against."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-nolimit"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch.object(
                app_state.state.settings, "container_memory_limit", None
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                side_effect=AssertionError("must not measure"),
            ),
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_workspace_bag_override_resolved(self, app_state, db, user):
        """The fit check uses the workspace's resolved limit (bag
        override > deploy default), not just the deploy default."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"],
            "admit-override",
            settings={"memory_limit": "16g"},
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=10 * GIB,
            ),
        ):
            with pytest.raises(WorkspaceCapacityError, match="17.0 GB"):
                await app_state.state.container_registry.admission.admit(
                    _spec(ws["id"], {"memory_limit": "16g"})
                )

    async def test_unmeasurable_fails_open(self, app_state, db, user, caplog):
        """A host whose memory cannot be measured admits without the
        check (one-time warning) — unmeasurable must not brick starts."""
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "admit-unmeasurable"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                side_effect=OSError("no meminfo"),
            ),
            caplog.at_level("WARNING"),
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )
        assert any(
            "cannot measure host memory" in r.message for r in caplog.records
        )


# --- per-user quota gate ---


async def _running_workspace(app_state, user_id, name):
    """Create a workspace and mark it running in the registry."""
    ws = await app_state.state.workspaces.create_workspace(user_id, name)
    await app_state.state.model.workspaces.update_workspace_container(
        ws["id"], f"cid-{name}"
    )
    app_state.state.container_registry.track_activity(f"cid-{name}", ws["id"])
    return ws


class TestUserQuotaGate:
    async def test_unlimited_by_default(self, app_state, db, user):
        for i in range(3):
            await _running_workspace(app_state, user["id"], f"uw-{i}")
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "uw-next"
        )
        await app_state.state.container_registry.admission.admit(
            _spec(ws["id"])
        )

    async def test_refused_at_cap(self, app_state, db, user):
        await _running_workspace(app_state, user["id"], "q-a")
        await _running_workspace(app_state, user["id"], "q-b")
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "q-next"
        )
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            2,
        ):
            with pytest.raises(WorkspaceCapacityError) as excinfo:
                await app_state.state.container_registry.admission.admit(
                    _spec(ws["id"])
                )
        msg = str(excinfo.value)
        assert "quota" in msg
        assert "2" in msg
        assert "KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER" in msg
        assert "Stop a workspace first" in msg

    async def test_under_cap_admits(self, app_state, db, user):
        await _running_workspace(app_state, user["id"], "u-a")
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "u-next"
        )
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            2,
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_other_users_do_not_count(self, app_state, db, user):
        other = await app_state.state.model.users.create_user(
            "other@example.com", None, verified=True
        )
        await _running_workspace(app_state, other["id"], "o-a")
        await _running_workspace(app_state, other["id"], "o-b")
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "own-next"
        )
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            2,
        ):
            await app_state.state.container_registry.admission.admit(
                _spec(ws["id"])
            )

    async def test_start_in_flight_counts(self, app_state, db, user):
        """A sibling workspace whose start/stop lock is held counts
        against the cap — closes the two-workspaces-starting-at-once
        race (the second start sees the first's lock)."""
        starting = await app_state.state.workspaces.create_workspace(
            user["id"], "starting-ws"
        )
        await app_state.state.model.workspaces.update_workspace_container(
            starting["id"], "cid-starting"
        )
        registry = app_state.state.container_registry
        async with registry._get_workspace_lock(starting["id"]):
            ws = await app_state.state.workspaces.create_workspace(
                user["id"], "race-next"
            )
            with patch.object(
                app_state.state.settings,
                "max_running_workspaces_per_user",
                1,
            ):
                with pytest.raises(WorkspaceCapacityError, match="quota"):
                    await registry.admission.admit(_spec(ws["id"]))

    async def test_admitted_workspace_excluded(self, app_state, db, user):
        """The workspace being started never counts against its owner's
        cap (its own start lock is held by definition)."""
        running = await _running_workspace(app_state, user["id"], "me-a")
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            1,
        ):
            # Re-admitting the RUNNING workspace itself (a path that
            # cannot happen post-adoption-check, but the exclusion must
            # hold regardless): only me-a exists and it is excluded.
            await app_state.state.container_registry.admission.admit(
                _spec(running["id"])
            )

    async def test_unknown_workspace_passes(self, app_state, db, user):
        """Admission is not a 404 check — an unknown workspace is other
        layers' business."""
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            1,
        ):
            await _running_workspace(app_state, user["id"], "k-a")
            await app_state.state.container_registry.admission.admit(
                _spec("ws-does-not-exist")
            )


# --- the start choke point ---


class TestChokePoint:
    async def test_start_refused_through_choke_point(
        self, app_state, db, user
    ):
        """The single start choke point raises WorkspaceCapacityError —
        the error every start path (API, WS, auto-start, crash restart)
        funnels through."""
        registry = app_state.state.container_registry
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "choke-ws"
        )
        await _running_workspace(app_state, user["id"], "choke-running")
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            1,
        ):
            with pytest.raises(WorkspaceCapacityError, match="quota"):
                await registry.start_container(_spec(ws["id"]))

    async def test_admission_runs_after_drain_gate(self, app_state, db, user):
        """The drain refusal wins over the capacity refusal (a draining
        node is not negotiating capacity)."""
        from klangk.exceptions import NodeDrainingError

        registry = app_state.state.container_registry
        registry.draining = True
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "drain-wins"
        )
        await _running_workspace(app_state, user["id"], "drain-running")
        try:
            with patch.object(
                app_state.state.settings,
                "max_running_workspaces_per_user",
                1,
            ):
                with pytest.raises(NodeDrainingError, match="draining"):
                    await registry.start_container(_spec(ws["id"]))
        finally:
            registry.draining = False

    async def test_gate_open_proceeds_to_later_failures(
        self, app_state, db, user
    ):
        """With capacity available the start proceeds past the gate (it
        may still fail later on podman — that is not the gate's
        business)."""
        registry = app_state.state.container_registry
        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "gate-open"
        )
        with (
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            # Stub the measurement: this test asserts the GATE lets an
            # admissible start through, not that the CI host happens to
            # have >9 GB free (the macOS runners have 7 GB and would
            # legitimately refuse the default 8g limit + 1g margin).
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=100 * GIB,
            ),
        ):
            try:
                await registry.start_container(_spec(ws["id"]))
            except WorkspaceCapacityError:  # pragma: no cover
                pytest.fail("capacity gate refused an admissible start")
            except Exception:
                pass  # later failure (podman etc.) is fine — gate passed

    async def test_adoption_path_not_readmitted(self, app_state, db, user):
        """A reconnect to an already-running workspace is not
        re-admitted: its capacity is already committed (the gate sits
        after the running-container adoption check)."""
        registry = app_state.state.container_registry
        running = await _running_workspace(app_state, user["id"], "adopt-me")
        # A start that would be refused by the raw quota check (cap 1,
        # one running) must still connect via the adoption path.
        app_state.state.podman = types.SimpleNamespace(
            inspect_container=AsyncMock(
                return_value={"State": {"Running": True}}
            )
        )
        with patch.object(
            app_state.state.settings,
            "max_running_workspaces_per_user",
            1,
        ):
            await registry.admission.admit(_spec(running["id"]))
            cid, status = await registry.start_container(
                _spec(
                    running["id"],
                    existing_container_id=f"cid-{running['id']}",
                )
            )
        assert status == "connected"

    async def test_reconfigure_swaps_app(self, app_state):
        control = AdmissionControl(app_state)
        control.reconfigure(app_state)
        assert control.app is app_state


# --- boot auto-start path ---


class TestAutoStartPath:
    async def test_capacity_refusal_logged_not_fatal(
        self, app_state, db, user, caplog
    ):
        """Boot auto-start routes through the same choke point: a
        capacity refusal is logged per workspace (a clear warning, not
        a traceback) and the boot continues — later workspaces still
        get their turn (#2525)."""
        await app_state.state.workspaces.create_workspace(
            user["id"], "as-one", auto_start=True
        )
        await app_state.state.workspaces.create_workspace(
            user["id"], "as-two", auto_start=True
        )
        with (
            patch.object(app_state.state.settings, "allow_autostart", "1"),
            patch.object(
                app_state.state.settings, "admission_memory_enabled", True
            ),
            patch(
                "klangk.container.admission.available_memory_bytes",
                return_value=64 * MIB,
            ),
            patch("klangk.workspaces.random.uniform", lambda a, b: 0.0),
            caplog.at_level("WARNING"),
        ):
            started = await app_state.state.workspaces.auto_start_workspaces()
        assert started == 0
        refusals = [
            r
            for r in caplog.records
            if "Failed to auto-start" in r.getMessage()
            and "host at capacity" in (r.exc_text or "")
        ]
        assert len(refusals) == 2


# --- shared measurement helper (eviction refactor) ---


class TestMacosMeasure:
    async def test_returns_total_and_available(self):
        from klangk.container.eviction import (
            macos_available_fraction,
            macos_measure,
        )

        total = 16384 * 10000

        async def fake_runner(*cmd: str) -> str:
            if cmd[0] == "sysctl":
                return str(total)
            return (
                "Mach Virtual Memory Statistics: (page size of 16384 "
                "bytes)\n"
                "Pages free: 1000.\n"
                "Pages inactive: 1000.\n"
                "Pages speculative: 1000.\n"
            )

        measured_total, available = await macos_measure(runner=fake_runner)
        assert measured_total == total
        assert available == 3000 * 16384
        # The fraction wrapper still agrees.
        fraction = await macos_available_fraction(runner=fake_runner)
        assert fraction == pytest.approx(0.3)
