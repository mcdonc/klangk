"""Shared utilities: env var resolution, bounded async queue."""

import asyncio
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


_REJECT_PROXY = resolve_env_bool("KLANGK_REJECT_PROXY_HEADERS")


def derive_hosting_info(headers) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or request headers.

    Returns (hostname, proto, base_path). Env vars take precedence over headers.
    Works with both Request.headers and WebSocket.headers.

    Forwarded headers (``X-Forwarded-Host``, ``X-Forwarded-Proto``,
    ``X-Forwarded-Prefix``) are trusted by default since klangk's own
    nginx sets them.  Set ``KLANGK_REJECT_PROXY_HEADERS=1`` to ignore
    them in hardened deployments where the backend port is exposed
    directly.
    """
    hostname = resolve_env_secret("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_secret("KLANGK_HOSTING_PROTO")
    base_path = resolve_env_secret("KLANGK_HOSTING_BASE_PATH")
    trust = not _REJECT_PROXY
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
