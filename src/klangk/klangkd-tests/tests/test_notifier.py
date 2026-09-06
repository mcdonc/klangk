"""Admin notification fan-out (#3250).

Covers the :class:`AdminNotifier` gates (allowlist / channels /
throttle), both delivery channels' failure semantics, the guarded
``notify_event`` helper, the settings coercion for the three
``KLANGKD_ADMIN_NOTIFICATION_*`` vars, and the route-level emit sites
(account lifecycle through the admin funnel, disable/enable, the
inactivity sweeper's own file, and admission's memory gate in
test_admission.py).
"""

import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import ValidationError

import test_api
from test_api import _admin_login
from httpx import ASGITransport, AsyncClient

from _helpers import make_settings
from klangk.api.auth import _find_or_create_user
from klangk.notifier import (
    DEFAULT_NOTIFY_EVENTS,
    AdminNotifier,
    notify_event,
    render_notification_body,
)
from klangk.settings import KlangkSettings

# test_api's app fixture, re-bound under a module-local name (the
# test_audit_events.py pattern): route-level tests drive the real
# router stack against it.
api_app = test_api.app


@pytest.fixture
async def api_client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _notifier(env=None, *, email=None):
    """A notifier over a minimal app state (the test_emailsvc shape).

    ``email`` replaces the EmailService stub so channel tests can
    inject failures; the settings come from ``env``.
    """
    settings = make_settings(env)
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    app.state.email = email or types.SimpleNamespace(send_plain=AsyncMock())
    notifier = AdminNotifier(app)
    return notifier, app


