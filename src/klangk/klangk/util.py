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

from . import workspace_settings as bridge_ws_settings

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
CMD_TIMEOUT_SECONDS = 10


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
            timeout=CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {CMD_TIMEOUT_SECONDS}s"
    except OSError as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, (
            f"exited with code {proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip(), None


def _resolved_or_empty(
    contents: str | None, err: OSError | str | None, log_msg: str
) -> str:
    """Empty string on a failed file:/cmd: resolution (the error is
    logged by the caller's message); otherwise the resolved contents."""
    if err is not None:
        logger.error(log_msg, err)
        return ""
    assert contents is not None
    return contents


def resolve_file_value(value: str) -> str:
    """Resolve a value that may have a 'file:' or 'cmd:' prefix.

    If the value starts with 'file:', reads the file and returns its
    stripped contents. If it starts with 'cmd:', runs the command and
    returns its stripped stdout. Otherwise returns the value as-is.
    A failed resolution logs and yields "" rather than surfacing
    the raw prefixed value.
    """
    if value.startswith("file:"):
        contents, err = read_file_value(value)
        return _resolved_or_empty(contents, err, "Cannot read secret file: %s")
    if value.startswith("cmd:"):
        contents, err = run_cmd_value(value)
        return _resolved_or_empty(
            contents, err, "Cannot resolve secret via cmd: %s"
        )
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
    pick the server port (and to seed ``KLANGKD_PORT_RANGE_START``)
    instead of a hardcoded value, so concurrent runs — xdist workers,
    or several suites on one machine — don't collide (#1393). This
    generalizes the ``_find_free_port`` helper first introduced in
    ``test_proxy_acl_e2e.py``.

    The port is released before this returns, so there is an inherent
    TOCTOU window before the caller rebinds it (e.g. uvicorn at server
    startup, or a workspace container binding a hosted-app port). For
    the workspace-port range the allocator's own :func:`port_in_use`
    check (run inside :func:`scan_free_ports`) is the backstop: it skips
    any port a concurrent run grabbed in the meantime.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # Loopback (not INADDR_ANY "") for ephemeral pickup — same
        # free-port behavior, matches the test_proxy_acl_e2e pattern.
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


def authority_has_port(authority: str) -> bool:
    """True if a ``host[:port]`` authority names an explicit port.

    Used by :meth:`Util.browser_listener_hostname` (#2732) to tell a
    port-less hostname (``localhost``, an outer proxy's standard-port
    authority) from one that already carries its port. IPv6 literals
    arrive bracketed per RFC 3986, so the colon inside ``[::1]`` is not
    a port separator; ``[::1]:8997`` is. A bare (unbracketed) IPv6
    literal is indistinguishable from ``host:port`` by suffix alone, so
    it reports True — callers treat it as already-ported and leave it
    alone, which is the safe outcome for a form no supported client
    sends.
    """
    if authority.startswith("["):
        return not authority.endswith("]")
    _host, sep, port = authority.rpartition(":")
    return bool(sep) and port.isdigit()


def _unbracketed_host(candidate: str) -> str | None:
    """Strip IPv6 brackets from a colon-bearing candidate.

    ``None`` for a bare (unbracketed) colon-bearing form — every bare
    IPv6 literal, including v4-mapped loopback like ``::ffff:127.0.0.1``
    — which cannot take a bare :port append and is left alone by the
    caller.
    """
    if ":" not in candidate:
        return candidate
    if candidate.startswith("[") and candidate.endswith("]"):
        return candidate[1:-1]
    return None


def _is_loopback_literal(candidate: str) -> bool:
    """``localhost`` or any parseable loopback IP literal (covers
    127.0.0.0/8 dotted-quads and ``::1``)."""
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def is_portless_loopback_host(authority: str) -> bool:
    """True if *authority* is a port-less loopback hostname (#2732).

    The synthetic local values :meth:`Util.browser_listener_hostname`
    retargets at the browser listener: the no-request floor
    ``localhost`` and the ``Host: localhost`` / ``Host: 127.x`` a CLI
    handshake sends. Case-insensitive (Host is case-insensitive per RFC
    7230) and covers 127.0.0.0/8 dotted-quad + bracketed ``::1``.
    Port-bearing authorities and non-loopback hosts (remote intent;
    never rewritten) are False. Any unbracketed colon-bearing form —
    every bare IPv6 literal, including v4-mapped loopback like
    ``::ffff:127.0.0.1`` — is left alone: appending ``:<port>`` to it
    would emit an authority no URL parser accepts, and bracketing is
    not this helper's job. Shorthand like ``127.1`` (rejected by
    ``ipaddress``) and the FQDN dot form ``localhost.`` are also left
    alone — value-noise edges no supported client sends.
    """
    if not authority or authority_has_port(authority):
        return False
    candidate = _unbracketed_host(authority.lower())
    return candidate is not None and _is_loopback_literal(candidate)


def _parse_trusted_entry(token: str) -> ipaddress._BaseAddress | None:
    """One trusted-proxy token: a bare IP address, else a CIDR network
    (non-strict, so a bare host address widens to its network), else
    ``None`` for an invalid entry."""
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        pass
    try:
        return ipaddress.ip_network(token, strict=False)
    except ValueError:
        return None


def _canonical_ip_or_raw(candidate: str | None) -> str | None:
    """The canonical (``str()``-normalized) form of a parseable IP
    address; the raw string (or ``None``) when it does not parse."""
    if candidate is None:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return candidate


# Loopback addresses used by ``Util.client_is_loopback`` (the none-mode
# /auth/local self-defense). This is the *real* loopback range
# (127.0.0.0/8 + ::1), not the three-string allowlist the startup bind
# gate uses — see main._LOOPBACK_BINDINGS for why that one is intentionally
# strict.
_LOOPBACK_ADDRS = {
    ipaddress.ip_address("127.0.0.1"),
    ipaddress.ip_address("::1"),
}


def _first_forwarded_hop(headers) -> str:
    """The first hop of X-Forwarded-For (the client as the trusted
    proxy saw it), or "" when the header is absent."""
    xff = headers.get("x-forwarded-for") or ""
    return xff.split(",")[0].strip() if xff else ""


def trusted_forwarded_ip(headers) -> str | None:
    """The parsed IP from trusted proxy headers (X-Real-IP, then the first
    hop of X-Forwarded-For), or None when absent or unparseable (garbage or
    a proxy chain appending client-controlled text — fall back to the peer
    instead of trusting an unvalidated string)."""
    real_ip = headers.get("x-real-ip") or ""
    if not real_ip:
        real_ip = _first_forwarded_hop(headers)
    if not real_ip:
        return None
    try:
        return str(ipaddress.ip_address(real_ip))
    except ValueError:
        return None


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

    def __init__(self, app):
        self.app = app
        # Instance identity: resolved once at startup by resolve_instance_id()
        # and cached here — no module global (#1553).
        self._instance_id: str | None = None
        # UDS mode flag (#1396): set to True only when the server is bound to a
        # UNIX domain socket. Over a UDS there is no TCP peer, so uvicorn
        # leaves ``request.client`` as ``None``. The socket file is 0600 in a
        # 0700 dir, both owned by the klangk user, so the only processes that
        # can open it run as the klangk user (the proxy and uvicorn do). A ``None``
        # peer over a UDS is therefore treated as the trusted reverse proxy —
        # same as a loopback peer over TCP — but the trust boundary is the
        # same-uid boundary, not a proxy-vs-attacker boundary. Default False:
        # unit/e2e tests that launch uvicorn over TCP are unaffected.
        self.uds_mode = False

    def reconfigure(self, app) -> None:
        self.app = app

    def set_uds_mode(self, enabled: bool) -> None:
        """Mark whether the server is bound to a UDS. Called from the lifespan
        when the bind is a socket; never set by tests that use TCP or the ASGI
        TestClient.
        """
        self.uds_mode = bool(enabled)

    def customize_dir(self) -> str:
        """Root customization directory (``KLANGKD_CUSTOMIZE_DIR``).

        Defaults to ``<state_dir>/custom`` (derived in ``require_dirs``).
        """
        return self.app.state.settings.customize_dir

    # --- Instance identity ------------------------------------------------

    #: Filename of the instance-ID file within ``data_dir``.
    INSTANCE_ID_FILENAME = "instance-id"

    def instance_id_path(self) -> Path:
        """Return ``<data_dir>/instance-id`` for this instance's data dir.

        Resolves ``data_dir`` from ``self.settings``. Does **not** open the
        SQLite DB — only the path is computed.
        """
        return (
            Path(self.app.state.settings.data_dir) / self.INSTANCE_ID_FILENAME
        )

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
    # ``runtime_dir()`` fallback chain: ``KLANGKD_STATE_DIR`` is required to
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
            Path(self.app.state.settings.state_dir)
            / f"klangk-{self.instance_id()}.pid"
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
    # upstream. klangk's proxy proxies to 127.0.0.1, so the default trusted
    # set is the loopback addresses; every deployment runs the backend
    # behind a local reverse proxy, so this works out of the box. If the
    # backend port is ever exposed directly to untrusted networks, requests
    # from those peers fall outside the trusted set and forwarded headers
    # are ignored (so an attacker cannot spoof X-Forwarded-Host to poison
    # verification/reset/OIDC links).
    #
    # KLANGKD_TRUSTED_PROXY_CIDRS: comma-separated CIDRs/IPs to trust
    # (default "127.0.0.1,::1").
    #
    # Back-compat: KLANGKD_REJECT_PROXY_HEADERS=1 (or true/yes) is honored as
    # a hard "reject always" override (trust nobody).

    def reject_proxy_headers(self) -> bool:
        """True if KLANGKD_REJECT_PROXY_HEADERS is set (hard trust-off)."""
        raw = self.app.state.settings.reject_proxy_headers
        return bool(raw and raw.strip().lower() in ("1", "true", "yes"))

    def trusted_proxy_cidrs(self) -> set[ipaddress._BaseAddress]:
        """Parse KLANGKD_TRUSTED_PROXY_CIDRS into a set of IPs/networks.

        The setting is a public CIDR/IP list (not a secret), already resolved
        at construction (#1461). Invalid entries are logged and skipped; if
        none are valid, defaults to loopback.
        """
        raw = self.app.state.settings.trusted_proxy_cidrs
        trusted: set[ipaddress._BaseAddress] = set()
        for token in filter(None, map(str.strip, (raw or "").split(","))):
            self._add_trusted_entry(trusted, token)
        if not trusted:
            trusted.add(ipaddress.ip_address("127.0.0.1"))
        return trusted

    @staticmethod
    def _add_trusted_entry(
        trusted: set[ipaddress._BaseAddress], token: str
    ) -> None:
        """Add one KLANGKD_TRUSTED_PROXY_CIDRS token: a bare IP, a CIDR
        network, or (logged and skipped) an invalid entry."""
        entry = _parse_trusted_entry(token)
        if entry is None:
            # Log without interpolating the value (CodeQL treats
            # env-var-derived data as potentially sensitive).
            logger.warning(
                "Ignoring an invalid KLANGKD_TRUSTED_PROXY_CIDRS entry"
            )
            return
        trusted.add(entry)

    def peer_trusted(self, client_host: str | None) -> bool:
        """True if the immediate peer is in the trusted proxy set."""
        if not client_host:
            return False
        try:
            ip = ipaddress.ip_address(client_host)
        except ValueError:
            return False
        return any(
            self._entry_matches(entry, ip)
            for entry in self.trusted_proxy_cidrs()
        )

    @staticmethod
    def _entry_matches(
        entry: ipaddress._BaseAddress, ip: ipaddress._BaseAddress
    ) -> bool:
        """True when *ip* is covered by a trusted entry — containment
        for a network entry, equality for a bare address."""
        if isinstance(entry, ipaddress._BaseNetwork):
            return ip in entry
        return entry == ip

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

    def effective_client_ip(
        self, headers=None, client_host: str | None = None
    ) -> str | None:
        """The effective client IP of a request, proxy-trust-aware.

        Forwarded headers (``X-Real-IP``, then the first hop of
        ``X-Forwarded-For``) are honored only when the immediate peer
        is a trusted proxy and ``KLANGKD_REJECT_PROXY_HEADERS`` is off —
        the same trust gate :meth:`client_is_loopback` and
        :meth:`derive_hosting_info` use, so a direct caller cannot
        spoof a workstation identity (#2586). The header value must
        parse as an IP address; a garbage or forged-unparseable value
        falls back to the peer rather than being persisted, logged,
        or served as a workstation identity. Returns the canonical
        (``str()``-normalized) address, or ``None`` when there is no
        client at all.
        """
        if self._forwarded_headers_trusted(headers, client_host):
            forwarded = trusted_forwarded_ip(headers)
            if forwarded is not None:
                return forwarded
        return _canonical_ip_or_raw(client_host)

    def _forwarded_headers_trusted(
        self, headers, client_host: str | None
    ) -> bool:
        """The shared forwarded-header trust gate: honored only when the
        immediate peer is the trusted proxy and the hard-off override is
        unset. ``headers is not None`` keeps request-less callers on the
        env-var path (:meth:`derive_hosting_info})."""
        return (
            (not self.reject_proxy_headers())
            and self.connection_peer_is_trusted(client_host)
            and headers is not None
        )

    def client_is_loopback(
        self, headers=None, client_host: str | None = None
    ) -> bool:
        """True if the *effective* client of this request is loopback.

        In ``KLANGKD_AUTH_MODES=none`` the ``/auth/local`` endpoint freely
        issues an admin token, so it must only be reachable from the
        operator's own machine. the proxy's per-location ``allow 127.0.0.1; deny
        all`` ACL is the primary control, but this re-checks as
        belt-and-suspenders — and to close the front-proxy bypass: if a
        loopback proxy sits in front of the proxy then every proxied request has
        ``$remote_addr=127.0.0.1`` and the proxy ACL admits everyone. The
        backend sees the real client in ``X-Real-IP``/``X-Forwarded-For`` and
        refuses non-loopback values independently.

        Fail-closed: a missing client (``None``) that is NOT behind a UDS, or
        an unparseable IP, rejects. Over a UDS a ``None`` client is treated
        as the trusted reverse proxy (same-uid socket access).
        """
        candidate = self.effective_client_ip(headers, client_host)
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
        headers, so setting ``KLANGKD_HOSTING_HOSTNAME`` / ``_PROTO`` /
        ``_BASE_PATH`` pins every URL the backend builds — independent of how
        a request arrives. With no env vars, forwarded headers are trusted
        only when the immediate peer is trusted.

        Both args are optional so the same resolver serves callers that have
        no request in hand (e.g. ``start_workspace`` at boot). With no
        headers the request branches are skipped and the env vars are the
        sole source, falling back to bare ``localhost`` / ``http`` / ``""``.

        #2732: a loopback hostname that was NOT taken from the env pin or
        a trusted ``X-Forwarded-Host`` is a synthetic local value (a UDS
        CLI handshake sends ``Host: localhost``; the no-request floor is
        bare ``localhost``). Every URL this resolver feeds is
        browser-facing, and browsers reach the deployment through the
        browser listener (``KLANGKD_PORT``), so the configured browser port
        is appended — see :meth:`browser_listener_hostname`.
        """
        hostname = self.app.state.settings.hosting_hostname
        proto = self.app.state.settings.hosting_proto
        base_path = self.app.state.settings.hosting_base_path
        trust = self._forwarded_headers_trusted(headers, client_host)
        hostname = self._hosting_hostname(headers, hostname, trust)
        if not proto:
            proto = self._hosting_proto(headers, trust)
        if base_path is None:
            base_path = self._hosting_base_path(headers, trust)
        return hostname, proto, base_path

    def _hosting_hostname(
        self, headers, env_hostname: str, trust: bool
    ) -> str:
        """Resolve the hosting hostname: env pin, else trusted
        X-Forwarded-Host, else the Host header, else ``localhost``. A
        synthetic (non-pinned, non-forwarded) loopback hostname is pointed
        at the browser listener (#2732)."""
        pinned = bool(env_hostname)
        hostname, forwarded = self._hostname_source(
            headers, env_hostname, trust
        )
        if not pinned and not forwarded:
            hostname = self.browser_listener_hostname(hostname)
        return hostname

    def _hostname_source(
        self, headers, env_hostname: str, trust: bool
    ) -> tuple[str, bool]:
        """(hostname, came-from-a-trusted-forwarded-header)."""
        if env_hostname:
            return env_hostname, False
        if headers is None:
            return "localhost", False
        return self._hostname_from_headers(headers, trust)

    def _hostname_from_headers(self, headers, trust: bool) -> tuple[str, bool]:
        """Trusted X-Forwarded-Host when present (flagged True), else the
        raw Host header, else ``localhost``."""
        if trust:
            forwarded_host = headers.get("x-forwarded-host")
            if forwarded_host:
                return forwarded_host, True
        return headers.get("host") or "localhost", False

    @staticmethod
    def _hosting_proto(headers, trust: bool) -> str:
        """Resolve the proto: trusted X-Forwarded-Proto, else ``http``."""
        if headers is not None and trust:
            return headers.get("x-forwarded-proto") or "http"
        return "http"

    @staticmethod
    def _hosting_base_path(headers, trust: bool) -> str:
        """Resolve the base path: trusted X-Forwarded-Prefix, else ``""``."""
        if headers is not None and trust:
            return headers.get("x-forwarded-prefix") or ""
        return ""

    def browser_listener_hostname(self, hostname: str) -> str:
        """Point a synthetic loopback hostname at the browser listener (#2732).

        ``/hosted/`` and every other browser-facing surface live on the
        browser listener (``KLANGKD_PORT``), never on the container-egress
        listener or the backend UDS. The synthetic loopback values this
        resolver produces when no operator intent is available (the
        no-request floor ``localhost``, and the ``Host: localhost`` a CLI
        sends over the backend UDS) name port 80 — a URL no deployment
        serves. When a browser listener is configured, append its port.

        No-op otherwise: a hostname that already carries a port (a direct
        browser request to the browser listener), a non-loopback hostname
        (carries remote intent; never rewritten), or headless mode
        (``KLANGKD_PORT`` unset — no browser listener exists to point at).
        The loopback test is :func:`is_portless_loopback_host` —
        case-insensitive, whole 127.0.0.0/8 range, ``::1`` bracketed.
        """
        port = self.app.state.settings.port
        if not port or not is_portless_loopback_host(hostname):
            return hostname
        return f"{hostname}:{port}"

    def cors_origins(self) -> list[str]:
        """Build the CORS allowed-origins list.

        Priority: KLANGKD_CORS_ORIGINS (comma-separated) > derived from the
        hosting env vars > the derived localhost authority (bare in headless
        mode, ``localhost:<KLANGKD_PORT>`` when a browser listener exists,
        #2732).

        Consistent with hosted-app URL construction: the port comes from
        KLANGKD_HOSTING_HOSTNAME (which carries host[:port]); it is never
        synthesized from KLANGKD_EGRESS_PORT (that is internal container
        wiring, not the browser origin). Origins carry no path, so
        KLANGKD_HOSTING_BASE_PATH is ignored here.
        """
        explicit = self.app.state.settings.cors_origins
        if explicit:
            return [o.strip() for o in explicit.split(",") if o.strip()]
        hostname, proto, _ = self.derive_hosting_info(None, None)
        return [f"{proto}://{hostname}"]

    def bridge_idle_timeout_for(self, workspace: dict | None) -> float:
        """Resolve the bridge idle timeout for a specific workspace (#864).

        Precedence: workspace ``settings.bridge_timeout`` override >
        ``KLANGKD_BRIDGE_TIMEOUT_SECONDS`` deploy default > 30.0s. Returns
        the resolved value as a float (always non-None — a stream always
        has some bound). A garbage deploy value is swallowed to the 30.0s
        default, matching the historical behavior.
        """
        raw = self.app.state.settings.bridge_timeout_seconds
        try:
            deploy_default = float(raw) if raw else None
        except (TypeError, ValueError):
            deploy_default = None
        resolved = bridge_ws_settings.resolve_bridge_timeout(
            workspace, deploy_default
        )
        return float(resolved) if resolved is not None else 30.0


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
        except asyncio.QueueFull:
            pass
