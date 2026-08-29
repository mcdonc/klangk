"""Tests for :mod:`klangk.consent.deciders` -- the live-decider registry (#2308)."""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

from klangk.consent.deciders import ConsentDeciderRegistry
from klangk.model.workspaces import EGRESS_MODE_INTERACTIVE

WS = "ws-aaaa1111-2222-3333-4444-555566667777"
WS2 = "ws-bbbb2222-3333-4444-5555-666677778888"


def _app(timeout: float = 45.0):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(consent_decider_timeout=timeout)
    return app


class _FakeSock:
    """Stand-in for a SafeWebSocket's outbound channel (registry broadcast)."""

    def __init__(self, *, raising: bool = False) -> None:
        self.sent: list[dict] = []
        self._raising = raising

    def send_json(self, msg: dict) -> None:
        if self._raising:
            raise RuntimeError("dead socket")
        self.sent.append(msg)


class TestConsentDeciderRegistry:
    async def test_no_decider_is_not_interactive(self):
        reg = ConsentDeciderRegistry(_app())
        assert reg.has_decider(WS) is False

    async def test_register_then_deregister(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "admin@example.com", _FakeSock())
        assert reg.has_decider(WS) is True
        reg.deregister("d1")
        assert reg.has_decider(WS) is False
        # deregister of unknown id is a no-op
        reg.deregister("nope")

    async def test_multiple_deciders_same_workspace(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x", _FakeSock())
        reg.register("d2", WS, "b@x", _FakeSock())
        reg.register("d3", WS, "c@x", _FakeSock())
        assert reg.has_decider(WS) is True
        # N concurrent deciders; removing one leaves the rest
        reg.deregister("d1")
        assert reg.has_decider(WS) is True
        assert reg.deciders_for(WS) == ["d2", "d3"]
        reg.deregister("d2")
        reg.deregister("d3")
        assert reg.has_decider(WS) is False

    async def test_decider_scoped_to_one_workspace(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x", _FakeSock())
        assert reg.has_decider(WS) is True
        # not visible to a different workspace
        assert reg.has_decider(WS2) is False
        assert reg.deciders_for(WS2) == []

    async def test_deploy_wide_decider_covers_every_workspace(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("admin", None, "admin@example.com", _FakeSock())
        assert reg.has_decider(WS) is True
        assert reg.has_decider(WS2) is True
        assert set(reg.deciders_for(WS)) == {"admin"}

    async def test_register_is_idempotent_on_decider_id(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x", _FakeSock())
        reg.register(
            "d1", WS, "a@x", _FakeSock()
        )  # reconnect with same id -> refresh, not dup
        assert reg.deciders_for(WS) == ["d1"]

    async def test_touch_extends_liveness(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.05))
        reg.register("d1", WS, "a@x", _FakeSock())
        # age it past the timeout, then ping to refresh
        reg._deciders["d1"]["seen"] -= 1.0
        assert reg.has_decider(WS) is False
        reg.touch("d1")
        assert reg.has_decider(WS) is True
        # touch of unknown id is a no-op
        reg.touch("nope")

    async def test_reaper_drops_stale_deciders(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.02))
        reg.register("d1", WS, "a@x", _FakeSock())
        reg.start()
        try:
            # mark stale; the reaper ticks at >= timeout
            reg._deciders["d1"]["seen"] -= 1.0
            await asyncio.sleep(0.08)
            assert reg.has_decider(WS) is False
            assert reg._deciders == {}
        finally:
            await reg.stop()

    async def test_reaper_keeps_fresh_deciders(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.05))
        reg.register("d1", WS, "a@x", _FakeSock())
        reg.start()
        try:
            await asyncio.sleep(0.04)
            reg.touch("d1")  # keep it alive across reaper ticks
            await asyncio.sleep(0.04)
            assert reg.has_decider(WS) is True
        finally:
            await reg.stop()

    async def test_stop_clears_all_and_is_idempotent(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x", _FakeSock())
        reg.register("d2", None, "b@x", _FakeSock())
        await reg.stop()
        assert reg._deciders == {}
        assert reg._reaper is None
        await reg.stop()  # idempotent


class TestConsentDeciderRegistryBroadcast:
    async def test_broadcast_sends_to_matching_deciders_only(self):
        reg = ConsentDeciderRegistry(_app())
        s1, s2, s3 = _FakeSock(), _FakeSock(), _FakeSock()
        reg.register("d1", WS, "a@x", s1)  # scoped to WS
        reg.register("d2", WS, "b@x", s2)  # scoped to WS
        reg.register("d3", WS2, "c@x", s3)  # different workspace
        delivered = reg.broadcast(WS, {"type": "egress_request"})
        assert delivered == 2
        assert len(s1.sent) == 1
        assert len(s2.sent) == 1
        assert s3.sent == []  # different workspace, not delivered

    async def test_broadcast_includes_deploy_wide_deciders(self):
        reg = ConsentDeciderRegistry(_app())
        deploy, scoped = _FakeSock(), _FakeSock()
        reg.register("admin", None, "admin@x", deploy)  # deploy-wide
        reg.register("d2", WS, "a@x", scoped)  # scoped to WS
        delivered = reg.broadcast(WS, {"type": "egress_request"})
        assert delivered == 2
        assert len(deploy.sent) == 1
        assert len(scoped.sent) == 1

    async def test_broadcast_prunes_dead_deciders(self):
        reg = ConsentDeciderRegistry(_app())
        live = _FakeSock()
        dead = _FakeSock(raising=True)  # send_json raises -> dead socket
        reg.register("d1", WS, "a@x", live)
        reg.register("d2", WS, "b@x", dead)
        delivered = reg.broadcast(WS, {"type": "egress_resolved"})
        assert delivered == 1
        assert reg.has_decider(WS) is True  # d1 still live
        assert "d2" not in reg._deciders  # pruned on socket error

    async def test_broadcast_skips_stale_deciders(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.05))
        s = _FakeSock()
        reg.register("d1", WS, "a@x", s)
        reg._deciders["d1"]["seen"] -= 1.0  # age past timeout
        assert reg.broadcast(WS, {"type": "x"}) == 0
        assert s.sent == []


class _FakeWS:
    """Minimal stand-in for a fastapi WebSocket for handler-level tests."""

    def __init__(
        self, params: dict, incoming: list, headers: dict | None = None
    ):
        self.query_params = params
        self.headers = (
            headers
            if headers is not None
            else {"user-agent": "fake-decider/1.0"}
        )
        self._incoming = iter(incoming)
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_json(self, data) -> None:
        # SafeWebSocket's sender task writes each enqueued frame here.
        self.sent.append(json.dumps(data))

    async def receive_text(self) -> str:
        item = next(self._incoming)
        if isinstance(item, BaseException):
            raise item
        return item


def _ws_app(
    token_result,
    *,
    allowed: bool = True,
    snapshot=None,
    rules_frame=None,
    egress_mode: str = EGRESS_MODE_INTERACTIVE,
):
    """App with mocked auth + acl + coordinator, and a real registry."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(consent_decider_timeout=45.0)
    # #2394: a workspace-scoped decider's egress_mode is checked at connect.
    # Default interactive so existing connect/register tests keep working;
    # pass egress_mode="static" to exercise the structural rejection.
    app.state.model = types.SimpleNamespace(
        workspaces=types.SimpleNamespace(
            get_workspace=AsyncMock(
                return_value={"id": WS, "egress_mode": egress_mode}
            ),
        ),
    )
    app.state.consent_deciders = ConsentDeciderRegistry(app)
    app.state.auth = types.SimpleNamespace(
        get_user_from_token=AsyncMock(return_value=token_result)
    )
    app.state.acl = types.SimpleNamespace(
        get_principals=AsyncMock(
            return_value={
                "user_id": "u1",
                "group_ids": [],
                "authenticated": True,
            }
        ),
        check_permission=AsyncMock(return_value=allowed),
    )
    app.state.consent_coordinator = types.SimpleNamespace(
        snapshot=AsyncMock(return_value=snapshot or []),
        resolve=AsyncMock(return_value=None),
        rules_frame=AsyncMock(return_value=rules_frame),
        revoke=AsyncMock(return_value=True),
        # #2332: pause/unpause -- default to success (a real coordinator ack).
        pause=AsyncMock(return_value={"ok": True, "until": 1234.5}),
        unpause=AsyncMock(return_value={"ok": True}),
    )
    return app


class TestConsentDeciderWS:
    async def test_connect_registers_disconnect_deregisters(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "admin@example.com"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [WebSocketDisconnect()],  # connect, then immediately disconnect
        )
        await handle_consent_decider(ws, app)
        assert ws.accepted
        assert (
            app.state.consent_deciders.has_decider(WS) is False
        )  # deregistered

    async def test_missing_token_is_rejected(self):
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS({"workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4001, "Missing token")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

    async def test_missing_token_logs_refusal(self, caplog):
        # #2490: every pre-accept refusal logs its reason server-side (the
        # close code/reason never reach the client on a refused handshake).
        import logging

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS({"workspace": WS}, [], headers={"user-agent": "ua-x/9"})
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.decider"
        ):
            await handle_consent_decider(ws, app)
        assert "refused: Missing token" in caplog.text
        assert "ua=ua-x/9" in caplog.text

    async def test_invalid_token_is_rejected(self):
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app(None)
        ws = _FakeWS({"token": "bad"}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4001, "Invalid token")

    async def test_expired_token_is_rejected(self):
        from klangk import auth
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app(auth.Auth.TOKEN_EXPIRED)
        ws = _FakeWS({"token": "stale"}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4002, "Token expired")

    async def test_non_member_workspace_scoped_is_forbidden(self):
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, allowed=False)
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4003, "Forbidden")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

    async def test_forbidden_logs_user_and_workspace(self, caplog):
        # #2490: an authz refusal names the user + workspace in the log --
        # and never the token (it rides the query string; leaking it into
        # the log would leak a live JWT).
        import logging

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, allowed=False)
        ws = _FakeWS({"token": "SEKRET-JWT", "workspace": WS}, [])
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.decider"
        ):
            await handle_consent_decider(ws, app)
        assert "refused: Forbidden" in caplog.text
        assert "user=a@x" in caplog.text
        assert f"workspace={WS}" in caplog.text
        assert "SEKRET-JWT" not in caplog.text

    async def test_refusal_log_sanitizes_forged_workspace(self, caplog):
        # #2490: the workspace query param is attacker-controlled pre-auth;
        # a forged %0A must not inject new lines into the log record.
        import logging

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, allowed=False)
        ws = _FakeWS({"token": "tok", "workspace": f"{WS}\nFAKED line"}, [])
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.decider"
        ):
            await handle_consent_decider(ws, app)
        assert "\nFAKED" not in caplog.text

    async def test_static_workspace_scoped_decider_is_forbidden(self):
        # #2394: a workspace with egress_mode=static must refuse a
        # workspace-scoped consent decider at registration -- the
        # static/interactive boundary is structural, not just the
        # coordinator's hold-time gate.
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, egress_mode="static")
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4003, "workspace egress mode is not interactive")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

    async def test_static_refusal_logs_reason(self, caplog):
        # #2490: the egress-mode refusal names the mode mismatch in the log
        # -- the most common 403-storm cause (workspace flipped out of
        # interactive mid-session).
        import logging

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, egress_mode="static")
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.decider"
        ):
            await handle_consent_decider(ws, app)
        assert (
            "refused: workspace egress mode is not interactive" in caplog.text
        )

    async def test_allow_workspace_scoped_decider_is_forbidden(self):
        # #2406: an allow-mode workspace is default-permit and auto-allows
        # off-list egress (no consent prompt), so it must refuse a
        # workspace-scoped consent decider at registration just like static --
        # the decider TUI must never offer to decide for an allow workspace.
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, egress_mode="allow")
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4003, "workspace egress mode is not interactive")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

    async def test_missing_workspace_is_forbidden(self):
        # A workspace that vanished between the authz check and the egress_mode
        # read (a delete race) is refused just like an unauthorized one.
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        app.state.model.workspaces.get_workspace = AsyncMock(return_value=None)
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4003, "Forbidden")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

    async def test_vanished_workspace_refusal_is_logged(self, caplog):
        # #2490: the delete-race refusal is logged like every other one.
        import logging

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        app.state.model.workspaces.get_workspace = AsyncMock(return_value=None)
        ws = _FakeWS({"token": "tok", "workspace": WS}, [])
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.decider"
        ):
            await handle_consent_decider(ws, app)
        assert "refused: Forbidden" in caplog.text
        assert "user=a@x" in caplog.text

    async def test_deploy_wide_decider_unaffected_by_static_mode(self):
        # #2394: deploy-wide deciders (no ?workspace=) cover all interactive
        # workspaces without flipping a static one's behavior, so the
        # egress_mode check is skipped for them -- they register normally and
        # the workspace model is never consulted.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "admin@x"}, egress_mode="static")
        ws = _FakeWS(
            {"token": "tok"},  # no workspace -> deploy-wide
            [WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        assert ws.accepted is True
        assert ws.closed is None
        app.state.model.workspaces.get_workspace.assert_not_awaited()

    async def test_non_admin_deploy_wide_is_forbidden(self):
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"}, allowed=False)
        ws = _FakeWS({"token": "tok"}, [])  # no workspace -> deploy-wide
        await handle_consent_decider(ws, app)
        assert ws.closed == (4003, "Forbidden")
        assert ws.accepted is False

    async def test_ping_touches_and_pongs(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"ping"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        assert any(json.loads(m).get("type") == "pong" for m in ws.sent)

    async def test_snapshot_replayed_on_connect(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        frame = {
            "type": "egress_request",
            "workspace_id": WS,
            "request": {"id": "r1"},
        }
        app = _ws_app({"id": "u1", "email": "a@x"}, snapshot=[frame])
        ws = _FakeWS(
            {"token": "tok", "workspace": WS}, [WebSocketDisconnect()]
        )
        await handle_consent_decider(ws, app)
        assert any(
            json.loads(m).get("type") == "egress_request" for m in ws.sent
        )
        app.state.consent_coordinator.snapshot.assert_awaited_once_with(WS)

    async def test_rules_frame_pushed_on_connect(self):
        # On connect the in-effect rules snapshot is pushed too (#2335 slice
        # A), right after the pending-request snapshot.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        rules = {
            "type": "egress_rules",
            "workspace_id": WS,
            "allow_list": ["static.example.com"],
            "allowed": [],
            "denied": [],
            "paused": None,
        }
        app = _ws_app({"id": "u1", "email": "a@x"}, rules_frame=rules)
        ws = _FakeWS(
            {"token": "tok", "workspace": WS}, [WebSocketDisconnect()]
        )
        await handle_consent_decider(ws, app)
        assert any(
            json.loads(m).get("type") == "egress_rules" for m in ws.sent
        )
        app.state.consent_coordinator.rules_frame.assert_awaited_once_with(WS)

    async def test_verdict_resolves_the_hold(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "decider@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                json.dumps(
                    {
                        "type": "verdict",
                        "request_id": "rid",
                        "decision": "allowed",
                        "duration": "1w",
                    }
                ),
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.resolve.assert_awaited_once_with(
            "rid", "allowed", "u1", duration="1w", decider_workspace=WS
        )

    async def test_revoke_frame_calls_coordinator_and_acks(self):
        # #2339: a revoke frame -> coordinator.revoke(decider_workspace guard)
        # + a revoke_ack back to the decider.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        app.state.consent_coordinator.revoke = AsyncMock(return_value=True)
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"revoke","request_id":"r1"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.revoke.assert_awaited_once()
        args, kwargs = app.state.consent_coordinator.revoke.call_args
        assert args[0] == "r1"  # request_id
        assert kwargs["decider_workspace"] == WS  # defense-in-depth scope
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "revoke_ack"
        ]
        assert acks and acks[0]["ok"] is True
        assert acks[0]["request_id"] == "r1"

    async def test_pause_frame_calls_coordinator_and_acks(self):
        # #2332: a pause frame -> coordinator.pause(the decider's workspace) +
        # a pause_ack back to the decider.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"pause","duration":"15m"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.pause.assert_awaited_once_with(WS, "15m")
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is True
        assert acks[0]["until"] == 1234.5

    async def test_unpause_frame_calls_coordinator_and_acks(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"unpause"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.unpause.assert_awaited_once_with(WS)
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is True

    async def test_pause_deploy_wide_decider_nacks(self):
        # A deploy-wide decider (no workspace) has no single workspace to
        # pause -> nack without calling the coordinator.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "admin@x"})
        ws = _FakeWS(
            {"token": "tok"},  # no workspace -> deploy-wide
            ['{"type":"pause","duration":"1h"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.pause.assert_not_awaited()
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is False

    async def test_unpause_deploy_wide_decider_nacks(self):
        # A deploy-wide decider has no single workspace to unpause either.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "admin@x"})
        ws = _FakeWS(
            {"token": "tok"},  # no workspace -> deploy-wide
            ['{"type":"unpause"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.unpause.assert_not_awaited()
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is False

    async def test_pause_requires_share_terminals_not_just_terminal(self):
        # I1 (#2332): pause is a workspace-wide policy change, so it needs
        # share-terminals (collaborators + owners), not just the terminal
        # access that gates the connection. A terminal-only member (e.g. a
        # spectator) may connect + decide requests but NOT pause.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "spec@x"})

        async def check(resource, principals, perm):
            return perm == "terminal"  # terminal yes, share-terminals no

        app.state.acl.check_permission = AsyncMock(side_effect=check)
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"pause","duration":"15m"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.pause.assert_not_awaited()
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is False

    async def test_unpause_requires_share_terminals(self):
        # Same higher bar applies to unpause (a workspace-wide state change).
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "spec@x"})

        async def check(resource, principals, perm):
            return perm == "terminal"

        app.state.acl.check_permission = AsyncMock(side_effect=check)
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"unpause"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.unpause.assert_not_awaited()
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is False

    async def test_pause_allowed_with_share_terminals(self):
        # A collaborator (share-terminals) can pause; the coordinator is called
        # and a success pause_ack comes back.
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "collab@x"})

        async def check(resource, principals, perm):
            return perm in ("terminal", "share-terminals")

        app.state.acl.check_permission = AsyncMock(side_effect=check)
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"pause","duration":"1h"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.pause.assert_awaited_once_with(WS, "1h")
        acks = [
            json.loads(m)
            for m in ws.sent
            if json.loads(m).get("type") == "pause_ack"
        ]
        assert acks and acks[0]["ok"] is True

    async def test_verdict_invalid_decision_sends_error(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                json.dumps(
                    {
                        "type": "verdict",
                        "request_id": "rid",
                        "decision": "maybe",
                    }
                ),
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.resolve.assert_not_awaited()
        assert any(json.loads(m).get("type") == "error" for m in ws.sent)

    async def test_verdict_invalid_duration_sends_error(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                json.dumps(
                    {
                        "type": "verdict",
                        "request_id": "rid",
                        "decision": "allowed",
                        "duration": "2d",
                    }
                ),
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.resolve.assert_not_awaited()
        assert any(json.loads(m).get("type") == "error" for m in ws.sent)

    async def test_verdict_missing_request_id_sends_error(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                json.dumps(
                    {"type": "verdict", "decision": "denied"}  # no request_id
                ),
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        app.state.consent_coordinator.resolve.assert_not_awaited()
        assert any(json.loads(m).get("type") == "error" for m in ws.sent)

    async def test_deploy_wide_when_no_workspace_param(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "admin@example.com"})
        ws = _FakeWS({"token": "tok"}, [WebSocketDisconnect()])
        # While connected it would cover every workspace; after disconnect,
        # nothing remains.
        await handle_consent_decider(ws, app)
        assert app.state.consent_deciders.has_decider(WS) is False
        assert app.state.consent_deciders.has_decider(WS2) is False

    async def test_verdict_resolve_error_does_not_tear_down_connection(self):
        # a verdict whose resolve() raises is logged + swallowed -- the
        # connection survives to handle later messages (no whole-connection
        # teardown for one bad verdict).
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        app.state.consent_coordinator.resolve = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                json.dumps(
                    {
                        "type": "verdict",
                        "request_id": "rid",
                        "decision": "allowed",
                    }
                ),
                '{"type":"ping"}',  # must still be handled after the error
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        assert any(
            json.loads(m).get("type") == "pong" for m in ws.sent
        )  # connection survived
        app.state.consent_coordinator.resolve.assert_awaited_once()

    async def test_slow_client_is_dropped(self):
        # a full outbound queue (SlowClientError) drops the connection, like
        # the main /ws handler.
        from unittest.mock import patch
        from fastapi import WebSocketDisconnect

        from klangk.wshandler import safe_websocket as sw_mod
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"ping"}', WebSocketDisconnect()],
        )
        with patch.object(
            sw_mod.SafeWebSocket,
            "send_json",
            side_effect=sw_mod.SlowClientError("full"),
        ):
            await handle_consent_decider(ws, app)
        assert app.state.consent_deciders.has_decider(WS) is False  # dropped

    async def test_non_ping_and_invalid_json_are_ignored(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                "not-json",  # invalid JSON -> continue
                '"hello"',  # valid JSON but not a dict -> continue
                "123",  # valid JSON int, not a dict -> continue
                "[1, 2]",  # valid JSON list, not a dict -> continue
                '{"type":"hello"}',  # valid JSON, unknown type -> ignored
                '{"type":"ping"}',  # ping -> pong
                WebSocketDisconnect(),
            ],
        )
        await handle_consent_decider(ws, app)
        assert any(json.loads(m).get("type") == "pong" for m in ws.sent)
        assert (
            app.state.consent_deciders.has_decider(WS) is False
        )  # deregistered

    async def test_runtime_error_disconnect_breaks_cleanly(self):
        # Starlette raises RuntimeError on a mid-receive disconnect; the
        # handler treats it as a disconnect and still deregisters.
        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"id": "u1", "email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS}, [RuntimeError("disconnected")]
        )
        await handle_consent_decider(ws, app)
        assert app.state.consent_deciders.has_decider(WS) is False


class TestConsentDeciderRegistryReconfigure:
    async def test_reconfigure_swaps_app(self):
        import types as _types

        reg = ConsentDeciderRegistry(_app(timeout=10.0))
        new_app = _types.SimpleNamespace(
            state=_types.SimpleNamespace(
                settings=_types.SimpleNamespace(consent_decider_timeout=99.0)
            )
        )
        reg.reconfigure(new_app)
        assert reg.timeout == 99.0


class TestDeciderRegistryBranchGaps2834:
    """#2834 branch gate: start() is idempotent (a second start must not
    spawn a second reaper task)."""

    async def test_start_twice_keeps_single_reaper(self):
        reg = ConsentDeciderRegistry(_app())
        reg.start()
        first = reg._reaper
        assert first is not None
        reg.start()
        assert reg._reaper is first
        await reg.stop()
        assert reg._reaper is None
