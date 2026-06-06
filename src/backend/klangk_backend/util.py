"""Shared utilities: env var resolution, bounded async queue."""

import asyncio
import logging
import os
from typing import TypeVar

T = TypeVar("T")

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
            logger.error("Cannot read secret from %s: %s", path, e)
            return ""
    return value


def derive_hosting_info(headers) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or request headers.

    Returns (hostname, proto, base_path). Env vars take precedence over headers.
    Works with both Request.headers and WebSocket.headers.
    """
    hostname = resolve_env_secret("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_secret("KLANGK_HOSTING_PROTO")
    base_path = resolve_env_secret("KLANGK_HOSTING_BASE_PATH")
    if not hostname:
        forwarded_host = headers.get("x-forwarded-host")
        if forwarded_host:
            # Behind an external reverse proxy — trust its hostname as-is
            hostname = forwarded_host
        else:
            # Direct access (local dev) — use nginx port for hosted app URLs
            nginx_port = resolve_env_secret("KLANGK_NGINX_PORT")
            host = headers.get("host") or "localhost"
            if nginx_port:
                host_no_port = host.split(":")[0]
                hostname = f"{host_no_port}:{nginx_port}"
            else:
                hostname = host
    if not proto:
        proto = headers.get("x-forwarded-proto") or "http"
    if base_path is None:
        base_path = headers.get("x-forwarded-prefix") or ""
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
