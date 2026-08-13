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
import types

import pytest
import websockets
from textual.widgets import Button, ListView, Static

from klangk.cli.tui import consent as tui_consent
from klangk.cli.tui.consent import (
    ADDED,
    ERROR,
    IGNORED,
    PAUSE_ACK,
    PONG,
    RESOLVED,
    REVOKE_ACK,
    RULES,
    ConsentDeciderApp,
    ConsentDeciderController,
    ConsentRequest,
    ConsentRule,
    EgressRules,
    PauseState,
    QuitConfirmScreen,
    RulesScreen,
    build_detach_command,
    make_pause,
    make_ping,
    make_revoke,
    make_unpause,
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


def _rule(
    rid="a1",
    *,
    host="evil.example.com",
    port=443,
    decision="allowed",
    duration="5m",
    decided_at=100.0,
    process=None,
    decided_by="u@x",
):
    """One row of an egress_rules frame's allowed/denied list."""
    return {
        "id": rid,
        "workspace_id": "wsid",
        "dest_host": host,
        "dest_port": port,
        "pid": None,
        "process_name": process,
        "decision": decision,
        "duration": duration,
        "requested_at": 90.0,
        "decided_at": decided_at,
        "decided_by": decided_by,
    }


def _rules_frame(
    *,
    allow_list=("static.example.com",),
    allowed=(),
    denied=(),
    paused=None,
    workspace_id="wsid",
):
    """An egress_rules frame (#2335 slice A) as the server sends it."""
    return json.dumps(
        {
            "type": "egress_rules",
            "workspace_id": workspace_id,
            "allow_list": list(allow_list),
            "allowed": list(allowed),
            "denied": list(denied),
            "paused": paused,
        }
    )


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
            "duration": "tilrestart",
        }

    def test_make_verdict_carries_duration(self):
        msg = json.loads(make_verdict("r1", "allowed", "1d"))
        assert msg["duration"] == "1d"

    def test_make_verdict_denied(self):
        msg = json.loads(make_verdict("r1", "denied"))
        assert msg["decision"] == "denied"
        assert msg["duration"] == "tilrestart"

    def test_make_ping(self):
        assert json.loads(make_ping()) == {"type": "ping"}

    def test_make_revoke(self):
        # #2341: revoke frame carries only the request id.
        assert json.loads(make_revoke("r9")) == {
            "type": "revoke",
            "request_id": "r9",
        }

    def test_make_pause(self):
        # #2332: pause frame carries the window token.
        assert json.loads(make_pause("15m")) == {
            "type": "pause",
            "duration": "15m",
        }

    def test_make_unpause(self):
        # #2332: unpause frame clears the window (no payload).
        assert json.loads(make_unpause()) == {"type": "unpause"}


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

    async def test_rows_are_compact_both_visible(self):
        # Regression: each held-request row must be compact, not the full
        # viewport height, so multiple pending requests are visible at once
        # without scrolling. Previously ListItem defaulted to height:auto,
        # which expanded to fill the whole ListView -- so with 2 requests
        # only one row showed and the other lurked below the fold (you had
        # to scroll the ListView to see it).
        app = _make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            await pilot.pause()
            lv = app.query_one("#requests", ListView)
            rows = list(lv.children)
            assert len(rows) == 2
            # Each row is compact (host line + button line), nowhere near the
            # full viewport height.
            for row in rows:
                assert row.outer_size.height <= 4, (
                    f"row height {row.outer_size.height} too tall; "
                    "multiple requests would be hidden below the fold"
                )
            # Both rows fit within the viewport -- the 2nd needs no scrolling.
            viewport_h = lv.size.height
            bottoms = [row.region.y + row.region.height for row in rows]
            assert max(bottoms) <= viewport_h, (
                f"row bottoms {bottoms} exceed viewport height {viewport_h}; "
                "the 2nd row would require scrolling to see"
            )

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

    async def test_update_item_survives_unmounted_row(self):
        # Regression: _refresh's diff treats a just-appended row (request_id
        # set synchronously) as "existing" on the very next refresh, but its
        # child widgets mount asynchronously -- so _update_item's query_one
        # (".req-host") can fire before the row is mounted. It must not crash
        # (NoMatches is swallowed; the next tick repaints once mounted).
        app = _make_app()
        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="h.example.com",
            dest_port=443,
            process_name=None,
            pid=None,
            requested_at=0.0,
        )
        item = app._render_item(req)  # built but NOT mounted
        # Must not raise NoMatches.
        app._update_item(item, req)


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

    async def test_allow_button_decides_that_request(self):
        # The per-row Allow button (id "allow-<rid>") decides THAT request,
        # independent of which row is highlighted.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=types.SimpleNamespace(id="allow-r1")
                )
            )
            await pilot.pause()
            assert any('"allowed"' in s and '"r1"' in s for s in ws.sent)

    async def test_deny_button_decides_that_request(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=types.SimpleNamespace(id="deny-r1")
                )
            )
            await pilot.pause()
            assert any('"denied"' in s and '"r1"' in s for s in ws.sent)

    async def test_duration_selection_does_not_submit(self):
        # Clicking a global duration button selects it (highlights + stores) but
        # sends NO verdict -- only Allow/Deny submit (#2328).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            btn = app.query_one("#dur-1d", Button)
            app.on_button_pressed(types.SimpleNamespace(button=btn))
            await pilot.pause()
            assert ws.sent == []  # selecting a duration does NOT submit
            assert app._duration == "1d"
            assert btn.has_class("dur-sel")

    async def test_allow_submits_with_selected_duration(self):
        # Allow sends the verdict carrying the global selected duration.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(button=app.query_one("#dur-1h", Button))
            )
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=types.SimpleNamespace(id="allow-r1")
                )
            )
            await pilot.pause()
            assert any(
                '"allowed"' in s and '"r1"' in s and '"1h"' in s
                for s in ws.sent
            ), ws.sent

    async def test_duration_defaults_to_tilrestart(self):
        # A fresh row defaults to `tilrestart`; Allow without changing it sends
        # `tilrestart`.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=types.SimpleNamespace(id="allow-r1")
                )
            )
            await pilot.pause()
            assert any('"tilrestart"' in s for s in ws.sent), ws.sent

    async def test_duration_selector_guards(self):
        # Defensive guard: a button without a duration attr is a no-op.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app._select_duration(types.SimpleNamespace(duration=None))
            assert app._duration == "tilrestart"  # unchanged (default)

    async def test_pause_bar_mounts(self):
        # #2332: the pause control bar mounts with a window button + Cancel.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#pause-15m", Button) is not None
            assert app.query_one("#pause-1h", Button) is not None
            assert app.query_one("#pause-1d", Button) is not None
            assert app.query_one("#pause-cancel", Button) is not None

    async def test_pause_buttons_render_on_screen(self):
        # Regression (#2332): the ``Pause:`` label must not expand to fill the
        # bar (which pushed the window buttons off-screen). All four controls
        # must sit within the viewport, in order, after the label.
        app = _make_app()
        async with app.run_test(size=(120, 24)) as pilot:
            app._refresh()
            await pilot.pause()
            await pilot.pause()
            lbl = app.query_one("#pause-label", Static)
            assert (
                lbl.outer_size.width < 20
            )  # "Pause:" + padding, not full width
            prev_x = lbl.region.x
            for bid in (
                "#pause-15m",
                "#pause-1h",
                "#pause-1d",
                "#pause-cancel",
            ):
                b = app.query_one(bid, Button)
                # each button starts where the previous ended and is on-screen
                assert b.region.x >= prev_x
                assert b.region.x + b.region.width <= 120, bid
                assert b.region.width > 0
                prev_x = b.region.x + b.region.width

    async def test_pause_button_sends_pause_frame(self):
        # Pressing a window button sends a pause frame for that window.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=app.query_one("#pause-15m", Button)
                )
            )
            await pilot.pause()
            assert any('"pause"' in s and '"15m"' in s for s in ws.sent), (
                ws.sent
            )

    async def test_cancel_button_sends_unpause_frame(self):
        # The Cancel button clears an active pause.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=app.query_one("#pause-cancel", Button)
                )
            )
            await pilot.pause()
            assert any('"unpause"' in s for s in ws.sent), ws.sent

    async def test_pause_button_disconnected_flashes(self):
        # No WS -> the press flashes instead of crashing.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=app.query_one("#pause-15m", Button)
                )
            )
            await pilot.pause()
            assert app._flash_until > 0  # a flash was set

    async def test_pause_highlights_bar_when_active(self):
        # An egress_rules frame with a live pause flags the bar label.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": 9999.0})
            )
            app._refresh()
            await pilot.pause()
            label = app.query_one("#pause-label", Static)
            assert label.has_class("pause-active")

    async def test_status_shows_indefinite_pause(self):
        # A pause with no fixed expiry (until=None) reads "paused until restart".
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": None})
            )
            app._refresh()
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "paused until restart" in str(status.content)

    async def test_pump_flashes_failed_pause_ack(self):
        # A pause_ack with ok=False flashes so the decider knows it didn't take.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([json.dumps({"type": "pause_ack", "ok": False})])
            await app._pump(ws)
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "pause failed" in str(status.content)

    async def test_pause_send_failure_flashes(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([], send_fail=True)
            app._ws = ws
            await pilot.pause()
            await app._send_pause(ws, "15m")
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "pause send failed" in str(status.content)

    async def test_unpause_send_failure_flashes(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([], send_fail=True)
            app._ws = ws
            await pilot.pause()
            await app._send_unpause(ws)
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "unpause send failed" in str(status.content)

    async def test_only_selected_duration_has_background_at_first_render(self):
        # Regression (#2360): the first duration button ("once") grabbed
        # initial focus on mount and, with no explicit transparent :focus,
        # rendered the white focus background -- so it read as "selected"
        # alongside the real ``dur-sel`` default (``tilrestart``). Only the
        # selected button may carry a background; every other (focused or
        # not) must be transparent at first render. Asserted opacity-only so
        # it holds across light/dark themes (the exact accent hue is the
        # theme's business; the bug was a *second* visible background).
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            opaque = [
                d
                for d in tui_consent.SELECTABLE_DURATIONS
                if app.query_one(f"#dur-{d}", Button).styles.background.a != 0
            ]
            assert opaque == [tui_consent.DURATION_DEFAULT], (
                f"expected only {tui_consent.DURATION_DEFAULT!r} to carry a "
                f"background at first render, got {opaque!r}"
            )

    async def test_selected_duration_keeps_accent_when_focused(self):
        # The ``.dur-sel`` rules must be specific enough to outrank the
        # transparent ``#duration-selector Button:focus`` (#2360): selecting a
        # duration both adds ``dur-sel`` and focuses the button, and the
        # accent must survive focus -- else the just-selected button goes
        # transparent and looks unselected. The previously-selected default
        # drops to transparent.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            btn = app.query_one(f"#dur-{tui_consent.DURATION_5M}", Button)
            app.on_button_pressed(types.SimpleNamespace(button=btn))
            btn.focus()
            await pilot.pause()
            assert app.focused is btn  # focus really landed on it
            assert btn.has_class("dur-sel")
            assert btn.styles.background.a != 0, (
                f"selected+focused button lost its background "
                f"(got {btn.styles.background!r})"
            )
            default_btn = app.query_one(
                f"#dur-{tui_consent.DURATION_DEFAULT}", Button
            )
            assert not default_btn.has_class("dur-sel")
            assert default_btn.styles.background.a == 0


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


async def test_request_row_renders_host_horizontal_and_buttons_onscreen():
    """Regression guard: the per-row host must render on ONE line (a width fight
    once wrapped it one-character-per-line) and the Allow/Deny buttons must land
    on-screen (the same fight once pushed them past the right edge)."""
    app = _make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        app.controller.apply_frame(_req_frame("r1", host="ford.com"))
        app._refresh()
        await pilot.pause()
        host = app.query_one(".req-host", Static)
        allow = app.query_one("#allow-r1", Button)
        deny = app.query_one("#deny-r1", Button)
        # Host text on one line, not wrapped one-character-per-line.
        assert host.region.height == 1, (
            f"host wrapped vertically: {host.region}"
        )
        assert host.region.width >= len("ford.com"), (
            f"host too narrow: {host.region}"
        )
        # Both buttons fully on-screen (not pushed off the right edge).
        screen_w = app.size.width
        assert allow.region.right <= screen_w, (
            f"allow off-screen: {allow.region}"
        )
        assert deny.region.right <= screen_w, f"deny off-screen: {deny.region}"
        # Buttons flat (height 1) so a row is compact, not a 3-row bordered button.
        assert allow.region.height == 1, f"allow not flat: {allow.region}"
        assert deny.region.height == 1, f"deny not flat: {deny.region}"


async def test_refresh_keeps_surviving_row_object_across_ticks():
    """Anti-flicker: a periodic refresh updates survivors in place, never
    clear+rebuild (which flashed the whole list every second)."""
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(_req_frame("r1", host="a.com"))
        app._refresh()
        await pilot.pause()
        before = list(app.query_one("#requests", ListView).children)
        assert len(before) == 1
        app._refresh()  # second tick, nothing changed
        await pilot.pause()
        after = list(app.query_one("#requests", ListView).children)
        assert before[0] is after[0], "refresh rebuilt the row (flicker)"


async def test_refresh_appends_new_and_drops_resolved_in_place():
    """New rows append; resolved rows drop; untouched rows keep their identity."""
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(_req_frame("r1", host="a.com"))
        app._refresh()
        await pilot.pause()
        lv = app.query_one("#requests", ListView)
        item_r1 = next(
            c for c in lv.children if getattr(c, "request_id", None) == "r1"
        )
        # r2 arrives -> both present, r1 unchanged.
        app.controller.apply_frame(_req_frame("r2", host="b.com"))
        app._refresh()
        await pilot.pause()
        assert {getattr(c, "request_id", None) for c in lv.children} == {
            "r1",
            "r2",
        }
        assert (
            next(
                c
                for c in lv.children
                if getattr(c, "request_id", None) == "r1"
            )
            is item_r1
        )
        # r1 resolved -> removed; r2 survives.
        app.controller.apply_frame(
            json.dumps(
                {
                    "type": "egress_resolved",
                    "request_id": "r1",
                    "decision": "allowed",
                }
            )
        )
        app._refresh()
        await pilot.pause()
        assert {getattr(c, "request_id", None) for c in lv.children} == {"r2"}


def test_reset_clears_pending():
    """reset() drops all pending rows (called at the top of each connection)."""
    c = ConsentDeciderController()
    c.apply_frame(_req_frame("r1"))
    c.apply_frame(_req_frame("r2"))
    assert len(c.pending) == 2
    c.reset()
    assert c.pending == {}
    assert c.ordered() == []


async def test_pump_drops_stale_rows_on_empty_snapshot_reconnect():
    """On (re)connect the server snapshot is authoritative. Rows that resolved
    while disconnected (no egress_resolved ever received) must not linger as
    (0s) ghosts -- the orphan-reap / klangkd-restart case sends an EMPTY
    snapshot, so _pump must clear the queue even when no frames arrive."""
    app = _make_app()
    async with app.run_test() as pilot:
        # stale row from a prior session (resolved server-side while offline)
        app.controller.apply_frame(_req_frame("r1", host="a.com"))
        await pilot.pause()
        assert len(app.controller.pending) == 1
        # reconnect: server reaped orphans / nothing held -> empty snapshot
        await app._pump(FakeWS([]))
        await pilot.pause()
        assert app.controller.pending == {}, app.controller.pending


async def test_pump_repoulates_from_snapshot_after_reset():
    """reset() must not lose live rows: the snapshot that follows a reconnect
    repopulates the queue."""
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(_req_frame("stale", host="old.com"))
        await pilot.pause()
        # reconnect: snapshot carries a different, currently-held request
        await app._pump(FakeWS([_req_frame("live", host="new.com")]))
        await pilot.pause()
        assert set(app.controller.pending) == {"live"}


# ---------------------------------------------------------------------------
# Controller: egress_rules parsing + ordering + expiry (#2335 slice B)
# ---------------------------------------------------------------------------


class TestControllerRulesParsing:
    def test_rules_frame_stored_and_returned(self):
        c = ConsentDeciderController()
        action, payload = c.apply_frame(
            _rules_frame(allow_list=["a.com", "b.com"])
        )
        assert action == RULES
        assert isinstance(payload, EgressRules)
        assert c.rules is payload
        assert payload.workspace_id == "wsid"
        assert payload.allow_list == ("a.com", "b.com")
        assert payload.allowed == ()
        assert payload.denied == ()
        assert payload.paused is None  # #2332 not landed

    def test_rules_parse_rows_into_consentrule(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                allowed=[_rule("a1", host="x.com", port=443, duration="1h")],
                denied=[
                    _rule("d1", host="y.com", port=None, decision="denied")
                ],
            )
        )
        assert len(payload.allowed) == 1
        rule = payload.allowed[0]
        assert isinstance(rule, ConsentRule)
        assert rule.dest_host == "x.com"
        assert rule.dest_port == 443
        assert rule.duration == "1h"
        assert rule.decision == "allowed"
        assert len(payload.denied) == 1
        assert payload.denied[0].dest_port is None

    def test_rules_missing_workspace_id_ignored(self):
        c = ConsentDeciderController()
        bad = json.dumps(
            {"type": "egress_rules", "allow_list": ["a.com"]}
        )  # no workspace_id
        assert c.apply_frame(bad) == (IGNORED, None)
        assert c.rules is None

    def test_rules_missing_fields_degrade_to_empty(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            json.dumps({"type": "egress_rules", "workspace_id": "wsid"})
        )
        assert payload.allow_list == ()
        assert payload.allowed == ()
        assert payload.denied == ()
        assert payload.paused is None

    def test_rules_allow_list_non_list_degrades(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            json.dumps(
                {
                    "type": "egress_rules",
                    "workspace_id": "wsid",
                    "allow_list": "not-a-list",
                }
            )
        )
        assert payload.allow_list == ()

    def test_rules_malformed_row_skipped(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                allowed=[
                    _rule("good"),
                    "not-a-dict",  # skipped
                    {"id": "no-host"},  # kept (host defaults to "")
                ]
            )
        )
        assert [r.id for r in payload.allowed] == ["good", "no-host"]

    def test_rules_port_bool_excluded(self):
        # bool is an int subclass; a bool port must not coerce to 1.
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(allowed=[_rule("a1", port=True)])
        )
        assert payload.allowed[0].dest_port is None

    def test_rules_paased_none_yields_none_pause(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(_rules_frame(paused=None))
        assert payload.paused is None

    def test_rules_paased_true_with_until_parsed(self):
        # Forward-looking: #2332 will send {"paused": true, "until": epoch}.
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(paused={"paused": True, "until": 200.0})
        )
        assert payload.paused == PauseState(until=200.0)

    def test_rules_paased_false_yields_none(self):
        # Not paused -> no pause section renders.
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(paused={"paused": False, "until": 200.0})
        )
        assert payload.paused is None

    def test_rules_paased_until_non_numeric_is_none(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(paused={"paused": True, "until": "soon"})
        )
        assert payload.paused == PauseState(until=None)

    def test_rules_non_dict_paused_yields_none(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(_rules_frame(paused="yes"))
        assert payload.paused is None


class TestControllerRulesOrdering:
    def test_allowed_newest_decided_first(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                allowed=[
                    _rule("old", decided_at=100.0),
                    _rule("new", decided_at=300.0),
                    _rule("mid", decided_at=200.0),
                ]
            )
        )
        assert [r.id for r in payload.allowed] == ["new", "mid", "old"]

    def test_denied_newest_decided_first(self):
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                denied=[
                    _rule("d_old", decided_at=100.0, decision="denied"),
                    _rule("d_new", decided_at=300.0, decision="denied"),
                ]
            )
        )
        assert [r.id for r in payload.denied] == ["d_new", "d_old"]

    def test_undecided_rows_sort_last(self):
        # A NULL decided_at (future migration) sorts after decided rows.
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                allowed=[
                    _rule("undecided", decided_at=None),
                    _rule("decided", decided_at=100.0),
                ]
            )
        )
        assert [r.id for r in payload.allowed] == ["decided", "undecided"]

    def test_ordering_stable_for_ties(self):
        # Equal decided_at preserves insertion order (stable sort).
        c = ConsentDeciderController()
        _, payload = c.apply_frame(
            _rules_frame(
                allowed=[
                    _rule("first", decided_at=100.0),
                    _rule("second", decided_at=100.0),
                ]
            )
        )
        assert [r.id for r in payload.allowed] == ["first", "second"]