def _mock_httpx_client(post_response=None):
    """A mock httpx.AsyncClient usable as an async context manager (the
    test_oidc.py shape)."""
    client = MagicMock()
    if post_response is not None:
        client.post = AsyncMock(return_value=post_response)
    else:
        client.post = AsyncMock(side_effect=RuntimeError("network gone"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


class TestSettings:
    def test_defaults(self):
        settings = make_settings({})
        assert settings.admin_notification_emails is None
        assert settings.admin_notification_webhook_url is None
        assert settings.admin_notify_events is None

    def test_emails_comma_separated_env(self):
        settings = make_settings(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": " a@x.com , b@x.com "}
        )
        assert settings.admin_notification_emails == [
            "a@x.com",
            "b@x.com",
        ]

    def test_emails_empty_string_is_off(self):
        settings = make_settings({"KLANGKD_ADMIN_NOTIFICATION_EMAILS": " "})
        assert settings.admin_notification_emails is None

    def test_webhook_url(self):
        settings = make_settings(
            {"KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL": "https://hook.x/e"}
        )
        assert settings.admin_notification_webhook_url == "https://hook.x/e"

    def test_notify_events_unknown_name_aborts_startup(self):
        with pytest.raises(ValidationError, match="unknown event"):
            make_settings(
                {"KLANGKD_ADMIN_NOTIFY_EVENTS": "user.create,typo.event"}
            )

    def test_notify_events_native_list(self, tmp_path):
        """The YAML config source delivers a native list — validated the
        same as the comma-separated env form."""
        config = tmp_path / "k.yaml"
        config.write_text(
            "state_dir: " + str(tmp_path / "state") + "\n"
            "data_dir: " + str(tmp_path / "data") + "\n"
            "admin_notify_events:\n"
            "  - user.create\n"
            "  - user.delete\n"
        )
        settings = KlangkSettings(
            env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
            config_file=str(config),
        )
        assert settings.admin_notify_events == ["user.create", "user.delete"]


class TestGates:
    def test_default_allowlist_is_every_supported_event(self):
        notifier, _ = _notifier()
        assert notifier.notify_events() == frozenset(DEFAULT_NOTIFY_EVENTS)

    def test_explicit_allowlist(self):
        notifier, _ = _notifier({"KLANGKD_ADMIN_NOTIFY_EVENTS": "user.create"})
        assert notifier.notify_events() == frozenset({"user.create"})

    def test_native_empty_list_is_explicitly_off(self, tmp_path):
        """YAML ``admin_notify_events: []`` silences every event while
        the channels stay configured — the deliberate off switch (the
        blank env string cannot do this, fail-safe)."""
        config = tmp_path / "k.yaml"
        config.write_text(
            "state_dir: " + str(tmp_path / "state") + "\n"
            "data_dir: " + str(tmp_path / "data") + "\n"
            "admin_notification_emails:\n"
            "  - sa@x.com\n"
            "admin_notify_events: []\n"
        )
        settings = KlangkSettings(
            env={"KLANGKD_DATA_DIR": str(tmp_path / "data")},
            config_file=str(config),
        )
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        app.state.email = types.SimpleNamespace(send_plain=AsyncMock())
        notifier = AdminNotifier(app)
        assert notifier.channels_configured()
        assert notifier.notify_events() == frozenset()
        assert not notifier.should_notify("user.create", "user.create")

    def test_blank_allowlist_falls_back_to_defaults(self):
        """Blanking the env var must not silently disable notifications:
        empty input → None → the default allowlist. The channels are the
        master switch (no channels → nothing notifies regardless)."""
        notifier, _ = _notifier({"KLANGKD_ADMIN_NOTIFY_EVENTS": " "})
        assert notifier.notify_events() == frozenset(DEFAULT_NOTIFY_EVENTS)

    def test_no_channels_means_no_notify(self):
        notifier, _ = _notifier()
        assert not notifier.channels_configured()
        assert not notifier.should_notify("user.create", "user.create")

    def test_event_outside_allowlist_means_no_notify(self):
        notifier, _ = _notifier(
            {
                "KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com",
                "KLANGKD_ADMIN_NOTIFY_EVENTS": "user.create",
            }
        )
        assert notifier.channels_configured()
        assert not notifier.should_notify("user.delete", "user.delete")

    def test_email_channel_alone_suffices(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        assert notifier.channels_configured()
        assert notifier.should_notify("user.create", "user.create")

    def test_webhook_channel_alone_suffices(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL": "https://hook.x/e"}
        )
        assert notifier.should_notify("user.create", "user.create")


class TestThrottle:
    def test_lifecycle_events_never_throttled(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        assert notifier.throttle_allows("user.create", "user.create")
        assert notifier.throttle_allows("user.create", "user.create")

    def test_persistent_condition_throttled_per_window(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        assert notifier.throttle_allows("audit.failure", "audit.failure")
        # Within the window: blocked.
        assert not notifier.throttle_allows("audit.failure", "audit.failure")
        # Past the window: allowed again.
        notifier.last_notified["audit.failure"] = time.monotonic() - 301
        assert notifier.throttle_allows("audit.failure", "audit.failure")

    async def test_audit_failure_throttled_per_source_table(self):
        """One bucket per table: a container_events storm must not mask
        the first audit_events degradation alert (#3250 review)."""
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        with patch.object(AdminNotifier, "deliver", AsyncMock()) as d:
            notifier.notify_admins(
                "audit.failure", detail={"table": "container_events"}
            )
            notifier.notify_admins(
                "audit.failure", detail={"table": "audit_events"}
            )
            # Same table again — throttled.
            notifier.notify_admins(
                "audit.failure", detail={"table": "container_events"}
            )
            await asyncio.sleep(0)
        assert d.await_count == 2

    def test_reconfigure_resets_throttle_clock(self):
        notifier, app = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        assert notifier.throttle_allows("resource.low", "resource.low")
        assert not notifier.throttle_allows("resource.low", "resource.low")
        notifier.reconfigure(app)
        assert notifier.throttle_allows("resource.low", "resource.low")


class TestNotifyAdmins:
    async def test_gated_off_creates_no_task(self):
        notifier, app = _notifier()  # no channels
        with patch.object(AdminNotifier, "deliver", AsyncMock()) as deliver:
            notifier.notify_admins("user.create")
            await asyncio.sleep(0)
        deliver.assert_not_called()

    async def test_delivers_in_background_with_payload(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        with patch.object(AdminNotifier, "deliver", AsyncMock()) as deliver:
            notifier.notify_admins(
                "user.delete",
                actor_email="admin@x.com",
                target_type="user",
                target_id="u1",
                detail={"email": "gone@x.com"},
                source_ip="10.0.0.9",
            )
            await asyncio.sleep(0)
        deliver.assert_awaited_once()
        payload = deliver.await_args.args[0]
        assert payload["event"] == "user.delete"
        assert payload["actor_email"] == "admin@x.com"
        assert payload["detail"] == {"email": "gone@x.com"}
        assert payload["source_ip"] == "10.0.0.9"
        assert "T" in payload["timestamp"]  # ISO-8601 stamp

    async def test_throttled_event_not_delivered_twice(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        with patch.object(AdminNotifier, "deliver", AsyncMock()) as deliver:
            notifier.notify_admins("audit.failure")
            notifier.notify_admins("audit.failure")
            await asyncio.sleep(0)
        assert deliver.await_count == 1

    def test_no_running_loop_drops_without_raising(self):
        """A sync caller outside the event loop must not crash the
        action being notified — the dispatch logs and returns."""
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "sa@x.com"}
        )
        # No running loop in a sync test → create_task raises
        # RuntimeError → the warning branch, never an exception. A
        # throttled event also proves the gate ran before the drop.
        notifier.notify_admins("audit.failure")
        assert "audit.failure" in notifier.last_notified


class TestEmailChannel:
    async def test_sends_to_every_recipient(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "a@x.com,b@x.com"}
        )
        payload = {
            "event": "user.create",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "actor_id": None,
            "actor_email": "admin@x.com",
            "target_type": "user",
            "target_id": "u1",
            "detail": {"email": "new@x.com"},
            "source_ip": None,
        }
        await notifier.deliver_via_email(payload)
        assert app_send_calls(notifier) == ["a@x.com", "b@x.com"]

    async def test_one_failed_recipient_does_not_stop_the_rest(self, caplog):
        """Best-effort per recipient: the first bounce is logged, the
        second recipient still gets the notification."""
        email = types.SimpleNamespace(send_plain=AsyncMock())
        email.send_plain.side_effect = [
            RuntimeError("smtp down"),
            None,
        ]
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_EMAILS": "a@x.com,b@x.com"},
            email=email,
        )
        payload = {"event": "user.create"}
        with caplog.at_level("WARNING"):
            await notifier.deliver_via_email(payload)
        assert email.send_plain.await_count == 2
        assert "admin notification email" in caplog.text

    async def test_unreadable_recipients_are_logged_not_raised(self, caplog):
        """The settings read itself is guarded: a broken config object
        logs and returns rather than killing the deliver task."""

        class exploding_settings:
            @property
            def admin_notification_emails(self):
                raise RuntimeError("settings unreadable")

        app = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=exploding_settings())
        )
        notifier = AdminNotifier(app)
        with caplog.at_level("WARNING"):
            await notifier.deliver_via_email({"event": "user.create"})
        assert "recipients unreadable" in caplog.text


class TestWebhookChannel:
    async def test_disabled_channel_posts_nothing(self):
        notifier, _ = _notifier()
        with patch("httpx.AsyncClient") as client_cls:
            await notifier.deliver_via_webhook({"event": "user.create"})
        client_cls.assert_not_called()

    async def test_posts_json_payload(self):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL": "https://hook.x/e"}
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        ctx, client = _mock_httpx_client(post_response=resp)
        payload = {"event": "user.create", "detail": {}}
        with patch("httpx.AsyncClient", return_value=ctx):
            await notifier.deliver_via_webhook(payload)
        client.post.assert_awaited_once_with("https://hook.x/e", json=payload)
        resp.raise_for_status.assert_called_once_with()

    async def test_failure_is_logged_not_raised(self, caplog):
        notifier, _ = _notifier(
            {"KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL": "https://hook.x/e"}
        )
        ctx, _client = _mock_httpx_client(post_response=None)
        with (
            patch("httpx.AsyncClient", return_value=ctx),
            caplog.at_level("WARNING"),
        ):
            await notifier.deliver_via_webhook({"event": "user.create"})
        assert "admin notification webhook" in caplog.text


class TestDeliver:
    async def test_runs_both_channels(self):
        """deliver() fans out to email + webhook concurrently."""
        notifier, _ = _notifier(
            {
                "KLANGKD_ADMIN_NOTIFICATION_EMAILS": "a@x.com",
                "KLANGKD_ADMIN_NOTIFICATION_WEBHOOK_URL": ("https://hook.x/e"),
            }
        )
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        ctx, _client = _mock_httpx_client(post_response=resp)
        with patch("httpx.AsyncClient", return_value=ctx):
            await notifier.deliver(
                {
                    "event": "user.create",
                    "timestamp": "t",
                    "actor_id": None,
                    "actor_email": None,
                    "target_type": None,
                    "target_id": None,
                    "detail": {},
                    "source_ip": None,
                }
            )
        assert app_send_calls(notifier) == ["a@x.com"]
        assert _client.post.await_count == 1


class TestRenderBody:
    def test_renders_present_fields_and_detail(self):
        body = render_notification_body(
            {
                "event": "user.delete",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "actor_id": "a1",
                "actor_email": "admin@x.com",
                "target_type": "user",
                "target_id": "u9",
                "detail": {"email": "gone@x.com"},
                "source_ip": "10.0.0.9",
            }
        )
        assert "Event: user.delete" in body
        assert "Actor: admin@x.com" in body
        assert "Source IP: 10.0.0.9" in body
        assert "Detail: {'email': 'gone@x.com'}" in body
        assert "KLANGKD_ADMIN_NOTIFICATION_" in body

    def test_skips_absent_fields(self):
        body = render_notification_body(
            {
                "event": "user.create",
                "timestamp": "t",
                "actor_id": None,
                "actor_email": None,
                "target_type": None,
                "target_id": None,
                "detail": {},
                "source_ip": None,
            }
        )
        assert "Actor" not in body
        assert "Detail" not in body
        assert "Target" not in body


class TestNotifyEventHelper:
    def test_app_without_notifier_is_a_noop(self):
        app = types.SimpleNamespace(state=types.SimpleNamespace())
        notify_event(app, "user.create")  # must not raise

    def test_app_with_notifier_fans_out(self):
        notifier = Mock()
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(notifier=notifier)
        )
        notify_event(app, "user.unlock", detail={"email": "u@x.com"})
        notifier.notify_admins.assert_called_once_with(
            "user.unlock", detail={"email": "u@x.com"}
        )


