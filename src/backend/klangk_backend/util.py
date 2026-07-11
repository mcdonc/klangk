"""Shared utilities: env var resolution, bounded async queue, hosting info."""

import asyncio
import ipaddress
import logging
import os
import subprocess
from typing import TypeVar

# resolve_env_value / resolve_env_bool now live in the settings module (#1394)
# and delegate to the KlangkSettings singleton.  Re-exported here for backward
# compat — the ~85 call sites that do ``from .util import resolve_env_value``
# keep working without changes.
from .settings import resolve_env_value, resolve_env_bool

T = TypeVar("T")

# Versioned API prefix — used by api.py (router mount) and acl.py
# (resource path extraction). Defined here to avoid circular imports.
API_PREFIX = "/api/v1"

logger = logging.getLogger(__name__)


def read_file_value(value: str) -> tuple[str | None, OSError | None]:
    """Strip a 'file:' prefix and read the referenced file.

    Returns (contents, None) on success, where contents is the
    file's text stripped of surrounding whitespace, or (None, error)
    on failure, where error is the OSError raised while reading.

    Shared by resolve_env_value and resolve_file_value, which differ
    only in their default value and log message on failure.
    """
    path = value[5:]
    try:
        with open(path) as f:
            return f.read().strip(), None
    except OSError as e:
        e.filename = e.filename or path
        return None, e


# Maximum time a `cmd:`-prefixed value may run before being killed.
# Guards against a hung command (e.g. a vault CLI waiting on a prompt)
# blocking startup.
_CMD_TIMEOUT_SECONDS = 10


