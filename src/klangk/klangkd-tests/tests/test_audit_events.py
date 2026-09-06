"""Identity and privilege audit events (#3205).

Covers the ``audit_events`` table (migration 0034), the
``AuditEventsModel`` CRUD + prune, the recording choke points in
``auth.py`` (``issue_token`` login rows, ``_reject_bad_credentials``
login.failed rows, session-limit revocation), the HTTP emit sites
(account CRUD, group/ACL/role changes, self-service account changes,
logout), and the ``GET /events/audit`` listing.
"""

from unittest.mock import AsyncMock, patch

import pytest

import test_api
from test_api import _admin_login, _auth_headers
from httpx import ASGITransport, AsyncClient
from klangk import auth as klangk_auth
from klangk.model.audit_events import filter_clause, row_to_dict

# test_api's app fixture, re-bound under a module-local name: pytest
# registers it here (fixture discovery walks the module namespace) and
# an assignment — unlike an import — is not shadowed by test parameters
# of the same name (F811).
api_app = test_api.app


@pytest.fixture
async def api_client(api_app):
    """An HTTP client over ``api_app`` (test_api's client fixture depends
    on a fixture named ``app``, which this module does not register)."""
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _events(api_app, name):
    """The audit rows for one event name, newest first."""
    return await api_app.state.model.audit_events.list_events(event=name)


async def _rows_for(app_state, name):
    return await app_state.state.model.audit_events.list_events(event=name)


class TestFilterClause:
    def test_no_filters(self):
        assert filter_clause(None, None, None) == ("", [])
        assert filter_clause("", "", "") == ("", [])

    def test_event_only(self):
        where, params = filter_clause("login", None, None)
        assert where == " WHERE event LIKE '%' || ? || '%'"
        assert params == ["login"]

    def test_actor_only_matches_id_or_email(self):
        where, params = filter_clause(None, "someone", None)
        assert "actor_id LIKE" in where and "actor_email LIKE" in where
        assert params == ["someone", "someone"]

    def test_target_only(self):
        where, params = filter_clause(None, None, "ws-1")
        assert where == " WHERE target_id LIKE '%' || ? || '%'"
        assert params == ["ws-1"]

    def test_all_three_join_with_and(self):
        where, params = filter_clause("login", "u1", "ws-1")
        assert where.count("AND") == 2
        assert len(params) == 4


