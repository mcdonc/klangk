"""SafeWebSocket: bounded-queue WebSocket writer, and broadcast helper."""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from ._constants import _SEND_QUEUE_SIZE

logger = logging.getLogger(__name__)


class SlowClientError(Exception):
    """Raised when the outbound queue is full (client can't keep up)."""


# Exceptions that indicate a dead or broken WebSocket connection.
_WS_ERRORS = (
    SlowClientError,
    WebSocketDisconnect,
    RuntimeError,
    ConnectionError,
    OSError,
)


class SafeWebSocket:
    """Bounded-queue WebSocket writer.

    All outbound messages are placed on a bounded asyncio.Queue.
    A dedicated sender task drains the queue and writes to the
    underlying WebSocket, serializing concurrent sends.  If the
    queue is full the client is too slow — we drop it immediately
    rather than blocking the read loop or forwarder tasks.
    """

    def __init__(
        self, websocket: WebSocket, *, maxsize: int = _SEND_QUEUE_SIZE
    ):
        self._sock = websocket
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(
            maxsize=maxsize
        )
        self._sender_task: asyncio.Task | None = None
        self._closed = False

    def start_sender(self) -> None:
        """Launch the background sender coroutine."""
        self._sender_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self) -> None:
        """Drain the outbound queue and write to the WebSocket."""
        try:
            while True:
                msg = await self._queue.get()
                if msg is None:
                    break
                await self._sock.send_json(msg)
        except asyncio.CancelledError:
            raise
        except _WS_ERRORS:
            # Socket gone — nothing to do, cleanup handles the rest.
            pass

    async def stop_sender(self) -> None:
        """Signal the sender task to exit and wait for it."""
        self._closed = True
        task = self._sender_task
        if task is None:
            return
        # Sentinel to break out of the loop.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # Queue is full — cancel the task directly.
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Sender task failed unexpectedly")
        self._sender_task = None

    def send_json(self, data: dict) -> None:
        """Enqueue *data* for sending.  Non-blocking.

        Raises ``SlowClientError`` if the queue is full or the sender
        has been stopped.
        """
        if self._closed:
            raise SlowClientError("sender stopped — cannot enqueue")
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            raise SlowClientError("outbound queue full — closing slow client")

    async def accept(self) -> None:
        await self._sock.accept()

    async def receive_text(self) -> str:
        return await self._sock.receive_text()

    async def close(self, code: int = 1000) -> None:
        await self._sock.close(code=code)

    @property
    def headers(self):
        """Proxy header access to the underlying WebSocket."""
        return self._sock.headers

    @property
    def client(self):
        """Proxy client (peer address) access to the underlying WebSocket.

        Used by derive_hosting_info to scope X-Forwarded-* trust to the
        real connection peer (see util.peer_trusted).
        """
        return self._sock.client

    @property
    def raw(self) -> WebSocket:
        """Access the underlying WebSocket (e.g. for identity checks)."""
        return self._sock


def broadcast_to_set(subscribers: set[SafeWebSocket], message: dict) -> int:
    """Send *message* to each socket in *subscribers*, removing dead ones.

    Returns the number of live subscribers the message was delivered to.
    """
    dead = []
    delivered = 0
    for sub in list(subscribers):
        try:
            sub.send_json(message)
            delivered += 1
        except _WS_ERRORS:
            dead.append(sub)
    for sub in dead:
        subscribers.discard(sub)
    return delivered
