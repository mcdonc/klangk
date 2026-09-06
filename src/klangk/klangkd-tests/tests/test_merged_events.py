"""Time-correlated merged audit stream (#3251).

Covers the ``MergedEventsModel`` union read over the three audit
tables (``audit_events``, ``container_events``, ``egress_consent``),
its filters (time window, actor, workspace, event name), pagination
across the merge, the per-table ``rows_by_ids`` detail fetch, and the
``GET /events`` HTTP surface (``manage-events`` gated).
"""

import pytest

import test_api
from test_api import _admin_login, _auth_headers
from httpx import ASGITransport, AsyncClient
from klangk.model.egress_consent import DECISION_ALLOWED, DECISION_DENIED
from klangk.model.container_events import CAUSE_API, EVENT_START, EVENT_STOP
from klangk.model.merged_events import MergedEventFilters

# test_api's app fixture, re-bound under a module-local name (the
# test_audit_events.py pattern).
api_app = test_api.app


@pytest.fixture
async def api_client(api_app):
    """An HTTP client over ``api_app``."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _retimestamp(app_state, table, row_id, when, column="created_at"):
    """Set one row's event timestamp directly (the record() paths stamp
    wall-clock time; the merge tests need controlled interleaving)."""
    async with app_state.state.db.transaction() as db:
        await db.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?", (when, row_id)
        )


async def _retimestamp_newest_audit(app_state, when) -> None:
    """Retimestamp the newest audit row (the one just recorded)."""
    rows = await app_state.state.model.audit_events.list_events(limit=1)
    await _retimestamp(app_state, "audit_events", rows[0]["id"], when)


async def _other_user(app_state, email="other@example.com"):
    """A second real user (consent rows' deciders FK to users)."""
    return await app_state.state.model.users.create_user(
        email, "not-a-real-hash", verified=True
    )


class TestMergedEventsModel:
    async def test_merge_orders_across_tables_by_time(
        self, app_state, db, user
    ):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "merge-ws"
        )
        # One row per table at staggered times: audit oldest, container
        # middle, egress newest.
        await app_state.state.model.audit_events.record(
            "login", actor_id=user["id"], actor_email=user["email"]
        )
        await _retimestamp_newest_audit(app_state, 1000.0)
        cid = await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API, actor_id=user["id"]
        )
        await _retimestamp(app_state, "container_events", cid, 2000.0)
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "example.com"
        )
        await _retimestamp(
            app_state,
            "egress_consent",
            consent["id"],
            3000.0,
            column="requested_at",
        )
        rows = await merged.list_events()
        assert [r["source"] for r in rows[:3]] == [
            "egress",
            "container",
            "audit",
        ]
        # The merged shape: common correlation fields + the full origin
        # row in data (never the hmac tag).
        audit = next(r for r in rows if r["source"] == "audit")
        assert audit["event"] == "login"
        assert audit["actor_email"] == user["email"]
        assert audit["workspace_id"] is None
        assert audit["data"]["actor_id"] == user["id"]
        assert "hmac" not in audit["data"]
        container = next(r for r in rows if r["source"] == "container")
        assert container["workspace_id"] == ws["id"]
        assert container["data"]["cause"] == CAUSE_API
        egress = next(r for r in rows if r["source"] == "egress")
        assert egress["event"] == "egress.pending"
        assert egress["actor_id"] is None
        assert egress["data"]["dest_host"] == "example.com"

    async def test_decided_consent_names_decision_and_decider(
        self, app_state, db, user
    ):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "decide-ws"
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "denied.example"
        )
        await app_state.state.model.egress_consent.decide(
            consent["id"], DECISION_DENIED, user["id"]
        )
        rows = await merged.list_events(
            MergedEventFilters(event="egress.denied")
        )
        assert len(rows) == 1
        assert rows[0]["event"] == "egress.denied"
        assert rows[0]["actor_id"] == user["id"]
        assert rows[0]["data"]["decided_by"] == user["id"]

    async def test_time_window(self, app_state, db, user):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "window-ws"
        )
        # One row per table at 100 / 200 / 300.
        await app_state.state.model.audit_events.record(
            "login", actor_id=user["id"]
        )
        await _retimestamp_newest_audit(app_state, 100.0)
        cid = await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API
        )
        await _retimestamp(app_state, "container_events", cid, 200.0)
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "window.example"
        )
        await _retimestamp(
            app_state,
            "egress_consent",
            consent["id"],
            300.0,
            column="requested_at",
        )

        async def sources(**kwargs):
            rows = await merged.list_events(MergedEventFilters(**kwargs))
            return {r["source"] for r in rows}

        assert await sources(since=150.0) == {"container", "egress"}
        assert await sources(until=250.0) == {"audit", "container"}
        assert await sources(since=150.0, until=250.0) == {"container"}

    async def test_actor_filter_matches_all_sources(self, app_state, db, user):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "actor-ws"
        )
        other = await _other_user(app_state)
        await app_state.state.model.audit_events.record(
            "user.delete", actor_id=user["id"], actor_email=user["email"]
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API, actor_id=user["id"]
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "actor.example"
        )
        await app_state.state.model.egress_consent.decide(
            consent["id"], DECISION_ALLOWED, user["id"]
        )
        # Unrelated rows that must not match: the other user in each
        # table.
        await app_state.state.model.audit_events.record(
            "login", actor_id=other["id"], actor_email=other["email"]
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_STOP, CAUSE_API, actor_id=other["id"]
        )
        other_consent = (
            await app_state.state.model.egress_consent.create_request(
                ws["id"], "other.example"
            )
        )
        await app_state.state.model.egress_consent.decide(
            other_consent["id"], DECISION_DENIED, other["id"]
        )
        rows = await merged.list_events(MergedEventFilters(actor=user["id"]))
        assert {r["source"] for r in rows} == {"audit", "container", "egress"}
        assert all(r["actor_id"] == user["id"] for r in rows)

    async def test_actor_filter_matches_email_via_users_join(
        self, app_state, db, user
    ):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "email-ws"
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API, actor_id=user["id"]
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "email.example"
        )
        await app_state.state.model.egress_consent.decide(
            consent["id"], DECISION_ALLOWED, user["id"]
        )
        # The audit table stores its email denormalized; container and
        # egress rows resolve theirs through the users table.
        await app_state.state.model.audit_events.record(
            "login", actor_id=user["id"], actor_email=user["email"]
        )
        email_fragment = user["email"].split("@")[0]
        rows = await merged.list_events(
            MergedEventFilters(actor=email_fragment)
        )
        assert {r["source"] for r in rows} == {"audit", "container", "egress"}

    async def test_workspace_filter_matches_all_sources(
        self, app_state, db, user
    ):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "acme-production"
        )
        other_ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "unrelated-ws"
        )
        await app_state.state.model.audit_events.record(
            "workspace.member.add",
            actor_id=user["id"],
            target_type="workspace",
            target_id=ws["id"],
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API, actor_id=user["id"]
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "acme.example"
        )
        await _retimestamp(
            app_state,
            "egress_consent",
            consent["id"],
            500.0,
            column="requested_at",
        )
        # Rows tied to the unrelated workspace must not match.
        await app_state.state.model.audit_events.record(
            "workspace.member.add",
            actor_id=user["id"],
            target_type="workspace",
            target_id=other_ws["id"],
        )
        await app_state.state.model.container_events.record(
            other_ws["id"], EVENT_STOP, CAUSE_API
        )
        for workspace in (ws["id"], "acme"):
            rows = await merged.list_events(
                MergedEventFilters(workspace=workspace)
            )
            assert {r["source"] for r in rows} == {
                "audit",
                "container",
                "egress",
            }

    async def test_event_filter(self, app_state, db, user):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "event-ws"
        )
        await app_state.state.model.audit_events.record(
            "login.failed", actor_id=None
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "event.example"
        )
        await app_state.state.model.egress_consent.decide(
            consent["id"], DECISION_ALLOWED, user["id"]
        )
        login_rows = await merged.list_events(
            MergedEventFilters(event="login")
        )
        assert all(r["event"] == "login.failed" for r in login_rows)
        start_rows = await merged.list_events(
            MergedEventFilters(event="start")
        )
        assert all(r["source"] == "container" for r in start_rows)
        egress_rows = await merged.list_events(
            MergedEventFilters(event="egress")
        )
        assert {r["event"] for r in egress_rows} == {"egress.allowed"}
        denied_rows = await merged.list_events(
            MergedEventFilters(event="egress.denied")
        )
        assert denied_rows == []

    async def test_pagination_across_the_merge(self, app_state, db, user):
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "page-ws"
        )
        for i in range(5):
            await app_state.state.model.audit_events.record(
                "login", actor_id=user["id"]
            )
            await _retimestamp_newest_audit(app_state, 1000.0 + i)
            await app_state.state.model.container_events.record(
                ws["id"], EVENT_START, CAUSE_API
            )
        total = await merged.count_events()
        assert total == 10
        page1 = await merged.list_events(limit=4, offset=0)
        page2 = await merged.list_events(limit=4, offset=4)
        page3 = await merged.list_events(limit=4, offset=8)
        assert len(page1) == len(page2) == 4
        assert len(page3) == 2
        ids = [(r["source"], str(r["id"])) for r in page1 + page2 + page3]
        assert len(set(ids)) == 10
        stamps = [r["created_at"] for r in page1 + page2 + page3]
        assert stamps == sorted(stamps, reverse=True)

    async def test_list_and_count_without_filters(self, app_state, db, user):
        """The filters-defaulting path (None -> no filter)."""
        merged = app_state.state.model.merged_events
        await app_state.state.model.audit_events.record(
            "login", actor_id=user["id"]
        )
        assert await merged.count_events() == 1
        rows = await merged.list_events()
        assert len(rows) == 1
        assert rows[0]["source"] == "audit"

    async def test_same_timestamp_tie_break_is_deterministic(
        self, app_state, db, user
    ):
        """Rows sharing an instant order by source, then id (newest
        first within a source) — the tie-break pagination rests on."""
        merged = app_state.state.model.merged_events
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "tie-ws"
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "tie.example"
        )
        when = 1234.5
        await _retimestamp(
            app_state,
            "egress_consent",
            consent["id"],
            when,
            column="requested_at",
        )
        first = await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API
        )
        await _retimestamp(app_state, "container_events", first, when)
        second = await app_state.state.model.container_events.record(
            ws["id"], EVENT_STOP, CAUSE_API
        )
        await _retimestamp(app_state, "container_events", second, when)
        await app_state.state.model.audit_events.record(
            "login", actor_id=user["id"]
        )
        a1 = (await app_state.state.model.audit_events.list_events(limit=1))[0]
        await _retimestamp(app_state, "audit_events", a1["id"], when)
        rows = await merged.list_events()
        # Same instant: audit < container < egress alphabetically, and
        # within one source the higher id (newer insert) reads first.
        assert [(r["source"], r["id"]) for r in rows[:3]] == [
            ("audit", a1["id"]),
            ("container", second),
            ("container", first),
        ]
        assert rows[3]["source"] == "egress"

    async def test_rows_by_ids_empty_short_circuits(self, app_state, db):
        model = app_state.state.model
        assert await model.audit_events.rows_by_ids([]) == {}
        assert await model.container_events.rows_by_ids([]) == {}
        assert await model.egress_consent.rows_by_ids([]) == {}