class TestControllerRulesExpiry:
    def test_rule_remaining_timed(self):
        c = ConsentDeciderController(clock=lambda: 130.0)
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="5m",
            decided_at=100.0,
            decided_by=None,
        )  # 300s window -> 100+300=400; at 130 -> 270s left
        assert c.rule_remaining(rule) == 270.0

    def test_rule_remaining_clamps_at_zero(self):
        c = ConsentDeciderController(clock=lambda: 9999.0)
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="5m",
            decided_at=100.0,
            decided_by=None,
        )
        assert c.rule_remaining(rule) == 0.0

    def test_rule_remaining_tilrestart_is_none(self):
        c = ConsentDeciderController()
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="tilrestart",
            decided_at=100.0,
            decided_by=None,
        )
        assert c.rule_remaining(rule) is None

    def test_rule_remaining_forever_is_none(self):
        c = ConsentDeciderController()
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="forever",
            decided_at=100.0,
            decided_by=None,
        )
        assert c.rule_remaining(rule) is None

    def test_rule_remaining_unknown_duration_is_none(self):
        c = ConsentDeciderController()
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="3fortnights",
            decided_at=100.0,
            decided_by=None,
        )
        assert c.rule_remaining(rule) is None

    def test_rule_remaining_null_decided_at_is_none(self):
        c = ConsentDeciderController()
        rule = ConsentRule(
            id="a1",
            dest_host="h",
            dest_port=1,
            process_name=None,
            decision="allowed",
            duration="5m",
            decided_at=None,
            decided_by=None,
        )
        assert c.rule_remaining(rule) is None

    def test_pause_remaining_timed(self):
        c = ConsentDeciderController(clock=lambda: 150.0)
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=200.0),
        )
        assert c.pause_remaining(rules) == 50.0

    def test_pause_remaining_indefinite_is_none(self):
        c = ConsentDeciderController()
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=None),
        )
        assert c.pause_remaining(rules) is None

    def test_pause_remaining_not_paused_is_none(self):
        c = ConsentDeciderController()
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=None,
        )
        assert c.pause_remaining(rules) is None


