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
from klangk.container.spec import ContainerStartSpec
from klangk.model.container_events import (
    ACTOR_AGENT,
    ACTOR_SYSTEM,
    ACTOR_USER,
    CAUSE_AUTO_START,
    CAUSE_IDLE_TIMEOUT,
    CAUSE_STOP,
    EVENT_START,
    EVENT_STOP,
    actor_type_for,
)
from klangk.model.users import AGENT_USER_ID


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
