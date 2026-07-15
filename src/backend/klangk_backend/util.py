"""Shared utilities: bounded async queue, file:/cmd: resolution, request trust.

Settings-dependent helpers live on :class:`Util` (``app.state.util``); pure
helpers (``read_file_value``, ``resolve_file_value``, ``sanitize_disposition_name``,
:class:`BoundedOutputQueue`) stay module-level.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import subprocess
import uuid
from pathlib import Path
from typing import TypeVar

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


def sanitize_disposition_name(name: str) -> str:
    """Sanitize a filename for use in a Content-Disposition header.

    Strips characters that would break or inject into the header value
    (double quotes, backslashes, path separators).
    """
    return name.replace("/", "_").replace("\\", "_").replace('"', "")


# --- OS-level TCP port discovery (moved from model/ports.py, #1547) -----
# These are pure socket probes — they never touch the DB, so they live in
# util (next to the loopback / network-trust helpers) rather than in the
# ``model`` persistence layer. ``model.ports`` re-exports them for back-compat.

# Highest valid TCP port.  scan_free_ports will not scan past this, so an
# exhausted range fails fast instead of looping forever.
MAX_PORT = 65535


def port_in_use(port: int) -> bool:
    """Check if a port is bound at the OS level.

    Binds ``0.0.0.0`` (all interfaces) deliberately: workspace host ports
    are published by podman with no host IP, i.e. on ``0.0.0.0`` (see
    ``podman.create``'s ``-p host:container``), so the probe must detect a
    bind on *any* interface to predict a publish collision. Binding the
    probe to loopback would miss ports held only on an external interface
    and let the allocator hand out a port podman then fails to bind. This
    is why CodeQL's ``py/bind-socket-all-network-interfaces`` (alert #155)
    is a false positive here.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def free_port() -> int:
    """Return a free TCP port on loopback for ephemeral use.

    Binds ``127.0.0.1:0`` so the OS assigns an ephemeral port, then
    releases it and returns the number. Used by the E2E harnesses to
    pick the server port (and to seed ``KLANGK_PORT_RANGE_START``)
    instead of a hardcoded value, so concurrent runs — xdist workers,
    or several suites on one machine — don't collide (#1393). This
    generalizes the ``_find_free_port`` helper first introduced in
    ``test_nginx_acl_e2e.py``.

    The port is released before this returns, so there is an inherent
    TOCTOU window before the caller rebinds it (e.g. uvicorn at server
    startup, or a workspace container binding a hosted-app port). For
    the workspace-port range the allocator's own :func:`port_in_use`
    check (run inside :func:`scan_free_ports`) is the backstop: it skips
    any port a concurrent run grabbed in the meantime.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Loopback (not INADDR_ANY "") for ephemeral pickup — same
        # free-port behavior, matches the test_nginx_acl_e2e pattern.
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scan_free_ports(start: int, count: int, used: set[int]) -> list[int]:
    """Find ``count`` free ports at or after ``start``.

    Skips ports already in ``used`` (DB-allocated) and ports reported as
    bound by the OS.  This is synchronous because it performs blocking
    ``socket.bind()`` checks; ``model.find_and_allocate_ports`` runs it in
    an executor so the event loop is not stalled.  Raises ``ValueError`` if
    fewer than ``count`` free ports are available before ``MAX_PORT``.
    """
    ports: list[int] = []
    port = start
    while len(ports) < count:
        if port > MAX_PORT:
            raise ValueError(
                f"Could not allocate {count} free ports starting at "
                f"{start}: exhausted at {MAX_PORT}"
            )
        if port not in used and not port_in_use(port):
            ports.append(port)
        port += 1
    return ports


# Loopback addresses used by ``Util.client_is_loopback`` (the none-mode
# /auth/local self-defense). This is the *real* loopback range
# (127.0.0.0/8 + ::1), not the three-string allowlist the startup bind
# gate uses — see main._LOOPBACK_BINDINGS for why that one is intentionally
# strict.
_LOOPBACK_ADDRS = {
    ipaddress.ip_address("127.0.0.1"),
    ipaddress.ip_address("::1"),
}


class Util:
    """App-state-owned helpers that transitively depend on settings (#1503).

    Holds the proxy-trust / forwarded-header logic, hosting-info derivation,
    the customize-dir resolver, the instance identity, and the instance's PID
    file — everything in ``util.py`` that reads config. Config is read from
    ``self.settings`` at call time, not frozen at import (the #1426
    anti-pattern). The UDS-mode flag (set at bind time from the lifespan)
    lives on the instance too.

    Wired onto ``app.state.util`` in ``build_app``; consumers reach it via
    ``app_state.util`` or ``request.app.state.util``.
    """

    def __init__(self, app_state):
        self.app_state = app_state
        self.settings = app_state.settings
        # Instance identity: resolved once at startup by resolve_instance_id()
        # and cached here — no module global (#1553).
        self._instance_id: str | None = None
        # UDS mode flag (#1396): set to True only when the server is bound to a
        # UNIX domain socket. Over a UDS there is no TCP peer, so uvicorn
        # leaves ``request.client`` as ``None``. The socket file is 0600 in a
        # 0700 dir, both owned by the klangk user, so the only processes that
        # can open it run as the klangk user (nginx and uvicorn do). A ``None``
        # peer over a UDS is therefore treated as the trusted reverse proxy —
        # same as a loopback peer over TCP — but the trust boundary is the
        # same-uid boundary, not an nginx-vs-attacker boundary. Default False:
        # unit/e2e tests that launch uvicorn over TCP are unaffected.
        self.uds_mode = False

    def set_uds_mode(self, enabled: bool) -> None:
        """Mark whether the server is bound to a UDS. Called from the lifespan
        when the bind is a socket; never set by tests that use TCP or the ASGI
        TestClient.
        """
        self.uds_mode = bool(enabled)

    def customize_dir(self) -> str:
        """Root customization directory (``KLANGK_CUSTOMIZE_DIR``).

        Defaults to ``<state_dir>/custom`` (derived in ``_require_dirs``).
        """
        return self.settings.customize_dir

    # --- Instance identity ------------------------------------------------

    #: Filename of the instance-ID file within ``data_dir``.
    INSTANCE_ID_FILENAME = "instance-id"

    def instance_id_path(self) -> Path:
        """Return ``<data_dir>/instance-id`` for this instance's data dir.

        Resolves ``data_dir`` from ``self.settings``. Does **not** open the
        SQLite DB — only the path is computed.
        """
        return Path(self.settings.data_dir) / self.INSTANCE_ID_FILENAME

    def resolve_instance_id(self) -> str:
        """Read the instance ID from ``<data_dir>/instance-id``, creating it if absent.

        Called once at startup (top of the lifespan, before seed/admin setup).
        If the file exists its (stripped) contents are used; otherwise a UUID-4
        is generated and written **atomically** — ``instance-id.tmp`` then
        ``os.replace`` — since the file is the only copy and a torn write
        would be fatal. An empty/garbage file is regenerated the same way.

        The resolved value is cached on this ``Util`` instance for the process
        lifetime; :meth:`instance_id` returns the cache and never touches the
        filesystem.
        """
        path = self.instance_id_path()
        resolved: str | None = None
        if path.exists():
            resolved = path.read_text().strip() or None

        if resolved is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            resolved = str(uuid.uuid4())
            tmp = path.parent / f"{path.name}.tmp"
            tmp.write_text(resolved)
            os.replace(tmp, path)

        self._instance_id = resolved
        return resolved

    def instance_id(self) -> str:
        """Return the instance ID, resolving it lazily on first use.

        Startup calls :meth:`resolve_instance_id` explicitly to write the file
        early (so external readers (e.g. E2E harnesses that read it directly
        to scope container cleanup) never race a long startup), but a read
        always works — if resolve hasn't run yet, it resolves now using
        ``self.settings``. No module global: the resolved value lives on this
        ``Util`` instance (#1553).
        """
        if self._instance_id is None:
            self.resolve_instance_id()
        return self._instance_id

    # --- PID file ---------------------------------------------------------
    #
    # The PID file is per-process runtime state — the same kind of artifact
    # as the UDS socket (``<state_dir>/klangk.sock``) and rendered nginx.conf,
    # so it lives directly in ``state_dir`` (which the settings validator
    # requires and even documents as the pid-file home). There is no separate
    # ``runtime_dir()`` fallback chain: ``KLANGK_STATE_DIR`` is required to
    # boot, so it is always present by the time a PID file path is computed.
    # (Earlier releases probed XDG_RUNTIME_DIR / ``/run/user/<uid>`` /
    # ``~/.klangk/run`` — portable-fallback logic from when state_dir was
    # optional (#773); dead weight now that it's required.) The helpers read
    # :meth:`instance_id`, so there is no ``instance_id`` argument to thread.

    def pid_file_path(self) -> Path:
        """Return the PID file path for this instance's ID.

        Lives in ``state_dir`` next to the UDS socket. The name embeds the
        instance ID (``klangk-<id>.pid``) so multiple klangk instances per
        user don't collide on one PID file.
        """
        return (
            Path(self.settings.state_dir) / f"klangk-{self.instance_id()}.pid"
        )

    def check_pid_file(self) -> int | None:
        """Check if another instance is running.

        Returns the PID of the running process, or None if no live process
        holds the PID file.  Removes stale PID files automatically.
        """
        path = self.pid_file_path()
        try:
            pid = int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError):
            # Process is dead or PID is invalid — stale PID file.
            path.unlink(missing_ok=True)
            return None
        except PermissionError:
            # Process exists but we can't signal it (different user).
            return pid
        # Don't treat our own PID as a conflict (e.g., after a crash that
        # left the PID file behind and the OS recycled the PID).
        if pid == os.getpid():
            return None
        return pid

    def write_pid_file(self) -> None:
        """Write the current PID to the instance PID file."""
        path = self.pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))

    def remove_pid_file(self) -> None:
        """Remove the PID file (best-effort)."""
        try:
            path = self.pid_file_path()
            # Only remove if it contains our PID (another instance may
            # have overwritten it after we were signalled to stop).
            if path.read_text().strip() == str(os.getpid()):
                path.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    # --- Proxy trust / forwarded headers ---------------------------------
    #
    # Forwarded headers (X-Forwarded-Host/-Proto/-Prefix) are trusted ONLY
    # when the immediate connection comes from a configured trusted proxy
    # upstream. klangk's nginx proxies to 127.0.0.1, so the default trusted
    # set is the loopback addresses; every deployment runs the backend
    # behind a local reverse proxy, so this works out of the box. If the
    # backend port is ever exposed directly to untrusted networks, requests
    # from those peers fall outside the trusted set and forwarded headers
    # are ignored (so an attacker cannot spoof X-Forwarded-Host to poison
    # verification/reset/OIDC links).
    #
    # KLANGK_TRUSTED_PROXY_CIDRS: comma-separated CIDRs/IPs to trust
    # (default "127.0.0.1,::1").
    #
    # Back-compat: KLANGK_REJECT_PROXY_HEADERS=1 (or true/yes) is honored as
    # a hard "reject always" override (trust nobody).

    def reject_proxy_headers(self) -> bool:
        """True if KLANGK_REJECT_PROXY_HEADERS is set (hard trust-off)."""
        raw = self.settings.reject_proxy_headers
        return bool(raw and raw.strip().lower() in ("1", "true", "yes"))

    def trusted_proxy_cidrs(self) -> set[ipaddress._BaseAddress]:
        """Parse KLANGK_TRUSTED_PROXY_CIDRS into a set of IPs/networks.

        The setting is a public CIDR/IP list (not a secret), already resolved
        at construction (#1461). Invalid entries are logged and skipped; if
        none are valid, defaults to loopback.
        """
        raw = self.settings.trusted_proxy_cidrs
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
                    # Log without interpolating the value (CodeQL treats
                    # env-var-derived data as potentially sensitive).
                    logger.warning(
                        "Ignoring an invalid KLANGK_TRUSTED_PROXY_CIDRS entry"
                    )
        if not trusted:
            trusted.add(ipaddress.ip_address("127.0.0.1"))
        return trusted

    def peer_trusted(self, client_host: str | None) -> bool:
        """True if the immediate peer is in the trusted proxy set."""
        if not client_host:
            return False
        try:
            ip = ipaddress.ip_address(client_host)
        except ValueError:
            return False
        for entry in self.trusted_proxy_cidrs():
            if isinstance(entry, ipaddress._BaseNetwork):
                if ip in entry:
                    return True
            elif entry == ip:
                return True
        return False

    def connection_peer_is_trusted(self, client_host: str | None) -> bool:
        """True if the immediate connection peer is the trusted reverse proxy.

        Over TCP this is :meth:`peer_trusted`. Over a UDS there is no peer IP
        (``client_host`` is ``None``); the socket file perms restrict access to
        klangk-uid processes, so a ``None`` peer is treated as trusted when
        ``uds_mode`` is set.
        """
        return self.peer_trusted(client_host) or (
            client_host is None and self.uds_mode
        )

    def client_is_loopback(
        self, headers=None, client_host: str | None = None
    ) -> bool:
        """True if the *effective* client of this request is loopback.

        In ``KLANGK_AUTH_MODES=none`` the ``/auth/local`` endpoint freely
        issues an admin token, so it must only be reachable from the
        operator's own machine. nginx's per-location ``allow 127.0.0.1; deny
        all`` ACL is the primary control, but this re-checks as
        belt-and-suspenders — and to close the front-proxy bypass: if a
        loopback proxy sits in front of nginx then every proxied request has
        ``$remote_addr=127.0.0.1`` and the nginx ACL admits everyone. The
        backend sees the real client in ``X-Real-IP``/``X-Forwarded-For`` and
        refuses non-loopback values independently.

        Fail-closed: a missing client (``None``) that is NOT behind a UDS, or
        an unparseable IP, rejects. Over a UDS a ``None`` client is treated
        as the trusted reverse proxy (same-uid socket access).
        """
        candidate = client_host
        trust = (
            (not self.reject_proxy_headers())
            and self.connection_peer_is_trusted(client_host)
            and headers is not None
        )
        if trust:
            real_ip = headers.get("x-real-ip") or ""
            if not real_ip:
                xff = headers.get("x-forwarded-for") or ""
                real_ip = xff.split(",")[0].strip() if xff else ""
            if real_ip:
                candidate = real_ip
        if candidate is None and self.uds_mode:
            return True
        try:
            return ipaddress.ip_address(candidate) in _LOOPBACK_ADDRS
        except ValueError:
            return False

    def derive_hosting_info(
        self, headers=None, client_host: str | None = None
    ) -> tuple[str, str, str]:
        """Derive hosting hostname, proto, and base path from env or headers.

        Returns (hostname, proto, base_path). Env vars take precedence over
        headers, so setting ``KLANGK_HOSTING_HOSTNAME`` / ``_PROTO`` /
        ``_BASE_PATH`` pins every URL the backend builds — independent of how
        a request arrives. With no env vars, forwarded headers are trusted
        only when the immediate peer is trusted.

        Both args are optional so the same resolver serves callers that have
        no request in hand (e.g. ``start_workspace`` at boot). With no
        headers the request branches are skipped and the env vars are the
        sole source, falling back to bare ``localhost`` / ``http`` / ``""``.
        """
        hostname = self.settings.hosting_hostname
        proto = self.settings.hosting_proto
        base_path = self.settings.hosting_base_path
        trust = (
            (not self.reject_proxy_headers())
            and self.connection_peer_is_trusted(client_host)
            and headers is not None
        )
        if not hostname and headers is not None:
            if trust:
                forwarded_host = headers.get("x-forwarded-host")
                if forwarded_host:
                    hostname = forwarded_host
            if not hostname:
                hostname = headers.get("host") or "localhost"
        if not hostname:
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

    def cors_origins(self) -> list[str]:
        """Build the CORS allowed-origins list.

        Priority: KLANGK_CORS_ORIGINS (comma-separated) > derived from the
        hosting env vars > bare localhost.

        Consistent with hosted-app URL construction: the port comes from
        KLANGK_HOSTING_HOSTNAME (which carries host[:port]); it is never
        synthesized from KLANGK_EGRESS_PORT (that is internal container
        wiring, not the browser origin). Origins carry no path, so
        KLANGK_HOSTING_BASE_PATH is ignored here.
        """
        explicit = self.settings.cors_origins
        if explicit:
            return [o.strip() for o in explicit.split(",") if o.strip()]
        hostname, proto, _ = self.derive_hosting_info(None, None)
        return [f"{proto}://{hostname}"]

    def bridge_idle_timeout(self) -> float:
        """Max seconds between streamed browser chunks before giving up.

        Bounds the gap between chunks (not the total query duration), so a
        long-but-progressing stream never times out. Override with
        KLANGK_BRIDGE_TIMEOUT_SECONDS (the settings field is parsed here).
        """
        raw = self.settings.bridge_timeout_seconds
        try:
            return float(raw) if raw else 30.0
        except (TypeError, ValueError):
            return 30.0


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