def test_fmt_duration_tiers():
    assert tui_consent._fmt_duration(5) == "5s"
    assert tui_consent._fmt_duration(45) == "45s"
    assert tui_consent._fmt_duration(90) == "1m"
    assert tui_consent._fmt_duration(300) == "5m"
    assert tui_consent._fmt_duration(3600) == "1h"
    assert tui_consent._fmt_duration(7200) == "2h"
    assert tui_consent._fmt_duration(86400) == "1d"
    assert tui_consent._fmt_duration(604800) == "1w"
    assert tui_consent._fmt_duration(1209600) == "2w"


def test_reset_clears_rules():
    """reset() drops the cached rules snapshot too (called on reconnect)."""
    c = ConsentDeciderController()
    c.apply_frame(_rules_frame(allow_list=["a.com"]))
    assert c.rules is not None
    c.reset()
    assert c.rules is None
    assert c.pending == {}


# ---------------------------------------------------------------------------
# Rules screen: render, refresh, switch, WS worker stays connected (#2335 B)
# ---------------------------------------------------------------------------


class TestControllerRevokeAck:
    """``revoke_ack`` frame handling (#2341 slice D)."""

    @staticmethod
    def _controller_with(allowed=(), denied=()) -> ConsentDeciderController:
        c = ConsentDeciderController()
        c.apply_frame(
            _rules_frame(allow_list=(), allowed=allowed, denied=denied)
        )
        return c

    def test_ok_removes_from_allowed(self):
        c = self._controller_with(
            allowed=[_rule("a1", host="x.com"), _rule("a2", host="y.com")]
        )
        outcome, payload = c.apply_frame(
            json.dumps({"type": "revoke_ack", "request_id": "a1", "ok": True})
        )
        assert outcome == REVOKE_ACK
        assert payload == ("a1", True)
        assert [r.id for r in c.rules.allowed] == ["a2"]

    def test_ok_removes_from_denied(self):
        c = self._controller_with(
            denied=[_rule("d1", decision="denied", host="x.com")]
        )
        c.apply_frame(
            json.dumps({"type": "revoke_ack", "request_id": "d1", "ok": True})
        )
        assert c.rules.denied == ()

    def test_fail_leaves_rules_intact(self):
        c = self._controller_with(allowed=[_rule("a1", host="x.com")])
        outcome, payload = c.apply_frame(
            json.dumps({"type": "revoke_ack", "request_id": "a1", "ok": False})
        )
        assert payload == ("a1", False)
        assert [r.id for r in c.rules.allowed] == ["a1"]

    def test_ok_unknown_id_is_noop(self):
        c = self._controller_with(allowed=[_rule("a1", host="x.com")])
        c.apply_frame(
            json.dumps({"type": "revoke_ack", "request_id": "zzz", "ok": True})
        )
        assert [r.id for r in c.rules.allowed] == ["a1"]

    def test_ok_when_no_rules_is_safe(self):
        # No egress_rules frame yet -> rules is None; a success ack must not
        # crash (nothing to remove).
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "revoke_ack", "request_id": "a1", "ok": True})
        )
        assert outcome == REVOKE_ACK
        assert payload == ("a1", True)
        assert c.rules is None

    def test_missing_request_id_returns_none_rid(self):
        c = self._controller_with(allowed=[_rule("a1")])
        outcome, payload = c.apply_frame(
            json.dumps({"type": "revoke_ack", "ok": True})
        )
        assert outcome == REVOKE_ACK
        assert payload == (None, True)
        assert [r.id for r in c.rules.allowed] == ["a1"]  # not removed