class TestAuditEventsModel:
    async def test_record_and_list_newest_first(self, app_state, db):
        events = app_state.state.model.audit_events
        await events.record(
            "login",
            actor_id="u1",
            actor_email="u1@example.com",
            target_type="user",
            target_id="u1",
            detail={"via": "password"},
            source_ip="10.0.0.1",
            user_agent="pytest-agent",
            method="POST",
            referer="https://klangk.example/login",
        )
        await events.record(
            "logout", actor_id="u1", actor_email="u1@example.com"
        )
        rows = await events.list_events()
        assert [r["event"] for r in rows] == ["logout", "login"]
        login = rows[1]
        assert login["actor_id"] == "u1"
        assert login["actor_email"] == "u1@example.com"
        assert login["target_type"] == "user"
        assert login["target_id"] == "u1"
        assert login["detail"] == {"via": "password"}
        assert login["source_ip"] == "10.0.0.1"
        assert login["user_agent"] == "pytest-agent"
        assert login["method"] == "POST"
        assert login["referer"] == "https://klangk.example/login"
        assert login["created_at"] > 0
        # The off-request row records NULL for both #3255 fields —
        # no HTTP request backs it.
        assert rows[0]["method"] is None
        assert rows[0]["referer"] is None

    async def test_row_without_detail(self, app_state, db):
        events = app_state.state.model.audit_events
        await events.record("logout")
        rows = await events.list_events()
        assert rows[0]["detail"] is None
        assert rows[0]["actor_id"] is None

    def test_row_to_dict_parses_detail(self):
        row = (
            1,
            "login",
            "u1",
            "u1@example.com",
            "user",
            "u1",
            '{"via": "password"}',
            "10.0.0.1",
            "ua",
            "POST",
            "https://klangk.example/login",
            1.0,
            None,
        )
        d = row_to_dict(row)
        assert d["detail"] == {"via": "password"}
        assert d["method"] == "POST"
        assert d["referer"] == "https://klangk.example/login"

    def test_row_to_dict_none_detail_stays_none(self):
        row = (
            1,
            "logout",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1.0,
            None,
        )
        assert row_to_dict(row)["detail"] is None

    async def test_list_and_count_filters(self, app_state, db):
        events = app_state.state.model.audit_events
        await events.record(
            "login",
            actor_id="u1",
            actor_email="alice@example.com",
            target_type="user",
            target_id="u1",
        )
        await events.record(
            "group.member.add",
            actor_id="u2",
            actor_email="bob@example.com",
            target_type="group",
            target_id="g1",
        )
        assert await events.count_events() == 2
        assert await events.count_events(event="login") == 1
        assert await events.count_events(actor="alice") == 1
        assert await events.count_events(actor="bob@example") == 1
        assert await events.count_events(target="g1") == 1
        assert await events.count_events(event="workspace") == 0
        rows = await events.list_events(event="login", limit=10, offset=0)
        assert [r["event"] for r in rows] == ["login"]

    async def test_prune_disabled_when_both_knobs_zero(self, app_state, db):
        events = app_state.state.model.audit_events
        await events.record("login")
        app_state.state.settings.audit_events_retention_days = 0
        app_state.state.settings.audit_events_row_cap = 0
        assert await events.prune(now=9e9) == 0
        assert await events.count_events() == 1

    async def test_prune_retention_window(self, app_state, db):
        events = app_state.state.model.audit_events
        await events.record("login")
        app_state.state.settings.audit_events_retention_days = 30
        app_state.state.settings.audit_events_row_cap = 0
        # Now is far past the 30-day window -> the row goes.
        assert await events.prune(now=9e9) == 1
        assert await events.count_events() == 0

    async def test_prune_row_cap_keeps_newest(self, app_state, db):
        events = app_state.state.model.audit_events
        for _ in range(5):
            await events.record("login")
        app_state.state.settings.audit_events_retention_days = 0
        app_state.state.settings.audit_events_row_cap = 2
        assert await events.prune() == 3
        assert await events.count_events() == 2

    async def test_row_cap_is_per_class(self, app_state, db):
        """A flood of unauthenticated login.failed rows can evict only
        other login.failed rows — never the privileged-action history
        (#3205 review)."""
        events = app_state.state.model.audit_events
        await events.record("user.delete", target_type="user")
        for _ in range(4):
            await events.record("login.failed")
        app_state.state.settings.audit_events_retention_days = 0
        app_state.state.settings.audit_events_row_cap = 2
        assert await events.prune() == 2
        assert await events.count_events(event="user.delete") == 1
        assert await events.count_events(event="login.failed") == 2

    async def test_prune_both_passes(self, app_state, db):
        events = app_state.state.model.audit_events
        for _ in range(4):
            await events.record("login")
        app_state.state.settings.audit_events_retention_days = 30
        app_state.state.settings.audit_events_row_cap = 2
        # Far-future clock: retention deletes everything first, so the
        # cap pass finds nothing left to trim.
        assert await events.prune(now=9e9) == 4
        assert await events.count_events() == 0

    async def test_record_best_effort_swallows_failure(
        self, app_state, db, caplog
    ):
        """A failed audit write is logged, never raised (#3205) — and
        alerts the SA/ISSO stream as audit.failure (#3250,
        SV-222484/485)."""
        import logging
        from unittest.mock import Mock

        events = app_state.state.model.audit_events
        spy = Mock()
        app_state.state.notifier = spy
        with patch.object(
            events,
            "record",
            AsyncMock(side_effect=RuntimeError("db locked")),
        ):
            with caplog.at_level(
                logging.WARNING, logger="klangk.model.audit_events"
            ):
                await events.record_best_effort("login")
        assert "audit_events write failed" in caplog.text
        assert await events.count_events() == 0
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "audit.failure"
        assert kwargs["detail"]["table"] == "audit_events"
        assert kwargs["detail"]["failed_event"] == "login"


