"""Container lifecycle audit events (#2915).

Covers the ``container_events`` table (migration 0019), the
``ContainerEventsModel`` CRUD, and the recording hooks at the two
lifecycle choke points — ``ContainerRegistry.start_container`` and
``stop_and_remove_container`` — including actor classification
(user/agent/system), the best-effort failure path, and the netns-owner
attribution for egress-filtered workspaces.
"""

from unittest.mock import AsyncMock, patch

import pytest

from test_container import patch_podman
from test_wshandler import _base_conn
from klangk.container.spec import ContainerStartSpec
from klangk.exceptions import AuditWriteError, NodeDrainingError
from klangk.model.container_events import (
    ACTOR_AGENT,
    ACTOR_SYSTEM,
    ACTOR_USER,
    CAUSE_API,
    CAUSE_AUTO_START,
    CAUSE_DRAIN,
    CAUSE_IDLE_TIMEOUT,
    CAUSE_LOGOUT,
    CAUSE_RESTART,
    CAUSE_SHUTDOWN,
    CAUSE_STOP,
    CAUSE_WS_CONNECT,
    EVENT_START,
    EVENT_STOP,
    actor_type_for,
)
from klangk.model.users import AGENT_USER_ID
from klangk.podman import PodmanError


@pytest.fixture
async def registry(app_state, db):
    """The conftest app_state plus a podman instance for patch_podman."""
    from klangk.podman import Podman

    app_state.state.podman = Podman(app_state)
    return app_state.state.container_registry


class TestActorClassification:
    def test_none_is_system(self):
        assert actor_type_for(None) == ACTOR_SYSTEM

    def test_agent_identity_is_agent(self):
        assert actor_type_for(AGENT_USER_ID) == ACTOR_AGENT

    def test_any_other_id_is_user(self):
        assert actor_type_for("some-user-id") == ACTOR_USER