class TestMergedEventsAPI:
    async def _seed(self, app_state, user):
        """One row in each table tied to one actor + workspace (three
        rows total — the admin's own login adds a fourth audit row)."""
        ws = await app_state.state.model.workspaces.create_workspace(
            user["id"], "api-ws"
        )
        await app_state.state.model.audit_events.record(
            "workspace.member.add",
            actor_id=user["id"],
            actor_email=user["email"],
            target_type="workspace",
            target_id=ws["id"],
        )
        await app_state.state.model.container_events.record(
            ws["id"], EVENT_START, CAUSE_API, actor_id=user["id"]
        )
        consent = await app_state.state.model.egress_consent.create_request(
            ws["id"], "api.example"
        )
        await app_state.state.model.egress_consent.decide(
            consent["id"], DECISION_ALLOWED, user["id"]
        )
        return ws

    async def test_lists_merged_stream_for_admin(
        self, api_client, api_app, admin_user
    ):
        await self._seed(api_app, admin_user)
        headers = await _admin_login(api_client)
        resp = await api_client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        sources = {i["source"] for i in body["items"]}
        assert sources == {"audit", "container", "egress"}
        # Three seeded rows + the admin's own login row.
        assert body["total"] == 4
        for item in body["items"]:
            assert "hmac" not in item
            assert "hmac" not in item["data"]
            if item["source"] == "audit" and item["event"] == "login":
                continue
            assert item["workspace_name"] == "api-ws"
            assert item["actor_email"] == admin_user["email"]

    async def test_actor_filter_across_sources(
        self, api_client, api_app, admin_user
    ):
        await self._seed(api_app, admin_user)
        headers = await _admin_login(api_client)
        resp = await api_client.get(
            "/api/v1/events",
            params={"actor": admin_user["id"]},
            headers=headers,
        )
        body = resp.json()
        assert {i["source"] for i in body["items"]} == {
            "audit",
            "container",
            "egress",
        }

    async def test_time_window_params(self, api_client, api_app, admin_user):
        await self._seed(api_app, admin_user)
        headers = await _admin_login(api_client)
        now = 4102444800.0  # far future: nothing was stamped this late
        resp = await api_client.get(
            "/api/v1/events",
            params={"since": now},
            headers=headers,
        )
        assert resp.json()["total"] == 0
        resp = await api_client.get(
            "/api/v1/events",
            params={"until": now},
            headers=headers,
        )
        assert resp.json()["total"] == 4

    async def test_pagination_envelope(self, api_client, api_app, admin_user):
        await self._seed(api_app, admin_user)
        headers = await _admin_login(api_client)
        resp = await api_client.get(
            "/api/v1/events",
            params={"limit": 2, "offset": 1},
            headers=headers,
        )
        body = resp.json()
        assert body["limit"] == 2
        assert body["offset"] == 1
        assert body["total"] == 4
        assert len(body["items"]) == 2

    async def test_requires_manage_events(self, api_client, user):
        headers = await _auth_headers(api_client)
        resp = await api_client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 403
