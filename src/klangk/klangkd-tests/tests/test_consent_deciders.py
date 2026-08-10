"""Tests for :mod:`klangk.consent_deciders` -- the live-decider registry (#2308)."""

from __future__ import annotations

import asyncio
import types

from klangk.consent_deciders import ConsentDeciderRegistry

WS = "ws-aaaa1111-2222-3333-4444-555566667777"
WS2 = "ws-bbbb2222-3333-4444-5555-666677778888"


def _app(timeout: float = 45.0):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(consent_decider_timeout=timeout)
    return app


class TestConsentDeciderRegistry:
    async def test_no_decider_is_not_interactive(self):
        reg = ConsentDeciderRegistry(_app())
        assert reg.has_decider(WS) is False

    async def test_register_then_deregister(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "admin@example.com")
        assert reg.has_decider(WS) is True
        reg.deregister("d1")
        assert reg.has_decider(WS) is False
        # deregister of unknown id is a no-op
        reg.deregister("nope")

    async def test_multiple_deciders_same_workspace(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x")
        reg.register("d2", WS, "b@x")
        reg.register("d3", WS, "c@x")
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
        reg.register("d1", WS, "a@x")
        assert reg.has_decider(WS) is True
        # not visible to a different workspace
        assert reg.has_decider(WS2) is False
        assert reg.deciders_for(WS2) == []

    async def test_deploy_wide_decider_covers_every_workspace(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("admin", None, "admin@example.com")
        assert reg.has_decider(WS) is True
        assert reg.has_decider(WS2) is True
        assert set(reg.deciders_for(WS)) == {"admin"}

    async def test_register_is_idempotent_on_decider_id(self):
        reg = ConsentDeciderRegistry(_app())
        reg.register("d1", WS, "a@x")
        reg.register(
            "d1", WS, "a@x"
        )  # reconnect with same id -> refresh, not dup
        assert reg.deciders_for(WS) == ["d1"]

    async def test_touch_extends_liveness(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.05))
        reg.register("d1", WS, "a@x")
        # age it past the timeout, then ping to refresh
        reg._deciders["d1"]["seen"] -= 1.0
        assert reg.has_decider(WS) is False
        reg.touch("d1")
        assert reg.has_decider(WS) is True
        # touch of unknown id is a no-op
        reg.touch("nope")

    async def test_reaper_drops_stale_deciders(self):
        reg = ConsentDeciderRegistry(_app(timeout=0.02))
        reg.register("d1", WS, "a@x")
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
        reg.register("d1", WS, "a@x")
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
        reg.register("d1", WS, "a@x")
        reg.register("d2", None, "b@x")
        await reg.stop()
        assert reg._deciders == {}
        assert reg._reaper is None
        await reg.stop()  # idempotent


class _FakeWS:
    """Minimal stand-in for a fastapi WebSocket for handler-level tests."""

    def __init__(self, params: dict, incoming: list):
        self.query_params = params
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

    async def receive_text(self) -> str:
        item = next(self._incoming)
        if isinstance(item, BaseException):
            raise item
        return item


def _ws_app(token_result):
    """App with mocked auth + a real ConsentDeciderRegistry."""
    from unittest.mock import AsyncMock

    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(consent_decider_timeout=45.0)
    app.state.consent_deciders = ConsentDeciderRegistry(app)
    app.state.auth = types.SimpleNamespace(
        get_user_from_token=AsyncMock(return_value=token_result)
    )
    return app


class TestConsentDeciderWS:
    async def test_connect_registers_disconnect_deregisters(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"email": "admin@example.com"})
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

        app = _ws_app({"email": "a@x"})
        ws = _FakeWS({"workspace": WS}, [])
        await handle_consent_decider(ws, app)
        assert ws.closed == (4001, "Missing token")
        assert ws.accepted is False
        assert app.state.consent_deciders.has_decider(WS) is False

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

    async def test_ping_touches_and_pongs(self):
        import json

        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            ['{"type":"ping"}', WebSocketDisconnect()],
        )
        await handle_consent_decider(ws, app)
        assert any(json.loads(m).get("type") == "pong" for m in ws.sent)

    async def test_deploy_wide_when_no_workspace_param(self):
        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"email": "admin@example.com"})
        ws = _FakeWS({"token": "tok"}, [WebSocketDisconnect()])
        # While connected it would cover every workspace; after disconnect,
        # nothing remains.
        await handle_consent_decider(ws, app)
        assert app.state.consent_deciders.has_decider(WS) is False
        assert app.state.consent_deciders.has_decider(WS2) is False

    async def test_non_ping_and_invalid_json_are_ignored(self):
        import json

        from fastapi import WebSocketDisconnect

        from klangk.wshandler.decider import handle_consent_decider

        app = _ws_app({"email": "a@x"})
        ws = _FakeWS(
            {"token": "tok", "workspace": WS},
            [
                "not-json",  # invalid JSON -> continue
                '{"type":"hello"}',  # valid JSON, not a ping -> ignored
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

        app = _ws_app({"email": "a@x"})
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