class TestControllerPauseAck:
    """``pause_ack`` frame handling (#2332)."""

    def test_ok_carries_until(self):
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "pause_ack", "ok": True, "until": 200.0})
        )
        assert outcome == PAUSE_ACK
        assert payload == (True, 200.0)

    def test_fail_returns_false_ok(self):
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "pause_ack", "ok": False, "until": None})
        )
        assert outcome == PAUSE_ACK
        assert payload == (False, None)

    def test_missing_until_is_none(self):
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "pause_ack", "ok": True})
        )
        assert outcome == PAUSE_ACK
        assert payload == (True, None)

    def test_non_numeric_until_is_none(self):
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "pause_ack", "ok": True, "until": "soon"})
        )
        assert outcome == PAUSE_ACK
        assert payload == (True, None)

    def test_bool_until_rejected(self):
        # isinstance(True, int) is True -- must not coerce True -> 1.0.
        c = ConsentDeciderController()
        outcome, payload = c.apply_frame(
            json.dumps({"type": "pause_ack", "ok": True, "until": True})
        )
        assert payload == (True, None)


class TestRulesScreen:
    async def test_r_opens_screen_and_q_returns(self):
        app = _make_app()
        async with app.run_test() as pilot:
            assert not isinstance(app.screen, RulesScreen)
            app.action_rules()
            await pilot.pause()
            assert isinstance(app.screen, RulesScreen)
            # `q` on the rules screen pops back (does NOT quit the app).
            app.screen.action_back()
            await pilot.pause()
            assert not isinstance(app.screen, RulesScreen)
            assert app.is_running

    async def test_escape_returns(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            assert isinstance(app.screen, RulesScreen)
            app.screen.action_back()
            await pilot.pause()
            assert not isinstance(app.screen, RulesScreen)

    async def test_r_does_not_stack_a_second_screen(self):
        # Guard: pressing r while already on the rules screen is a no-op.
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            screen = app.screen
            app.action_rules()  # already viewing rules -> ignored
            await pilot.pause()
            assert app.screen is screen
            assert len(app.screen_stack) == 2  # default + rules (no 3rd)

    async def test_screen_renders_all_sections(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(
                    allow_list=["static.example.com"],
                    allowed=[
                        _rule("a1", host="allow.example.com", duration="1h"),
                    ],
                    denied=[
                        _rule(
                            "d1",
                            host="deny.example.com",
                            decision="denied",
                            duration="15m",
                        ),
                    ],
                )
            )
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "Static allow-list" in body
            assert "static.example.com" in body
            assert "Active allows (1)" in body
            assert "allow.example.com:443" in body
            assert "expires in" in body
            assert "Active denies (1)" in body
            assert "deny.example.com:443" in body
            assert "left" in body
            # #2332 not landed -> no pause section.
            assert "Pause" not in body

    async def test_screen_empty_state(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "(none)" in body  # empty allow-list + allows + denies

    async def test_screen_no_rules_yet(self):
        # Before the first egress_rules frame lands, show a placeholder.
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "no rules received yet" in body

    async def test_screen_shows_forever_and_restart_labels(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(
                    allowed=[
                        _rule("a1", duration="forever"),
                        _rule("a2", duration="tilrestart"),
                    ],
                )
            )
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "forever" in body
            assert "until restart" in body

    async def test_screen_shows_pause_section_when_paused(self):
        # Forward-looking (#2332): a real pause dict renders the section.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(
                    paused={"paused": True, "until": time.time() + 120.0},
                )
            )
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "Pause" in body
            assert "Filtering paused" in body

    async def test_screen_shows_indefinite_pause(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": None}),
            )
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "paused until restart" in body

    async def test_screen_status_shows_held_count(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_rules_frame(allow_list=["a.com"]))
            app.action_rules()
            await pilot.pause()
            status = str(app.screen.query_one("#rules-status", Static).content)
            assert "wsname" in status
            assert "1 held" in status
            assert "rules" in status

    async def test_refresh_rules_live_updates_on_frame(self):
        # A new egress_rules frame (e.g. a co-decider allowed a request) shows
        # up on the already-open rules screen without popping/re-pushing.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_rules_frame(allow_list=["a.com"]))
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "Active allows (0)" in body
            # A refreshed frame arrives over the shared WS pump:
            app.controller.apply_frame(
                _rules_frame(
                    allow_list=["a.com"],
                    allowed=[_rule("a1", host="new.example.com")],
                )
            )
            app._refresh()  # the 1s tick also refreshes the active screen
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "Active allows (1)" in body
            assert "new.example.com:443" in body

    async def test_ws_worker_survives_switch_and_delivers_frame(
        self, monkeypatch
    ):
        # Real acceptance criterion: pushing/popping the rules screen must NOT
        # tear down or reconnect the WS worker, AND a frame delivered over the
        # still-open socket while the rules screen is up must reach it. Drives
        # the real _ws_loop (not the autouse no-op stub) with a blocking WS that
        # stays connected so we can feed a frame mid-stream.
        delivered = asyncio.Queue()

        class LiveWS:
            sent = []

            async def recv(self):
                return await delivered.get()

            async def send(self, d):
                self.sent.append(d)

        ws = LiveWS()
        monkeypatch.setattr(
            tui_consent, "ws_connect", lambda *a, **k: FakeCM(ws)
        )
        app = _make_app(reconnect_delays=(0.0,))
        async with app.run_test() as pilot:
            task = asyncio.create_task(_real_ws_loop(app))
            try:
                await pilot.pause()
                await asyncio.sleep(0.05)
                assert app._connected is True  # the real loop connected

                # Switch to the rules screen mid-stream.
                app.action_rules()
                await pilot.pause()
                assert isinstance(app.screen, RulesScreen)
                body = str(
                    app.screen.query_one("#rules-content", Static).content
                )
                assert "no rules received yet" in body  # nothing delivered yet

                # A frame arrives over the SAME connection while viewing rules.
                await delivered.put(
                    _rules_frame(
                        allow_list=["a.com"],
                        allowed=[_rule("a1", host="new.example.com")],
                    )
                )
                await pilot.pause()
                await asyncio.sleep(0.05)
                body = str(
                    app.screen.query_one("#rules-content", Static).content
                )
                assert "a.com" in body
                assert "Active allows (1)" in body
                assert "new.example.com:443" in body

                # Pop back; the worker is still on the same connection.
                app.screen.action_back()
                await pilot.pause()
                assert app._connected is True
            finally:
                app._stop = True
                # Unblock the parked recv() so the loop can exit cleanly.
                await delivered.put(_rules_frame())
                await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_refresh_rules_before_mount_is_noop(self):
        screen = RulesScreen()
        # Not mounted -> refresh_rules must not raise.
        screen.refresh_rules()

    async def test_allow_keypress_on_rules_screen_is_safe_noop(self):
        # `a` from the rules screen must not crash (no focused request there);
        # it resolves no queue row because the rules screen has focus.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.action_rules()
            await pilot.pause()
            app.action_allow()  # must not raise
            await pilot.pause()
            assert ws.sent == []  # nothing decided from the rules screen


