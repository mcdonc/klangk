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
import threading
import time
import types

import pytest
import websockets
from textual.widgets import Button, ListView, OptionList, Static

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

    async def test_refresh_shows_refused_state(self):
        # #2490: after repeated 403 refusals the status line says the decider
        # slowed its retry ("refused — retrying every 60s"), not
        # "reconnecting".
        app = _make_app()
        async with app.run_test() as pilot:
            app._refused = True
            app._refresh()
            await pilot.pause()
            status = app.query_one("#status", Static)
            assert "refused — retrying every 60s" in str(status.content)

    def test_user_agent_has_distinctive_prefix(self):
        # The UA names this client so klangkd's refusal log can attribute a
        # 403 to the consent decider (#2490).
        assert tui_consent.USER_AGENT.startswith("klangk-consent-decide/")
        assert tui_consent.user_agent() == tui_consent.USER_AGENT

    def test_user_agent_falls_back_when_not_installed(self, monkeypatch):
        def boom(name):
            raise tui_consent.PackageNotFoundError(name)

        monkeypatch.setattr(tui_consent, "_pkg_version", boom)
        assert tui_consent.user_agent() == "klangk-consent-decide/dev"

    async def test_refresh_shows_active_flash(self):
        # While a flash is active (within TTL), _refresh renders it instead of
        # the normal status (so flashes survive the 1s periodic refresh).
        app = _make_app()
        async with app.run_test() as pilot:
            app.flash("something broke")
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

    async def test_refresh_focuses_newly_arrived_hold(self):
        # A newly-arrived hold grabs focus so the user can act on it at once.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0  # focus r1
            await pilot.pause()
            # a new hold arrives
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            assert app._focused_request_id() == "r2"

    async def test_refresh_focuses_hold_above_after_resolution(self):
        # r1 (older, above) and r2; focus r2; r2 resolved -> focus r1 (above).
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 1  # focus r2
            await pilot.pause()
            app.controller.apply_frame(
                json.dumps(
                    {
                        "type": "egress_resolved",
                        "request_id": "r2",
                        "decision": "denied",
                    }
                )
            )
            app._refresh()
            await pilot.pause()
            assert app._focused_request_id() == "r1"  # the hold above

    async def test_refresh_focuses_new_top_when_resolved_hold_was_topmost(
        self,
    ):
        # Focus the topmost hold; resolve it -> focus the new top (was below).
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0  # focus r1 (top)
            await pilot.pause()
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
            assert app._focused_request_id() == "r2"  # now the top

    def test_select_by_id_none_is_noop(self):
        # Defensive guard: must not raise and must not query unmounted widgets.
        _make_app()._select_by_id(None)

    async def test_select_index_clamps_to_bounds(self):
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            app._select_index(99)  # above the top -> last row
            await pilot.pause()
            assert app.query_one("#requests", ListView).index == 1
            app._select_index(-5)  # below the bottom -> first row
            await pilot.pause()
            assert app.query_one("#requests", ListView).index == 0

    async def test_select_index_empty_list_is_noop(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._select_index(0)  # no rows -> must not raise
            await pilot.pause()
            assert app.query_one("#requests", ListView).index is None

    async def test_refresh_no_focus_when_last_hold_resolved(self):
        # Resolving the only hold empties the list; _select_index must no-op.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            app.query_one("#requests", ListView).index = 0
            await pilot.pause()
            app.controller.apply_frame(
                json.dumps({"type": "egress_resolved", "request_id": "r1"})
            )
            app._refresh()
            await pilot.pause()
            assert (
                app._focused_request_id() is None
            )  # list empty, nothing focused

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

    async def test_allow_defaults_to_tilrestart(self):
        # A bare Allow (button or `a`) sends the default duration; the
        # duration is never armed beforehand (#2511).
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
            assert any(
                '"allowed"' in s and '"r1"' in s and '"tilrestart"' in s
                for s in ws.sent
            ), ws.sent

    async def test_a_key_allows_with_default_duration(self):
        # `a` on the highlighted row sends allowed + tilrestart directly --
        # the common case stays one keypress (#2511).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert any(
                '"allowed"' in s and '"r1"' in s and '"tilrestart"' in s
                for s in ws.sent
            ), ws.sent

    async def test_d_key_denies_with_default_duration(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert any(
                '"denied"' in s and '"r1"' in s and '"tilrestart"' in s
                for s in ws.sent
            ), ws.sent

    async def test_A_opens_picker_enter_sends_picked_duration(self):
        # `A` opens the per-row picker; Enter on a picked duration submits
        # allow with exactly that duration (#2511).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            assert isinstance(app.screen, tui_consent.DurationPickerScreen)
            picker = app.screen
            assert picker.request_id == "r1"
            assert picker.decision == tui_consent.DECISION_ALLOWED
            ol = app.screen.query_one("#picker-durations", OptionList)
            ol.highlighted = tui_consent.SELECTABLE_DURATIONS.index("1d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert any(
                '"allowed"' in s and '"r1"' in s and '"1d"' in s
                for s in ws.sent
            ), ws.sent
            # The modal is gone after submit.
            assert not isinstance(app.screen, tui_consent.DurationPickerScreen)

    async def test_D_opens_picker_enter_sends_picked_duration(self):
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            assert isinstance(app.screen, tui_consent.DurationPickerScreen)
            ol = app.screen.query_one("#picker-durations", OptionList)
            ol.highlighted = tui_consent.SELECTABLE_DURATIONS.index("forever")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert any(
                '"denied"' in s and '"r1"' in s and '"forever"' in s
                for s in ws.sent
            ), ws.sent

    async def test_picker_enter_alone_sends_default_duration(self):
        # The picker highlights the default first, so bare Enter repeats the
        # bare-`a` outcome (#2511).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert any(
                '"allowed"' in s and '"tilrestart"' in s for s in ws.sent
            ), ws.sent

    async def test_picker_escape_sends_nothing(self):
        # Dismissing the picker without a pick sends no verdict (#2511).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert ws.sent == []
            assert not isinstance(app.screen, tui_consent.DurationPickerScreen)

    async def test_picker_offers_selectable_durations_only(self):
        # Every human-facing duration is offered, in token order; the
        # test-only 5s is not (#2487).
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            ol = app.screen.query_one("#picker-durations", OptionList)
            assert ol.option_count == len(tui_consent.SELECTABLE_DURATIONS)
            prompts = [
                str(ol.get_option_at_index(i).prompt)
                for i in range(ol.option_count)
            ]
            assert prompts == list(tui_consent.SELECTABLE_DURATIONS)
            assert "5s" not in prompts

    async def test_A_with_no_focused_row_is_noop(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("A")
            await pilot.press("D")
            await pilot.pause()
            assert not isinstance(app.screen, tui_consent.DurationPickerScreen)

    async def test_A_on_rules_screen_is_noop(self):
        # No row is visible on the rules screen, so A/D must not open a
        # picker (nor a/d decide) there.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, tui_consent.RulesScreen)
            await pilot.press("A")
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, tui_consent.RulesScreen)

    async def test_keys_inert_while_picker_open(self):
        # While the picker modal is up, a/d/A/D must not decide the row
        # behind it -- only Enter submits, Esc cancels (#2511).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app._refresh()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("a")
            await pilot.press("d")
            await pilot.press("A")
            await pilot.press("D")
            await pilot.pause()
            assert ws.sent == []
            assert isinstance(app.screen, tui_consent.DurationPickerScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert ws.sent == []

    async def test_picker_decides_second_row(self):
        # The picker targets the *highlighted* row, not always the first.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(_req_frame("r1", host="a.com"))
            app.controller.apply_frame(_req_frame("r2", host="b.com"))
            app._refresh()
            await pilot.pause()
            lv = app.query_one("#requests", ListView)
            lv.index = 1
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            picker = app.screen
            assert isinstance(picker, tui_consent.DurationPickerScreen)
            assert picker.request_id == "r2"
            assert "b.com" in str(
                app.screen.query_one("#picker-title", Static).content
            )
            ol = app.screen.query_one("#picker-durations", OptionList)
            ol.highlighted = tui_consent.SELECTABLE_DURATIONS.index("1w")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert any(
                '"allowed"' in s and '"r2"' in s and '"1w"' in s
                for s in ws.sent
            ), ws.sent

    async def test_pause_bar_mounts(self):
        # #2332: the pause control bar mounts Unpaused + the three windows +
        # a countdown.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            for bid in ("#pause-none", "#pause-15m", "#pause-1h", "#pause-1d"):
                assert app.query_one(bid, Button) is not None, bid
            assert app.query_one("#pause-countdown", Static) is not None

    async def test_pause_buttons_render_on_screen(self):
        # All four controls sit on-screen, in order.
        app = _make_app()
        async with app.run_test(size=(120, 24)) as pilot:
            app._refresh()
            await pilot.pause()
            await pilot.pause()
            prev_x = 0
            for bid in ("#pause-none", "#pause-15m", "#pause-1h", "#pause-1d"):
                b = app.query_one(bid, Button)
                assert b.region.x >= prev_x, bid
                assert b.region.x + b.region.width <= 120, bid
                assert b.region.width > 0, bid
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

    async def test_unpaused_button_sends_unpause_frame(self):
        # The Unpaused button clears an active pause.
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            await pilot.pause()
            app.on_button_pressed(
                types.SimpleNamespace(
                    button=app.query_one("#pause-none", Button)
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

    async def test_pause_highlights_matching_button(self):
        # The button matching the user's last pause request is highlighted.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._pause_duration = "15m"
            app._refresh()
            await pilot.pause()
            assert app.query_one("#pause-15m", Button).has_class(
                "pause-active"
            )
            assert not app.query_one("#pause-none", Button).has_class(
                "pause-active"
            )

    async def test_unpaused_highlighted_by_default(self):
        # With no pause requested, the Unpaused button is the active one.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh()
            await pilot.pause()
            assert app.query_one("#pause-none", Button).has_class(
                "pause-active"
            )

    async def test_countdown_shows_indefinite_pause(self):
        # An indefinite pause (until=None) reads "paused until restart" next to
        # the pause buttons (#2383).
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": None})
            )
            app._refresh()
            await pilot.pause()
            countdown = app.query_one("#pause-countdown", Static)
            assert "paused until restart" in str(countdown.content)

    async def test_countdown_shows_live_pause(self):
        # A live finite window counts down next to the buttons.
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._pause_duration = "15m"
            app.controller._clock = lambda: 100.0  # deterministic countdown
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": 200.0})
            )
            app._refresh()
            await pilot.pause()
            countdown = app.query_one("#pause-countdown", Static)
            assert "paused 1m" in str(countdown.content)
            assert app.query_one("#pause-15m", Button).has_class(
                "pause-active"
            )

    async def test_expired_pause_clears_countdown_and_highlight(self):
        # #2498: a finite window that elapsed locally renders no pause state
        # -- no stale "paused 0s", and the Unpaused button re-lights -- without
        # waiting for a server frame (the 1s refresh drives it).
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._pause_duration = "15m"
            app.controller._clock = lambda: 300.0  # past until=200
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": 200.0})
            )
            app._refresh()
            await pilot.pause()
            countdown = app.query_one("#pause-countdown", Static)
            assert str(countdown.content) == ""
            assert app.query_one("#pause-none", Button).has_class(
                "pause-active"
            )
            assert not app.query_one("#pause-15m", Button).has_class(
                "pause-active"
            )
            # And the stale window doesn't re-light when a post-expiry frame
            # (paused=None) eventually lands.
            app.controller.apply_frame(_rules_frame())
            app._refresh()
            await pilot.pause()
            assert app.query_one("#pause-none", Button).has_class(
                "pause-active"
            )

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

    async def test_refused_403_slows_retry_and_heals(
        self, monkeypatch, caplog
    ):
        # #2490: a pre-accept refusal is HTTP 403 (uvicorn answers every
        # pre-accept close with 403 -- even an expired token, whose 4002
        # close code never reaches us). First refusal refreshes the JWT and
        # retries fast; once refusals pile up the loop drops to a fixed slow
        # interval instead of stopping -- and a later successful connect
        # clears the refused flag (self-heal: a mid-session flip back to
        # interactive recovers without restarting the shell). The refused
        # flag is transient (healed on connect #3), so the slow-retry branch
        # is asserted via its one-time warning log.
        import logging

        calls = {"n": 0}

        def connect(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise websockets.InvalidStatus(
                    types.SimpleNamespace(status_code=403)
                )
            return FakeCM(FakeWS([]))  # 3rd+ connect succeeds, then drops

        monkeypatch.setattr(tui_consent, "ws_connect", connect)
        monkeypatch.setattr(tui_consent, "REFUSED_RETRY_INTERVAL", 0.0)
        app = _make_app(reconnect_delays=(0.0,))

        async def fake_refresh():
            return "refreshed-token"

        monkeypatch.setattr(app, "refresh_token", fake_refresh)
        with caplog.at_level(logging.WARNING, logger="klangk.cli.tui.consent"):
            task = asyncio.create_task(_real_ws_loop(app))
            for _ in range(100):  # wait for the healing connect
                if calls["n"] >= 3:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            app._stop = True
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert "registration refused (403) repeatedly" in caplog.text
        assert app.token == "refreshed-token"
        assert app._refused is False  # healed by the successful connect
        assert calls["n"] >= 3  # kept retrying (no dead stop)

    async def test_refused_403_keeps_slow_retrying_forever(self, monkeypatch):
        # While refused the loop never stops on its own: it retries at the
        # (patched-to-zero) slow interval for the app's lifetime -- bounded
        # spam on the server side, automatic recovery on the client side.
        calls = {"n": 0}

        def refuse_403(*a, **kw):
            calls["n"] += 1
            raise websockets.InvalidStatus(
                types.SimpleNamespace(status_code=403)
            )

        async def fake_refresh():
            return None  # no new token available

        monkeypatch.setattr(tui_consent, "ws_connect", refuse_403)
        monkeypatch.setattr(tui_consent, "REFUSED_RETRY_INTERVAL", 0.0)
        app = _make_app(reconnect_delays=(0.0,))
        monkeypatch.setattr(app, "refresh_token", fake_refresh)
        task = asyncio.create_task(_real_ws_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert app._refused is True
        assert calls["n"] > 5  # still attempting, well past the 2nd refusal

    async def test_handshake_503_keeps_retrying(self, monkeypatch):
        # A non-403 handshake failure (gateway 503) is transient: the loop
        # keeps the normal reconnect backoff and never sets _refused.
        calls = {"n": 0}

        def refuse_503(*a, **kw):
            calls["n"] += 1
            raise websockets.InvalidStatus(
                types.SimpleNamespace(status_code=503)
            )

        monkeypatch.setattr(tui_consent, "ws_connect", refuse_503)
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
        assert calls["n"] >= 2  # still retrying
        assert app._refused is False

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

        monkeypatch.setattr(app, "refresh_token", fake_refresh)
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
        assert await app.refresh_token() == "fresh"

    async def test_refresh_token_failure_is_swallowed(self, monkeypatch):
        app = _make_app()

        def boom(url, tok):
            raise RuntimeError("nope")

        monkeypatch.setattr(tui_consent, "refresh_token", boom)
        assert await app.refresh_token() is None

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

    def test_pause_expired_timed_window_elapsed(self):
        # #2498: a finite until in the past is expired -- the views must
        # prune it locally (the server never re-broadcasts on natural expiry).
        c = ConsentDeciderController(clock=lambda: 150.0)
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=100.0),
        )
        assert c.pause_expired(rules) is True

    def test_pause_expired_at_boundary(self):
        # until == clock counts as elapsed (nothing left to count down).
        c = ConsentDeciderController(clock=lambda: 150.0)
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=150.0),
        )
        assert c.pause_expired(rules) is True

    def test_pause_expired_live_window_is_false(self):
        c = ConsentDeciderController(clock=lambda: 150.0)
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=200.0),
        )
        assert c.pause_expired(rules) is False

    def test_pause_expired_indefinite_is_false(self):
        # "until restart" has no window to elapse -- it must keep rendering.
        c = ConsentDeciderController()
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=PauseState(until=None),
        )
        assert c.pause_expired(rules) is False

    def test_pause_expired_not_paused_is_false(self):
        c = ConsentDeciderController()
        rules = EgressRules(
            workspace_id="wsid",
            allow_list=(),
            allowed=(),
            denied=(),
            paused=None,
        )
        assert c.pause_expired(rules) is False


