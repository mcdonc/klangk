"""Tests for :mod:`klangk.sidecar_connections` (#2339)."""

from __future__ import annotations

import types

from klangk.sidecar_connections import SidecarConnections


def _app():
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    return app


class _Sock:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


class _DeadSock:
    def send_json(self, msg: dict) -> None:
        raise ConnectionError("socket gone")


class TestSidecarConnections:
    async def test_register_get_deregister(self):
        reg = SidecarConnections(_app())
        sock = _Sock()
        reg.register("ws-1", sock)
        assert reg.get("ws-1") is sock
        reg.deregister("ws-1")
        assert reg.get("ws-1") is None

    async def test_send_drop_no_connection_returns_none(self):
        reg = SidecarConnections(_app())
        assert reg.send_drop("ws-1", "h", "allowed") is None

    async def test_send_drop_sends_frame_and_resolve_ack(self):
        reg = SidecarConnections(_app())
        sock = _Sock()
        reg.register("ws-1", sock)
        fut = reg.send_drop("ws-1", "host.com", "allowed")
        assert fut is not None and not fut.done()
        frame = sock.sent[0]
        assert frame["type"] == "drop_rule"
        assert frame["host"] == "host.com"
        assert frame["decision"] == "allowed"
        reg.resolve_ack(frame["id"], True)
        assert fut.result() is True

    async def test_send_drop_send_failure_returns_none(self):
        # socket died between get() and send() -> treat as "no sidecar"
        reg = SidecarConnections(_app())
        reg.register("ws-1", _DeadSock())
        assert reg.send_drop("ws-1", "h", "allowed") is None

    async def test_resolve_ack_unknown_is_noop(self):
        reg = SidecarConnections(_app())
        reg.resolve_ack("nope", True)  # late/unknown ack -> no error

    async def test_deregister_fails_pending_ack(self):
        reg = SidecarConnections(_app())
        reg.register("ws-1", _Sock())
        fut = reg.send_drop("ws-1", "h", "denied")
        reg.deregister("ws-1")  # sidecar gone -> its ack fails
        assert fut.result() is False

    async def test_stop_clears_and_fails_pending(self):
        reg = SidecarConnections(_app())
        reg.register("ws-1", _Sock())
        fut = reg.send_drop("ws-1", "h", "allowed")
        await reg.stop()
        assert fut.result() is False
        assert reg.get("ws-1") is None

    async def test_start_is_noop(self):
        # lifespan symmetry: start() must not blow up
        SidecarConnections(_app()).start()

    async def test_reconfigure_swaps_app(self):
        app1 = _app()
        reg = SidecarConnections(app1)
        app2 = _app()
        reg.reconfigure(app2)
        assert reg.app is app2