class TestContainerEventsModel:
    async def test_record_and_list_newest_first(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", actor_id="u1")
        await events.record("ws-a", EVENT_STOP, CAUSE_STOP, actor_id="u1")
        await events.record("ws-b", EVENT_START, CAUSE_AUTO_START)
        rows = await events.list_events()
        assert [r["workspace_id"] for r in rows] == ["ws-b", "ws-a", "ws-a"]
        assert rows[0]["actor_type"] == ACTOR_SYSTEM
        assert rows[0]["actor_id"] is None
        assert rows[2]["actor_type"] == ACTOR_USER
        assert rows[2]["actor_id"] == "u1"

    async def test_list_filter_limit_offset(self, app_state, db):
        events = app_state.state.model.container_events
        for i in range(5):
            await events.record(
                "ws-a", EVENT_START, "api", container_id=f"cid-{i}"
            )
        await events.record("ws-b", EVENT_START, "api")
        assert await events.count_events() == 6
        assert await events.count_events("ws-a") == 5
        page = await events.list_events("ws-a", limit=2, offset=1)
        assert [r["container_id"] for r in page] == ["cid-3", "cid-2"]

    async def test_record_carries_cause_and_namespace(self, app_state, db):
        events = app_state.state.model.container_events
        await events.record(
            "ws-a",
            EVENT_START,
            CAUSE_IDLE_TIMEOUT,
            actor_id=None,
            container_id="cid-9",
            network_namespace="sidecar-cid",
        )
        row = (await events.list_events("ws-a"))[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == CAUSE_IDLE_TIMEOUT
        assert row["container_id"] == "cid-9"
        assert row["network_namespace"] == "sidecar-cid"
        assert row["created_at"] > 0


class TestContainerEventsPrune2924:
    """Retention + deploy-wide row cap (#2924), mirroring the egress-consent
    prune tests (#2303). Every row is history — no in-effect exemptions."""

    RETENTION_DEFAULT = 90  # matches Settings.container_events_retention_days

    async def _set_created_at(self, app_state, container_id, created_at):
        async with app_state.state.db.transaction() as conn:
            await conn.execute(
                "UPDATE container_events SET created_at = ?"
                " WHERE container_id = ?",
                (created_at, container_id),
            )

    async def _ids(self, app_state):
        return {
            r["container_id"]
            for r in await app_state.state.model.container_events.list_events()
        }

    async def test_prune_deletes_old_rows(self, app_state, db):
        """Rows past the retention window go; fresh rows stay."""
        import time as _time

        events = app_state.state.model.container_events
        now = _time.time()
        old = now - (self.RETENTION_DEFAULT + 5) * 86400
        await events.record("ws-a", EVENT_START, "api", container_id="old-1")
        await events.record(
            "ws-a", EVENT_STOP, CAUSE_STOP, container_id="old-2"
        )
        await events.record("ws-b", EVENT_START, "api", container_id="new-1")
        for cid in ("old-1", "old-2"):
            await self._set_created_at(app_state, cid, old)

        assert await events.prune(now=now) == 2
        assert await self._ids(app_state) == {"new-1"}

    async def test_prune_fresh_rows_untouched(self, app_state, db):
        """Rows inside the window are never candidates (also covers the
        now=None default — wall clock, nothing old)."""
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="x")
        assert await events.prune() == 0
        assert await self._ids(app_state) == {"x"}

    async def test_prune_disabled_when_both_zero(self, app_state, db):
        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 0
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="x")
        await self._set_created_at(app_state, "x", 1.0)  # ancient
        assert await events.prune() == 0
        assert await self._ids(app_state) == {"x"}

    async def test_prune_row_cap_keeps_newest(self, app_state, db):
        """Over the deploy-wide cap, the oldest rows go, newest stay."""
        import time as _time

        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 3
        events = app_state.state.model.container_events
        now = _time.time()
        for i, cid in enumerate(["e1", "e2", "e3", "e4", "e5"]):
            await events.record(
                f"ws-{i % 2}", EVENT_START, "api", container_id=cid
            )
            await self._set_created_at(app_state, cid, now - 1000 + i * 10)

        assert await events.prune(now=now) == 2
        assert await self._ids(app_state) == {"e3", "e4", "e5"}
        assert await events.count_events() == 3

    async def test_prune_row_cap_is_deploy_wide(self, app_state, db):
        """The cap counts rows across workspaces (an audit log bounds the
        table, not per-workspace fairness)."""
        import time as _time

        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 2
        events = app_state.state.model.container_events
        now = _time.time()
        # oldest first, interleaved across two workspaces
        rows = [
            ("ws-a", "c1"),
            ("ws-b", "c2"),
            ("ws-a", "c3"),
            ("ws-b", "c4"),
        ]
        for i, (ws, cid) in enumerate(rows):
            await events.record(ws, EVENT_START, "api", container_id=cid)
            await self._set_created_at(app_state, cid, now - 1000 + i * 10)

        assert await events.prune(now=now) == 2
        assert await self._ids(app_state) == {"c3", "c4"}

    async def test_prune_row_cap_tiebreak_highest_id(self, app_state, db):
        """Equal created_at: the higher id is the newer (the same tie-break
        list_events uses), so it survives the cap."""
        import time as _time

        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 1
        events = app_state.state.model.container_events
        ts = _time.time() - 100
        await events.record("ws-a", EVENT_START, "api", container_id="first")
        await events.record("ws-a", EVENT_START, "api", container_id="second")
        await self._set_created_at(app_state, "first", ts)
        await self._set_created_at(app_state, "second", ts)

        assert await events.prune(now=ts + 1) == 1
        assert await self._ids(app_state) == {"second"}

    async def test_prune_under_cap_deletes_nothing(self, app_state, db):
        """At or under the cap the pass is a no-op."""
        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 5
        events = app_state.state.model.container_events
        await events.record("ws-a", EVENT_START, "api", container_id="x")
        assert await events.prune() == 0
        assert await self._ids(app_state) == {"x"}

    async def test_prune_retention_zero_cap_active(self, app_state, db):
        """Retention off, cap on: only the cap pass runs."""
        import time as _time

        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 1
        events = app_state.state.model.container_events
        now = _time.time()
        ancient = now - (self.RETENTION_DEFAULT + 5) * 86400
        await events.record("ws-a", EVENT_START, "api", container_id="ancient")
        await events.record("ws-a", EVENT_START, "api", container_id="fresh")
        await self._set_created_at(app_state, "ancient", ancient)
        await self._set_created_at(app_state, "fresh", now)

        # retention is off, so the ancient row survives on age grounds; the
        # cap keeps only the newest row.
        assert await events.prune(now=now) == 1
        assert await self._ids(app_state) == {"fresh"}

    async def test_prune_cap_zero_retention_active(self, app_state, db):
        """Cap off, retention on: only the retention pass runs."""
        import time as _time

        app_state.state.settings.container_events_row_cap = 0
        events = app_state.state.model.container_events
        now = _time.time()
        ancient = now - (self.RETENTION_DEFAULT + 5) * 86400
        await events.record("ws-a", EVENT_START, "api", container_id="ancient")
        await events.record("ws-a", EVENT_START, "api", container_id="fresh")
        await self._set_created_at(app_state, "ancient", ancient)
        await self._set_created_at(app_state, "fresh", now)

        assert await events.prune(now=now) == 1
        assert await self._ids(app_state) == {"fresh"}

    async def test_prune_live_settings_read(self, app_state, db):
        """The knobs are read live off app.state.settings (SIGHUP-reload
        shape): flipping them between prunes re-arms the passes."""
        import time as _time

        events = app_state.state.model.container_events
        now = _time.time()
        ancient = now - (self.RETENTION_DEFAULT + 5) * 86400
        await events.record("ws-a", EVENT_START, "api", container_id="ancient")
        await self._set_created_at(app_state, "ancient", ancient)

        app_state.state.settings.container_events_retention_days = 0
        app_state.state.settings.container_events_row_cap = 0
        assert await events.prune(now=now) == 0
        app_state.state.settings.container_events_retention_days = (
            self.RETENTION_DEFAULT
        )
        assert await events.prune(now=now) == 1


class TestMigration:
    async def test_table_and_index_created(self, app_state, db):
        tables = await app_state.state.db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'container_events'"
        )
        assert tables, "container_events table missing after init_db"
        indexes = await app_state.state.db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
            " AND name = 'idx_container_events_ws_time'"
        )
        assert indexes, "container_events index missing after init_db"