def test_fmt_duration_tiers():
    assert tui_consent.fmt_duration(5) == "5s"
    assert tui_consent.fmt_duration(45) == "45s"
    assert tui_consent.fmt_duration(90) == "1m"
    assert tui_consent.fmt_duration(300) == "5m"
    assert tui_consent.fmt_duration(3600) == "1h"
    assert tui_consent.fmt_duration(7200) == "2h"
    assert tui_consent.fmt_duration(86400) == "1d"
    assert tui_consent.fmt_duration(604800) == "1w"
    assert tui_consent.fmt_duration(1209600) == "2w"


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

    async def test_screen_hides_expired_pause(self):
        # #2498: a finite window that elapsed locally renders no Pause section
        # (no stale "resumes in 0s") -- cleared by the 1s refresh, not a frame.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller._clock = lambda: 300.0  # past until=200
            app.controller.apply_frame(
                _rules_frame(paused={"paused": True, "until": 200.0})
            )
            app.action_rules()
            await pilot.pause()
            body = str(app.screen.query_one("#rules-content", Static).content)
            assert "Filtering paused" not in body
            assert "[bold]Pause[/bold]" not in body

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
    # unknown duration) hit fmt_duration(None) -> TypeError. The parser
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

    async def test_burst_rebuilds_keep_focus_on_focused_rule(self):
        # #2362: two rule-set-changing frames delivered back-to-back -- no
        # refresh cycle between them, so the first rebuild's clear() has
        # reset the highlight before the second captures it -- must not
        # drop the restore. The capture falls back to the id remembered
        # from the last Highlighted event, so `x` afterwards revokes the
        # row the decider actually focused, not index 0 (the newest rule).
        app = _make_app()
        async with app.run_test() as pilot:
            ws = FakeWS([])
            app._ws = ws
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1"), _rule("a2"), _rule("a3")])
            )
            app.action_rules()
            await pilot.pause()
            lv = app.screen.query_one("#rules-list", ListView)
            lv.index = 2  # focus a3; index 0 is the newest rule
            await pilot.pause()
            # Burst: a1 revoked elsewhere while n1 is allowed elsewhere.
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("n1"), _rule("a2"), _rule("a3")])
            )
            app.screen.refresh_rules()  # rebuild 1 clears the highlight
            app.controller.apply_frame(
                _rules_frame(
                    allowed=[
                        _rule("n1"),
                        _rule("n2"),
                        _rule("a2"),
                        _rule("a3"),
                    ]
                )
            )
            app.screen.refresh_rules()  # rebuild 2, same refresh cycle
            assert lv.highlighted_child is None  # the burst reset it
            await pilot.pause()
            await pilot.pause()  # deferred restores land
            lv = app.screen.query_one("#rules-list", ListView)
            assert lv.highlighted_child is not None
            assert getattr(lv.highlighted_child, "rule_id", None) == "a3"
            # `x` revokes the focused row (a3), not the newest (n2).
            app.screen.action_revoke()
            await pilot.pause()
            assert any('"revoke"' in s and '"a3"' in s for s in ws.sent), (
                ws.sent
            )
            assert not any('"n2"' in s for s in ws.sent), ws.sent

    async def test_focus_clamps_to_neighbor_when_focused_rule_removed(self):
        # #2362: when the focused rule itself leaves the snapshot (revoke
        # ack elsewhere / expiry), the highlight falls to a deterministic
        # neighbor -- the old focus position clamped -- never silently to
        # index 0 of a reordered list.
        app = _make_app()
        async with app.run_test() as pilot:
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1"), _rule("a2"), _rule("a3")])
            )
            app.action_rules()
            await pilot.pause()
            lv = app.screen.query_one("#rules-list", ListView)
            lv.index = 2  # focus the last row, a3
            await pilot.pause()
            app.controller.apply_frame(
                _rules_frame(allowed=[_rule("a1"), _rule("a2")])
            )
            app.screen.refresh_rules()
            await pilot.pause()
            await pilot.pause()  # restore lands; a3 is gone -> clamp
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
        shown = {b.key: b.description for b in app.BINDINGS if b.show}
        assert shown == {
            "a": "Allow",
            "A": "Allow…",
            "d": "Deny",
            "D": "Deny…",
            "r": "Rules",
            "q": "Quit",
        }
        # Q, Esc, Ctrl-A are active but hidden from the Footer
        assert {b.key for b in app.BINDINGS if not b.show} == {
            "Q",
            "escape",
            "ctrl+a",
        }

    def test_apply_bindings_persistent_labels_ctrl_a_toggle(self):
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        app._apply_bindings()
        shown = {b.key: b.description for b in app.BINDINGS if b.show}
        # The Footer advertises the shell wrapper's C-a p toggle.
        assert shown == {
            "a": "Allow",
            "A": "Allow…",
            "d": "Deny",
            "D": "Deny…",
            "r": "Rules",
            "ctrl+a": "Hide/Show",
        }
        ctrl_a = next(b for b in app.BINDINGS if b.key == "ctrl+a" and b.show)
        assert ctrl_a.key_display == "Ctrl-a p"
        # q, Q, Esc are active but hidden from the Footer
        assert {b.key for b in app.BINDINGS if not b.show} == {
            "q",
            "Q",
            "escape",
        }

    def test_q_key_standalone_exits(self):
        # No popup context -> q quits immediately (today's behaviour).
        app = _make_app()
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        app.action_q_key()
        assert exited == [True]

    async def test_q_key_persistent_hides_not_quit(self, monkeypatch):
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
        async with app.run_test():
            app.action_q_key()  # detaches off-loop; wait for it to land
            for _ in range(200):
                if ran:
                    break
                await asyncio.sleep(0.01)
            assert exited == []
            assert ran == [
                build_detach_command("/tmp/k.sock", "klangk-consent-w")
            ]

    async def test_escape_hides_in_persistent_mode(self, monkeypatch):
        # Escape detaches the viewer (hides) and does NOT quit/deregister.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        ran: list[list[str]] = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            for _ in range(200):
                if ran:
                    break
                await asyncio.sleep(0.01)
            assert exited == []
            assert ran == [
                build_detach_command("/tmp/k.sock", "klangk-consent-w")
            ]

    async def test_escape_quits_in_standalone_mode(self):
        app = _make_app()
        exited = []
        app.exit = lambda: exited.append(True)  # type: ignore[method-assign]
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert exited == [True]

    def test_hide_viewer_noop_without_popup(self, monkeypatch):
        from unittest.mock import MagicMock

        app = _make_app()
        run = MagicMock()
        monkeypatch.setattr(tui_consent.subprocess, "run", run)
        app._hide_viewer()
        assert not run.called

    def test_hide_schedule_noop_without_popup(self):
        # Standalone: scheduling neither spawns a task nor detaches.
        app = _make_app()
        app._schedule_viewer_hide()
        assert app._hide_task is None

    async def test_hide_schedule_dedupes_in_flight_detach(self, monkeypatch):
        # q-mashing reuses the in-flight detach; once it lands, the next
        # hide schedules a fresh one.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        calls: list[bool] = []
        release = threading.Event()

        def slow_hide():
            calls.append(True)
            release.wait(timeout=5)

        monkeypatch.setattr(app, "_hide_viewer", slow_hide)
        async with app.run_test():
            app._schedule_viewer_hide()
            app._schedule_viewer_hide()
            app._schedule_viewer_hide()
            for _ in range(200):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) == 1  # deduped onto the in-flight detach
            release.set()
            task = app._hide_task
            assert task is not None
            while not task.done():
                await asyncio.sleep(0.01)
            # After the detach finished, the next hide schedules a new one.
            app._schedule_viewer_hide()
            for _ in range(200):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) == 2
            task = app._hide_task
            assert task is not None
            while not task.done():
                await asyncio.sleep(0.01)

    def test_hide_viewer_swallows_subprocess_error(self, monkeypatch):
        # A stale session / missing tmux must never crash the decider.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )

        def boom(*a, **k):
            raise OSError("no tmux")

        monkeypatch.setattr(tui_consent.subprocess, "run", boom)
        app._hide_viewer()  # must not raise

    def test_show_popup_noop_without_popup_context(self, monkeypatch):
        # Standalone: no popup socket/session, nothing to show.
        from unittest.mock import MagicMock

        app = _make_app()
        run = MagicMock()
        monkeypatch.setattr(tui_consent.subprocess, "run", run)
        assert app._show_popup() is False
        assert not run.called

    def test_show_popup_noop_when_already_open(self, monkeypatch):
        # Hidden session already has a viewer (popup open) -> don't re-show;
        # the goal (popup visible) holds, so this counts as shown.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(
            tui_consent, "hidden_has_client", lambda sock, sess: True
        )
        called: list = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda *a, **k: called.append(a)
        )
        assert app._show_popup() is True
        assert called == []

    def test_show_popup_targets_outer_clients(self, monkeypatch):
        # A held request arrived: show the popup on each outer (shell) client.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(
            tui_consent, "hidden_has_client", lambda sock, sess: False
        )
        monkeypatch.setattr(
            tui_consent,
            "outer_clients",
            lambda sock, sess: ["clientA", "clientB"],
        )
        ran: list[list[str]] = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        app._show_popup()
        assert len(ran) == 2
        # each is a display-popup -c <client> on the local socket
        assert ran[0][:5] == [
            "tmux",
            "-S",
            "/tmp/k.sock",
            "display-popup",
            "-c",
        ]
        assert ran[0][5] == "clientA"
        assert ran[1][5] == "clientB"
        # the viewer attaches to the hidden session (last positional)
        assert ran[0][-1].endswith("attach -t klangk-consent-w")
        assert app._show_popup() is True

    def test_show_popup_false_when_no_outer_clients(self, monkeypatch):
        # No outer client could be targeted (e.g. a contended tmux server
        # timed out list-clients): nothing shown -> False, so the worker
        # knows to retry (#2699 review).
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(
            tui_consent, "hidden_has_client", lambda sock, sess: False
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: []
        )
        ran: list = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        assert app._show_popup() is False
        assert ran == []

    def test_show_popup_swallows_subprocess_error(self, monkeypatch):
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(
            tui_consent, "hidden_has_client", lambda sock, sess: False
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: ["clientA"]
        )

        def boom(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(tui_consent.subprocess, "run", boom)
        app._show_popup()  # must not raise


class TestPopupShowOffLoop:
    """#2699: the ADDED-path popup show runs off the UI event loop.

    ``_show_popup`` is synchronous tmux subprocess work — two
    ``list-clients`` queries plus a ``display-popup`` that blocks until
    the popup is dismissed (it always outlives its 3 s timeout, then is
    killed; the popup stays up). Called inline from the async pump it
    froze the event loop, so the Allow/Deny row rendered ~seconds after
    the popup wrapper appeared while the hold countdown burned.
    """

    async def test_slow_tmux_does_not_delay_row_render(self, monkeypatch):
        # A contended/slow tmux server (here: list-clients takes 1.5s) must
        # not delay the row render: the show is scheduled off the loop and
        # the render is not gated on it.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        started = threading.Event()

        def slow_has_client(sock, sess):
            started.set()
            time.sleep(1.5)
            return False

        monkeypatch.setattr(tui_consent, "hidden_has_client", slow_has_client)
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: ["clientA"]
        )
        async with app.run_test():
            t0 = time.monotonic()
            # One ADDED frame, then the socket closes.
            await app._pump(
                FakeWS([_req_frame("r1", host="evil.example.com")])
            )
            elapsed = time.monotonic() - t0
            # The pump returned (and rendered) without waiting for tmux.
            # Inline, this took >= 1.5s; off-loop it is effectively instant.
            assert elapsed < 1.0, (
                f"row render gated on the popup show ({elapsed:.2f}s)"
            )
            # The row IS rendered despite the show still being in flight.
            assert len(app.query_one("#requests", ListView).children) == 1
            # ...and the show did run (on its worker thread).
            assert started.wait(timeout=5)
            # Let the in-flight show finish so run_test teardown is clean.
            task = app._show_popup_task
            assert task is not None
            while not task.done():
                await asyncio.sleep(0.05)

    async def test_snapshot_reconnect_surfaces_row_and_popup(
        self, monkeypatch
    ):
        # Acceptance: a request held while the decider's WS was reconnecting
        # arrives in the connect snapshot -- the row renders and the popup is
        # scheduled for it promptly on reconnect (#2699).
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(
            tui_consent, "hidden_has_client", lambda sock, sess: False
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: ["clientA"]
        )
        ran: list[list[str]] = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        async with app.run_test() as pilot:
            # Reconnect: the server's snapshot replays the currently-held
            # request as an egress_request frame.
            await app._pump(
                FakeWS([_req_frame("r1", host="evil.example.com")])
            )
            await pilot.pause()
            assert set(app.controller.pending) == {"r1"}
            assert len(app.query_one("#requests", ListView).children) == 1
            # ...and the popup show was scheduled off-loop and ran.
            for _ in range(100):
                if ran:
                    break
                await asyncio.sleep(0.02)
            assert ran, "popup show never ran"
            assert ran[0][:5] == [
                "tmux",
                "-S",
                "/tmp/k.sock",
                "display-popup",
                "-c",
            ]

    async def test_schedule_dedupes_in_flight_show(self, monkeypatch):
        # A burst of held requests (snapshot replay) must not pile up shows:
        # while one is in flight, later ADDED frames reuse it.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        calls = []
        release = threading.Event()

        def slow_show():
            calls.append(True)
            release.wait(timeout=5)

        monkeypatch.setattr(app, "_show_popup", slow_show)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._schedule_popup_show()
            app._schedule_popup_show()
            app._schedule_popup_show()
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.02)
            assert len(calls) == 1  # deduped onto the in-flight show
            release.set()
            task = app._show_popup_task
            while not task.done():
                await asyncio.sleep(0.05)
            # After the show finished, the next request schedules a new one.
            app._schedule_popup_show()
            for _ in range(100):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert len(calls) == 2
            task = app._show_popup_task
            while not task.done():
                await asyncio.sleep(0.05)

    def test_schedule_noop_without_popup_context(self):
        # Standalone decider: scheduling neither spawns a task nor shows.
        app = _make_app()
        app._schedule_popup_show()
        assert app._show_popup_task is None

    async def test_popup_show_worker_swallows_errors(self, monkeypatch):
        # The fire-and-forget worker must never surface an exception (an
        # unretrieved task exception is log noise at best).
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )

        def boom():
            raise RuntimeError("show blew up")

        monkeypatch.setattr(app, "_show_popup", boom)
        async with app.run_test():
            await app._popup_show_worker()  # must not raise

    async def test_popup_show_retries_when_nothing_targeted(self, monkeypatch):
        # A failed first attempt (no outer client found — e.g. a contended
        # tmux server timing out list-clients) must not strand requests
        # that arrived during its dedupe window: the worker retries and
        # shows the popup on the next attempt (#2699 review).
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(tui_consent, "POPUP_SHOW_RETRY_DELAY", 0.0)
        attempts = []
        clients = iter([[], ["clientA"]])
        monkeypatch.setattr(
            tui_consent,
            "hidden_has_client",
            lambda sock, sess: attempts.append("has") or False,
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: next(clients)
        )
        ran: list = []
        monkeypatch.setattr(
            tui_consent.subprocess, "run", lambda cmd, **kw: ran.append(cmd)
        )
        async with app.run_test():
            app.controller.apply_frame(_req_frame("r1"))  # still held
            await app._popup_show_worker()
            assert len(attempts) == 2  # failed once, retried, succeeded
            assert len(ran) == 1  # the retry showed the popup
            assert ran[0][5] == "clientA"

    async def test_popup_show_bounded_attempts_then_gives_up(
        self, monkeypatch
    ):
        # Every attempt targets nothing: the worker stops after
        # POPUP_SHOW_ATTEMPTS tries (no infinite retry loop). The slot
        # frees, so the next ADDED frame schedules a fresh worker.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(tui_consent, "POPUP_SHOW_RETRY_DELAY", 0.0)
        attempts = []
        monkeypatch.setattr(
            tui_consent,
            "hidden_has_client",
            lambda sock, sess: attempts.append("has") or False,
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: []
        )
        async with app.run_test():
            app.controller.apply_frame(_req_frame("r1"))  # still held
            await app._popup_show_worker()  # returns (does not spin)
            assert len(attempts) == tui_consent.POPUP_SHOW_ATTEMPTS

    async def test_popup_show_no_retry_when_nothing_pending(self, monkeypatch):
        # Nothing is held anymore (the lone request resolved while the
        # show was in flight): a failed attempt does not retry — there is
        # nothing left to surface.
        app = _make_app(
            popup_socket="/tmp/k.sock", popup_session="klangk-consent-w"
        )
        monkeypatch.setattr(tui_consent, "POPUP_SHOW_RETRY_DELAY", 0.0)
        attempts = []
        monkeypatch.setattr(
            tui_consent,
            "hidden_has_client",
            lambda sock, sess: attempts.append("has") or False,
        )
        monkeypatch.setattr(
            tui_consent, "outer_clients", lambda sock, sess: []
        )
        async with app.run_test():
            assert app.controller.pending == {}
            await app._popup_show_worker()
            assert len(attempts) == 1  # shown False, but nothing pending