class TestAuthChokePoints:
    async def test_login_records_event_with_metadata(
        self, app_state, user, db
    ):
        await app_state.state.auth.login(
            klangk_auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            ),
            source_ip="10.9.8.7",
            user_agent="audit-probe/1.0",
        )
        rows = await _rows_for(app_state, "login")
        assert len(rows) == 1
        row = rows[0]
        assert row["actor_id"] == user["id"]
        assert row["actor_email"] == "testuser@example.com"
        assert row["detail"] == {"via": "password"}
        assert row["source_ip"] == "10.9.8.7"
        assert row["user_agent"] == "audit-probe/1.0"

    async def test_via_distinguishes_the_issuing_path(
        self, app_state, user, db
    ):
        await app_state.state.auth.issue_token(
            user["id"], user["email"], via="oidc"
        )
        rows = await _rows_for(app_state, "login")
        assert rows[0]["detail"] == {"via": "oidc"}

    async def test_failed_login_for_known_user(self, app_state, user, db):
        with pytest.raises(Exception):
            await app_state.state.auth.login(
                klangk_auth.LoginRequest(
                    identifier="testuser@example.com",
                    password="wrong-password",
                ),
                source_ip="10.0.0.9",
            )
        rows = await _rows_for(app_state, "login.failed")
        assert len(rows) == 1
        assert rows[0]["target_id"] == user["id"]
        assert rows[0]["detail"] == {"identifier": "testuser@example.com"}
        assert rows[0]["source_ip"] == "10.0.0.9"
        assert rows[0]["actor_id"] is None

    async def test_failed_login_for_unknown_identifier(self, app_state, db):
        with pytest.raises(Exception):
            await app_state.state.auth.login(
                klangk_auth.LoginRequest(
                    identifier="ghost@example.com", password="whatever"
                ),
                source_ip="10.0.0.9",
            )
        rows = await _rows_for(app_state, "login.failed")
        assert rows[0]["target_id"] is None
        assert rows[0]["detail"] == {"identifier": "ghost@example.com"}

    async def test_failed_login_identifier_is_bounded(self, app_state, db):
        """An attacker-chosen identifier is truncated in the audit row
        (#3205 review) — the text a login.failed detail can carry is
        bounded even though the identifier itself is not."""
        with pytest.raises(Exception):
            await app_state.state.auth.login(
                klangk_auth.LoginRequest(
                    identifier="x" * 5000, password="whatever"
                ),
            )
        rows = await _rows_for(app_state, "login.failed")
        assert len(rows[0]["detail"]["identifier"]) == (
            klangk_auth.AUDIT_IDENTIFIER_MAX
        )

    async def test_locked_out_attempt_is_audited(self, app_state, user, db):
        """A 429 from an active lockout leaves its own login.failed row
        (#3205 review) — the locked-out window must not be blank in the
        audit stream."""
        req = klangk_auth.LoginRequest(
            identifier="testuser@example.com", password="wrong"
        )
        for _ in range(app_state.state.settings.login_lockout_failures):
            with pytest.raises(Exception):
                await app_state.state.auth.login(
                    req, source_ip="10.0.0.9", user_agent="audit-probe"
                )
        # The next attempt hits the lockout -> 429, still audited.
        with pytest.raises(Exception) as exc:
            await app_state.state.auth.login(
                req, source_ip="10.0.0.9", user_agent="audit-probe"
            )
        assert exc.value.status_code == 429
        locked = [
            row
            for row in await _rows_for(app_state, "login.failed")
            if row["detail"].get("reason") == "locked-out"
        ]
        assert locked[0]["source_ip"] == "10.0.0.9"
        assert locked[0]["user_agent"] == "audit-probe"
        assert locked[0]["detail"]["identifier"] == "testuser@example.com"

    async def test_session_limit_revocation_records_event(
        self, app_state, user, db
    ):
        app_state.state.settings.max_sessions_per_user = 1
        await app_state.state.auth.login(
            klangk_auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            )
        )
        await app_state.state.auth.login(
            klangk_auth.LoginRequest(
                identifier="testuser@example.com", password="testpass"
            ),
            source_ip="10.0.0.2",
        )
        rows = await _rows_for(app_state, "session.revoke")
        assert len(rows) == 1
        assert rows[0]["detail"]["reason"] == "session-limit"
        assert rows[0]["detail"]["revoked"] == 1
        assert rows[0]["detail"]["cap"] == 1
        assert rows[0]["source_ip"] == "10.0.0.2"

    async def test_register_records_event(self, app_state, db):
        await app_state.state.auth.register(
            klangk_auth.RegisterRequest(
                email="reg-audit@example.com", password="newpass1"
            ),
            verified=True,
        )
        rows = await _rows_for(app_state, "user.register")
        assert rows[0]["detail"] == {
            "email": "reg-audit@example.com",
            "verified": True,
        }
        # The auto-login is its own row, tagged with its path.
        logins = await _rows_for(app_state, "login")
        assert logins[0]["detail"] == {"via": "register"}