def run_cmd_value(value: str) -> tuple[str | None, str | None]:
    """Strip a 'cmd:' prefix and run the referenced command.

    Returns (stdout, None) on success, where stdout is the command's
    output stripped of surrounding whitespace, or (None, error_msg) on
    failure, where error_msg is a human-readable description. Mirrors
    [read_file_value] so the two prefixes share the same resolve flow.

    The command runs via the shell (``shell=True``) so it may use pipes
    and shell features (e.g. ``cmd:aws secretsmanager get-secret-value
    ... | jq -r .SecretString``). Only values an operator explicitly
    prefixes with ``cmd:`` are ever executed.
    """
    command = value[4:]
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_CMD_TIMEOUT_SECONDS}s"
    except OSError as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, (
            f"exited with code {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip(), None


# NOTE: resolve_env_value / resolve_env_bool are imported from .settings
# at the top of this module.  The implementations that lived here have been
# moved to klangk_backend.settings (#1394).


def resolve_file_value(value: str) -> str:
    """Resolve a value that may have a 'file:' or 'cmd:' prefix.

    If the value starts with 'file:', reads the file and returns its
    stripped contents. If it starts with 'cmd:', runs the command and
    returns its stripped stdout. Otherwise returns the value as-is.
    """
    if value.startswith("file:"):
        contents, err = read_file_value(value)
        if err is not None:
            logger.error("Cannot read secret file: %s", err)
            return ""
        assert contents is not None
        return contents
    if value.startswith("cmd:"):
        contents, err = run_cmd_value(value)
        if err is not None:
            logger.error("Cannot resolve secret via cmd: %s", err)
            return ""
        assert contents is not None
        return contents
    return value


def customize_dir() -> str:
    """Return the root customization directory.

    Resolves ``KLANGK_CUSTOMIZE_DIR`` (default ``~/.klangk/custom``).
    Subsystems look for well-known subdirectories (``certs/``,
    ``branding/``, ``email-templates/``) under this path when their
    per-feature env var is unset.  See #1360.
    """
    return resolve_env_value(
        "KLANGK_CUSTOMIZE_DIR",
        str(os.path.join(os.path.expanduser("~"), ".klangk", "custom")),
    )


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


def load_trusted_proxy_cidrs() -> set[ipaddress._BaseAddress]:
    # KLANGK_TRUSTED_PROXY_CIDRS is a public CIDR/IP list (not a secret), so read
    # it via os.environ rather than resolve_env_value (which treats its input
    # as a secret and would both support an unwanted "file:" prefix and trip
    # CodeQL's clear-text-logging taint check when we log invalid entries).
    raw = resolve_env_value("KLANGK_TRUSTED_PROXY_CIDRS")
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
                # Log without interpolating the value: CodeQL (correctly, in
                # general) treats env-var-derived data as potentially
                # sensitive, so we avoid logging the raw token. Operators can
                # inspect their own KLANGK_TRUSTED_PROXY_CIDRS to find the bad
                # entry.
                logger.warning(
                    "Ignoring an invalid KLANGK_TRUSTED_PROXY_CIDRS entry"
                )
    if not trusted:
        trusted.add(ipaddress.ip_address("127.0.0.1"))
    return trusted


_TRUSTED_PROXY_CIDRS = load_trusted_proxy_cidrs()


# UDS mode flag (#1396): set to True only when the server is bound to a UNIX
# domain socket. Over a UDS there is no TCP peer, so uvicorn leaves
# ``request.client`` as ``None``. The socket file is 0600 in a 0700 dir, both
# owned by the klangk user, so the only processes that can open it run as the
# klangk user (nginx and uvicorn do). A ``None`` peer over a UDS is therefore
# treated as the trusted reverse proxy — same as a loopback peer over TCP —
# but the trust boundary is the same-uid boundary, not an nginx-vs-attacker
# boundary: any klangk-uid process can open the socket, and we trust that
# klangk-uid processes do not forge the X-Real-IP/X-Forwarded-* headers.
# That is the single-user threat model ``none`` mode already assumes.
# The trust helpers below consult this flag so they honor nginx's forwarded
# headers instead of failing closed on the missing client. Default False:
# unit/e2e tests that launch uvicorn over TCP are unaffected, and the
# fail-closed property for a genuinely-missing client is preserved (see
# TestClientIsLoopback.test_missing_client_host_rejected).
_UDS_MODE = False


def set_uds_mode(enabled: bool) -> None:
    """Mark whether the server is bound to a UDS (so a ``None`` client peer
    is treated as the trusted reverse proxy). Called from the lifespan when
    the bind is a socket; never set by tests that use TCP or the ASGI
    TestClient.
    """
    global _UDS_MODE
    _UDS_MODE = bool(enabled)


def _connection_peer_is_trusted(client_host: str | None) -> bool:
    """True if the immediate connection peer is the trusted reverse proxy.

    Over TCP this is :func:`peer_trusted` (peer IP in the trusted CIDR set).
    Over a UDS there is no peer IP (``client_host`` is ``None``); the socket
    file perms restrict access to klangk-uid processes (nginx among them), so
    a ``None`` peer is treated as the trusted reverse proxy when
    ``_UDS_MODE`` is set — the trust boundary is the same-uid boundary (see
    ``_UDS_MODE``). ``None`` outside UDS mode stays untrusted (fail-closed)
    — preserving the missing-client rejection.
    """
    return peer_trusted(client_host) or (client_host is None and _UDS_MODE)


def peer_trusted(client_host: str | None) -> bool:
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


# Loopback addresses used by ``client_is_loopback`` (the none-mode
# /auth/local self-defense). This is the *real* loopback range
# (127.0.0.0/8 + ::1), not the three-string allowlist the startup bind
# gate uses — see main._LOOPBACK_BINDINGS for why that one is intentionally
# strict.
_LOOPBACK_ADDRS = {
    ipaddress.ip_address("127.0.0.1"),
    ipaddress.ip_address("::1"),
}


def client_is_loopback(headers=None, client_host: str | None = None) -> bool:
    """True if the *effective* client of this request is loopback.

    In ``KLANGK_AUTH_MODES=none`` the ``/auth/local`` endpoint freely issues an
    admin token, so it must only be reachable from the operator's own machine.
    nginx's per-location ``allow 127.0.0.1; deny all`` ACL is the primary
    control, but the backend re-checks here as belt-and-suspenders — and, more
    importantly, to close the front-proxy bypass: if a loopback proxy (caddy,
    traefik, a sidecar) sits in front of nginx then *every* proxied request has
    ``$remote_addr=127.0.0.1`` and the nginx ACL admits everyone. The backend
    sees the real client in ``X-Real-IP``/``X-Forwarded-For`` (set by nginx)
    and refuses non-loopback values independently.

    Resolution mirrors :func:`derive_hosting_info`: forwarded headers are
    trusted only when the immediate peer (``client_host``) is in
    ``KLANGK_TRUSTED_PROXY_CIDRS`` (default loopback — every klangk deployment
    runs the backend behind a local reverse proxy). A request that arrives
    directly from a non-loopback peer (e.g. a workspace container bypassing
    nginx) has its forwarded headers ignored, and the peer itself is
    non-loopback, so it is rejected.

    Fail-closed: a missing client (``None``) that is NOT behind a UDS, or an
    unparseable IP, rejects. Over a UDS a ``None`` client is treated as the
    trusted reverse proxy (same-uid access to the socket; see
    ``_UDS_MODE``), so its forwarded headers ARE consulted — the loopback
    TCP case in disguise. A direct UDS connection (no nginx, no forwarded
    headers) is treated as loopback: the socket's file permissions (0700
    parent dir) restrict access to the same uid.
    """
    candidate = client_host
    trust = (
        (not _REJECT_PROXY)
        and _connection_peer_is_trusted(client_host)
        and headers is not None
    )
    if trust:
        # nginx sets X-Real-IP to $remote_addr (the real client). Prefer it;
        # fall back to the first hop in X-Forwarded-For. An empty/garbage
        # header leaves the trusted peer (loopback) as candidate — which is
        # loopback, so it still admits. A *spoofed* header from an untrusted
        # peer never reaches here (trust gate above).
        real_ip = headers.get("x-real-ip") or ""
        if not real_ip:
            xff = headers.get("x-forwarded-for") or ""
            real_ip = xff.split(",")[0].strip() if xff else ""
        if real_ip:
            candidate = real_ip
    # Direct UDS connection: client_host is None, no forwarded headers,
    # but _UDS_MODE is set → same-uid trust boundary, treat as loopback.
    if candidate is None and _UDS_MODE:
        return True
    try:
        return ipaddress.ip_address(candidate) in _LOOPBACK_ADDRS
    except ValueError:
        return False


def derive_hosting_info(
    headers=None, client_host: str | None = None
) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or request headers.

    Returns (hostname, proto, base_path). Env vars take precedence over
    headers, so setting ``KLANGK_HOSTING_HOSTNAME`` / ``KLANGK_HOSTING_PROTO`` /
    ``KLANGK_HOSTING_BASE_PATH`` pins every URL the backend builds —
    independent of how a request arrives (or whether one arrives at all).

    With no env vars set, the request headers provide the values:
    forwarded headers (``X-Forwarded-Host``, ``X-Forwarded-Proto``,
    ``X-Forwarded-Prefix``) are trusted ONLY when the immediate peer
    (``client_host``) is in ``KLANGK_TRUSTED_PROXY_CIDRS`` (default
    ``127.0.0.1,::1`` — every klangk deployment runs the backend behind a
    local reverse proxy). This prevents an attacker who reaches the backend
    port directly from spoofing the host/proto to poison the
    verification/reset/OIDC links the backend generates.

    Both args are optional so the same resolver serves callers that have no
    request in hand — chiefly ``start_workspace`` (autostart/create,
    which runs at boot with no connection). With no headers the request
    branches are skipped and the env vars are the sole source, falling back
    to bare ``localhost`` / ``http`` / ``""``.

    The port is NOT synthesized from ``KLANGK_NGINX_PORT``: that var is the
    internal port containers use to reach the backend's llm-proxy/bridge,
    not the public port a browser hits (they only coincide in the default
    single-host topology; behind a real proxy/ingress the public port is
    unrelated). The port comes from the authority itself — either
    ``KLANGK_HOSTING_HOSTNAME`` (which carries ``host[:port]``) or the
    ``Host`` / ``X-Forwarded-Host`` header (both carry host and port), used
    verbatim. ``X-Forwarded-For`` is not consulted — it carries the client
    IP chain, not a host, so it has no role in URL composition.

    Pass the real connection peer (``request.client.host`` for HTTP,
    ``websocket.client.host`` for WS). When ``client_host`` is unavailable
    forwarded headers are ignored (fail-closed).

    ``KLANGK_REJECT_PROXY_HEADERS=1`` (back-compat) forces trust off entirely.
    """
    hostname = resolve_env_value("KLANGK_HOSTING_HOSTNAME")
    proto = resolve_env_value("KLANGK_HOSTING_PROTO")
    base_path = resolve_env_value("KLANGK_HOSTING_BASE_PATH")
    trust = (
        (not _REJECT_PROXY)
        and _connection_peer_is_trusted(client_host)
        # Only a real request can inform a forwarded header; an eager
        # start (no connection) must fall back to env / bare localhost.
        and headers is not None
    )
    if not hostname and headers is not None:
        if trust:
            forwarded_host = headers.get("x-forwarded-host")
            if forwarded_host:
                hostname = forwarded_host
        if not hostname:
            # The Host header carries the host (and port) the client
            # actually used to reach us — use it verbatim rather than
            # substituting an internal port (wrong behind a proxy). The
            # override is KLANGK_HOSTING_HOSTNAME when the request is
            # absent or uninformative.
            hostname = headers.get("host") or "localhost"
    if not hostname:
        # No env var and no (or uninformative) request: bare localhost.
        # The deployer sets KLANGK_HOSTING_HOSTNAME (with its port) to get
        # a reachable URL; no port is guessed.
        hostname = "localhost"
    if not proto:
        if headers is not None and trust:
            proto = headers.get("x-forwarded-proto") or "http"
        else:
            proto = "http"
    if base_path is None:
        if headers is not None and trust:
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
