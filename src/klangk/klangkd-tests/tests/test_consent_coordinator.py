"""Tests for :mod:`klangk.consent_coordinator` -- the synchronous hold/resolve
coordinator (#2311) -- and the egress-sidecar WebSocket endpoint that drives it.

The coordinator gate-checks each blocked egress (hold iff interactive + a live
decider, else static deny), holds the request in-process (a Future) until a
verdict (#2244 ``resolve``), a timeout, or shutdown, and fail-closes throughout.
"""

from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, Mock

from fastapi import WebSocketDisconnect

from klangk import auth
from klangk.consent_coordinator import ConsentCoordinator

FULL_WS = "aaaa1111bbbb-cccc-dddd-eeee-ffffffffffff"


def _request(req_id="rid-1", host="1.2.3.4", port=443):
    return {
        "id": req_id,
        "workspace_id": FULL_WS,
        "dest_host": host,
        "dest_port": port,
        "decision": "pending",
    }


def _app(
    *,
    timeout: float = 30.0,
    rate_limit: int = 50,
    count_pending: int = 0,
    request=None,
    egress_mode: str = "interactive",
    workspace_exists: bool = True,
    has_decider: bool = True,
    decide_row=None,
    pending_rows=None,
):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        egress_consent_timeout=timeout,
        egress_consent_rate_limit=rate_limit,
    )
    egress_consent = AsyncMock()
    egress_consent.count_pending = AsyncMock(return_value=count_pending)
    egress_consent.create_request = AsyncMock(return_value=request)
    egress_consent.record_static_denial = AsyncMock(return_value=_denial())
    egress_consent.decide = AsyncMock(return_value=decide_row)
    egress_consent.expire_pending = AsyncMock(return_value=True)
    egress_consent.list_requests = AsyncMock(return_value=pending_rows or [])
    workspaces = AsyncMock()
    workspaces.get_workspace = AsyncMock(
        return_value={"egress_mode": egress_mode} if workspace_exists else None
    )
    app.state.model = types.SimpleNamespace(
        egress_consent=egress_consent, workspaces=workspaces
    )
    app.state.consent_deciders = types.SimpleNamespace(
        has_decider=lambda workspace_id: has_decider,
        broadcast=Mock(return_value=0),
    )
    return app


def _denial():
    return {
        "id": "sid",
        "workspace_id": FULL_WS,
        "dest_host": "1.2.3.4",
        "dest_port": 443,
        "decision": "denied",
        "decided_by": None,
    }


class TestConsentCoordinatorGate:
    async def test_no_decider_records_static_and_denies_at_once(self):
        app = _app(has_decider=False)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = fut.result()
        assert verdict == {"decision": "deny", "reason": "static"}
        app.state.model.egress_consent.record_static_denial.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 443
        )
        app.state.model.egress_consent.create_request.assert_not_awaited()
        assert coord._holds == {}

    async def test_not_opted_in_denies_as_static(self):
        app = _app(egress_mode="static", has_decider=True)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result()["reason"] == "static"

    async def test_rate_limited_denies_without_hold(self):
        app = _app(count_pending=50, rate_limit=50, request=_request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "rate_limited"}
        app.state.model.egress_consent.create_request.assert_not_awaited()
        assert coord._holds == {}

    async def test_duplicate_pending_denies(self):
        app = _app(request=None)  # create_request dedup -> None
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "duplicate"}
        assert coord._holds == {}

    async def test_interactive_with_decider_creates_hold(self):
        app = _app(request=_request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert not fut.done()  # held, awaiting verdict
        assert "rid-1" in coord._holds
        app.state.model.egress_consent.create_request.assert_awaited_once()

    async def test_hold_db_error_fail_closes_to_deny(self):
        # a DB/model failure during the static-deny recording must not crash the
        # caller or strand the hold -- hold() returns a resolved deny verdict.
        app = _app(egress_mode="static", has_decider=True)
        app.state.model.egress_consent.record_static_denial = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "error"}
        assert coord._holds == {}

    async def test_hold_interactive_db_error_fail_closes_to_deny(self):
        # same resilience on the interactive path (create_request raises).
        app = _app(request=_request())
        app.state.model.egress_consent.create_request = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        assert fut.result() == {"decision": "deny", "reason": "error"}
        assert coord._holds == {}