class TestRegistryRecording:
    async def test_start_records_created_transition(
        self, app_state, db, monkeypatch
    ):
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-1", "created")),
        ):
            cid, status = await registry.start_container(
                ContainerStartSpec(
                    workspace_id="ws-a",
                    home_path="/home/x",
                    audit_cause=CAUSE_AUTO_START,
                )
            )
        assert (cid, status) == ("cid-1", "created")
        row = (
            await app_state.state.model.container_events.list_events("ws-a")
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == CAUSE_AUTO_START
        assert row["actor_type"] == ACTOR_SYSTEM
        assert row["container_id"] == "cid-1"

    async def test_connected_attach_records_nothing(
        self, app_state, db, monkeypatch
    ):
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-1", "connected")),
        ):
            await registry.start_container(
                ContainerStartSpec(workspace_id="ws-a", home_path="/home/x")
            )
        assert (
            await app_state.state.model.container_events.count_events("ws-a")
            == 0
        )

    async def test_stop_records_with_actor_and_netns(
        self, app_state, db, registry, workspace
    ):
        ws_id = workspace["id"]
        registry.track_activity("cid-1", ws_id)
        # A live sidecar mapping (#2915): the netns owner is captured on
        # the stop row even though teardown pops it.
        registry._ws_netns_owner[ws_id] = "sidecar-cid"
        with patch_podman(registry):
            ok = await registry.stop_and_remove_container(
                "cid-1",
                workspace_id=ws_id,
                cause=CAUSE_STOP,
                actor_id="some-user-id",
            )
        assert ok is True
        row = (
            await app_state.state.model.container_events.list_events(ws_id)
        )[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_STOP
        assert row["actor_type"] == ACTOR_USER
        assert row["actor_id"] == "some-user-id"
        assert row["container_id"] == "cid-1"
        assert row["network_namespace"] == "sidecar-cid"
        assert ws_id not in registry._ws_netns_owner

    async def test_failed_remove_records_nothing(
        self, app_state, db, registry
    ):
        from klangk.podman import PodmanError

        registry.track_activity("cid-dead", "ws-x")
        with patch_podman(
            registry,
            remove_container=AsyncMock(side_effect=PodmanError(500, "boom")),
        ):
            ok = await registry.stop_and_remove_container(
                "cid-dead", workspace_id="ws-x", cause=CAUSE_STOP
            )
        assert ok is False
        assert (
            await app_state.state.model.container_events.count_events("ws-x")
            == 0
        )

    async def test_unresolvable_workspace_records_nothing(
        self, app_state, db, registry
    ):
        with patch_podman(registry):
            ok = await registry.stop_and_remove_container(
                "cid-orphan", cause=CAUSE_STOP
            )
        assert ok is True
        assert await app_state.state.model.container_events.count_events() == 0

    async def test_audit_write_failure_is_not_fatal(self, app_state, db):
        registry = app_state.state.container_registry
        with patch.object(
            app_state.state.model.container_events,
            "record",
            AsyncMock(side_effect=RuntimeError("db gone")),
        ):
            # Neither start nor stop may raise when the audit write fails.
            await registry.record_container_event(
                "ws-a", "cid-1", EVENT_START, cause="api"
            )

    async def test_netns_defaults_to_live_mapping(self, app_state, db):
        registry = app_state.state.container_registry
        registry._ws_netns_owner["ws-a"] = "sidecar-live"
        await registry.record_container_event(
            "ws-a", "cid-1", EVENT_START, cause="api"
        )
        row = (
            await app_state.state.model.container_events.list_events("ws-a")
        )[0]
        assert row["network_namespace"] == "sidecar-live"


class TestAuditWriteFailureVisibility3154:
    """Every audit-write failure — best-effort paths included — is
    counted so /audit exposes it (#3154, security finding)."""

    async def test_best_effort_failure_bumps_counter(
        self, app_state, db, registry
    ):
        from unittest.mock import Mock

        spy = Mock()
        app_state.state.notifier = spy
        with patch.object(
            app_state.state.model.container_events,
            "record",
            AsyncMock(side_effect=RuntimeError("db gone")),
        ):
            await registry.record_container_event(
                "ws-a", "cid-1", EVENT_START, cause=CAUSE_API
            )
        assert registry.audit_write_failures == 1
        # The same failure alerts the SA/ISSO stream (#3250,
        # SV-222484/485), naming the source table.
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "audit.failure"
        assert kwargs["detail"] == {"table": "container_events"}

    async def test_finalize_failure_bumps_counter(self, app_state, db):
        registry = app_state.state.container_registry
        event_id = await app_state.state.model.container_events.record(
            "ws-a", EVENT_START, CAUSE_API
        )
        with patch.object(
            app_state.state.model.container_events,
            "finalize_event",
            AsyncMock(side_effect=RuntimeError("db gone")),
        ):
            await registry._finalize_prewritten_event(
                event_id, container_id="cid-2"
            )
        assert registry.audit_write_failures == 1


class TestAuditFailClosedModel3154:
    """The model primitives behind audit-before-act (#3154): record
    returns its row id, finalize fills only the known post-transition
    columns, retract removes a row whose transition never happened."""

    async def test_record_returns_row_id(self, app_state, db):
        events = app_state.state.model.container_events
        event_id = await events.record(
            "ws-a", EVENT_START, CAUSE_API, actor_id="u1"
        )
        assert isinstance(event_id, int)
        row = (await events.list_events("ws-a"))[0]
        assert row["id"] == event_id

    async def test_finalize_fills_known_columns_only(self, app_state, db):
        events = app_state.state.model.container_events
        event_id = await events.record("ws-a", EVENT_START, CAUSE_API)
        row = (await events.list_events("ws-a"))[0]
        assert row["container_id"] is None  # the pre-write shape
        await events.finalize_event(
            event_id, container_id="cid-9", network_namespace="net-1"
        )
        row = (await events.list_events("ws-a"))[0]
        assert row["container_id"] == "cid-9"
        assert row["network_namespace"] == "net-1"
        # None means "still unknown", never "clear the column".
        await events.finalize_event(event_id)
        await events.finalize_event(event_id, container_id="cid-10")
        row = (await events.list_events("ws-a"))[0]
        assert row["container_id"] == "cid-10"
        assert row["network_namespace"] == "net-1"

    async def test_retract_deletes_the_row(self, app_state, db):
        events = app_state.state.model.container_events
        event_id = await events.record("ws-a", EVENT_START, CAUSE_API)
        await events.retract_event(event_id)
        assert await events.count_events("ws-a") == 0


class TestAuditFailClosedStop3154:
    """Fail-closed stops (#3154): audit-before-act on the interactive
    causes, never on the autonomous ones."""

    async def test_interactive_stop_refused_before_teardown(
        self, app_state, db, registry, workspace
    ):
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-fc", ws_id)
        registry._ws_netns_owner[ws_id] = "sidecar-fc"
        with patch_podman(registry) as mocks:
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                with pytest.raises(AuditWriteError):
                    await registry.stop_and_remove_container(
                        "cid-fc",
                        workspace_id=ws_id,
                        cause=CAUSE_STOP,
                        actor_id="u1",
                    )
        # Refused before ANY side effect: no podman removal, no registry
        # teardown, no expected-stop bookkeeping, no row.
        mocks.remove_container.assert_not_awaited()
        assert ws_id not in registry.stopping
        assert registry.states[ws_id].container_id == "cid-fc"
        assert registry._ws_netns_owner[ws_id] == "sidecar-fc"
        assert registry.audit_write_failures == 1
        assert (
            await app_state.state.model.container_events.count_events(ws_id)
            == 0
        )

    async def test_interactive_stop_prewrites_final_row(
        self, app_state, db, registry, workspace
    ):
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-fc2", ws_id)
        registry._ws_netns_owner[ws_id] = "sidecar-fc2"
        with patch_podman(registry):
            ok = await registry.stop_and_remove_container(
                "cid-fc2",
                workspace_id=ws_id,
                cause=CAUSE_STOP,
                actor_id="u1",
            )
        assert ok is True
        row = (
            await app_state.state.model.container_events.list_events(ws_id)
        )[0]
        # All fields were known at pre-write; the row IS the final row.
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_STOP
        assert row["actor_id"] == "u1"
        assert row["container_id"] == "cid-fc2"
        assert row["network_namespace"] == "sidecar-fc2"

    async def test_failed_stop_retracts_prewritten_row(
        self, app_state, db, registry
    ):
        app_state.state.settings.audit_fail_closed = True
        registry.track_activity("cid-fail", "ws-x")
        with patch_podman(
            registry,
            remove_container=AsyncMock(side_effect=PodmanError(500, "boom")),
        ):
            ok = await registry.stop_and_remove_container(
                "cid-fail", workspace_id="ws-x", cause=CAUSE_STOP
            )
        assert ok is False
        # The stop did not happen, so the pre-written row must be gone.
        assert (
            await app_state.state.model.container_events.count_events("ws-x")
            == 0
        )
        assert registry.audit_write_failures == 0

    async def test_autonomous_stop_never_refused(
        self, app_state, db, registry
    ):
        """An idle-timeout stop with the audit DB down must still stop
        the container — refusing would keep it running AND lose the
        record (#3154)."""
        app_state.state.settings.audit_fail_closed = True
        registry.track_activity("cid-idle", "ws-i")
        with patch_podman(registry) as mocks:
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                ok = await registry.stop_and_remove_container(
                    "cid-idle", workspace_id="ws-i", cause=CAUSE_IDLE_TIMEOUT
                )
        assert ok is True
        mocks.remove_container.assert_awaited_once()
        assert registry.audit_write_failures == 1

    async def test_stop_refusal_precedes_killed_callback_teardown(
        self, app_state, db, registry, workspace
    ):
        """#3154 review B1: with the PRODUCTION on_workspace_killed
        wiring (reset_workspace_state → remove_state — what
        wire_registry_callbacks installs), a refused /stop must NOT
        lose the registry's tracking of the still-running container:
        the audit pre-write precedes notify_workspace_killed."""
        from klangk import wshandler

        app_state.state.sockets = wshandler.WebSocketState(app_state)

        async def on_killed(ws_id, container_id=None):
            await wshandler.reset_workspace_state(
                app_state.state.sockets,
                ws_id,
                expected_container_id=container_id,
            )

        registry.set_on_workspace_killed(on_killed)
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-b1", ws_id)
        with patch_podman(registry) as mocks:
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                with pytest.raises(AuditWriteError):
                    # The /stop route's ordering: pre-write, THEN notify,
                    # THEN stop.
                    pending = await registry.prewrite_stop_event(
                        ws_id,
                        "cid-b1",
                        cause=CAUSE_STOP,
                        actor_id="u1",
                    )
                    await registry.notify_workspace_killed(
                        ws_id, container_id="cid-b1"
                    )
                    await registry.stop_and_remove_container(
                        "cid-b1",
                        workspace_id=ws_id,
                        cause=CAUSE_STOP,
                        actor_id="u1",
                        pending_event=pending,
                    )
        mocks.remove_container.assert_not_awaited()
        # The tracking survived: the refusal happened before the
        # killed callback could tear it down.
        assert registry.states[ws_id].container_id == "cid-b1"

    async def test_mode_flip_mid_request_cannot_arm_late_refusal(
        self, app_state, db, registry, workspace
    ):
        """#3154 review: the /stop route evaluates the fail-closed gate
        once; a SIGHUP flipping the mode on mid-request (after the
        route's ungated pre-write, before the stop) must not arm the
        in-method pre-write — that refusal would fire after the route
        already emitted its death frames (``prewrite_decided``)."""
        ws_id = workspace["id"]
        registry.track_activity("cid-flip", ws_id)
        # The route half: mode off — the pre-write is skipped, no row.
        assert (
            await registry.prewrite_stop_event(
                ws_id, "cid-flip", cause=CAUSE_STOP, actor_id="u1"
            )
            is None
        )
        # The SIGHUP flip — and with it, a broken audit DB.
        app_state.state.settings.audit_fail_closed = True
        with patch_podman(registry) as mocks:
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                ok = await registry.stop_and_remove_container(
                    "cid-flip",
                    workspace_id=ws_id,
                    cause=CAUSE_STOP,
                    actor_id="u1",
                    prewrite_decided=True,
                )
        assert ok is True
        mocks.remove_container.assert_awaited_once()
        # The stop's own best-effort row failed — counted, not fatal.
        assert registry.audit_write_failures == 1

    async def test_rebound_stop_retracts_prewritten_row(
        self, app_state, db, registry, workspace
    ):
        """A stop whose target was re-bound away (stopped but not torn
        down — result False) retracts the pre-written row: the stop of
        THAT container did not complete (#3154)."""
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-new", ws_id)  # the re-bind
        with patch_podman(registry):
            ok = await registry.stop_and_remove_container(
                "cid-old",  # the stale id the caller held
                workspace_id=ws_id,
                cause=CAUSE_STOP,
                actor_id="u1",
            )
        assert ok is False
        assert (
            await app_state.state.model.container_events.count_events(ws_id)
            == 0
        )

    async def test_route_prewrite_flows_through_pending_event(
        self, app_state, db, registry, workspace
    ):
        """The /stop route's two-phase form (#3154 review B1):
        prewrite_stop_event's id passed back as ``pending_event``
        writes the row exactly once — the in-method pre-write is
        skipped, not duplicated."""
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-pp", ws_id)
        with patch_podman(registry):
            pending = await registry.prewrite_stop_event(
                ws_id, "cid-pp", cause=CAUSE_STOP, actor_id="u1"
            )
            assert pending is not None
            ok = await registry.stop_and_remove_container(
                "cid-pp",
                workspace_id=ws_id,
                cause=CAUSE_STOP,
                actor_id="u1",
                pending_event=pending,
            )
        assert ok is True
        assert (
            await app_state.state.model.container_events.count_events(ws_id)
            == 1
        )

    async def test_default_off_stays_best_effort(
        self, app_state, db, registry
    ):
        """The default (KLANGKD_AUDIT_FAIL_CLOSED unset) keeps the #2915
        behavior: an interactive stop succeeds, failure only counted."""
        assert app_state.state.settings.audit_fail_closed is False
        registry.track_activity("cid-be", "ws-be")
        with patch_podman(registry) as mocks:
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                ok = await registry.stop_and_remove_container(
                    "cid-be", workspace_id="ws-be", cause=CAUSE_STOP
                )
        assert ok is True
        mocks.remove_container.assert_awaited_once()
        assert registry.audit_write_failures == 1


class TestAuditFailClosedStart3154:
    """Fail-closed starts (#3154): the row is pre-written before the
    attempt, finalized with the podman ids on success, retracted when
    no transition happened."""

    async def test_interactive_start_refused_before_container(
        self, app_state, db, monkeypatch
    ):
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        inner = AsyncMock(return_value=("cid-new", "created"))
        with patch.object(registry, "start_container_inner", inner):
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                with pytest.raises(AuditWriteError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id="ws-s",
                            home_path="/home/x",
                            audit_cause=CAUSE_API,
                            audit_actor_id="u1",
                        )
                    )
        # Refused before the podman attempt.
        inner.assert_not_awaited()
        assert registry.audit_write_failures == 1
        assert "ws-s" not in registry.states

    async def test_interactive_start_prewrites_then_finalizes(
        self, app_state, db, monkeypatch
    ):
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-ok", "created")),
        ):
            await registry.start_container(
                ContainerStartSpec(
                    workspace_id="ws-s2",
                    home_path="/home/x",
                    audit_cause=CAUSE_API,
                    audit_actor_id="u1",
                )
            )
        row = (
            await app_state.state.model.container_events.list_events("ws-s2")
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == CAUSE_API
        assert row["actor_id"] == "u1"
        assert row["container_id"] == "cid-ok"

    async def test_connected_attach_retracts_prewritten_row(
        self, app_state, db, monkeypatch
    ):
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-old", "connected")),
        ):
            cid, status = await registry.start_container(
                ContainerStartSpec(workspace_id="ws-s3", home_path="/home/x")
            )
        assert (cid, status) == ("cid-old", "connected")
        # 'connected' is no transition — the pre-written row is retracted.
        assert (
            await app_state.state.model.container_events.count_events("ws-s3")
            == 0
        )

    async def test_retract_failure_leaves_over_record(
        self, app_state, db, monkeypatch
    ):
        """If the post-refusal retract fails, the pre-written row stays
        as an over-record of an attempted transition — counted and
        logged, never fatal (#3154; the safe direction for a trail)."""
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-old", "connected")),
        ):
            with patch.object(
                app_state.state.model.container_events,
                "retract_event",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                await registry.start_container(
                    ContainerStartSpec(
                        workspace_id="ws-r", home_path="/home/x"
                    )
                )
        assert registry.audit_write_failures == 1
        assert (
            await app_state.state.model.container_events.count_events("ws-r")
            == 1
        )

    async def test_ws_connect_start_excluded_from_gate(
        self, app_state, db, monkeypatch
    ):
        """The normal web-UI start path (WS connect) is user-initiated
        but NOT an API POST — excluded from the gate per the issue's
        scope; its audit failures stay counted-and-swallowed (#3154
        review nit)."""
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-ws", "created")),
        ):
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                cid, status = await registry.start_container(
                    ContainerStartSpec(
                        workspace_id="ws-wsc",
                        home_path="/home/x",
                        audit_cause=CAUSE_WS_CONNECT,
                        audit_actor_id="u1",
                    )
                )
        assert (cid, status) == ("cid-ws", "created")
        assert registry.audit_write_failures == 1

    async def test_restart_start_half_refusal_leaves_stopped(
        self, app_state, db, registry, workspace
    ):
        """A /restart whose stop half succeeded (under its own audited
        row) but whose start pre-write fails: AuditWriteError out of the
        start, stop row stands, workspace left stopped for a retry —
        the same shape as a mid-restart capacity refusal (#3154)."""
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        registry.track_activity("cid-rs", ws_id)
        calls = {"n": 0}

        async def flaky_record(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("db gone")
            return await real_record(*args, **kwargs)

        events = app_state.state.model.container_events
        real_record = events.record
        with patch_podman(registry):
            with patch.object(events, "record", side_effect=flaky_record):
                ok = await registry.stop_and_remove_container(
                    "cid-rs",
                    workspace_id=ws_id,
                    cause=CAUSE_RESTART,
                    actor_id="u1",
                )
                assert ok is True
                with pytest.raises(AuditWriteError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id=ws_id,
                            home_path="/tmp/home",
                            audit_cause=CAUSE_RESTART,
                            audit_actor_id="u1",
                        )
                    )
        row = (await events.list_events(ws_id))[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_RESTART
        assert ws_id not in registry.states

    async def test_autonomous_start_never_refused(
        self, app_state, db, monkeypatch
    ):
        """Boot auto-start with the audit DB down still starts the
        workspace — the failure is counted, not fatal (#3154)."""
        app_state.state.settings.audit_fail_closed = True
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_shared_home_dir",
            AsyncMock(),
        )
        with patch.object(
            registry,
            "start_container_inner",
            AsyncMock(return_value=("cid-a", "created")),
        ):
            with patch.object(
                app_state.state.model.container_events,
                "record",
                AsyncMock(side_effect=RuntimeError("db gone")),
            ):
                cid, status = await registry.start_container(
                    ContainerStartSpec(
                        workspace_id="ws-s4",
                        home_path="/home/x",
                        audit_cause=CAUSE_AUTO_START,
                    )
                )
        assert (cid, status) == ("cid-a", "created")
        assert registry.audit_write_failures == 1

    async def test_pre_create_refusal_retracts_prewritten_row(
        self, app_state, db, registry, workspace
    ):
        """A drain refusal after the pre-write leaves no phantom row
        (#3154): no container, no start."""
        app_state.state.settings.audit_fail_closed = True
        with patch_podman(registry):
            with patch.object(
                registry,
                "new_starts_blocked_reason",
                return_value="node is draining",
            ):
                with pytest.raises(NodeDrainingError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id=workspace["id"],
                            home_path="/tmp/home",
                        )
                    )
        assert (
            await app_state.state.model.container_events.count_events(
                workspace["id"]
            )
            == 0
        )

    async def test_bringup_failure_finalizes_prewritten_row(
        self, app_state, db, registry, workspace
    ):
        """The #2915 failed-start backstop under fail-closed: a container
        created but whose bringup raised is real, so the pre-written row
        is finalized with its container id rather than retracted."""
        app_state.state.settings.audit_fail_closed = True
        ws_id = workspace["id"]
        from klangk import ssl_trust
        from klangk.terminal import Terminal

        app_state.state.ssl_trust = ssl_trust.SSLTrust(app_state)
        app_state.state.terminal = Terminal(app_state)
        with patch_podman(registry):
            with patch.object(
                registry,
                "bringup",
                AsyncMock(side_effect=RuntimeError("exec failed")),
            ):
                with pytest.raises(RuntimeError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id=ws_id,
                            home_path="/tmp/home",
                            audit_cause=CAUSE_API,
                            audit_actor_id="u-9",
                        )
                    )
        row = (
            await app_state.state.model.container_events.list_events(ws_id)
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == CAUSE_API
        assert row["actor_id"] == "u-9"
        assert row["container_id"] == "new-cid"


class TestStartWorkspaceThreading:
    async def test_actor_and_cause_ride_the_spec(self, app_state, db):
        registry = app_state.state.container_registry
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            return spec.existing_container_id or "cid-new", "created"

        with patch.object(registry, "start_container", fake_start):
            cid, status = await app_state.state.workspaces.start_workspace(
                {
                    "id": "ws-a",
                    "user_id": "u1",
                    "container_id": "cid-old",
                },
                actor_id="u1",
                cause=CAUSE_AUTO_START,
            )
        assert (cid, status) == ("cid-old", "created")
        assert captured["spec"].audit_actor_id == "u1"
        assert captured["spec"].audit_cause == CAUSE_AUTO_START

    async def test_defaults_are_api_and_system_actor(self, app_state, db):
        registry = app_state.state.container_registry
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            return "cid-new", "created"

        with patch.object(registry, "start_container", fake_start):
            await app_state.state.workspaces.start_workspace(
                {"id": "ws-a", "user_id": "u1"}
            )
        assert captured["spec"].audit_actor_id is None
        assert captured["spec"].audit_cause == "api"


class TestWsConnectAttribution:
    async def test_ws_connect_start_carries_connecting_user(
        self, app_state, db, registry, user, workspace
    ):
        """The normal web-UI start path (first WS connection) records the
        connecting user with the ws_connect cause (#2915 review)."""
        from klangk.wshandler import WebSocketState

        app_state.state.sockets = WebSocketState(app_state)
        conn = _base_conn(user=user, app_state=app_state)
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            return "cid-ws", "created"

        with patch.object(
            app_state.state.container_registry,
            "start_container",
            fake_start,
        ):
            await conn.start_workspace_container(workspace["id"], workspace)
        assert captured["spec"].audit_cause == CAUSE_WS_CONNECT
        assert captured["spec"].audit_actor_id == user["id"]


class TestLogoutAttribution:
    async def _stop_user_containers(self, app_state, db, registry, uid):
        with patch.object(
            app_state.state.model.workspaces,
            "get_user_workspaces_with_containers",
            AsyncMock(
                return_value=[{"id": "ws-lo", "container_id": "cid-lo"}]
            ),
        ):
            with patch_podman(registry):
                await registry.stop_user_containers(uid)
        return (
            await app_state.state.model.container_events.list_events("ws-lo")
        )[0]

    async def test_logout_stop_attributes_the_user(
        self, app_state, db, registry
    ):
        row = await self._stop_user_containers(app_state, db, registry, "u-1")
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_LOGOUT
        assert row["actor_type"] == ACTOR_USER
        assert row["actor_id"] == "u-1"

    async def test_logout_stop_attributes_the_agent(
        self, app_state, db, registry
    ):
        row = await self._stop_user_containers(
            app_state, db, registry, AGENT_USER_ID
        )
        assert row["actor_type"] == ACTOR_AGENT
        assert row["actor_id"] == AGENT_USER_ID


class TestLabeledSweepAttribution:
    async def test_drain_sweep_records_labeled_workspace_stop(
        self, app_state, db, registry
    ):
        from klangk.container.sidecar import labeled_workspace_id

        leftover = {
            "Id": "cid-sweep",
            "Labels": {
                "klangk.workspace": "ws-sweep",
                "klangk.role": "workspace",
            },
        }
        assert labeled_workspace_id(leftover) == "ws-sweep"
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[leftover])
        ):
            assert await registry._sweep_drain_leftovers() == 1
        row = (
            await app_state.state.model.container_events.list_events(
                "ws-sweep"
            )
        )[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_DRAIN
        assert row["actor_type"] == ACTOR_SYSTEM

    async def test_sidecar_label_is_not_a_workspace(self):
        from klangk.container.sidecar import labeled_workspace_id

        sidecar = {
            "Id": "net-cid",
            "Labels": {
                "klangk.workspace": "ws-x",
                "klangk.role": "network-sidecar",
            },
        }
        assert labeled_workspace_id(sidecar) is None
        assert labeled_workspace_id({"Id": "x"}) is None

    async def test_shutdown_sweep_records_labeled_workspace_stop(
        self, app_state, db, registry, monkeypatch
    ):
        """The shutdown orphan loop attributes labeled workspace containers
        (#2915 review): a prior-session workspace stopped at shutdown
        keeps its stop row."""
        orphan = {
            "Id": "cid-orphan",
            "Labels": {
                "klangk.workspace": "ws-orphan",
                "klangk.role": "workspace",
            },
        }
        registry._cid_to_wsid.pop("cid-orphan", None)
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[orphan])
        ):
            with patch.object(
                registry, "notify_workspace_killed", AsyncMock()
            ):
                await registry.shutdown()
        row = (
            await app_state.state.model.container_events.list_events(
                "ws-orphan"
            )
        )[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == CAUSE_SHUTDOWN


class TestFailedStartBackstop:
    async def test_bringup_failure_still_records_start(
        self, app_state, db, registry, workspace
    ):
        """A container created but whose bringup raises is real and
        tracked — the start row must exist (#2915 review) so a later
        crash_teardown stop is not an orphan in the audit trail."""
        ws_id = workspace["id"]
        from klangk import ssl_trust
        from klangk.terminal import Terminal

        app_state.state.ssl_trust = ssl_trust.SSLTrust(app_state)
        app_state.state.terminal = Terminal(app_state)
        with patch_podman(registry):
            with patch.object(
                registry,
                "bringup",
                AsyncMock(side_effect=RuntimeError("exec failed")),
            ):
                with pytest.raises(RuntimeError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id=ws_id,
                            home_path="/tmp/home",
                            audit_cause=CAUSE_WS_CONNECT,
                            audit_actor_id="u-9",
                        )
                    )
        row = (
            await app_state.state.model.container_events.list_events(ws_id)
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == CAUSE_WS_CONNECT
        assert row["actor_id"] == "u-9"
        assert row["container_id"] == "new-cid"

    async def test_pre_create_refusal_records_nothing(
        self, app_state, db, registry, workspace
    ):
        """A refusal before any container exists (drain gate) leaves no
        start row — no container, no start."""
        with patch_podman(registry):
            with patch.object(
                registry,
                "new_starts_blocked_reason",
                return_value="node is draining",
            ):
                from klangk.exceptions import NodeDrainingError

                with pytest.raises(NodeDrainingError):
                    await registry.start_container(
                        ContainerStartSpec(
                            workspace_id=workspace["id"],
                            home_path="/tmp/home",
                        )
                    )
        assert (
            await app_state.state.model.container_events.count_events(
                workspace["id"]
            )
            == 0
        )


class TestSidecarLifecycle:
    async def test_sidecar_start_recorded(self, app_state, db, registry):
        """A filtered workspace's sidecar create lands as a system-caused
        sidecar_start row with no netns owner (it IS the owner)."""
        with patch_podman(registry):
            cid = await registry.start_network_sidecar(
                "ws-sc", ["allow.example.com"]
            )
        assert cid == "new-cid"
        row = (
            await app_state.state.model.container_events.list_events("ws-sc")
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == "sidecar_start"
        assert row["container_role"] == "network-sidecar"
        assert row["actor_type"] == ACTOR_SYSTEM
        assert row["container_id"] == "new-cid"
        assert row["network_namespace"] is None

    async def test_sidecar_stop_recorded_via_label_removal(
        self, app_state, db, registry
    ):
        """The label-based sidecar removal choke point (workspace teardown,
        stale-generation clear, failure teardown) records sidecar_stop."""
        sidecar = {
            "Id": "net-cid",
            "Labels": {
                "klangk.workspace": "ws-sc",
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[sidecar])
        ):
            assert await registry._remove_network_sidecar("ws-sc") is True
        row = (
            await app_state.state.model.container_events.list_events("ws-sc")
        )[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == "sidecar_stop"
        assert row["container_role"] == "network-sidecar"
        assert row["container_id"] == "net-cid"
        assert row["network_namespace"] is None

    async def test_sidecar_role_never_inherits_netns_owner(
        self, app_state, db, registry
    ):
        registry._ws_netns_owner["ws-sc"] = "sidecar-live"
        await registry.record_container_event(
            "ws-sc",
            "cid-x",
            EVENT_STOP,
            cause="sidecar_stop",
            container_role="network-sidecar",
        )
        row = (
            await app_state.state.model.container_events.list_events("ws-sc")
        )[0]
        assert row["network_namespace"] is None


class TestSweepSidecarAttribution:
    async def test_drain_sweep_records_sidecar_stop(
        self, app_state, db, registry
    ):
        leftover = {
            "Id": "net-cid",
            "Labels": {
                "klangk.workspace": "ws-sc",
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[leftover])
        ):
            assert await registry._sweep_drain_leftovers() == 1
        row = (
            await app_state.state.model.container_events.list_events("ws-sc")
        )[0]
        assert row["event"] == EVENT_STOP
        # Sidecar sweep stops route through the label-based choke point,
        # so their cause is sidecar_stop (not the sweep's cause).
        assert row["cause"] == "sidecar_stop"
        assert row["container_role"] == "network-sidecar"
        assert row["network_namespace"] is None
        assert row["actor_type"] == ACTOR_SYSTEM

    async def test_drain_sweep_unlabeled_sidecar_records_nothing(
        self, app_state, db, registry
    ):
        """A sidecar without its workspace label cannot be correlated —
        stopped (counted) but not recorded."""
        leftover = {
            "Id": "net-cid",
            "Labels": {"klangk.role": "network-sidecar"},
        }
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[leftover])
        ):
            assert await registry._sweep_drain_leftovers() == 1
        assert await app_state.state.model.container_events.count_events() == 0


class TestSweepSidecarSingleRow:
    async def test_workspace_then_sidecar_sweep_records_each_once(
        self, app_state, db, registry
    ):
        """The blocking double-record from review: a drain sweep whose
        leftover list holds a workspace container AND its sidecar must
        produce exactly one stop row per container — the sidecar routes
        through the label-based choke point, so the already-removed
        sidecar's stale sweep entry finds nothing to record."""
        ws_leftover = {
            "Id": "cid-ws",
            "Labels": {
                "klangk.workspace": "ws-pair",
                "klangk.role": "workspace",
            },
        }
        sidecar = {
            "Id": "net-cid",
            "Labels": {
                "klangk.workspace": "ws-pair",
                "klangk.role": "network-sidecar",
            },
        }
        label_lists = [[sidecar], []]

        async def fake_list(filter_arg):
            if "klangk.instance=" in filter_arg:
                return [ws_leftover, sidecar]
            # workspace-label listing: the sidecar once, then gone
            # (removed by the workspace stop's teardown).
            return label_lists.pop(0) if label_lists else []

        with patch_podman(
            registry, list_containers=AsyncMock(side_effect=fake_list)
        ):
            assert await registry._sweep_drain_leftovers() == 2
        rows = await app_state.state.model.container_events.list_events(
            "ws-pair"
        )
        stops = [r for r in rows if r["event"] == EVENT_STOP]
        assert len(stops) == 2
        by_role = {r["container_role"]: r for r in stops}
        assert by_role["workspace"]["cause"] == CAUSE_DRAIN
        assert by_role["workspace"]["container_id"] == "cid-ws"
        assert by_role["network-sidecar"]["cause"] == "sidecar_stop"
        assert by_role["network-sidecar"]["container_id"] == "net-cid"
        assert by_role["network-sidecar"]["network_namespace"] is None
        assert by_role["network-sidecar"]["actor_type"] == ACTOR_SYSTEM


class TestReaperAttribution:
    async def test_instance_reap_records_both_roles(
        self, app_state, db, registry
    ):
        leftover_ws = {
            "Id": "cid-reap-ws",
            "Labels": {
                "klangk.workspace": "ws-reap",
                "klangk.role": "workspace",
            },
        }
        leftover_sc = {
            "Id": "cid-reap-sc",
            "Labels": {
                "klangk.workspace": "ws-reap",
                "klangk.role": "network-sidecar",
            },
        }
        with patch_podman(
            registry,
            list_containers=AsyncMock(return_value=[leftover_ws, leftover_sc]),
        ):
            await registry.reap_instance_containers()
        rows = {
            r["container_id"]: r
            for r in await app_state.state.model.container_events.list_events(
                "ws-reap"
            )
        }
        assert rows["cid-reap-ws"]["cause"] == "reap"
        assert rows["cid-reap-ws"]["container_role"] == "workspace"
        assert rows["cid-reap-sc"]["cause"] == "reap"
        assert rows["cid-reap-sc"]["container_role"] == "network-sidecar"

    async def test_labelless_reap_records_nothing(
        self, app_state, db, registry
    ):
        leftover = {"Id": "cid-anon", "Labels": {"klangk.managed": "true"}}
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[leftover])
        ):
            await registry.reap_instance_containers()
        assert await app_state.state.model.container_events.count_events() == 0