class TestBranchGaps2834:
    """#2834 branch gate: decider view/controller guard outcomes."""

    def test_apply_resolved_non_string_id_skips_pop(self):
        from klangk.cli.tui.consent import ConsentDeciderController

        c = ConsentDeciderController()
        c.pending["r1"] = object()
        status, _payload = c._apply_resolved({"request_id": 42})
        assert status == "resolved"
        assert "r1" in c.pending  # non-string id: nothing popped

    async def test_pause_ack_ok_does_not_flash(self):
        from klangk.cli.tui.consent import PAUSE_ACK

        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            flashed = []
            app.flash = lambda msg: flashed.append(msg)
            refreshed = []
            app._refresh = lambda: refreshed.append(1)
            app._react(PAUSE_ACK, (True, 123.0))
            assert flashed == []
            assert refreshed == [1]

    async def test_host_line_without_port(self):
        from klangk.cli.tui.consent import ConsentRequest

        req = ConsentRequest(
            id="r1",
            workspace_id="wsid",
            dest_host="plain.example",
            dest_port=None,
            process_name=None,
            pid=None,
            requested_at=time.time(),
        )
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            line = app._host_line(req)
            assert "plain.example" in line

    async def test_select_by_id_not_in_list_is_noop(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._select_by_id("never-seen")  # no crash, selection untouched

    async def test_button_without_decision_prefix_ignored(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            decided = []
            app._decide_id = lambda *a, **k: decided.append(a)

            class _B:
                id = "mystery-prefix"

            import unittest.mock

            event = unittest.mock.MagicMock()
            event.button = _B()
            app.on_button_pressed(event)
            assert decided == []

    async def test_duration_picker_submit_outside_decider_app(self):
        # The picker's Enter when the app beneath is NOT the decider
        # (nested in the main TUI): nothing decided, just dismissed.
        import unittest.mock as um

        from textual.app import App as TextualApp

        from klangk.cli.tui.consent import (
            DURATION_DEFAULT,
            SELECTABLE_DURATIONS,
            DurationPickerScreen,
        )

        class _OtherApp(TextualApp):
            pass

        decided = []
        app = _OtherApp()
        picker = DurationPickerScreen("r1", "allow", "evil.example")
        with um.patch.object(
            DurationPickerScreen,
            "_decide_id",
            lambda self, *a: decided.append(a),
            create=True,
        ):
            async with app.run_test() as pilot:
                app.push_screen(picker)
                await pilot.pause()
                event = um.MagicMock()
                event.option_index = SELECTABLE_DURATIONS.index(
                    DURATION_DEFAULT
                )
                picker.on_option_list_option_selected(event)
                await pilot.pause()
        assert decided == []

    async def test_rule_highlight_loop_exhaust_is_noop(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            from klangk.cli.tui.consent import RulesScreen

            rules_screen = RulesScreen()
            app.push_screen(rules_screen)
            await pilot.pause()
            event = types.SimpleNamespace(
                item=types.SimpleNamespace(rule_id="ghost"),
                list_view=rules_screen.query_one("#rules-list"),
            )
            rules_screen.on_list_view_highlighted(
                event
            )  # not in children: no-op

    async def test_rebuild_rule_list_none_rules_clears(self):
        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            from klangk.cli.tui.consent import RulesScreen

            rules_screen = RulesScreen()
            app.push_screen(rules_screen)
            await pilot.pause()
            rules_screen._rebuild_rule_list(None)
            await pilot.pause()
            assert list(rules_screen.query_one("#rules-list").children) == []

    def test_rule_item_without_port(self):
        from klangk.cli.tui.consent import ConsentRule

        rule = ConsentRule(
            id="a1",
            dest_host="plain.example",
            dest_port=None,
            process_name=None,
            decision="allowed",
            duration="5m",
            decided_at=2.0,
            decided_by="u@x",
        )
        from klangk.cli.tui.consent import RulesScreen

        from textual.widgets import ListItem

        item = RulesScreen._rule_item(rule, "allowed")
        assert isinstance(item, ListItem)

    def test_rule_line_without_port(self):
        from klangk.cli.tui.consent import (
            ConsentDeciderController,
            ConsentRule,
            RulesScreen,
        )

        rule = ConsentRule(
            id="a1",
            dest_host="plain.example",
            dest_port=None,
            process_name=None,
            decision="allowed",
            duration="5m",
            decided_at=2.0,
            decided_by="u@x",
        )
        controller = ConsentDeciderController()
        line = RulesScreen._rule_line(rule, controller, deny=False)
        assert "plain.example" in line


class TestFinalBranchGaps2834:
    async def test_rebuild_rules_to_empty_skips_focus_clamp(self):
        # A rules frame that fails to parse clears the list without
        # rebuilding rows (rules is None -> the 1731 arm), and the
        # deferred focus-restore then finds no children, skipping the
        # index clamp (the 1749 arm) instead of crashing or jumping.
        # Driven against a stub list view so the clear()'s child removal
        # and the deferred callback are synchronous, not scheduler-bound
        # (textual schedules both, which is flaky under xdist load).
        from klangk.cli.tui.consent import RulesScreen

        class _StubLV:
            def __init__(self):
                self.children = [
                    types.SimpleNamespace(rule_id="a1"),
                    types.SimpleNamespace(rule_id="a2"),
                ]
                self.index = 1
                self.highlighted_child = self.children[1]
                self.captured = []

            def clear(self):
                self.children = []

            def call_after_refresh(self, cb):
                self.captured.append(cb)

        app = _make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = RulesScreen()
            app.push_screen(screen)
            await pilot.pause()
            stub = _StubLV()
            screen.query_one = lambda selector, cls=None: stub
            screen._rebuild_rule_list(None)
            assert stub.children == []  # cleared, no rows rebuilt
            assert stub.captured  # the restore was scheduled
            for cb in stub.captured:
                cb()  # no children: the clamp is skipped, nothing raises
            assert stub.index == 1  # untouched by the skipped clamp