async def test_deny_keypress_on_rules_screen_is_safe_noop():
    # `d` from the rules screen must not decide a hidden queue row either.
    app = _make_app()
    async with app.run_test() as pilot:
        ws = FakeWS([])
        app._ws = ws
        app.controller.apply_frame(_req_frame("r1", host="a.com"))
        app._refresh()
        await pilot.pause()
        app.query_one("#requests", ListView).index = 0  # highlight r1
        await pilot.pause()
        app.action_rules()
        await pilot.pause()
        app.action_deny()  # on rules screen -> guarded no-op
        await pilot.pause()
        assert ws.sent == []


async def test_rules_screen_empty_allow_list_shows_none():
    # rules present but allow_list empty -> "(none)" (not "no rules received").
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(
            _rules_frame(allow_list=[], allowed=[_rule("a1")])
        )
        app.action_rules()
        await pilot.pause()
        body = str(app.screen.query_one("#rules-content", Static).content)
        assert "(none)" in body  # empty allow-list section
        assert "Active allows (1)" in body


async def test_refresh_isolates_rules_render_failure(monkeypatch):
    # A render bug in the rules screen must never propagate out of _refresh
    # (which the 1s interval calls) -- mirrors the _pump render isolation.
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(_rules_frame(allow_list=["a.com"]))
        app.action_rules()
        await pilot.pause()

        def boom(self):
            raise RuntimeError("rules render broke")

        monkeypatch.setattr(RulesScreen, "refresh_rules", boom)
        app._refresh()  # must not raise
        await pilot.pause()


