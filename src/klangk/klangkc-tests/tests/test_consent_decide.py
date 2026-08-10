"""Tests for the synchronous consent-decider TUI (#2310).

Covers the pure :class:`ConsentDeciderController` (protocol state machine),
the module-level frame builders, and the :class:`ConsentDeciderApp` Textual
view (WS pump, ping loop, reconnect, key actions) under the 100% coverage
gate. The on-mount WS worker is stubbed for view tests (it is exercised
directly via the captured real loop).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets
from textual.widgets import ListView, Static

from klangk.cli.tui import consent as tui_consent
from klangk.cli.tui.consent import (
    ADDED,
    ERROR,
    IGNORED,
    PONG,
    RESOLVED,
    ConsentDeciderApp,
    ConsentDeciderController,
    ConsentRequest,
    make_ping,
    make_verdict,
)

# Capture the real WS loop before any test stubs it, so the reconnect test can
# invoke it directly while view tests run the app without a live worker.
_real_ws_loop = ConsentDeciderApp._ws_loop


class FakeWS:
    """recv()-based WS that replays frames and records sends.

    Mirrors the real ``websockets`` connection: ``recv()`` raises
    ``ConnectionClosed`` when the (frame) stream is exhausted. ``send_fail``
    makes ``send()`` raise, and ``close_exc`` overrides the close exception
    (to simulate an auth 4001/4002 close).
    """

    def __init__(self, frames, *, send_fail=False, close_exc=None):
        self._frames = list(frames)
        self.sent = []
        self.send_fail = send_fail
        self.close_exc = close_exc

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise self.close_exc or websockets.ConnectionClosed(None, None)

    async def send(self, data):
        if self.send_fail:
            raise websockets.ConnectionClosed(None, None)
        self.sent.append(data)


class FakeCM:
    """Async context manager yielding a FakeWS (stands in for ws_connect)."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *a):
        return False


def _req_frame(rid="r1", host="evil.example.com", port=443, **extra):
    req = {
        "id": rid,
        "workspace_id": "wsid",
        "dest_host": host,
        "dest_port": port,
        "process_name": None,
        "pid": None,
        "requested_at": 100.0,
    }
    req.update(extra)
    return json.dumps({"type": "egress_request", "request": req})


def _make_app(**kw):
    app = ConsentDeciderApp(
        "http://server", "tok", "wsid", "wsname", hold_timeout=30.0, **kw
    )
    return app


@pytest.fixture(autouse=True)
def _stub_ws_worker(monkeypatch):
    """Replace the on-mount WS worker with a no-op for view tests."""

    async def _noop(self):
        return None

    monkeypatch.setattr(ConsentDeciderApp, "_ws_loop", _noop)


# ---------------------------------------------------------------------------
# Controller: frame parsing + state
# ---------------------------------------------------------------------------