class TestLogoutAudit:
    async def test_logout_records_event(self, api_client, api_app, user):
        headers = await _auth_headers(api_client)
        resp = await api_client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        rows = await _events(api_app, "logout")
        assert len(rows) == 1
        assert rows[0]["actor_id"] == user["id"]
        assert rows[0]["source_ip"] is not None  # testclient's host

    async def test_logout_with_dead_token_records_nothing(
        self, api_client, api_app
    ):
        resp = await api_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 200
        assert await _events(api_app, "logout") == []


class TestRequestMethodCapture:
    """#3255: rows minted from an HTTP request carry its method and
    Referer (SV-222447); the Referer is capped at capture time."""

    async def test_login_row_carries_method_and_referer(
        self, api_client, api_app, user
    ):
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={"Referer": "https://klangk.example/login"},
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "login"))[0]
        assert row["method"] == "POST"
        assert row["referer"] == "https://klangk.example/login"

    async def test_no_referer_header_records_null(
        self, api_client, api_app, user
    ):
        await _auth_headers(api_client)
        row = (await _events(api_app, "login"))[0]
        assert row["method"] == "POST"
        assert row["referer"] is None

    async def test_long_referer_is_truncated_at_capture(
        self, api_client, api_app, user
    ):
        from klangk.util import REFERER_STORE_MAX

        resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={"Referer": "https://klangk.example/pad?x=" + "a" * 5000},
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "login"))[0]
        assert len(row["referer"]) == REFERER_STORE_MAX
        assert row["referer"].startswith("https://klangk.example/pad?x=")

    async def test_failed_login_row_carries_method_and_referer(
        self, api_client, api_app, user
    ):
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "wrong-password",
            },
            headers={"Referer": "https://klangk.example/login"},
        )
        assert resp.status_code == 401
        row = (await _events(api_app, "login.failed"))[0]
        assert row["method"] == "POST"
        assert row["referer"] == "https://klangk.example/login"

    async def test_logout_row_carries_method(self, api_client, api_app, user):
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            "/api/v1/auth/logout",
            headers={**headers, "Referer": "https://klangk.example/settings"},
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "logout"))[0]
        assert row["method"] == "POST"
        assert row["referer"] == "https://klangk.example/settings"