async def test_q_and_escape_keypress_pop_back_via_pilot():
    # The actual `q`/Esc keypress on the rules screen must pop back (screen
    # binding) rather than trigger the app-level `q` -> quit. Locks down the
    # binding-preference behavior so the screen can't accidentally quit the app.
    app = _make_app()
    async with app.run_test() as pilot:
        app.action_rules()
        await pilot.pause()
        assert isinstance(app.screen, RulesScreen)
        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, RulesScreen)
        assert app.is_running  # did NOT quit
        # And Escape works the same way.
        app.action_rules()
        await pilot.pause()
        assert isinstance(app.screen, RulesScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RulesScreen)
        assert app.is_running


async def test_deny_with_null_decided_at_renders_without_crash():
    # Regression: the deny branch of _rule_line once formatted rem before
    # checking it for None, so a timed deny with a null decided_at (or an
    # unknown duration) hit _fmt_duration(None) -> TypeError. The parser
    # permits these rows, so rendering must degrade to a blank label rather
    # than crash (which would silently stale the whole rules body).
    app = _make_app()
    async with app.run_test() as pilot:
        app.controller.apply_frame(
            _rules_frame(
                denied=[
                    _rule(
                        "d1",
                        host="deny.example.com",
                        decision="denied",
                        duration="5m",
                        decided_at=None,
                    ),
                    _rule(
                        "d2",
                        host="deny2.example.com",
                        decision="denied",
                        duration="3fortnights",  # unknown -> rem None
                    ),
                ],
            )
        )
        app.action_rules()
        await pilot.pause()
        body = str(app.screen.query_one("#rules-content", Static).content)
        assert "deny.example.com:443" in body
        assert "deny2.example.com:443" in body
        assert "Active denies (2)" in body


