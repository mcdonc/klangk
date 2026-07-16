"""Transport resolver: UDS or TCP from a server spec string.

Every outbound CLI call (HTTP and WebSocket) routes through this module
so transport selection is centralized.  The TCP path delegates to the
bare ``httpx`` / ``websockets`` module functions so existing test mocks
(which patch those functions) keep working unchanged.

Detection rule (from #1399):
- ``http://`` or ``https://`` prefix → TCP.
- Absolute path (starts with ``/``) → UDS.
- Anything else → error (relative paths are rejected).
"""

from __future__ import annotations

import contextlib
import socket as _socket
from dataclasses import dataclass

import httpx
import websockets


@dataclass(frozen=True, slots=True)
class ServerTransport:
    """Resolved transport for a server spec."""

    is_uds: bool
    uds_path: str | None
    base_url: str  # e.g. "http://host:8995" or "http://localhost" (UDS)
    ws_uri: str  # e.g. "ws://host:8995/ws" or "ws://localhost/ws" (UDS)
    server_spec: str  # original spec for back-reference


def resolve_transport(server_spec: str) -> ServerTransport:
    """Classify *server_spec* as TCP (URL) or UDS (socket path).

    Raises ``ValueError`` on a relative / bare non-URL value.
    """
    if server_spec.startswith("http://") or server_spec.startswith("https://"):
        # TCP — derive WS URI from the URL.
        if server_spec.startswith("http://"):
            ws_uri = server_spec.replace("http://", "ws://", 1) + "/ws"
        else:
            ws_uri = server_spec.replace("https://", "wss://", 1) + "/ws"
        return ServerTransport(
            is_uds=False,
            uds_path=None,
            base_url=server_spec,
            ws_uri=ws_uri,
            server_spec=server_spec,
        )

    # Not a URL — must be an absolute socket path.
    if not server_spec.startswith("/"):
        raise ValueError(
            f"socket path must be absolute (got {server_spec!r}). "
            "Use an http(s):// URL for TCP or an absolute path for UDS."
        )
    return ServerTransport(
        is_uds=True,
        uds_path=server_spec,
        base_url="http://localhost",
        ws_uri="ws://localhost/ws",
        server_spec=server_spec,
    )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def http_request(
    server_spec: str,
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request, routing through UDS or TCP as appropriate.

    On the TCP path this calls ``httpx.request`` directly (the module-level
    function that existing tests patch).  On the UDS path it constructs an
    ``httpx.Client`` with a UDS transport.
    """
    transport = resolve_transport(server_spec)
    url = f"{transport.base_url}{path}"
    if not transport.is_uds:
        return httpx.request(method, url, **kwargs)
    t = httpx.HTTPTransport(uds=transport.uds_path)
    with httpx.Client(transport=t, base_url=transport.base_url) as client:
        return client.request(method, path, **kwargs)


def http_stream(
    server_spec: str,
    method: str,
    path: str,
    **kwargs,
):
    """Streaming HTTP request, routing through UDS or TCP.

    Returns a context manager yielding an ``httpx.Response``.
    On the TCP path this calls ``httpx.stream`` directly.
    """
    transport = resolve_transport(server_spec)
    url = f"{transport.base_url}{path}"
    if not transport.is_uds:
        return httpx.stream(method, url, **kwargs)
    t = httpx.HTTPTransport(uds=transport.uds_path)
    client = httpx.Client(transport=t, base_url=transport.base_url)
    return _uds_stream_cm(client, method, path, **kwargs)


@contextlib.contextmanager
def _uds_stream_cm(client, method, path, **kwargs):
    """Context manager wrapping a UDS httpx.Client stream."""
    with client:
        with client.stream(method, path, **kwargs) as resp:
            yield resp


# ---------------------------------------------------------------------------
# WebSocket helper
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def ws_connect(
    server_spec: str,
    *,
    token: str,
    max_size: int | None = None,
    **kwargs,
):
    """Connect a WebSocket, routing through UDS or TCP.

    On the TCP path this calls ``websockets.connect`` directly (the
    module-level function tests patch).  On the UDS path it opens a
    preconnected ``AF_UNIX`` socket and passes it via ``sock=``.

    Yields the open WebSocket connection.
    """
    transport = resolve_transport(server_spec)
    uri = f"{transport.ws_uri}?token={token}"
    ws_kwargs = dict(kwargs)
    if max_size is not None:
        ws_kwargs["max_size"] = max_size

    if not transport.is_uds:
        async with websockets.connect(uri, **ws_kwargs) as ws:
            yield ws
        return

    # UDS: open a preconnected AF_UNIX socket.
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.connect(transport.uds_path)
        async with websockets.connect(uri, sock=sock, **ws_kwargs) as ws:
            yield ws
    finally:
        sock.close()