class TestAdminUserAudit:
    async def test_create_user_records_event(
        self, api_client, api_app, admin_user
    ):
        headers = await _admin_login(api_client)
        resp = await api_client.post(
            "/api/v1/users",
            json={
                "email": "audit-created@example.com",
                "password": "newpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.create")
        assert rows[0]["actor_id"] == admin_user["id"]
        assert rows[0]["detail"]["status"] == "created"
        assert rows[0]["detail"]["email"] == "audit-created@example.com"

    async def test_update_user_records_changed_fields(
        self, api_client, api_app, admin_user, user
    ):
        headers = await _admin_login(api_client)
        resp = await api_client.patch(
            f"/api/v1/users/{user['id']}",
            json={"handle": "audit-handle", "disabled": False},
            headers=headers,
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.update")
        assert rows[0]["detail"]["fields"] == ["handle", "disabled"]
        assert rows[0]["target_id"] == user["id"]

    async def test_admin_password_reset_emits_password_change(
        self, api_client, api_app, admin_user, user
    ):
        """An admin-forced reset is a user.password.change row too
        (#3205 review), so incident queries on the event name see it;
        likewise an admin email change."""
        headers = await _admin_login(api_client)
        resp = await api_client.patch(
            f"/api/v1/users/{user['id']}",
            json={
                "password": "newpass1",
                "email": "admin-moved@example.com",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        pw = await _events(api_app, "user.password.change")
        assert pw[0]["detail"] == {"via": "admin"}
        assert pw[0]["actor_id"] == admin_user["id"]
        assert pw[0]["target_id"] == user["id"]
        email = await _events(api_app, "user.email.change")
        assert email[0]["detail"] == {
            "email": "admin-moved@example.com",
            "via": "admin",
        }

    async def test_delete_user_records_email(
        self, api_client, api_app, admin_user
    ):
        headers = await _admin_login(api_client)
        create = await api_client.post(
            "/api/v1/users",
            json={
                "email": "audit-doomed@example.com",
                "password": "newpass1",
            },
            headers=headers,
        )
        user_id = create.json()["id"]
        resp = await api_client.delete(
            f"/api/v1/users/{user_id}", headers=headers
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.delete")
        # The user row is gone; the audit detail keeps the identity.
        assert rows[0]["detail"] == {"email": "audit-doomed@example.com"}
        assert rows[0]["target_id"] == user_id

    async def test_unlock_records_event(
        self, api_client, api_app, admin_user, user
    ):
        headers = await _admin_login(api_client)
        resp = await api_client.post(
            f"/api/v1/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.unlock")
        assert rows[0]["target_id"] == user["id"]


class TestGroupAudit:
    async def test_group_lifecycle_and_membership(
        self, api_client, api_app, admin_user, user
    ):
        headers = await _admin_login(api_client)
        created = await api_client.post(
            "/api/v1/groups",
            json={"name": "audit-group"},
            headers=headers,
        )
        assert created.status_code == 200
        group_id = created.json()["id"]
        assert (await _events(api_app, "group.create"))[0]["detail"] == {
            "name": "audit-group"
        }

        added = await api_client.post(
            f"/api/v1/groups/{group_id}/members",
            json={"user_id": user["id"]},
            headers=headers,
        )
        assert added.status_code == 200
        row = (await _events(api_app, "group.member.add"))[0]
        assert row["target_id"] == group_id
        assert row["detail"] == {"user_id": user["id"]}

        removed = await api_client.delete(
            f"/api/v1/groups/{group_id}/members/{user['id']}",
            headers=headers,
        )
        assert removed.status_code == 200
        assert (await _events(api_app, "group.member.remove"))[0][
            "detail"
        ] == {"user_id": user["id"]}

        updated = await api_client.patch(
            f"/api/v1/groups/{group_id}",
            json={"description": "audited"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert (await _events(api_app, "group.update"))[0][
            "target_id"
        ] == group_id

        deleted = await api_client.delete(
            f"/api/v1/groups/{group_id}", headers=headers
        )
        assert deleted.status_code == 200
        assert (await _events(api_app, "group.delete"))[0]["detail"] == {
            "name": "audit-group"
        }

    async def test_admin_acl_replace_records_event(
        self, api_client, api_app, admin_user
    ):
        headers = await _admin_login(api_client)
        resp = await api_client.put(
            "/api/v1/acl/resource",
            params={"resource": "/audit-test"},
            json=[],
            headers=headers,
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "acl.replace")
        assert rows[0]["target_id"] == "/audit-test"
        assert rows[0]["detail"] == {"entries": 0}


class TestWorkspaceShareAudit:
    async def test_member_share_events(
        self, api_client, api_app, app_state, user, admin_user
    ):
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "audit-ws"
        )
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            f"/api/v1/workspaces/{ws['id']}/members",
            json={"email": admin_user["email"]},
            headers=headers,
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "workspace.member.add"))[0]
        assert row["target_id"] == ws["id"]
        assert row["detail"]["member"] == admin_user["id"]
        assert row["actor_id"] == user["id"]

        removed = await api_client.delete(
            f"/api/v1/workspaces/{ws['id']}/members/{admin_user['id']}",
            headers=headers,
        )
        assert removed.status_code == 200
        assert (await _events(api_app, "workspace.member.remove"))[0][
            "detail"
        ]["member"] == admin_user["id"]

    async def test_group_share_events(
        self, api_client, api_app, app_state, user, admin_user
    ):
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "audit-ws"
        )
        group = await app_state.state.model.users.create_group("audit-g")
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            f"/api/v1/workspaces/{ws['id']}/groups",
            json={"group_id": group["id"]},
            headers=headers,
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "workspace.group.add"))[0]
        assert row["detail"]["group_id"] == group["id"]

        removed = await api_client.delete(
            f"/api/v1/workspaces/{ws['id']}/groups/{group['id']}",
            headers=headers,
        )
        assert removed.status_code == 200
        assert (await _events(api_app, "workspace.group.remove"))[0]["detail"][
            "group_id"
        ] == group["id"]

    async def test_role_change_event(
        self, api_client, api_app, app_state, user, admin_user
    ):
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "audit-ws"
        )
        headers = await _auth_headers(api_client)
        resp = await api_client.patch(
            f"/api/v1/workspaces/{ws['id']}/roles",
            json={"email": admin_user["email"], "role": "coders"},
            headers=headers,
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "workspace.role.change"))[0]
        assert row["target_id"] == ws["id"]
        assert row["detail"]["role"] == "coders"
        assert row["detail"]["member"] == admin_user["id"]

    async def test_workspace_acl_replace_event(
        self, api_client, api_app, app_state, user
    ):
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "audit-ws"
        )
        headers = await _auth_headers(api_client)
        resp = await api_client.put(
            f"/api/v1/workspaces/{ws['id']}/acl", json=[], headers=headers
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "acl.replace")
        assert rows[0]["target_id"] == ws["id"]
        assert rows[0]["detail"]["entries"] == 0

    async def test_transfer_event(
        self, api_client, api_app, app_state, user, admin_user
    ):
        ws = await app_state.state.model.workspaces.create_workspace_with_acl(
            user["id"], "audit-ws"
        )
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            f"/api/v1/workspaces/{ws['id']}/transfer",
            json={"email": admin_user["email"]},
            headers=headers,
        )
        assert resp.status_code == 200
        row = (await _events(api_app, "workspace.transfer"))[0]
        assert row["detail"]["to"] == admin_user["id"]


