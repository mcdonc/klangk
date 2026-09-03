"""Tests for :mod:`klangk.sidecar_connections` (#2339)."""

from __future__ import annotations

import asyncio
import types

from klangk.sidecar_connections import SidecarConnections
from klangk.wshandler.safe_websocket import SlowClientError


def _app():
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    return app


class _Sock:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


class _FullSock:
    """Mimics a SafeWebSocket whose send queue is full / sender stopped.

    ``SafeWebSocket.send_json`` only ever raises ``SlowClientError`` (queue
    full / ``_closed``) -- never ``ConnectionError``/``OSError`` -- so this is
    the realistic production failure shape for the send_drop None path.
    """

    def send_json(self, msg: dict) -> None:
        raise SlowClientError("outbound queue full")


class TestSidecarConnections:
    async def test_register_get_deregister(self):
        reg = SidecarConnections(_app())
        sock = _Sock()
        reg.register("ws-1", sock)
        assert reg.get("ws-1") is sock
        reg.deregister("ws-1", sock)
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
        # SafeWebSocket.send_json raises only SlowClientError (queue full /
        # sender stopped) -> treat as "no sidecar" (the only realistic send
        # failure; a dead-but-not-stopped socket accepts the enqueue and the
        # caller's wait_for timeout is the backstop).
        reg = SidecarConnections(_app())
        reg.register("ws-1", _FullSock())
        assert reg.send_drop("ws-1", "h", "allowed") is None
        assert reg._pending == {}  # nothing leaked

    async def test_send_drop_timeout_pops_pending(self):
        # A hung-but-connected sidecar: the caller's wait_for cancels the
        # Future on timeout -> the done-callback must pop the pending entry
        # (no process-lifetime leak, #2339 review #3).
        reg = SidecarConnections(_app())
        reg.register("ws-1", _Sock())
        fut = reg.send_drop("ws-1", "h", "allowed")
        assert fut is not None
        assert len(reg._pending) == 1
        fut.cancel()  # simulate asyncio.wait_for timeout cancel
        await asyncio.sleep(0)  # done-callback fires on the next loop tick
        assert reg._pending == {}

    async def test_resolve_ack_unknown_is_noop(self):
        reg = SidecarConnections(_app())
        reg.resolve_ack("nope", True)  # late/unknown ack -> no error

    async def test_deregister_fails_pending_ack(self):
        reg = SidecarConnections(_app())
        sock = _Sock()
        reg.register("ws-1", sock)
        fut = reg.send_drop("ws-1", "h", "denied")
        reg.deregister("ws-1", sock)  # sidecar gone -> its ack fails
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


class TestSidecarConnectionsBranchGaps2834:
    """#2834 branch gate: done-future skips in deregister/resolve/stop."""

    async def test_deregister_skips_done_future(self):
        # A pending ack whose future already resolved (the sidecar acked
        # just as it disconnected) is left alone, not re-failed.
        reg = SidecarConnections(_app())
        sock = _Sock()
        reg.register("ws-1", sock)
        fut = reg.send_drop("ws-1", "h", "allowed")
        fut.set_result(True)  # raced ack
        reg.deregister("ws-1", sock)  # must not raise / re-resolve
        assert fut.result() is True
        assert reg._pending == {}

    async def test_resolve_ack_unknown_id_is_noop(self):
        reg = SidecarConnections(_app())
        reg.resolve_ack("never-sent", True)  # late ack after cleanup

    async def test_stop_skips_done_futures(self):
        reg = SidecarConnections(_app())
        sock1, sock2 = _Sock(), _Sock()
        reg.register("ws-1", sock1)
        reg.register("ws-2", sock2)
        f1 = reg.send_drop("ws-1", "h", "allowed")
        f2 = reg.send_drop("ws-2", "h", "denied")
        f1.set_result(True)  # already acked
        await reg.stop()
        assert f2.result() is False  # the open one was fail-closed
        assert reg._pending == {} and reg._conns == {}


class TestSidecarConnectionsIdentityGuard3069:
    """#3069: a stale socket's teardown must not drop a replacement's
    registration (sidecar reconnect race)."""

    async def test_stale_socket_deregister_keeps_replacement(self):
        reg = SidecarConnections(_app())
        old, new = _Sock(), _Sock()
        reg.register("ws-1", old)
        reg.register("ws-1", new)  # reconnect repoints the entry
        reg.deregister("ws-1", old)  # stale socket's handler finally runs
        assert reg.get("ws-1") is new  # replacement still registered

    async def test_replacement_deregister_still_works(self):
        reg = SidecarConnections(_app())
        old, new = _Sock(), _Sock()
        reg.register("ws-1", old)
        reg.register("ws-1", new)
        reg.deregister("ws-1", old)
        reg.deregister("ws-1", new)  # the live one may drop it
        assert reg.get("ws-1") is None

    async def test_stale_deregister_leaves_pending_acks(self):
        # A drop sent over the replacement's socket must not be failed by
        # the stale socket's teardown either.
        reg = SidecarConnections(_app())
        old, new = _Sock(), _Sock()
        reg.register("ws-1", old)
        reg.register("ws-1", new)
        fut = reg.send_drop("ws-1", "h", "allowed")
        reg.deregister("ws-1", old)
        assert not fut.done()  # ack still awaited on the live socket
        reg.resolve_ack(next(iter(reg._pending)), True)
        assert fut.result() is True

    async def test_unknown_workspace_deregister_is_noop(self):
        reg = SidecarConnections(_app())
        reg.deregister("ws-x", _Sock())  # nothing registered -> no error