class TestControllerFrames:
    def test_egress_request_added(self):
        c = ConsentDeciderController()
        action, payload = c.apply_frame(_req_frame())
        assert action == ADDED
        assert isinstance(payload, ConsentRequest)
        assert payload.id == "r1"
        assert payload.dest_port == 443
        assert "r1" in c.pending

    def test_snapshot_and_live_both_add(self):
        c = ConsentDeciderController()
        c.apply_frame(_req_frame("a"))
        c.apply_frame(_req_frame("b"))
        assert set(c.pending) == {"a", "b"}

    def test_egress_resolved_removes(self):
        c = ConsentDeciderController()
        c.apply_frame(_req_frame("r1"))
        action, payload = c.apply_frame(
            json.dumps(
                {
                    "type": "egress_resolved",
                    "request_id": "r1",
                    "decision": "allowed",
                }
            )
        )
        assert action == RESOLVED
        assert payload == ("r1", "allowed")
        assert "r1" not in c.pending

    def test_egress_resolved_unknown_id_is_noop(self):
        c = ConsentDeciderController()
        action, payload = c.apply_frame(
            json.dumps(
                {
                    "type": "egress_resolved",
                    "request_id": "ghost",
                    "decision": "expired",
                }
            )
        )
        assert action == RESOLVED
        assert c.pending == {}

    def test_pong(self):
        c = ConsentDeciderController()
        assert c.apply_frame(json.dumps({"type": "pong"})) == (PONG, None)

    def test_error_carries_message(self):
        c = ConsentDeciderController()
        action, payload = c.apply_frame(
            json.dumps({"type": "error", "message": "bad decision"})
        )
        assert action == ERROR
        assert payload == "bad decision"

    def test_error_missing_message(self):
        c = ConsentDeciderController()
        action, payload = c.apply_frame(json.dumps({"type": "error"}))
        assert action == ERROR
        assert payload == ""

    def test_non_json_ignored(self):
        c = ConsentDeciderController()
        assert c.apply_frame("not json{") == (IGNORED, None)
        assert c.apply_frame(None) == (IGNORED, None)  # type: ignore[arg-type]

    def test_non_dict_ignored(self):
        c = ConsentDeciderController()
        assert c.apply_frame(json.dumps([1, 2, 3])) == (IGNORED, None)
        assert c.apply_frame(json.dumps("x")) == (IGNORED, None)

    def test_unknown_type_ignored(self):
        c = ConsentDeciderController()
        assert c.apply_frame(json.dumps({"type": "mystery"})) == (
            IGNORED,
            None,
        )

    def test_request_missing_id_ignored(self):
        c = ConsentDeciderController()
        bad = json.dumps(
            {"type": "egress_request", "request": {"workspace_id": "wsid"}}
        )
        assert c.apply_frame(bad) == (IGNORED, None)
        assert c.pending == {}

    def test_request_missing_workspace_ignored(self):
        c = ConsentDeciderController()
        bad = json.dumps({"type": "egress_request", "request": {"id": "r1"}})
        assert c.apply_frame(bad) == (IGNORED, None)

    def test_request_missing_request_object_ignored(self):
        c = ConsentDeciderController()
        assert c.apply_frame(json.dumps({"type": "egress_request"})) == (
            IGNORED,
            None,
        )

    def test_port_none_and_garbage(self):
        c = ConsentDeciderController()
        _, p = c.apply_frame(_req_frame(port=None))
        assert p.dest_port is None
        c2 = ConsentDeciderController()
        _, p2 = c2.apply_frame(_req_frame(port="80"))  # type: ignore[arg-type]
        assert p2.dest_port is None  # non-numeric -> None

    def test_requested_at_missing_defaults_zero(self):
        c = ConsentDeciderController()
        _, p = c.apply_frame(
            json.dumps(
                {
                    "type": "egress_request",
                    "request": {"id": "r1", "workspace_id": "wsid"},
                }
            )
        )
        assert p.requested_at == 0.0


class TestControllerOrdering:
    def test_ordered_oldest_first(self):
        c = ConsentDeciderController()
        c.apply_frame(_req_frame("b", requested_at=200.0))
        c.apply_frame(_req_frame("a", requested_at=100.0))
        ordered = c.ordered()
        assert [r.id for r in ordered] == ["a", "b"]


class TestControllerCountdown:
    def test_remaining_uses_clock_and_timeout(self):
        c = ConsentDeciderController(hold_timeout=10.0, clock=lambda: 105.0)
        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="h",
            dest_port=1,
            process_name=None,
            pid=None,
            requested_at=100.0,
        )
        assert c.remaining(req) == 5.0

    def test_remaining_clamps_at_zero(self):
        c = ConsentDeciderController(hold_timeout=10.0, clock=lambda: 999.0)
        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="h",
            dest_port=1,
            process_name=None,
            pid=None,
            requested_at=0.0,
        )
        assert c.remaining(req) == 0.0

    def test_remaining_with_real_epoch_clock(self):
        # Regression (#2320 review #1): the server stamps requested_at with
        # time.time() (epoch wall-clock), so the controller's DEFAULT clock
        # must be time.time -- mixing in time.monotonic made the countdown
        # ~1.7e9s and it never visually counted down.
        c = ConsentDeciderController(hold_timeout=10.0)  # default = time.time
        now = time.time()
        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="h",
            dest_port=1,
            process_name=None,
            pid=None,
            requested_at=now - 4,
        )
        # ~6s left, allowing scheduler/clock slack on either side.
        assert 3.0 <= c.remaining(req) <= 7.0


# ---------------------------------------------------------------------------
# Module-level frame builders
# ---------------------------------------------------------------------------


class TestFrameBuilders:
    def test_make_verdict_allowed(self):
        msg = json.loads(make_verdict("r1", "allowed"))
        assert msg == {
            "type": "verdict",
            "request_id": "r1",
            "decision": "allowed",
            "scope": "once",
        }

    def test_make_verdict_denied(self):
        msg = json.loads(make_verdict("r1", "denied"))
        assert msg["decision"] == "denied"

    def test_make_ping(self):
        assert json.loads(make_ping()) == {"type": "ping"}


# ---------------------------------------------------------------------------
# App: rendering, pump, ping, reconnect, actions
# ---------------------------------------------------------------------------