class TestSelfServiceAudit:
    async def test_change_password_records_both_events(
        self, api_client, api_app, user
    ):
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "NewPass1!",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        change = (await _events(api_app, "user.password.change"))[0]
        assert change["actor_id"] == user["id"]
        assert change["detail"] == {"via": "self-service"}
        revoke = (await _events(api_app, "session.revoke"))[0]
        assert revoke["detail"]["reason"] == "password-change"

    async def test_change_email_records_event(self, api_client, api_app, user):
        from klangk import emailsvc as emailsvc_mod

        headers = await _auth_headers(api_client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await api_client.post(
                "/api/v1/auth/change-email",
                json={
                    "email": "audit-moved@example.com",
                    "password": "testpass",
                },
                headers=headers,
            )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.email.change")
        assert rows[0]["detail"] == {"email": "audit-moved@example.com"}

    async def test_change_handle_records_event(
        self, api_client, api_app, user
    ):
        headers = await _auth_headers(api_client)
        resp = await api_client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "audit-handle", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 200
        rows = await _events(api_app, "user.handle.change")
        assert rows[0]["detail"] == {"handle": "audit-handle"}

    async def test_reset_password_records_events(
        self, api_client, api_app, user
    ):
        a = api_app.state.auth
        row = await api_app.state.model.users.get_user_by_email(user["email"])
        token = a.create_password_reset_token(
            user["id"], a.reset_token_binding(row["password_hash"])
        )
        resp = await api_client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "NewPass1!"},
        )
        assert resp.status_code == 200
        change = (await _events(api_app, "user.password.change"))[0]
        assert change["detail"] == {"via": "password-reset"}
        login = (await _events(api_app, "login"))[0]
        assert login["detail"] == {"via": "password-reset"}


class TestAuditEventsEndpoint:
    async def test_lists_events_for_admin(
        self, api_client, api_app, admin_user, user
    ):
        headers = await _admin_login(api_client)
        resp = await api_client.get("/api/v1/events/audit", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert len(body["items"]) == body["total"]
        item = body["items"][0]
        assert "hmac" not in item
        assert item["event"] in {"login", "user.create"}
        # The #3255 request fields ship on the wire for every row.
        assert "method" in item
        assert "referer" in item

    async def test_filters_by_event(self, api_client, api_app, admin_user):
        headers = await _admin_login(api_client)
        resp = await api_client.get(
            "/api/v1/events/audit",
            params={"event": "login"},
            headers=headers,
        )
        body = resp.json()
        assert body["total"] >= 1
        assert all(i["event"] == "login" for i in body["items"])

    async def test_pagination(self, api_client, api_app, admin_user):
        headers = await _admin_login(api_client)
        # Two logins -> at least two rows to page across.
        await _admin_login(api_client)
        page1 = await api_client.get(
            "/api/v1/events/audit",
            params={"limit": 1},
            headers=headers,
        )
        page2 = await api_client.get(
            "/api/v1/events/audit",
            params={"limit": 1, "offset": 1},
            headers=headers,
        )
        assert page1.json()["total"] >= 2
        assert page1.json()["items"][0]["id"] != page2.json()["items"][0]["id"]

    async def test_requires_manage_events(self, api_client, user):
        headers = await _auth_headers(api_client)
        resp = await api_client.get("/api/v1/events/audit", headers=headers)
        assert resp.status_code == 403