class TestConsentCoordinatorFanout:
    async def test_hold_broadcasts_egress_request_to_deciders(self):
        app = _app(request=_request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.assert_called_once()
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert ws_arg == FULL_WS
        assert frame["type"] == "egress_request"
        assert frame["workspace_id"] == FULL_WS
        assert frame["request"]["id"] == "rid-1"

    async def test_resolve_broadcasts_egress_resolved(self):
        row = _request()
        row["decision"] = "allowed"
        app = _app(request=_request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        await coord.resolve("rid-1", "allowed", "once", "a@x")
        # second broadcast is the resolved frame (first was the egress_request)
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert frame == {
            "type": "egress_resolved",
            "workspace_id": FULL_WS,
            "request_id": "rid-1",
            "decision": "allowed",
        }

    async def test_resolve_rejects_verdict_outside_decider_workspace(self):
        # defense-in-depth: a workspace-scoped decider may not decide another
        # workspace's request; the hold stays for a scoped decider.
        row = _request()
        row["decision"] = "allowed"
        app = _app(request=_request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "once", "a@x", decider_workspace="other-ws"
        )
        assert verdict is None
        assert "rid-1" in coord._holds  # hold untouched
        app.state.model.egress_consent.decide.assert_not_awaited()

    async def test_timeout_broadcasts_expired(self):
        app = _app(timeout=0.05, request=_request())
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        await asyncio.sleep(0.12)
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert frame["type"] == "egress_resolved"
        assert frame["decision"] == "expired"

    async def test_snapshot_replays_pending_requests(self):
        rows = [_request("a"), _request("b")]
        app = _app(pending_rows=rows)
        coord = ConsentCoordinator(app)
        frames = await coord.snapshot(FULL_WS)
        assert [f["request"]["id"] for f in frames] == ["a", "b"]
        assert all(f["type"] == "egress_request" for f in frames)
        app.state.model.egress_consent.list_requests.assert_awaited_once_with(
            FULL_WS, decision="pending"
        )

    async def test_snapshot_deploy_wide_is_empty(self):
        app = _app()
        coord = ConsentCoordinator(app)
        assert await coord.snapshot(None) == []
        app.state.model.egress_consent.list_requests.assert_not_awaited()


class TestConsentCoordinatorResolve:
    async def test_resolve_allow_records_and_releases(self):
        row = _request()
        row["decision"] = "allowed"
        app = _app(request=_request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "allowed", "workspace", "a@x", duration="1d"
        )
        assert verdict == {
            "decision": "allow",
            "reason": "decided",
            "duration": "1d",
        }
        assert fut.result() == {
            "decision": "allow",
            "reason": "decided",
            "duration": "1d",
        }
        app.state.model.egress_consent.decide.assert_awaited_once_with(
            "rid-1", "allowed", "workspace", "a@x", "1d"
        )
        assert coord._holds == {}

    async def test_resolve_deny_releases_deny(self):
        row = _request()
        row["decision"] = "denied"
        app = _app(request=_request(), decide_row=row)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve(
            "rid-1", "denied", "once", "a@x", duration="15m"
        )
        assert verdict == {
            "decision": "deny",
            "reason": "decided",
            "duration": "15m",
        }
        assert fut.result()["decision"] == "deny"

    async def test_resolve_unknown_returns_none(self):
        app = _app(request=_request())
        coord = ConsentCoordinator(app)
        assert await coord.resolve("nope", "allowed", "once", "a@x") is None

    async def test_resolve_after_decide_returns_none_fail_closes(self):
        # decide() returns None (row no longer pending -- concurrent expiry):
        # the hold fail-closes to deny.
        app = _app(request=_request(), decide_row=None)
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await coord.resolve("rid-1", "allowed", "once", "a@x")
        assert verdict == {"decision": "deny", "reason": "gone"}
        assert fut.result()["decision"] == "deny"

    async def test_resolve_cancels_the_timeout(self):
        app = _app(timeout=0.05, request=_request(), decide_row=_request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        await coord.resolve("rid-1", "allowed", "once", "a@x")
        await asyncio.sleep(0.12)  # past the would-be timeout
        # timeout cancelled -> the row was NOT expired
        app.state.model.egress_consent.expire_pending.assert_not_awaited()
        assert fut.result()["decision"] == "allow"

    async def test_resolve_fail_closes_when_decide_raises(self):
        # decide() raising (DB error) must not orphan the Future -- the hold's
        # timeout is already cancelled, so resolve fail-closes to deny itself.
        app = _app(request=_request())
        app.state.model.egress_consent.decide = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        app.state.consent_deciders.broadcast.reset_mock()
        verdict = await coord.resolve("rid-1", "allowed", "once", "a@x")
        assert verdict == {"decision": "deny", "reason": "error"}
        assert fut.done() and fut.result()["decision"] == "deny"
        ws_arg, frame = app.state.consent_deciders.broadcast.call_args.args
        assert frame["type"] == "egress_resolved"
        assert frame["decision"] == "expired"
        assert coord._holds == {}  # hold gone, no orphan

    async def test_concurrent_resolves_first_decision_wins(self):
        # two deciders resolve the same hold concurrently: exactly one wins
        # (one decide() write), the other is a no-op (returns None).
        row = _request()
        row["decision"] = "allowed"
        app = _app(request=_request(), decide_row=row)
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        v1, v2 = await asyncio.gather(
            coord.resolve("rid-1", "allowed", "once", "a@x"),
            coord.resolve("rid-1", "denied", "once", "b@x"),
        )
        winners = [v for v in (v1, v2) if v is not None]
        assert len(winners) == 1  # exactly one winner
        assert (
            app.state.model.egress_consent.decide.await_count == 1
        )  # one DB write


class TestConsentCoordinatorTimeout:
    async def test_timeout_expires_and_denies(self):
        app = _app(timeout=0.05, request=_request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await fut
        assert verdict == {
            "decision": "deny",
            "reason": "timeout",
            "duration": "once",
        }
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )
        assert coord._holds == {}

    async def test_timeout_noops_if_resolved_first(self):
        # resolve wins the race -> the timeout task is cancelled before wake.
        app = _app(timeout=0.05, request=_request(), decide_row=_request())
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        await coord.resolve("rid-1", "allowed", "once", "a@x")
        await asyncio.sleep(0.12)
        app.state.model.egress_consent.expire_pending.assert_not_awaited()
        assert fut.result()["decision"] == "allow"

    async def test_fail_close_on_unknown_id_is_noop(self):
        app = _app()
        coord = ConsentCoordinator(app)
        # defensive: fail-closing a hold that is already gone does nothing.
        await coord._fail_close("never-held", reason="timeout")
        app.state.model.egress_consent.expire_pending.assert_not_awaited()

    async def test_timeout_still_denies_when_expire_raises(self):
        # expire_pending failing must not strand the hold -- it logs + still
        # resolves the Future deny.
        app = _app(timeout=0.05, request=_request())
        app.state.model.egress_consent.expire_pending = AsyncMock(
            side_effect=RuntimeError("db gone")
        )
        coord = ConsentCoordinator(app)
        fut = await coord.hold(FULL_WS, "1.2.3.4", 443)
        verdict = await fut
        assert verdict == {
            "decision": "deny",
            "reason": "timeout",
            "duration": "once",
        }
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid-1"
        )


class TestConsentCoordinatorStop:
    async def test_stop_fail_closes_all_holds(self):
        app = _app(request=_request("r1"))
        coord = ConsentCoordinator(app)
        await coord.hold(FULL_WS, "1.2.3.4", 443)
        # second hold with a distinct request id
        app.state.model.egress_consent.create_request = AsyncMock(
            return_value=_request("r2")
        )
        await coord.hold(FULL_WS, "5.6.7.8", 80)
        assert len(coord._holds) == 2
        await coord.stop()
        assert coord._holds == {}
        expired = {
            call.args[0]
            for call in app.state.model.egress_consent.expire_pending.await_args_list
        }
        assert expired == {"r1", "r2"}

    async def test_stop_is_idempotent(self):
        app = _app(request=_request())
        coord = ConsentCoordinator(app)
        await coord.stop()
        await coord.stop()
        assert coord._holds == {}


class TestConsentCoordinatorReconfigure:
    async def test_reconfigure_swaps_app(self):
        app = _app(request=_request())
        coord = ConsentCoordinator(app)
        new_app = _app(timeout=1.0, request=_request())
        coord.reconfigure(new_app)
        assert coord.app is new_app
        assert coord.timeout == 1.0


# --- egress-sidecar WebSocket endpoint --------------------------------------


class _FakeWS:
    """Minimal stand-in for a fastapi WebSocket for handler-level tests.

    Incoming messages come from an asyncio.Queue (``feed``), so a test can
    push an egress event, let the relay task run, then push a disconnect --
    mirroring the real sidecar's send-then-wait-for-verdict ordering.
    """

    def __init__(self, params: dict, headers: dict | None = None):
        self.query_params = params
        self.headers = headers or {}
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def send_json(self, data: dict) -> None:
        self.sent.append(json.dumps(data))

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def feed(self, item) -> None:
        await self._incoming.put(item)


def _sidecar_app(token_result=FULL_WS):
    """App with mocked workspace-token decode + a mock coordinator."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()

    def _decode(token):
        return token_result

    app.state.auth = types.SimpleNamespace(decode_workspace_token=_decode)
    coord = AsyncMock()
    app.state.consent_coordinator = coord
    return app, coord


class TestEgressSidecarWS:
    async def test_missing_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app()
        ws = _FakeWS({})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4001, "Missing token")
        assert ws.accepted is False

    async def test_authorization_header_token_accepted(self):
        # egress path (#2319): the JWT rides in the Authorization header (the
        # sidecar sends `Bearer <jwt>` so the egress site's forward_auth sees
        # it), not the ?token= query param. Both paths must authenticate.
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({}, headers={"authorization": "Bearer hdr-tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(0.05)
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", 443)
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_invalid_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app(token_result=None)
        ws = _FakeWS({"token": "bad"})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4001, "Invalid token")

    async def test_expired_token_rejected(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, _ = _sidecar_app(token_result=auth.Auth.WORKSPACE_TOKEN_EXPIRED)
        ws = _FakeWS({"token": "stale"})
        await handle_egress_sidecar(ws, app)
        assert ws.closed == (4002, "Token expired")

    async def test_static_egress_relays_deny_immediately(self):
        # coordinator returns an already-resolved deny Future (static path)
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(
            0.05
        )  # let the relay call hold + flush the verdict
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", 443)
        assert [json.loads(m) for m in ws.sent] == [
            {
                "type": "verdict",
                "id": "loc1",
                "decision": "deny",
                "duration": "once",
            }
        ]
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_held_egress_relays_verdict_when_resolved(self):
        # coordinator returns a pending Future the test resolves -> allow
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "loc1", "dst": "9.9.9.9", "dport": 53}
            )
        )
        await asyncio.sleep(0.05)  # relay is now awaiting the verdict Future
        fut.set_result(
            {"decision": "allow", "reason": "decided", "duration": "1d"}
        )
        await asyncio.sleep(0.05)  # relay sends the verdict
        assert [json.loads(m) for m in ws.sent] == [
            {
                "type": "verdict",
                "id": "loc1",
                "decision": "allow",
                "duration": "1d",
            }
        ]
        await ws.feed(WebSocketDisconnect())
        await handler

    async def test_non_egress_and_invalid_json_ignored(self):
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"decision": "deny", "reason": "static"})
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed("not-json")
        await ws.feed(json.dumps({"type": "ping"}))
        # valid JSON but not a dict (string / int / list / bool) must be
        # ignored, not crash the handler with AttributeError on .get().
        await ws.feed(json.dumps("hello"))
        await ws.feed(json.dumps(123))
        await ws.feed(json.dumps([1, 2]))
        await ws.feed(json.dumps(True))
        await ws.feed(
            json.dumps({"type": "egress", "id": 5, "dst": "1.2.3.4"})
        )  # bad id (not a str)
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "ok", "dst": "1.2.3.4", "dport": "x"}
            )
        )  # bad dport (not an int)
        await ws.feed(
            json.dumps(
                {"type": "egress", "id": "ok", "dst": "1.2.3.4", "dport": True}
            )
        )  # bad dport (bool, not an int -- isinstance(True, int) is True)
        await ws.feed(
            json.dumps({"type": "egress", "id": "ok", "dst": "1.2.3.4"})
        )
        await asyncio.sleep(0.05)
        await ws.feed(WebSocketDisconnect())
        await handler
        # only the well-formed egress event reached the coordinator
        coord.hold.assert_awaited_once_with(FULL_WS, "1.2.3.4", None)

    async def test_disconnect_cancels_in_flight_relay(self):
        # a held egress whose relay never resolves: disconnect cancels it,
        # and no verdict is sent.
        from klangk.wshandler.sidecar import handle_egress_sidecar

        app, coord = _sidecar_app()
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        coord.hold = AsyncMock(return_value=fut)
        ws = _FakeWS({"token": "tok"})
        handler = asyncio.create_task(handle_egress_sidecar(ws, app))
        await ws.feed(
            json.dumps(
                {
                    "type": "egress",
                    "id": "loc1",
                    "dst": "1.2.3.4",
                    "dport": 443,
                }
            )
        )
        await asyncio.sleep(0.05)  # relay is now awaiting the Future
        await ws.feed(RuntimeError())  # disconnect mid-hold
        await handler
        assert ws.sent == []  # relay cancelled before it could send
        assert not fut.done()  # the coordinator's hold Future is untouched