class TestNotifyEventHelperRaiseSafety:
    def test_broken_notifier_never_raises(self, caplog):
        """The helper is called from inside except blocks (the
        audit-failure sites); its own failure must be swallowed so it
        cannot mask the exception it annotates."""
        notifier = Mock()
        notifier.notify_admins.side_effect = RuntimeError("boom")
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(notifier=notifier)
        )
        with caplog.at_level("WARNING"):
            notify_event(app, "audit.failure")  # must not raise
        assert "dispatch for audit.failure failed" in caplog.text

    def test_stateless_app_never_raises(self):
        app = types.SimpleNamespace()
        notify_event(app, "user.create")  # no .state at all


class TestRouteEmitSites:
    """The API hooks: each lifecycle route reaches the notifier."""

    async def test_admin_unlock_notifies(
        self, api_client, api_app, admin_user, user
    ):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        resp = await api_client.post(
            f"/api/v1/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 200
        _, kwargs = spy.notify_admins.call_args
        assert spy.notify_admins.call_args.args[0] == "user.unlock"
        assert kwargs["actor_email"] == "testadmin@example.com"
        assert kwargs["target_id"] == user["id"]

    async def test_admin_disable_and_enable_notify(
        self, api_client, api_app, admin_user, user
    ):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        resp = await api_client.patch(
            f"/api/v1/users/{user['id']}",
            json={"disabled": True},
            headers=headers,
        )
        assert resp.status_code == 200
        events = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.disable" in events
        resp = await api_client.patch(
            f"/api/v1/users/{user['id']}",
            json={"disabled": False},
            headers=headers,
        )
        assert resp.status_code == 200
        events = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.enable" in events
        # The update funnel also fired its user.update rows.
        assert events.count("user.update") == 2

    async def test_registration_notifies(self, api_client, api_app):
        spy = Mock()
        api_app.state.notifier = spy
        from klangk.emailsvc import EmailService

        with patch.object(
            EmailService, "send_verification_email", new_callable=AsyncMock
        ):
            resp = await api_client.post(
                "/api/v1/auth/register",
                json={
                    "email": "newbie@example.com",
                    "password": "GoodPass123",
                },
            )
        assert resp.status_code in (200, 201)
        assert spy.notify_admins.call_args.args[0] == "user.register"
        assert spy.notify_admins.call_args.kwargs["target_type"] == "user"


class TestRouteEmitSitesSelfService:
    """The self-service account-change sites reach the notifier."""

    async def _user_headers(self, api_client):
        resp = await api_client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_change_password_notifies(self, api_client, api_app, user):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await self._user_headers(api_client)
        resp = await api_client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "NewPass456",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.password.change" in calls

    async def test_change_email_notifies(self, api_client, api_app, user):
        from klangk.emailsvc import EmailService

        spy = Mock()
        api_app.state.notifier = spy
        headers = await self._user_headers(api_client)
        with patch.object(
            EmailService, "send_verification_email", new_callable=AsyncMock
        ):
            resp = await api_client.post(
                "/api/v1/auth/change-email",
                json={"email": "moved@example.com", "password": "testpass"},
                headers=headers,
            )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.email.change" in calls

    async def test_change_handle_notifies(self, api_client, api_app, user):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await self._user_headers(api_client)
        resp = await api_client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "newhandle", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.handle.change" in calls

    async def test_accept_invite_notifies(
        self, api_client, api_app, admin_user
    ):
        from klangk.emailsvc import EmailService

        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        with patch.object(
            EmailService, "send_invitation_email", new_callable=AsyncMock
        ):
            create_resp = await api_client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "invitee@example.com"},
            )
        assert create_resp.status_code in (200, 201)
        inv_id = create_resp.json()["id"]
        token = api_app.state.auth.create_invitation_token(
            inv_id, "invitee@example.com"
        )
        resp = await api_client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "InvitedPass1"},
        )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.create" in calls
        create_details = [
            c.kwargs.get("detail", {})
            for c in spy.notify_admins.call_args_list
            if c.args[0] == "user.create"
        ]
        assert {"via": "invite", "email": "invitee@example.com"} in (
            create_details
        )


