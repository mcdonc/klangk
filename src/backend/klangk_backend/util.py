"""Shared utilities: env var resolution, bounded async queue."""

import asyncio
import ipaddress
import logging
import os
from typing import TypeVar

T = TypeVar("T")

# Versioned API prefix — used by api.py (router mount) and acl.py
# (resource path extraction). Defined here to avoid circular imports.
API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def resolve_env_secret(key: str, default: str | None = None) -> str | None:
    """Read an env var, dereferencing 'path:' prefixed values.

    If the value starts with 'file:', the remainder is treated as a
    file path and the file contents (stripped) are returned. If the
    file cannot be read, logs an error and returns None.
    """
    val = os.environ.get(key)
    if val is None:
        return default
    if val.startswith("file:"):
        path = val[5:]
        try:
            return open(path).read().strip()
        except OSError as e:
            logger.error("Cannot read %s from %s: %s", key, path, e)
            return None
    return val


def resolve_env_bool(key: str, default: bool = False) -> bool:
    """Read an env var as a boolean.

    Truthy values: "1", "true", "yes" (case-insensitive).
    Everything else is falsy.  Unset returns *default*.
    """
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")


def resolve_file_secret(value: str) -> str:
    """Resolve a value that may have a 'file:' prefix.

    If the value starts with 'file:', reads the file and returns
    its stripped contents. Otherwise returns the value as-is.
    """
    if value.startswith("file:"):
        path = value[5:]
        try:
            return open(path).read().strip()
        except OSError as e:
            logger.error("Cannot read secret file: %s", e)
            return ""
    return value


def sanitize_disposition_name(name: str) -> str:
    """Sanitize a filename for use in a Content-Disposition header.

    Strips characters that would break or inject into the header value
    (double quotes, backslashes, path separators).
    """
    return name.replace("/", "_").replace("\\", "_").replace('"', "")


# Forwarded headers (X-Forwarded-Host/-Proto/-Prefix) are trusted ONLY when
# the immediate connection comes from a configured trusted proxy upstream.
# klangk's nginx proxies to 127.0.0.1, so the default trusted set is the
# loopback addresses; every deployment runs the backend behind a local
# reverse proxy, so this works out of the box. If the backend port is ever
# exposed directly to untrusted networks, requests from those peers fall
# outside the trusted set and forwarded headers are ignored (so an attacker
# cannot spoof X-Forwarded-Host to poison verification/reset/OIDC links).
#
# KLANGK_TRUSTED_PROXY_CIDRS: comma-separated CIDRs/IPs to trust
# (default "127.0.0.1,::1").
#
# Back-compat: KLANGK_REJECT_PROXY_HEADERS=1 (or true/yes) is honored as a
# hard "reject always" override (trust nobody), matching the old opt-out.
_REJECT_PROXY = resolve_env_bool("KLANGK_REJECT_PROXY_HEADERS")


def _load_trusted_proxy_cidrs() -> set[ipaddress._BaseAddress]:
    raw = resolve_env_secret("KLANGK_TRUSTED_PROXY_CIDRS", "127.0.0.1,::1")
    trusted: set[ipaddress._BaseAddress] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            trusted.add(ipaddress.ip_address(token))
        except ValueError:
            try:
                net = ipaddress.ip_network(token, strict=False)
                trusted.add(net)
            except ValueError:
                logger.warning(
                    "Ignoring invalid KLANGK_TRUSTED_PROXY_CIDRS entry: %r",
                    token,
                )
    if not trusted:
        trusted.add(ipaddress.ip_address("127.0.0.1"))
    return trusted


_TRUSTED_PROXY_CIDRS = _load_trusted_proxy_cidrs()


def _peer_trusted(client_host: str | None) -> bool:
    """True if the immediate peer is in the trusted proxy set."""
    if not client_host:
        return False
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for entry in _TRUSTED_PROXY_CIDRS:
        if isinstance(entry, ipaddress._BaseNetwork):
            if ip in entry:
                return True
        elif entry == ip:
            return True
    return False


def derive_hosting_info(
    headers, client_host: str | None = None
) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or request headers.

    Returns (hostname, proto, base_path). Env vars take precedence over
    headers. Works with both Request.headers and WebSocket.headers.

    Forwarded headers (``X-Forwarded-Host``, ``X-Forwarded-Proto``,
    ``X-Forwarded-Prefix``) are trusted ONLY when the immediate peer
    (``client_host``) is in ``KLANGK_TRUSTED_PROXY_CIDRS`` (default
    ``127.0.0.1,::1`` — every klangk deployment runs the backend behind a
    local reverse proxy). This prevents an attacker who reaches the backend
    port directly from spoofing the host/proto to poison the
    verification/reset/OIDC links the backend generates.

    Pass the real connection peer (``request.client.host`` for HTTP,
    ``websocket.client.host`` for WS). When ``client_host`` is unavailable
    forwarded headers are ignored (fail-closed).

    ``KLANGK_REJECT_PROXY_HEADERS=1`` (back-compat) forces trust off entirely.
    """
    hostname = resolve_env_secret("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_secret("KLANGK_HOSTING_PROTO")
    base_path = resolve_env_secret("KLANGK_HOSTING_BASE_PATH")
    trust = (not _REJECT_PROXY) and _peer_trusted(client_host)
    if not hostname:
        if trust:
            forwarded_host = headers.get("x-forwarded-host")
            if forwarded_host:
                hostname = forwarded_host
        if not hostname:
            # Direct access (local dev) — use nginx port for hosted app URLs
            nginx_port = resolve_env_secret("KLANGK_NGINX_PORT")
            host = headers.get("host") or "localhost"
            if nginx_port:
                host_no_port = host.split(":")[0]
                hostname = f"{host_no_port}:{nginx_port}"
            else:
                hostname = host
    if not proto:
        if trust:
            proto = headers.get("x-forwarded-proto") or "http"
        else:
            proto = "http"
    if base_path is None:
        if trust:
            base_path = headers.get("x-forwarded-prefix") or ""
        else:
            base_path = ""
    return hostname, proto, base_path


class BoundedOutputQueue(asyncio.Queue[T | None]):
    """Bounded asyncio.Queue with non-blocking sentinel support.

    Used by TerminalSession and ExecSession to pass output from a
    producer (read loop) to a consumer (WebSocket forwarder) with
    back-pressure.  The sentinel (None) is sent non-blocking to
    avoid deadlocking when the consumer has already exited and the
    queue is full.
    """

    def send_sentinel(self) -> None:
        """Signal end-of-stream.  Non-blocking: if the queue is full
        the consumer has data to drain and will exit via the timeout
        check in the ``output()`` generator."""
        try:
            self.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover
            pass