class TestDependentRemovalAttribution:
    async def test_dependent_teardown_records_workspace_stop(
        self, app_state, db, registry
    ):
        dependent = {
            "Id": "cid-dep",
            "Labels": {
                "klangk.workspace": "ws-dep",
                "klangk.role": "workspace",
            },
        }
        with patch_podman(
            registry, list_containers=AsyncMock(return_value=[dependent])
        ):
            await registry._remove_dependent_workspace_containers("ws-dep")
        row = (
            await app_state.state.model.container_events.list_events("ws-dep")
        )[0]
        assert row["event"] == EVENT_STOP
        assert row["cause"] == "sidecar_dependent"
        assert row["container_role"] == "workspace"
        assert row["container_id"] == "cid-dep"


class TestSidecarStartFailureBackstop:
    async def test_start_row_survives_start_failure(
        self, app_state, db, registry
    ):
        from klangk.podman import PodmanError

        with patch_podman(registry):
            with patch.object(
                registry,
                "start_with_port_conflict_retry",
                AsyncMock(side_effect=PodmanError(500, "port clash")),
            ):
                with pytest.raises(PodmanError):
                    await registry.start_network_sidecar(
                        "ws-fail", ["allow.example.com"]
                    )
        row = (
            await app_state.state.model.container_events.list_events("ws-fail")
        )[0]
        assert row["event"] == EVENT_START
        assert row["cause"] == "sidecar_start"
        assert row["container_id"] == "new-cid"

    async def test_start_row_survives_readiness_timeout(
        self, app_state, db, registry
    ):
        from klangk.podman import PodmanError

        with patch_podman(registry):
            with patch.object(
                registry,
                "_wait_sidecar_proxy_ready",
                AsyncMock(side_effect=PodmanError(500, "never ready")),
            ):
                with pytest.raises(PodmanError):
                    await registry.start_network_sidecar(
                        "ws-fail2", ["allow.example.com"]
                    )
        row = (
            await app_state.state.model.container_events.list_events(
                "ws-fail2"
            )
        )[0]
        assert row["event"] == EVENT_START
        assert row["container_id"] == "new-cid"