class TestRulesRevoke:
    """The revoke action on the rules screen (#2341 slice D)."""

    @staticmethod
    def _list_ids(app) -> list:
        return [
            getattr(c, "rule_id", None)
            for c in app.screen.query_one("#rules-list", ListView).children
        ]

    async def test_x_sends_revoke_for_focused_rule(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1", host="allow.com")])
            )
            app.action_rules()
            await pilot.pause()
            app.screen.query_one("#rules-list", ListView).index = 0
            await pilot.pause()
            app.screen.action_revoke()
            await pilot.pause()
            assert any('"revoke"' in s and '"a1"' in s for s in ws.sent), (
                ws.sent
            )

    async def test_x_key_binding_sends_revoke(self):
        # The `x` binding (not just the method) routes to action_revoke.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1", host="x.com")])
            )
            app.action_rules()
            await pilot.pause()
            app.screen.query_one("#rules-list", ListView).index = 0
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert any('"revoke"' in s and '"a1"' in s for s in ws.sent), (
                ws.sent
            )

    async def test_revoke_with_no_focus_sends_nothing(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_rules_frame(allowed=[_rule("a1")]))
            app.action_rules()
            await pilot.pause()
            app.screen.action_revoke()  # nothing highlighted
            await pilot.pause()
            assert ws.sent == []

    async def test_revoke_while_disconnected_flashes(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_rules_frame(allowed=[_rule("a1")]))
            app.action_rules()
            await pilot.pause()
            app.screen.query_one("#rules-list", ListView).index = 0
            await pilot.pause()
            app.screen.action_revoke()  # app._ws is None
            await pilot.pause()
            status = app.screen.query_one("#rules-status", Static)
            assert "disconnected" in str(status.content)

    async def test_revoke_send_failure_flashes(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([], send_fail=True)
            app._ws = ws
            app.controller.apply_frame(_rules_frame(allowed=[_rule("a1")]))
            app.action_rules()
            await pilot.pause()
            app.screen.query_one("#rules-list", ListView).index = 0
            await pilot.pause()
            app.screen.action_revoke()
            await pilot.pause()
            status = app.screen.query_one("#rules-status", Static)
            assert "revoke send failed" in str(status.content)

    async def test_static_allowlist_not_in_revoke_list(self):
        # Scope guard: the static allow-list is NOT a revoke target -- only
        # consent allows/denies appear in #rules-list.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(
                    allow_list=["static.example.com"],
                    allowed=[_rule("a1", host="allow.com")],
                    denied=[_rule("d1", host="deny.com", decision="denied")],
                )
            )
            app.action_rules()
            await pilot.pause()
            assert self._list_ids(app) == ["a1", "d1"]

    async def test_revoke_ack_success_removes_row_from_list(self):
        # On confirmation the row leaves the list (and the cached rules).
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            ws = FakeWS(
                [
                    _rules_frame(allowed=[_rule("a1", host="x.com")]),
                    json.dumps(
                        {
                            "type": "revoke_ack",
                            "request_id": "a1",
                            "ok": True,
                        }
                    ),
                ]
            )
            await app._pump(ws)
            await pilot.pause()
            assert [r.id for r in app.controller.rules.allowed] == []
            assert self._list_ids(app) == []

    async def test_revoke_ack_failure_flashes_and_keeps_row(self):
        # A failed ack flashes + leaves the row enforced.
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_rules()
            await pilot.pause()
            ws = FakeWS(
                [
                    _rules_frame(allowed=[_rule("a1", host="x.com")]),
                    json.dumps(
                        {
                            "type": "revoke_ack",
                            "request_id": "a1",
                            "ok": False,
                        }
                    ),
                ]
            )
            await app._pump(ws)
            await pilot.pause()
            status = app.screen.query_one("#rules-status", Static)
            assert "revoke failed" in str(status.content)
            assert [r.id for r in app.controller.rules.allowed] == ["a1"]
            assert self._list_ids(app) == ["a1"]

    async def test_refresh_rebuilds_list_only_on_change(self):
        # A second refresh with unchanged rules hits the no-op early return
        # (no flicker) and leaves the list intact.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_rules_frame(allowed=[_rule("a1")]))
            app.action_rules()
            await pilot.pause()
            screen = app.screen
            screen.refresh_rules()  # unchanged -> early return
            await pilot.pause()
            assert self._list_ids(app) == ["a1"]

    async def test_rebuild_restores_focus_to_surviving_rule(self):
        # When the rule set changes but the focused rule survives, focus is
        # restored to it (covers the focus-restore loop in _rebuild_rule_list).
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1"), _rule("a2")])
            )
            app.action_rules()
            await pilot.pause()
            lv = app.screen.query_one("#rules-list", ListView)
            lv.index = 1  # focus a2
            await pilot.pause()
            # a1 revoked elsewhere -> rules refresh drops a1; a2 survives.
            app.controller.apply_frame(_rules_frame(allowed=[_rule("a2")]))
            app.screen.refresh_rules()
            await pilot.pause()
            await pilot.pause()  # let call_after_refresh land the restore
            lv = app.screen.query_one("#rules-list", ListView)
            assert lv.highlighted_child is not None
            assert getattr(lv.highlighted_child, "rule_id", None) == "a2"


