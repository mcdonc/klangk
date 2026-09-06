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
from urllib.parse import urlencode

import httpx
import websockets


@dataclass(frozen=True, slots=True)
class ServerTransport:
    """Resolved transport for a server spec."""

    is_uds: bool
    uds_path: str | None
    base_url: str  # e.g. "http://host:8995" or "http://localhost" (UDS)
    ws_uri: str  # e.g. "ws://host:8995/ws" or "ws://localhost/ws" (UDS)
    ws_base: str  # scheme://host[:port] — ws_uri minus the path ("/ws")
    server_spec: str  # original spec for back-reference


def is_http_url(server_spec: str) -> bool:
    """True for an ``http://`` / ``https://`` URL (TCP server spec)."""
    return server_spec.startswith("http://") or server_spec.startswith(
        "https://"
    )


def ws_scheme_base(server_spec: str) -> str:
    """Derive the WS scheme://host[:port] base from an http(s) URL."""
    if server_spec.startswith("http://"):
        return server_spec.replace("http://", "ws://", 1)
    return server_spec.replace("https://", "wss://", 1)


def tcp_transport(server_spec: str) -> ServerTransport:
    """TCP transport for an http(s) server spec."""
    ws_base = ws_scheme_base(server_spec)
    return ServerTransport(
        is_uds=False,
        uds_path=None,
        base_url=server_spec,
        ws_uri=ws_base + "/ws",
        ws_base=ws_base,
        server_spec=server_spec,
    )


def resolve_transport(server_spec: str) -> ServerTransport:
    """Classify *server_spec* as TCP (URL) or UDS (socket path).

    Raises ``ValueError`` on a relative / bare non-URL value, or when no
    server is configured (``None`` / empty) — so callers' ``except ValueError``
    handlers catch the no-server case instead of crashing on ``None.startswith``.
    """
    if not isinstance(server_spec, str) or not server_spec:
        raise ValueError(
            "no server configured — run `klangk login` or pass --server."
        )
    if is_http_url(server_spec):
        return tcp_transport(server_spec)

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
        ws_base="ws://localhost",
        server_spec=server_spec,
    )


def is_valid_server_spec(server_spec: str) -> bool:
    """True if *server_spec* is a usable server location.

    Mirrors ``resolve_transport``: an ``http(s)://`` URL (TCP) or an
    absolute path (UDS). Anything else — a bare name or relative path — is
    not usable and would raise from ``resolve_transport`` at request time,
    so callers should reject it up front (e.g. the TUI server picker).
    """
    try:
        resolve_transport(server_spec)
    except ValueError:
        return False
    return True


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
    path: str = "/ws",
    query: dict[str, str] | None = None,
    **kwargs,
):
    """Connect a WebSocket, routing through UDS or TCP.

    On the TCP path this calls ``websockets.connect`` directly (the
    module-level function tests patch).  On the UDS path it opens a
    preconnected ``AF_UNIX`` socket and passes it via ``sock=``.

    ``path`` selects the server WS endpoint (default ``/ws``; the consent
    decider client uses ``/ws/consent-decider``), and ``query`` adds extra
    query params. The auth ``token`` rides the handshake's
    ``Sec-WebSocket-Protocol`` header (``bearer, <jwt>``) rather than the
    URL — query strings land in proxy/server access logs (#3201).
    Yields the open WebSocket connection.
    """
    transport = resolve_transport(server_spec)
    qs = dict(query) if query else {}
    # A caller passing query={"token": ...} must not smuggle it back into
    # the URL (#2320 review #6; the header is the only token carrier).
    qs.pop("token", None)
    uri = f"{transport.ws_base}{path}"
    if qs:
        uri = f"{uri}?{urlencode(qs)}"
    ws_kwargs = dict(kwargs)
    ws_kwargs["subprotocols"] = ["bearer", token]
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