class TestOidcJitNotifies:
    """OIDC JIT provisioning notifies user.create on first login only."""

    async def test_first_login_creates_and_notifies(self, api_app):
        spy = Mock()
        api_app.state.notifier = spy
        user = await _find_or_create_user(
            api_app, "prov-1", "sub-jit-1", "jit@example.com"
        )
        assert user["email"] == "jit@example.com"
        args, kwargs = spy.notify_admins.call_args
        assert args[0] == "user.create"
        assert kwargs["detail"] == {"email": "jit@example.com", "via": "oidc"}

    async def test_subsequent_login_does_not_renotify(self, api_app):
        spy = Mock()
        api_app.state.notifier = spy
        await _find_or_create_user(
            api_app, "prov-1", "sub-jit-2", "again@example.com"
        )
        await _find_or_create_user(
            api_app, "prov-1", "sub-jit-2", "again@example.com"
        )
        assert spy.notify_admins.call_count == 1


class TestRouteEmitSitesAdminFunnel:
    """Admin create/delete and group-membership sites reach the
    notifier through the record_admin_event funnel."""

    async def test_admin_create_notifies(
        self, api_client, api_app, admin_user
    ):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        resp = await api_client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "made@example.com", "password": "MadePass12"},
        )
        assert resp.status_code in (200, 201)
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.create" in calls

    async def test_admin_delete_notifies(
        self, api_client, api_app, admin_user, user
    ):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        resp = await api_client.delete(
            f"/api/v1/users/{user['id']}", headers=headers
        )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "user.delete" in calls

    async def test_group_member_add_remove_notifies(
        self, api_client, api_app, admin_user, user
    ):
        spy = Mock()
        api_app.state.notifier = spy
        headers = await _admin_login(api_client)
        create_resp = await api_client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "notify-grp", "description": "for notify test"},
        )
        assert create_resp.status_code in (200, 201)
        group_id = create_resp.json()["id"]
        resp = await api_client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 200
        resp = await api_client.delete(
            f"/api/v1/groups/{group_id}/members/{user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        calls = [c.args[0] for c in spy.notify_admins.call_args_list]
        assert "group.member.add" in calls
        assert "group.member.remove" in calls


def app_send_calls(notifier) -> list:
    """The recipients the email stub was asked to send to."""
    return [
        c.args[0] for c in notifier.app.state.email.send_plain.await_args_list
    ]