# ---------------------------------------------------------------------------
# Persistent popup role: q hides the viewer, Q confirms a real quit (#2383).
# ---------------------------------------------------------------------------


class TestPersistentPopupRole:
    """The decider's behaviour when launched inside the hidden tmux session."""

    def test_build_detach_command(self):
        # The hide action detaches the popup viewer from the hidden session.
        assert build_detach_command("/tmp/k.sock", "klangk-consent-w") == [
            "tmux",
            "-S",
            "/tmp/k.sock",
            "detach-client",
            "-s",
            "klangk-consent-w",
        ]

    def test_persistent_flag_off_by_default(self):
        app = _make_app()
        assert not app._persistent

    def test_persistent_flag_on_with_session(self):
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        assert app._persistent

    def test_apply_bindings_standalone_labels_q_quit(self):
        app = _make_app()
        app._apply_bindings()
        labels = {b[0]: b[2] for b in app.BINDINGS}
        assert labels["q"] == "Quit"
        assert labels["Q"] == "Quit"

    def test_apply_bindings_persistent_labels_q_hide(self):
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        app._apply_bindings()
        labels = {b[0]: b[2] for b in app.BINDINGS}
        assert labels["q"] == "Hide"
        assert labels["Q"] == "Quit"

    def test_q_key_standalone_exits(self):
        # No popup context -> q quits immediately (today's behaviour).
        app = _make_app()
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        app.action_q_key()
        assert exited == [True]

    def test_q_key_persistent_hides_not_quit(self, monkeypatch):
        # Popup context -> q detaches the viewer and does NOT quit/deregister.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        ran: list[list[str]] = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        app.action_q_key()
        assert exited == []
        assert ran == [build_detach_command("/tmp/k.sock", "klangk-consent-w")]

    def test_hide_viewer_noop_without_popup(self, monkeypatch):
        from unittest.mock import MagicMock

        app = _make_app()
        run = MagicMock()
        monkeypatch.setattr(tui_consent.subprocess, "run", run)
        app._hide_viewer()
        assert not run.called

    def test_hide_viewer_swallows_subprocess_error(self, monkeypatch):
        # A stale session / missing tmux must never crash the decider.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )

        def boom(*a, **k):
            raise OSError("no tmux")

        monkeypatch.setattr(tui_consent.subprocess, "run", boom)
        app._hide_viewer()  # must not raise

    def test_on_confirm_quit_true_exits(self):
        app = _make_app()
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        app._on_confirm_quit(True)
        assert exited == [True]

    def test_on_confirm_quit_false_does_not_exit(self):
        app = _make_app()
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        app._on_confirm_quit(False)
        assert exited == []

    async def test_confirm_quit_pushes_confirm_screen(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.action_confirm_quit()
            await pilot.pause()
            assert isinstance(app.screen, QuitConfirmScreen)

    async def test_quit_confirm_yes_dismisses_true(self):
        got: list[bool] = []
        app = _make_app()
        async with app.run_test() as pilot:
            app.push_screen(QuitConfirmScreen(), lambda v: got.append(v))
            await pilot.pause()
            app.screen.action_yes()
            await pilot.pause()
        assert got == [True]

    async def test_quit_confirm_no_dismisses_false(self):
        got: list[bool] = []
        app = _make_app()
        async with app.run_test() as pilot:
            app.push_screen(QuitConfirmScreen(), lambda v: got.append(v))
            await pilot.pause()
            app.screen.action_no()
            await pilot.pause()
        assert got == [False]