class TestAppRender:
    async def test_compose_mounts_widgets(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#requests", ListView) is not None
            assert app.query_one("#status", Static) is not None

    async def test_refresh_shows_queue_and_status(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            lv = app.query_one("#requests", ListView)
            assert len(lv.children) == 2
            status = app.query_one("#status", Static)
            assert "wsname" in str(status.content)

    async def test_refresh_empty_hides_queue(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app._refresh()
            await pilot.pause()
            assert app.query_one("#empty", Static).display is True

    async def test_refresh_shows_active_flash(self):
        # While a flash is active (within TTL), _refresh renders it instead of
        # the normal status (so flashes survive the 1s periodic refresh).
        app = _make_app()
        async with app.run_test() as pilot:
            app._flash("something broke")
            app._refresh()
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "something broke" in str(status.content)

    async def test_refresh_before_mount_is_noop(self):
        # The worker may call _refresh before mount completes; it must not blow up.
        app = _make_app()
        app._refresh()  # not mounted -> guards return early

    async def test_refresh_preserves_focus_across_rebuild(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            lv = app.query_one("#requests", ListView)
            lv.index = 1  # focus r2
            await pilot.pause()
            app._refresh()  # rebuild keeps r2 focused
            await pilot.pause()
            assert app._focused_request_id() == "r2"

    async def test_render_item_with_process_and_port(self):
        app = _make_app()
        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="h.example.com",
            dest_port=443,
            process_name="curl",
            pid=42,
            requested_at=0.0,
        )
        item = app._render_item(req)
        assert item.request_id == "r1"


class TestAppPump:
    async def test_pump_applies_frames(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS(
                [
                    _req_frame("r1", host="evil.example.com"),
                    json.dumps({"type": "pong"}),
                    json.dumps(
                        {
                            "type": "egress_resolved",
                            "request_id": "r1",
                            "decision": "expired",
                        }
                    ),
                ]
            )
            await app._pump(ws)
            await pilot.pause()
            assert app.controller.pending == {}  # added then resolved

    async def test_pump_flashes_error_frame(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([json.dumps({"type": "error", "message": "nope"})])
            await app._pump(ws)
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "nope" in str(status.content)

    async def test_pump_returns_auth_close_on_4002(self):
        app = _make_app()
        exc = websockets.ConnectionClosed(
            websockets.frames.Close(4002, "Token expired"), None
        )
        async with app.run_test():
            ws = FakeWS([], close_exc=exc)
            assert await app._pump(ws) is True

    async def test_pump_returns_false_on_normal_close(self):
        app = _make_app()
        async with app.run_test():
            ws = FakeWS([_req_frame("r1")])
            assert await app._pump(ws) is False

    async def test_pump_isolates_render_failure(self, monkeypatch):
        # A render bug must NOT propagate out of _pump (which would tear down
        # the transport and replay the snapshot in a tight reconnect loop).
        app = _make_app()

        def boom():
            raise RuntimeError("render broke")

        monkeypatch.setattr(app, "_refresh", boom)
        async with app.run_test():
            ws = FakeWS([_req_frame("r1"), json.dumps({"type": "pong"})])
            # Must not raise; the render error is logged + swallowed.
            assert await app._pump(ws) is False


class TestAppPing:
    async def test_ping_loop_sends_ping(self):
        app = _make_app(ping_interval=0.01)
        ws = FakeWS([])
        task = asyncio.create_task(app._ping_loop(ws))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert any('"ping"' in s for s in ws.sent)

    async def test_ping_loop_stops_when_flag_set(self):
        app = _make_app(ping_interval=0.01)
        app._stop = True
        ws = FakeWS([])
        await asyncio.wait_for(app._ping_loop(ws), timeout=0.5)
        assert ws.sent == []  # returned before sending

    async def test_ping_loop_swallows_socket_error(self):
        # A socket close mid-send (ConnectionClosed) returns cleanly so the
        # task has no unobserved exception (#2320 review #5).
        app = _make_app(ping_interval=0.01)

        class DeadSend:
            sent = []

            async def send(self, d):
                raise websockets.ConnectionClosed(None, None)

        await asyncio.wait_for(app._ping_loop(DeadSend()), timeout=0.5)


class TestAppBackoff:
    def test_backoff_progresses_then_caps(self):
        app = _make_app(reconnect_delays=(1.0, 2.0, 5.0))
        assert app._backoff(1) == 1.0
        assert app._backoff(2) == 2.0
        assert app._backoff(3) == 5.0
        assert app._backoff(99) == 5.0  # clamped

    def test_backoff_empty_delays(self):
        app = _make_app(reconnect_delays=())
        assert app._backoff(1) == 0.0


class TestAppActions:
    async def test_action_allow_sends_verdict(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            lv = app.query_one("#requests", ListView)
            lv.index = 0
            await pilot.pause()
            app.action_allow()
            await pilot.pause()
            assert any('"allowed"' in s and '"r1"' in s for s in ws.sent)

    async def test_action_deny_sends_verdict(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0
            await pilot.pause()
            app.action_deny()
            await pilot.pause()
            assert any('"denied"' in s for s in ws.sent)

    async def test_action_with_no_focus_sends_nothing(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.action_allow()
            await pilot.pause()
            assert ws.sent == []

    async def test_action_while_disconnected_sends_nothing(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0
            await pilot.pause()
            # app._ws is None (disconnected)
            app.action_allow()
            await pilot.pause()

    async def test_send_failure_flashes(self):
        # A dropped socket between the ws check and the send surfaces a flash
        # rather than silently losing the verdict (#2320 review #2).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([], send_fail=True)
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0
            await pilot.pause()
            app.action_allow()
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "verdict send failed" in str(status.content)


class TestWsLoop:
    async def test_reconnect_after_drop(self, monkeypatch):
        calls = {"n": 0}

        def fake_connect(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")
            return FakeCM(FakeWS([]))  # connect ok, then stream ends

        monkeypatch.setattr(tui_consent, "ws_connect", fake_connect)
        app = _make_app(reconnect_delays=(0.0,))
        task = asyncio.create_task(_real_ws_loop(app))
        await asyncio.sleep(0.1)
        app._stop = True
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert calls["n"] >= 2  # failed once, then reconnected

    async def test_connect_exception_logs_and_retries(self, monkeypatch):
        # ws_connect always raises: the loop keeps retrying (never crashes).
        def always_fail(*a, **kw):
            raise OSError("refused")

        monkeypatch.setattr(tui_consent, "ws_connect", always_fail)
        app = _make_app(reconnect_delays=(0.0,))
        task = asyncio.create_task(_real_ws_loop(app))
        await asyncio.sleep(0.1)
        app._stop = True
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert app._connected is False

    async def test_refreshes_token_on_auth_close(self, monkeypatch):
        # A 4002 (expired token) close refreshes the JWT before reconnecting
        # (#2320 review #3), so the tool self-heals past JWT expiry instead of
        # spinning on a dead token.
        exc = websockets.ConnectionClosed(
            websockets.frames.Close(4002, "Token expired"), None
        )
        monkeypatch.setattr(
            tui_consent,
            "ws_connect",
            lambda *a, **k: FakeCM(FakeWS([], close_exc=exc)),
        )
        app = _make_app(reconnect_delays=(0.0,))

        async def fake_refresh():
            return "new-token"

        monkeypatch.setattr(app, "_refresh_token", fake_refresh)
        task = asyncio.create_task(_real_ws_loop(app))
        await asyncio.sleep(0.1)
        app._stop = True
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert app.token == "new-token"

    async def test_refresh_token_success(self, monkeypatch):
        app = _make_app()
        monkeypatch.setattr(
            tui_consent, "refresh_token", lambda url, tok: "fresh"
        )
        assert await app._refresh_token() == "fresh"

    async def test_refresh_token_failure_is_swallowed(self, monkeypatch):
        app = _make_app()

        def boom(url, tok):
            raise RuntimeError("nope")

        monkeypatch.setattr(tui_consent, "refresh_token", boom)
        assert await app._refresh_token() is None

    async def test_breaks_on_stop_mid_stream(self, monkeypatch):
        # _stop set while the pump is parked in a read: the loop exits via
        # the early `if self._stop: break` (no reconnect, no cancel needed).
        done = asyncio.Event()

        class SlowWS:
            sent = []

            async def recv(self):
                if done.is_set():
                    raise websockets.ConnectionClosed(None, None)
                await asyncio.sleep(0.01)

            async def send(self, d):
                self.sent.append(d)

        ws = SlowWS()
        monkeypatch.setattr(
            tui_consent, "ws_connect", lambda *a, **k: FakeCM(ws)
        )
        app = _make_app(reconnect_delays=(0.0,))
        task = asyncio.create_task(_real_ws_loop(app))
        await asyncio.sleep(0.05)  # connected, pumping
        app._stop = True
        done.set()  # release the read -> pump ends -> early break
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
