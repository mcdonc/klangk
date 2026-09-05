"""Python-owned reverse-proxy: Caddy engine (#1559).

This is klangkd's reverse-proxy engine (Caddy, the sole engine since
#1642). It has two responsibilities — render the proxy config from the
merged settings, and supervise the Caddy child process.

The two design choices behind this engine (see
issue #1559):

- **Config is delivered over Caddy's admin API, not rendered to a file on
  disk.** :class:`CaddyRenderer` produces a **Caddyfile string**
  (human-readable), and :class:`CaddyWatchdog` pushes it
  to a running Caddy via ``POST /load`` with ``Content-Type: text/caddyfile``
  (Caddy adapts it to JSON internally). There is no on-disk source of truth,
  no SIGHUP, no reload dance — a settings change is a fresh ``POST /load``.

- **The admin endpoint is a ``klangkd``-owned Unix domain socket.** Caddy is
  bootstrapped with ``CADDY_ADMIN=unix//<data_dir>/caddy-admin.sock|0600``
  (empty config, pinned to ``/dev/null`` so an accidental CWD ``Caddyfile``
  can't override it), so the only way to reach the admin API is via a
  process that can open that owner-only socket — i.e. ``klangkd`` and its
  children. No auth token / mTLS / loopback-TCP surface. The rendered
  Caddyfile re-declares ``admin unix//...`` in its global options so the
  binding survives reloads; the owner-only mode is enforced by the watchdog
  via ``os.chmod`` (Caddy's ``|0600`` address suffix is version-fragile, #1709).

The renderer is a pure function of the merged config (settings + the same
host-IP auto-detection probe). It takes the upstream
dial target as a parameter so tests can pass a TCP address while production
passes a ``unix//<socket>`` address. The pure host-IP / loopback helpers
live in this module (folded in from the former proxy_common.py, #2088).

Out of scope here (tracked in #1559): the ``caddy-l4`` layer-4 plugin
(everything klangk proxies is HTTP), and per-route live JSON mutations on
``/config/.../routes`` (Phase 3 — Phase 1 uses full-config ``POST /load``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import ctypes.util
import hashlib
import html.parser
import ipaddress
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from .container.basics import DEFAULT_PORTS_PER_WORKSPACE

# Pure host-IP / loopback probes + fallback subnets, folded into caddy (the
# sole engine) from the former proxy_common.py (#2088). Both auto-detect the
# pasta-NAT container source set the same way.
_LOOPBACK_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _is_loopback(addr: str) -> bool:
    """True for any address in 127.0.0.0/8 or ::1."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _LOOPBACK_NETS)


def _split_sources(raw: str) -> list[str]:
    """The stripped, non-empty entries of a comma-separated setting."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def _caddy_parseable_cidr(token: str) -> bool:
    """True when *token* parses the way Caddy's provisioner parses IPs
    and CIDRs (Go ``netip``): an IP, or ``IP/<prefix-length>`` with an
    ASCII-decimal prefix length.

    Python's :func:`ipaddress.ip_network` additionally accepts
    dotted-quad netmask/hostmask notation (``10.0.0.0/255.255.0.0``),
    which Go's ``netip.ParsePrefix`` rejects — and Caddy's *adapt*
    step passes those through, so the failure lands at ``POST /load``
    provision time (the exact kill/respawn wedge this validator
    exists to prevent; nginx accepts netmask notation, so a
    copy-pasted ``allow`` line hits it). The suffix check is
    ASCII-only because ``str.isdigit()`` admits non-ASCII decimal
    digits that Go's ``strconv.ParseUint`` rejects.
    """
    try:
        ipaddress.ip_network(token, strict=False)
    except ValueError:
        return False
    if "/" not in token:
        return True
    suffix = token.split("/", 1)[1]
    return suffix.isascii() and suffix.isdigit()


def _valid_cidr_tokens(tokens: list[str]) -> list[str]:
    """The tokens Caddy can consume as an IP address or CIDR.

    An invalid entry is warned and skipped: garbage would otherwise
    flow into the Caddyfile (a ``remote_ip`` matcher or
    ``trusted_proxies static`` argument), where Caddy rejects it at
    provision time — the ``POST /load`` fails and the watchdog's
    kill/respawn loop wedges the whole proxy on a typo'd setting.
    Skipping fails toward *less* access (narrower egress allowlist /
    narrower XFF trust), never more. The warning deliberately does not
    echo the entry value: the fields are env-sourced, and clear-text
    logging them is flagged by CodeQL
    (py/clear-text-logging-sensitive-data) — the operator re-reads
    their own short config list to spot the offender.
    """
    valid: list[str] = []
    for token in tokens:
        if _caddy_parseable_cidr(token):
            valid.append(token)
        else:
            logger.warning(
                "ignoring an invalid IP/CIDR entry — entries must be"
                " IPs or CIDRs (KLANGKD_TRUSTED_PROXY_CIDRS /"
                " KLANGKD_CONTAINER_SUBNETS)"
            )
    return valid


def _non_loopback(entries: list[str]) -> list[str]:
    """The non-loopback subset (loopback keeps full browser UI/API access)."""
    return [s for s in entries if not _is_loopback(s)]


def _warned_non_loopback(entries: list[str]) -> list[str]:
    """The non-loopback subset, warning when it is empty (the catch-all
    location then denies nothing — deny-by-default inactive)."""
    deny_entries = _non_loopback(entries)
    if not deny_entries:
        logger.warning(
            "container source set has no non-loopback entries — "
            "catch-all location / denies nothing (deny-by-default inactive)"
        )
    return deny_entries


def _explicit_source_entries(
    entries: list[str],
) -> tuple[list[str], list[str]]:
    """(acl, deny) from the explicit (pre-validated)
    KLANGKD_CONTAINER_SUBNETS entries."""
    return entries, _warned_non_loopback(entries)


def detect_host_ipv4s() -> list[str]:
    """Auto-detect this host's IPv4 addresses (the pasta-NAT container source set).

    Podman rootless default (pasta) shares the host network via userspace NAT,
    so container traffic to ``host.containers.internal`` arrives from the
    host's own IPv4. ``ip -4 addr show`` lists them (including 127.0.0.1 from
    ``lo`` — wanted for CONTAINER_ACL, filtered out of CONTAINER_DENY below).
    Returns ``[]`` on failure (caller falls back to RFC1918 ranges).
    """
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError, FileNotFoundError):
        return []
    addrs: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("inet "):
            # "inet 192.168.1.5/24 ..."
            token = line.split()[1]
            ip = token.split("/")[0]
            addrs.append(ip)
    return addrs


# Fallback subnets when auto-detection yields nothing:
# 172.16/12 + 10/8 (common container ranges), explicitly NOT 192.168/16
# (most common LAN range — allowing it would expose the LLM proxy to peers).
_FALLBACK_ACL_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8", "127.0.0.1"]
_FALLBACK_DENY_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8"]


# Fork-time preexec for the Caddy child: new session (for killpg) +
# PR_SET_PDEATHSIG (auto-SIGTERM if klangkd dies, #1533).
_PR_SET_PDEATHSIG = 1
_HAS_PDEATHSIG = sys.platform == "linux"
if _HAS_PDEATHSIG:  # pragma: no cover  – linux-only
    _libc = ctypes.CDLL(
        ctypes.util.find_library("c") or "libc.so.6", use_errno=True
    )


def _proxy_preexec() -> None:  # pragma: no cover  – runs in forked child
    os.setsid()
    if _HAS_PDEATHSIG:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


logger = logging.getLogger(__name__)


class AutoHttpsConfigError(ValueError):
    """Automatic TLS is armed but the environment cannot serve it (#3192).

    Raised when the armed config cannot be rendered/loaded (e.g. an older
    caddy that rejects the required global options). The boot path turns
    it into a fatal startup error; the SIGHUP path logs it at ERROR and
    keeps the last-known-good config running — either way it is never a
    silent fallthrough to plain HTTP.
    """


# ---------------------------------------------------------------------------
# Frontend hardening headers (#3149, #3219)
# ---------------------------------------------------------------------------


class _InlineScriptExtractor(html.parser.HTMLParser):
    """Collect the raw text of every inline ``<script>`` block (no ``src``).

    ``HTMLParser`` switches to CDATA (raw-text) mode inside ``script``
    elements, so :meth:`handle_data` receives the text-between-tags the
    browser hashes for a CSP ``'sha256-…'`` source token (after the HTML
    input stream's newline normalization, mirrored by the ``\r``-folding in
    :func:`inline_script_hash_tokens`). ``src``-bearing scripts are skipped
    — external files load under ``'self'`` and never consult a hash.
    Truly empty blocks (``<script></script>``) still yield a token: the
    browser runs the CSP check on them too. One known divergence from the
    spec's script-data *escaped* states (``<!--`` / ``<script`` double-
    escaping): the parser ends the element at the first ``</script>``. The
    drift is fail-closed — a mismatched hash blocks the script (broken
    boot, loudly), never an unintended allow — and is unreachable for
    Flutter-generated ``index.html``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._inline = False
        self._saw_data = False

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "script" and "src" not in dict(attrs):
            self._inline = True
            self._saw_data = False

    def handle_endtag(self, tag) -> None:
        if tag == "script":
            if self._inline and not self._saw_data:
                self.blocks.append("")
            self._inline = False

    def handle_data(self, data) -> None:
        if self._inline:
            self.blocks.append(data)
            self._saw_data = True


def inline_script_hash_tokens(html_text: str) -> list[str]:
    """CSP ``'sha256-…'`` source tokens for *html_text*'s inline scripts.

    Each token is the base64 SHA-256 of the script's text as the browser
    sees it — i.e. after the HTML input stream's newline normalization
    (``\r\n`` and lone ``\r`` fold to ``\n``), applied here so direct calls
    on CR-bearing text match too (:func:`csp_policy`'s ``read_text``
    universal-newline translation normalizes identically, making this
    idempotent there). With these tokens in ``script-src`` the served
    policy needs no ``'unsafe-inline'`` allowance for scripts (#3219).
    """
    extractor = _InlineScriptExtractor()
    extractor.feed(html_text)
    extractor.close()
    return [
        "sha256-"
        + base64.b64encode(
            hashlib.sha256(
                b.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
            ).digest()
        ).decode("ascii")
        for b in extractor.blocks
    ]


def csp_policy(frontend_dir: str | Path) -> str:
    """The Content-Security-Policy served on the browser listener's paths.

    Locked to first-party resources: the SPA's scripts, styles, images,
    fonts, workers, and WebSocket connections all stay same-origin, so
    every fetch directive is ``'self'`` (plus the tokens the Flutter web
    build genuinely needs — ``'wasm-unsafe-eval'`` for CanvasKit/skwasm's
    ``WebAssembly`` compile, ``data:``/``blob:`` images, ``'unsafe-inline'``
    **styles only**: Flutter injects runtime styles, and style injection is
    not a script-execution vector). The inline ``<script>`` blocks in the
    served ``index.html`` are allowed by SHA-256 hash tokens computed
    here — at Caddyfile-emit time, from the live ``frontend_dir`` (#3219) —
    so dropping ``'unsafe-inline'`` from ``script-src`` costs nothing: the
    blocks are static at build time. A frontend rebuild that alters them
    needs only a proxy reload (a SIGHUP settings swap triggers exactly
    that) to re-hash. When ``index.html`` is absent or unreadable the
    tokens are simply omitted — still no ``'unsafe-inline'`` (strictest
    posture; there is no UI to serve then).

    Same-origin ``ws:``/``wss:`` upgrades of the page origin are covered by
    ``'self'`` (CSP3), so the workspace WebSocket needs no bare scheme-source
    — and a bare ``ws:``/``wss:`` would permit a compromised script to open
    websockets to any host on the internet. No ``unsafe-eval`` (the
    beep/boingball features no longer JS-``eval``), no third-party origins
    (Roboto Mono is self-hosted, so fonts.gstatic.com is gone).
    ``frame-ancestors 'none'`` + X-Frame-Options DENY is the clickjacking
    posture. ``require-trusted-types-for 'script'`` (#3219) closes the
    DOM-based XSS sinks (``innerHTML`` & co., string ``script.src``, string
    ``eval``): they only accept TrustedTypes values routed through a
    policy. Two sanctioned policies cover the shipped frontend — the
    Flutter loader's own named ``'flutter-js'`` policy, and a minimal
    **default** policy inlined in ``index.html`` (``createScriptURL``
    only, permitting relative URLs, same-origin absolute URLs, and
    same-origin ``blob:`` URLs) that pdfrx's pdfium loader needs
    (plain-string ``script.src`` for the ``pdfium_client.js`` asset and
    a ``blob:`` wasm-worker URL). No ``createHTML``/``createScript``
    escape is defined, so markup and eval sinks stay blocked. The e2e
    ``csp-console.spec.ts`` asserts a violation-free console across
    login, workspace, terminal, files, and PDF flows, and positively
    verifies the pdfium script/Worker (the two TT sinks) came up.
    """
    try:
        html_text = (
            Path(frontend_dir)
            .joinpath("index.html")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError):
        # Unreadable OR non-UTF-8 (a ValueError, not an OSError — a
        # windows-1252 byte would otherwise raise out of the renderer and
        # wedge the watchdog in a kill/respawn loop). Either way: strict
        # hash-less policy + a loud breadcrumb for the operator, whose
        # symptom would otherwise be a blank page with only a browser-side
        # "Refused to execute inline script" console error to go on.
        logger.warning(
            "index.html unreadable or not UTF-8 under %s — serving the "
            "CSP without inline-script hash tokens (inline scripts will "
            "be blocked until this is fixed)",
            frontend_dir,
        )
        html_text = ""
    script_src = "script-src 'self' 'wasm-unsafe-eval'"
    tokens = inline_script_hash_tokens(html_text)
    if tokens:
        # Hash sources are quoted tokens: 'sha256-<b64>' (CSP3 grammar —
        # an unquoted sha256-… parses as a host-source and is ignored).
        script_src += " " + " ".join(f"'{t}'" for t in tokens)
    return (
        "default-src 'self'; "
        f"{script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "require-trusted-types-for 'script'"
    )


#: Browser-listener paths the hardening headers must NOT touch: the API,
#: the WebSocket endpoints, and the hosted-ports proxy (deployer-controlled
#: apps behind /hosted/ — a CSP imposed there could break them).
_CSP_EXCLUDED_PATHS = "/api /api/* /ws /ws/* /hosted /hosted/*"


def csp_block(policy: str) -> str:
    """The site-level ``header`` directives serving *policy*.

    Emitted into the browser site only (the egress listener serves
    containers, not documents). Caddy sorts ``header`` ahead of the
    ``handle`` blocks, so the headers land on whatever the matched request
    produces — the reverse-proxied backend response included.
    """
    return (
        f"	@frontend not path {_CSP_EXCLUDED_PATHS}\n"
        f'	header @frontend Content-Security-Policy "{policy}"\n'
        '	header @frontend X-Frame-Options "DENY"\n'
    )


def classify_caddy_line(line: str) -> tuple[int, str]:
    """Parse a Caddy JSON log line and return ``(log_level, message)``.

    Caddy emits structured JSON to stderr with ``level``, ``msg``, and
    optional ``logger`` fields.  This function maps them to Python log
    levels so klangkd can surface errors while suppressing routine
    startup noise (info/warn about TLS, HTTP/2, admin API, etc.).

    Non-JSON lines (e.g. panic stack traces) are treated as errors.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return logging.ERROR, line

    caddy_level = obj.get("level", "info")
    msg = obj.get("msg", line)
    caddy_logger = obj.get("logger", "")
    if caddy_logger:
        msg = f"[{caddy_logger}] {msg}"

    if caddy_level in ("error", "fatal", "panic"):
        return logging.ERROR, msg

    return logging.DEBUG, msg


def is_bind_error(line: str) -> bool:
    """Return True if *line* is a Caddy bind failure (admin, ingress, or egress).

    Caddy emits structured JSON to stderr when it can't bind a listener.
    Admin socket failures come from ``"logger":"admin"``; HTTP listener
    failures (ingress/egress ports) come from other loggers but contain
    the same bind-related keywords in the message. Detecting any of these
    lets the watchdog abort instead of respawning in a tight loop (#1917).
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    msg = (obj.get("msg") or "").lower()
    # Go's net package formats socket bind errors as
    # "bind: address already in use" or "bind: permission denied".
    return "address already in use" in msg or "bind: permission denied" in msg


# ---------------------------------------------------------------------------
# Upstream constructors (pure — no settings)
# ---------------------------------------------------------------------------


def uds_upstream(socket_path: str) -> str:
    """The Caddy ``reverse_proxy`` dial target for a UDS upstream (production).

    Caddy's UDS dial address is ``unix//path/to/sock`` — a literal ``unix//``
    prefix (two slashes) followed by the absolute socket path.
    """
    return f"unix//{socket_path}"


def tcp_upstream(host: str, port: str | int) -> str:
    """The ``reverse_proxy`` dial target for a TCP upstream (tests)."""
    return f"{host}:{port}"


# ---------------------------------------------------------------------------
# Admin API client (thin — ~the hand-rolled client #1559 settled on)
# ---------------------------------------------------------------------------


# Content-Type that makes Caddy's POST /load adapt a Caddyfile to JSON.
CADDYFILE_CONTENT_TYPE = "text/caddyfile"


async def post_load(
    admin_socket: str,
    caddyfile: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """Push ``caddyfile`` to Caddy's admin API (``POST /load``), full-config replace.

    Connects over the admin **Unix domain socket** (the only transport that
    can reach a UDS-bound admin endpoint — no loopback TCP). ``POST /load``
    atomically replaces the active config and blocks until the reload
    completes; on failure Caddy rolls back with zero downtime. Adapting the
    Caddyfile to JSON happens server-side thanks to the ``text/caddyfile``
    Content-Type.

    ``client`` is injectable so the unit suite can drive this against a fake
    without a running Caddy; in production the caller leaves it ``None`` and a
    short-lived UDS-backed client is constructed and closed here.
    """
    own_client = client is None
    if own_client:
        transport = httpx.AsyncHTTPTransport(uds=admin_socket)
        client = httpx.AsyncClient(transport=transport, timeout=timeout)
    try:
        resp = await client.post(
            "http://localhost/load",
            content=caddyfile,
            headers={"Content-Type": CADDYFILE_CONTENT_TYPE},
        )
        resp.raise_for_status()
        return resp
    finally:
        if own_client:
            await client.aclose()


# ---------------------------------------------------------------------------
# Renderer (settings-driven — owned instance)
# ---------------------------------------------------------------------------


class CaddyRenderer:
    """Settings-driven Caddy **Caddyfile** renderer (#1559).

    Constructed with ``app`` per the composition-root pattern, settings
    read live via
    ``self.app.state.settings`` (#1608). :meth:`render_config` returns a
    Caddyfile string covering the full proxy surface — two
    listeners, ``forward_auth`` token gate, IP matchers, ``request_body``
    max-size, UDS upstream, injected ``Authorization``.
    """

    def __init__(self, app) -> None:
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    # -- shared computation (Caddy-shaped output) ---

    def _container_source_entries(self) -> tuple[list[str], list[str]]:
        """Resolve the container source IP/CIDR set → ``(acl_entries, deny_entries)``.

        The container-source gate (the same set of sources regardless):

        - ``acl_entries``: every source, loopback included — drives the egress
          allowlist (containers connect from these IPs).
        - ``deny_entries``: non-loopback sources only — drives the browser
          catch-all guard. Loopback is excluded so a local browser keeps full
          UI/API access.
        """
        explicit = self.app.state.settings.container_subnets
        if explicit:
            tokens = _split_sources(str(explicit))
            entries = _valid_cidr_tokens(tokens)
            if len(entries) != len(tokens):
                logger.warning(
                    "KLANGKD_CONTAINER_SUBNETS: skipped %d invalid"
                    " entry/entries (the valid ones remain in effect)",
                    len(tokens) - len(entries),
                )
            return _explicit_source_entries(entries)
        addrs = detect_host_ipv4s()
        if addrs:
            return addrs, _non_loopback(addrs)
        logger.warning(
            "container subnet detection failed, using fallback RFC1918 ranges"
        )
        return list(_FALLBACK_ACL_SUBNETS), list(_FALLBACK_DENY_SUBNETS)

    def _egress_remote_ip_list(self) -> str:
        """The space-separated container-source set for the egress ``remote_ip`` matcher.

        The three container-egress locations allow ONLY container-source peers
        (deny everyone else). Caddy's ``remote_ip`` matcher is the right
        primitive: it keys on the immediate TCP peer (ignores
        ``trusted_proxies``), and egress is reached *directly* by containers
        via pasta NAT with no proxy in front to rewrite it.
        """
        acl_entries, _deny = self._container_source_entries()
        return " ".join(acl_entries)

    def _browser_deny_remote_ip_list(self) -> str:
        """The non-loopback container-source set for the browser catch-all deny.

        The browser catch-all refuses requests whose *immediate* TCP peer is a
        container source (pasta NAT) — capping brute-force surface (#1376) —
        but must NOT refuse a trusted proxy co-located on the host whose
        forwarded real client is a host IP (#1546). Caddy's ``remote_ip``
        matcher (immediate peer, ignores ``trusted_proxies``) is exactly
        nginx's ``$realip_remote_addr``: do **not** swap it for ``client_ip``
        (which would re-introduce the #1546 403).
        """
        _acl, deny_entries = self._container_source_entries()
        return " ".join(deny_entries)

    def _max_body_size(self) -> str:
        """Caddy ``request_body`` ``max_size`` from ``KLANGKD_FILE_UPLOAD_SIZE_MAX``.

        The setting is bytes (default 500 MB); Caddy's ``max_size`` accepts a
        size with a unit (``500MB``). Minimum 1MB.
        """
        # int-typed + validated at construction since #2603; None
        # (explicitly emptied) means the default.
        raw = self.app.state.settings.file_upload_size_max
        bytes_ = raw if raw is not None else 524288000
        mb = max(1, bytes_ // 1048576)
        return f"{mb}MB"

    def _reject_proxy_headers(self) -> bool:
        """True if KLANGKD_REJECT_PROXY_HEADERS is set (hard trust-off)."""
        raw = self.app.state.settings.reject_proxy_headers
        return bool(raw and str(raw).strip().lower() in ("1", "true", "yes"))

    def _trusted_proxy_cidrs(self) -> list[str]:
        """Validated KLANGKD_TRUSTED_PROXY_CIDRS entries (loopback if empty/invalid)."""
        entries = _valid_cidr_tokens(
            _split_sources(self.app.state.settings.trusted_proxy_cidrs or "")
        )
        return entries or ["127.0.0.1", "::1"]

    # -- automatic TLS (#3192) ---------------------------------------------

    @property
    def auto_https_armed(self) -> bool:
        """True when ``KLANGKD_TLS_HOSTNAME`` arms automatic TLS (#3192).

        Read live off settings (reloadable on SIGHUP — the watchdog's
        ``apply_pending_reload`` re-renders and re-POSTs the config, so an
        arm/disarm flows through without a restart).
        """
        return bool(self.app.state.settings.tls_hostname)

    def caddy_storage_dir(self) -> str:
        """Caddy's certificate storage dir: ``<state_dir>/caddy-storage``.

        Armed mode renders ``storage file_system <dir>`` so issued
        certificates + ACME account state survive restarts — without it
        Caddy defaults to ``~/.local/share/caddy``, which is wrong for a
        system-service klangkd (different $HOME, possibly wiped) and would
        re-issue on every restart, walking into CA rate limits. The
        watchdog mkdirs it before each config push.
        """
        return os.path.join(self.app.state.settings.state_dir, "caddy-storage")

    # -- global options ----------------------------------------------------

    def _global_block(
        self, admin_socket: str, *, full_global: bool = True
    ) -> str:
        """The global options block: admin UDS, HTTPS mode, storage, trust.

        - ``admin unix//...`` re-declares the admin endpoint on the
          klangkd-owned UDS so it survives every ``POST /load``.
        - ``auto_https off`` — **only when automatic TLS is not armed**
          (#3192). Unarmed (the default, outer-proxy or plain-HTTP
          deployments) keeps today's exact behavior: klangk serves plain
          HTTP because TLS terminates at an outer proxy or nowhere.
          Armed (``KLANGKD_TLS_HOSTNAME`` set) the directive is dropped
          so Caddy runs its ACME automation (HTTP-01 / TLS-ALPN, binding
          80/443 as needed), an explicit ``storage file_system`` keeps
          certificates under klangkd's state dir, and an ``email`` is
          registered with the CA when ``KLANGKD_ACME_EMAIL`` is set.
        - ``persist_config off`` — the admin API is the source of truth, not
          disk (mirrors the no-on-disk-config decision).
        - ``servers { trusted_proxies ... }`` when proxy-header trust is on —
          this is what makes ``{client_ip}`` resolve the real client from
          ``X-Forwarded-For`` (#1558), the Caddy equivalent of nginx's
          ``set_real_ip_from`` / ``real_ip_header`` realip directives.
          Suppressed entirely under ``KLANGKD_REJECT_PROXY_HEADERS`` (hard
          trust-off), in which case ``{client_ip}`` falls back to the
          immediate peer — matching nginx with no realip directives.
        """
        self._guard_armed_needs_full_global(full_global)
        lines = [
            # No |0600 mode suffix — only honored on Caddy >= 2.8; on older
            # Caddy it's folded into the socket path, breaking the bind
            # (#1709). Owner-only mode is enforced by the watchdog via
            # os.chmod (see CaddyWatchdog._wait_for_admin); Caddy doesn't
            # re-bind the admin on /load with an unchanged address, so one
            # chmod per bind persists across reloads.
            # origins localhost: explicitly allowlist the Host klangkd sends
            # over the UDS. Older Caddy (<2.11) defaults unix-socket admin
            # origins to [""] and rejects Host: localhost with 403, breaking
            # POST /load (#1709). Newer Caddy allows it by default — harmless.
            "	admin unix//" + admin_socket + " {",
            "		origins localhost",
            "	}",
        ]
        lines.extend(self._plain_http_directive())
        # persist_config + servers { trusted_proxies ... } are post-2.6.2
        # features (Ubuntu 24.04's apt caddy is 2.6.2; persist_config and the
        # servers/trusted_proxies option both postdate it, and
        # trusted_proxies_strict is 2.8+). The older system caddy rejects them
        # outright, refusing the whole config (#1709). Emit the full block only
        # when the detected caddy supports it (see CaddyWatchdog.start); else
        # fall back to the minimal block above (admin + auto_https). The cost
        # on older caddy: caddy autosaves the config (harmless — klangkd never
        # loads it, no --resume) and {client_ip} resolves the immediate peer
        # (no XFF parsing; fine for direct container/loopback connections).
        if full_global:
            if self.auto_https_armed:
                lines.extend(self._auto_https_global_directives())
            lines.append("	persist_config off")
            if not self._reject_proxy_headers():
                cidrs = " ".join(self._trusted_proxy_cidrs())
                lines.append("	servers {")
                lines.append(f"		trusted_proxies static {cidrs}")
                # trusted_proxies_strict: right-to-left XFF parsing (anti-
                # spoofing). The full_global probe includes it, so it's safe to
                # emit unconditionally here.
                lines.append("		trusted_proxies_strict")
                lines.append("	}")
        return "{\n" + "\n".join(lines) + "\n}\n"

    def _guard_armed_needs_full_global(self, full_global: bool) -> None:
        """Raise when automatic TLS is armed but the caddy can't load the
        full global block (#3192).

        An older system Caddy (e.g. Ubuntu 24.04's apt 2.6.2) rejects the
        full global block; the minimal fallback keeps ``auto_https off``
        baked in, which would silently serve plain HTTP — the exact failure
        arming exists to prevent. Fail loudly instead. The watchdog probes
        the binary before the first spawn, so boot aborts; a SIGHUP arm on
        an old Caddy surfaces as a failed reload (last-known-good config
        keeps running).
        """
        if not self.auto_https_armed or full_global:
            return
        raise AutoHttpsConfigError(
            "KLANGKD_TLS_HOSTNAME (automatic TLS) needs a Caddy "
            "new enough for the full global options block "
            "(persist_config / servers.trusted_proxies); the detected "
            "system Caddy is too old. Upgrade Caddy, or unset "
            "KLANGKD_TLS_HOSTNAME and terminate TLS at an outer "
            "proxy (docs/deployment/behind-a-proxy.md)."
        )

    def _internal_tls(self) -> bool:
        """True when the armed listener uses the internal (self-generated)
        certificate issuer — the TLS hop behind an outer proxy (#3192)."""
        return (
            self.app.state.settings.tls_issuer or ""
        ).strip().lower() == "internal"

    def _plain_http_directive(self) -> list[str]:
        """``auto_https off`` — emitted only in unarmed (plain-HTTP) mode.

        Armed mode returns nothing so Caddy runs its certificate automation
        (#3192); this split keeps :meth:`_global_block` at complexity rank A.
        """
        if self.auto_https_armed:
            return []
        return ["	auto_https off"]

    def _auto_https_global_directives(self) -> list[str]:
        """The armed-mode global directives (#3192).

        Both issuers pin an explicit certificate storage path under
        ``state_dir`` so issued material survives restarts instead of
        walking into CA rate limits (acme) or minting a fresh internal
        CA that no outer proxy trusts (internal). ACME mode additionally
        registers the CA account email when set; internal mode keeps
        the HTTP→HTTPS redirect off — the outer proxy redirects
        browsers itself, and the redirect's port-80 bind would fail
        for an unprivileged service user.
        """
        directives: list[str] = []
        if self._internal_tls():
            directives.append("\tauto_https disable_redirects")
        else:
            email = (self.app.state.settings.acme_email or "").strip()
            if email:
                directives.append(f"\temail {email}")
        directives.append(f"\tstorage file_system {self.caddy_storage_dir()}")
        return directives

    def _bootstrap_block(self, admin_socket: str) -> str:
        """Admin-only global block used as the child's initial ``--config``.

        The watchdog spawns Caddy with this as its ``--config`` (instead of
        ``/dev/null``) so Caddy binds the admin UDS at mode ``0600`` from the
        very first moment, on **any** Caddy version. With an empty config
        Caddy falls back to its default ``localhost:2019`` admin address, and
        ``CADDY_ADMIN`` only overrides that on Caddy >= 2.7 (it landed in
        caddy#5317) — but the watchdog runs the host's *system* Caddy
        (``shutil.which("caddy")``), which may be older, so the env var alone
        is unreliable and the child ends up on 2019, colliding with any other
        Caddy on the host and never serving the UDS the watchdog polls
        (#1709). The ``admin`` global option has been honored since Caddy
        v2.0, so a real bootstrap config is version-robust. Site blocks are
        deliberately absent — they arrive later via ``POST /load``, exactly
        as before; this only establishes the admin endpoint.
        """
        # Same admin directive as _global_block (unix//<sock> with origins
        # localhost — see _global_block); no mode suffix, no site blocks.
        return (
            "{\n"
            "	admin unix//" + admin_socket + " {\n"
            "		origins localhost\n"
            "	}\n"
            "}\n"
        )

    # -- shared reverse_proxy header bundle --------------------------------

    def _common_rp_headers(self) -> str:
        """The ``header_up`` lines every ``reverse_proxy`` shares.

        Caddy's ``reverse_proxy`` sets ``X-Forwarded-For``,
        ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` to derived values by
        default (its adaptor warns if you re-declare them), so we only set the
        two nginx needs that Caddy does *not* add:

        - ``X-Real-IP {client_ip}`` — the real client when a trusted proxy is
          in front (``trusted_proxies`` configured in :meth:`_global_block`),
          the immediate peer otherwise. This is the #1558 fix: the backend's
          IP-trust checks see the browser, not the outer proxy.
        - ``Host {host}`` — explicit (Caddy also defaults to this, but nginx
          sets it explicitly so we keep parity for eyeball-diffing).
        """
        return (
            "\t\t\theader_up Host {host}\n"
            "\t\t\theader_up X-Real-IP {client_ip}"
        )

    # -- egress locations --------------------------------------------------

    def _build_llm_block(self, upstream: str, guard: str) -> str:
        """The ``/llm-proxy/*`` location (#2073).

        Routes ``/llm-proxy/`` requests to the klangkd backend where the
        in-process ``litellm.Router`` handles them.  The container-source
        ACL guard and ``forward_auth`` workspace-token check (applied by
        the parent ``_egress_site``) protect the endpoint; no API key
        injection or URL rewriting is needed since the backend owns the
        LLM routing.
        """
        return (
            "	handle /llm-proxy/* {\n"
            f"{guard}"
            f"		reverse_proxy {upstream}\n"
            "	}\n"
        )

    def _egress_locations(self, upstream: str, container_srcs: str) -> str:
        """The container-egress locations, shared by headless and full modes.

        The ``forward_auth`` directive is the clean equivalent of nginx's
        ``auth_request``: a GET subrequest to the workspace-token verifier,
        forwarding the original ``Authorization`` header; on 2xx the proxied
        request proceeds, on 401 the verifier's response (JSON body +
        ``X-Token-Error``) is returned to the client. Every egress location
        additionally allows only container-source peers (``@notContainerSrc``
        → 403) — the same CONTAINER_ACL nginx enforces.
        """
        if container_srcs:
            not_src_matcher = (
                f"	@notContainerSrc not remote_ip {container_srcs}\n"
            )
            guard = "		respond @notContainerSrc 403\n"
        else:
            # No container sources at all → fail-closed (deny all egress),
            # matching nginx's bare ``deny all;`` with no allows.
            not_src_matcher = ""
            guard = "		respond 403\n"
        llm = self._build_llm_block(upstream, guard)
        delegate = (
            "	handle /api/v1/browser-delegate {\n"
            f"{guard}"
            f"		reverse_proxy {upstream} {{\n"
            f"{self._common_rp_headers()}\n"
            "			flush_interval -1\n"
            "		}\n"
            "	}\n"
        )
        # #2319: the sidecar's egress-sidecar WebSocket (held-egress verdicts).
        # The site-level forward_auth validates the sidecar's workspace JWT,
        # then this handle proxies the WS upgrade to the app. The sidecar sends
        # the JWT as an ``Authorization: Bearer`` header (not a query param) so
        # forward_auth sees it; ``flush_interval -1`` avoids buffering the
        # bidirectional verdict stream.
        egress_ws = (
            "	handle /ws/egress-sidecar {\n"
            f"{guard}"
            f"		reverse_proxy {upstream} {{\n"
            f"{self._common_rp_headers()}\n"
            "			flush_interval -1\n"
            "		}\n"
            "	}\n"
        )
        return not_src_matcher + egress_ws + llm + delegate

    def _egress_site(self, upstream: str, container_srcs: str) -> str:
        """The full container-egress site block (headless + full both render it)."""
        egress_port = self.app.state.settings.egress_port
        egress_listen = self.app.state.settings.egress_listen
        locations = self._egress_locations(upstream, container_srcs)
        return (
            f"http://:{egress_port} {{\n"
            f"	bind {egress_listen}\n"
            f"	request_body {{\n"
            f"		max_size {self._max_body_size()}\n"
            f"	}}\n"
            f"	# WS upgrades bypass forward_auth: Caddy copies the Upgrade\n"
            f"	# headers onto the auth subrequest, making IT a websocket, so\n"
            f"	# uvicorn routes the (HTTP) verify endpoint as a WS -> no match\n"
            f"	# -> StaticFiles 500. The egress WS endpoint (/ws/egress-sidecar)\n"
            f"	# self-authenticates via the Authorization header instead.\n"
            f"	@notWs {{\n"
            f"		not header Upgrade websocket\n"
            f"	}}\n"
            f"	forward_auth @notWs {upstream} {{\n"
            f"		uri /api/v1/auth/verify-workspace-token\n"
            f"	}}\n"
            f"{locations}}}\n"
        )

    # -- browser-only locations -------------------------------------------

    def _build_hosted_block(self) -> str:
        """The ``/hosted/<ws>/<port>/`` proxy (or nothing when disabled).

        Disabled entirely when ``KLANGKD_HOSTED_PORTS_PER_WORKSPACE`` is
        exactly 0 (mirrors the backend's ``ports_per_workspace_cap()``,
        #1237): a bare ``respond 404`` catch for ``^/hosted/``.

        Otherwise two matchers (mirroring the nginx ``location`` pair):

        - slash-less ``/hosted/<ws>/<port>`` → ``308`` redirect to the
          canonical trailing-slash form (so relative asset paths resolve);
        - ``/hosted/<ws>/<port>/<rest...>`` → strip the prefix and proxy to
          ``127.0.0.1:<port>`` (WebSocket upgrade is automatic in
          ``reverse_proxy``).
        """
        # int-typed since #2603; None (explicitly emptied) means the
        # default, 0 disables hosted ports. Compare against None so a
        # legitimate 0 is not swallowed by an `or`.
        _ports = self.app.state.settings.hosted_ports_per_workspace
        if (
            _ports if _ports is not None else DEFAULT_PORTS_PER_WORKSPACE
        ) == 0:
            return "	handle /hosted/* {\n		respond 404\n	}\n"
        return (
            "	@hostedSlashless path_regexp hostedsl ^/hosted/[^/]+/([0-9]+)$\n"
            "	handle @hostedSlashless {\n"
            "		redir {uri}/ 308\n"
            "	}\n"
            "	@hosted path_regexp hosted ^/hosted/[^/]+/([0-9]+)/(.*)$\n"
            "	handle @hosted {\n"
            "		rewrite * /{re.hosted.2}\n"
            "		reverse_proxy 127.0.0.1:{re.hosted.1}\n"
            "	}\n"
        )

    def _browser_site(
        self,
        upstream: str,
        container_srcs_deny: str,
    ) -> str:
        """The browser-listener site block (full mode only).

        Armed (``KLANGKD_TLS_HOSTNAME`` set, #3192) the site address is
        ``https://<host>:<port>`` so Caddy's automatic HTTPS manages the
        certificate for the name (ACME HTTP-01 / TLS-ALPN for the default
        issuer; a self-generated internal-CA certificate — the TLS hop
        behind an outer proxy — for ``tls-issuer: internal``, which also
        carries the ``tls internal`` site directive). Unarmed it stays
        ``http://:<port>`` — byte-identical to the pre-#3192 render.
        Everything else inside the block (bind, request_body, CSP, ACLs,
        routes) is identical either way.
        """
        csp = csp_policy(self.app.state.settings.frontend_dir)
        listen_addr = self.app.state.settings.listen
        port = self.app.state.settings.port
        fqdn = self.app.state.settings.tls_hostname
        if fqdn:
            site_addr = f"https://{fqdn}:{port}"
            tls_line = "	tls internal\n" if self._internal_tls() else ""
            self._warn_loopback_listen_when_armed(listen_addr)
        else:
            site_addr = f"http://:{port}"
            tls_line = ""
        hosted = self._build_hosted_block()
        if container_srcs_deny:
            deny_matcher = (
                f"	@containerSrc remote_ip {container_srcs_deny}\n"
            )
            deny_guard = "		respond @containerSrc 403\n"
        else:
            # No non-loopback container sources → nothing to deny on the
            # catch-all (local browsers + remotes all pass through); nginx's
            # geo ``default 0`` is the equivalent (never flags).
            deny_matcher = ""
            deny_guard = ""
        # nginx uses ``location =`` (exact) for /auth/local; mirror it
        # with a ``path`` matcher (exact by default) so only the bare
        # endpoint matches, not sub-paths.
        auth_local = (
            "	@notLoopback not remote_ip 127.0.0.1 ::1\n"
            "	@authlocal path /api/v1/auth/local\n"
            "	handle @authlocal {\n"
            "		respond @notLoopback 403\n"
            f"		reverse_proxy {upstream} {{\n"
            f"{self._common_rp_headers()}\n"
            "		}\n"
            "	}\n"
        )
        catch_all = (
            "	handle {\n"
            f"{deny_guard}"
            f"		reverse_proxy {upstream} {{\n"
            f"{self._common_rp_headers()}\n"
            "		}\n"
            "	}\n"
        )
        return (
            f"{site_addr} {{\n"
            f"{tls_line}"
            f"	bind {listen_addr}\n"
            f"	request_body {{\n"
            f"		max_size {self._max_body_size()}\n"
            f"	}}\n"
            f"{csp_block(csp)}"
            f"{deny_matcher}"
            f"{hosted}"
            f"{auth_local}"
            f"{catch_all}}}\n"
        )

    def _warn_loopback_listen_when_armed(self, listen_addr: str) -> None:
        """Warn when TLS is armed but the listener stays loopback-only
        (#3192). Consequences differ by issuer: ACME additionally cannot
        answer its challenge, while the internal issuer only loses
        reachability from the outer proxy — which may be co-located on
        the same host, so that shape is only warned about softly."""
        if listen_addr.strip() not in ("127.0.0.1", "::1", "localhost"):
            return
        if self._internal_tls():
            logger.warning(
                "KLANGKD_TLS_HOSTNAME is set (internal TLS) but "
                "KLANGKD_LISTEN is loopback-only (%s) — unreachable from "
                "an outer proxy on another host. Set KLANGKD_LISTEN (e.g. "
                "0.0.0.0) unless the proxy runs on this same host.",
                listen_addr,
            )
            return
        logger.warning(
            "KLANGKD_TLS_HOSTNAME is set (automatic TLS) but "
            "KLANGKD_LISTEN is loopback-only (%s) — the HTTPS listener "
            "is unreachable from other hosts and the ACME challenge "
            "cannot be answered, so certificate issuance will fail. "
            "Set KLANGKD_LISTEN (e.g. 0.0.0.0) for an internet-facing "
            "deployment.",
            listen_addr,
        )

    # -- main renderer -----------------------------------------------------

    def render_config(
        self, upstream: str, admin_socket: str, *, full_global: bool = True
    ) -> str:
        """Render the Caddyfile as a string.

        ``upstream`` is the Caddy ``reverse_proxy`` dial target for the
        backend (:func:`uds_upstream` for the production socket,
        :func:`tcp_upstream` for tests); ``admin_socket`` is the path of the
        admin UDS, re-declared in the global block so the binding survives
        reloads. Template selection keys off ``KLANGKD_PORT`` (#1542):
        **unset** ⇒ headless (egress listener only); **set** ⇒ full (browser
        + egress listeners). All other values come from the merged settings
        plus the host-IP auto-detection probe.
        """
        global_block = self._global_block(
            admin_socket, full_global=full_global
        )
        egress = self._egress_site(upstream, self._egress_remote_ip_list())
        if self.app.state.settings.port is None:
            return global_block + egress
        browser = self._browser_site(
            upstream, self._browser_deny_remote_ip_list()
        )
        return global_block + egress + browser

    # -- binary location ---------------------------------------------------

    def find_proxy_bin(self) -> str:
        """Locate the caddy binary: KLANGKD_PROXY_BIN > PATH > /usr/bin/caddy.

        ``KLANGKD_PROXY_BIN`` overrides for both engines; the Caddy fallbacks
        are caddy-specific (``shutil.which("caddy")`` → ``/usr/bin/caddy``).
        """
        configured = self.app.state.settings.proxy_bin
        if configured:
            return str(configured)
        found = shutil.which("caddy")
        if found:
            return found
        return "/usr/bin/caddy"


# ---------------------------------------------------------------------------
# Process supervision
# ---------------------------------------------------------------------------


def caddy_supports_full_global_block(bin_path: str) -> bool:
    """True if the caddy binary adapts klangkd's full global options block.

    klangkd's global block uses features that postdate the older system caddy a
    stock CI runner apt-installs (Ubuntu 24.04 ships caddy 2.6.2):
    ``persist_config`` and the ``servers { trusted_proxies ... }`` option both
    postdate 2.6.2, and ``trusted_proxies_strict`` is 2.8+. Rather than gate
    each by a fragile patch-level version map, probe the actual binary — feed a
    representative full global block to ``caddy adapt`` and check it parses. If
    not, the watchdog falls back to a minimal global block (admin + auto_https
    only) so klangkd loads on the older caddy too (#1709).
    """
    probe = (
        "{\n"
        "\tadmin unix//tmp/caddy-feature-probe.sock {\n"
        "\t\torigins localhost\n"
        "\t}\n"
        "\tauto_https off\n"
        "\tpersist_config off\n"
        "\tservers {\n"
        "\t\ttrusted_proxies static 127.0.0.1\n"
        "\t\ttrusted_proxies_strict\n"
        "\t}\n"
        "}\n"
    )
    probe_path: str | None = None
    try:
        # Write the probe to a real file — `caddy adapt --config -` (stdin)
        # only landed after 2.8, so reading stdin would conflate "feature
        # unsupported" with "stdin unsupported" on older caddy (#1709).
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".Caddyfile", delete=False
        ) as f:
            f.write(probe)
            probe_path = f.name
        r = subprocess.run(
            [
                bin_path,
                "adapt",
                "--adapter",
                "caddyfile",
                "--config",
                probe_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # probe failed to run — assume supported (rare; preserves features)
    finally:
        if probe_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(probe_path)


class CaddyWatchdog:
    """Owns the Caddy child process and pushes config over its admin API (#1559).

    CaddyWatchdog supervises the Caddy child. Instead of
    rendering a config file and pointing Caddy at it with ``-c``, this:

    1. bootstraps Caddy with ``CADDY_ADMIN=unix//<sock>`` (empty config, no
       file) — the admin endpoint comes up on a klangkd-owned UDS;
    2. waits for the admin UDS to accept a connection;
    3. pushes the rendered Caddyfile via :func:`post_load` (``POST /load``,
       ``text/caddyfile``) — full-config replace.

    On every respawn the Caddyfile is re-applied (config lives only in memory
    until ``/load`` runs). Constructed with ``app``; settings read live via
    ``self.app.state.settings`` (#1608). Stored on ``app.state.proxy_watchdog``
    (constructed in :func:`klangk.main.build_app`); the lifespan calls ``.start()`` /
    ``.stop()``.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._renderer = CaddyRenderer(app)
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Whether the caddy binary supports the full global block (persist_config
        # + servers/trusted_proxies/strict) — probed in start(). Defaults True
        # (feature-preserving) until then / if the probe can't run. See
        # caddy_supports_full_global_block (#1709).
        self._full_global: bool = True
        # Flagged by reconfigure() on a SIGHUP settings swap; applied
        # async by apply_pending_reload() (POST /load can't run in the
        # sync reconfigure loop). #1559 Phase 1: a settings change is a
        # fresh POST /load, not stale-until-restart.
        self._pending_reload = False
        # Set by _relay_stderr when Caddy emits a bind error (admin socket,
        # ingress, or egress port). The _watch loop checks this after the
        # process exits and aborts instead of respawning (#1917).
        self._bind_fatal = False

    def reconfigure(self, app) -> None:
        self.app = app
        self._renderer.reconfigure(app)
        # The SIGHUP path swapped in new settings; flag that the running
        # Caddy needs a fresh POST /load (applied async after the sync
        # reconfigure loop). No-op when the watchdog never started
        # (_KLANGKD_DISABLE_PROXY) — the flag is just never applied.
        self._pending_reload = True

    def _log_reload_failure(self, exc: Exception) -> None:
        """A failed SIGHUP reload: ERROR while armed, WARNING otherwise.

        Any failed reload with the live settings armed — refused at render
        time (``AutoHttpsConfigError``) or rejected by caddy at
        ``POST /load`` — leaves the same mismatch: settings say TLS is on
        while caddy keeps serving the last-known-good (plain-HTTP) config.
        That must be louder than a generic warning; a failed DISARM only
        keeps more TLS than configured, so it stays a warning (#3192).
        """
        if isinstance(exc, AutoHttpsConfigError) or (
            self._renderer.auto_https_armed
        ):
            logger.error(
                "SIGHUP: automatic TLS could not be applied — the running "
                "config is unchanged and the browser listener is still "
                "serving the previous scheme. Fix and reload again: %s",
                exc,
            )
        else:
            logger.warning(
                "caddy SIGHUP reload failed (running config unchanged): %s",
                exc,
            )

    async def apply_pending_reload(self) -> None:
        """Push the re-rendered Caddyfile if reconfigure() flagged one.

        Mirrors :meth:`klangk.main.Lifecycle.apply_pending_reseed`: the
        sync ``reconfigure()`` can't ``POST /load`` (it runs inside the
        SIGHUP subsystem loop, not a coroutine), so it flags and this
        async method — called by ``apply_reloaded_settings`` after the
        loop — does the push. No-op when the watchdog didn't start
        (``_KLANGKD_DISABLE_PROXY``) or nothing flagged. A push failure
        is logged + swallowed so a broken reload can't abort the wider
        SIGHUP (Caddy keeps its last-known-good config).
        """
        if not self._pending_reload:
            return
        self._pending_reload = False
        if self._task is None:
            return
        try:
            await self.load_config()
            logger.info("caddy config reloaded via admin API (SIGHUP)")
        except Exception as exc:  # noqa: BLE001
            self._log_reload_failure(exc)

    # -- paths / config ----------------------------------------------------

    @property
    def admin_socket(self) -> str:
        """The admin UDS **path** (bare filesystem path; what httpx dials).

        Read live from ``settings.caddy_admin_socket`` (default
        ``<state_dir>/caddy-admin.sock``, overridable via
        ``KLANGKD_CADDY_ADMIN_SOCKET`` for environments where the default would
        overflow the AF_UNIX sun_path bound, #1636). The httpx dial uses this
        bare path; the owner-only mode is enforced by the watchdog via
        ``os.chmod`` (see :attr:`admin_bind_address` — no ``|0600`` suffix).
        """
        return self.app.state.settings.caddy_admin_socket

    @property
    def admin_bind_address(self) -> str:
        """The Caddy bind address for the admin UDS: ``unix//<path>``.

        No ``|0600`` mode suffix — that syntax is only honored on Caddy >= 2.8;
        on older Caddy it's folded into the socket *path*, breaking the bind
        (#1709). The owner-only mode is enforced by :meth:`_wait_for_admin`
        via ``os.chmod`` (version-independent). The admin API accepts a full
        config replace including arbitrary upstreams, so the UDS must stay
        owner-only (#1559).
        """
        return f"unix//{self.admin_socket}"

    def _render_caddyfile(self) -> str:
        """Render the full Caddyfile (global + sites), UDS backend upstream."""
        uds_path = self.app.state.settings.socket
        return self._renderer.render_config(
            uds_upstream(uds_path),
            self.admin_socket,
            full_global=self._full_global,
        )

    def find_proxy_bin(self) -> str:
        return self._renderer.find_proxy_bin()

    def _ensure_storage_dir(self) -> None:
        """Create the armed-mode certificate storage dir (#3192).

        Caddy mkdirs the dir itself, but only once a config using it loads —
        creating it up front (idempotent) keeps the first ACME run against a
        definitely-writable path under ``state_dir`` and surfaces permission
        problems at push time rather than mid-issuance.
        """
        if self._renderer.auto_https_armed:
            os.makedirs(
                self._renderer.caddy_storage_dir(), mode=0o700, exist_ok=True
            )

    async def load_config(
        self,
        caddyfile: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """Render (if omitted) and ``POST /load`` the Caddyfile to running Caddy."""
        if caddyfile is None:
            # Render first: a render that refuses (e.g. an arm the binary
            # cannot load) must not leave an empty storage dir behind as
            # a side effect; an explicit caddyfile needs none at all
            # (#3192 sweep).
            caddyfile = self._render_caddyfile()
            self._ensure_storage_dir()
        return await post_load(self.admin_socket, caddyfile, client=client)

    # -- supervision -------------------------------------------------------

    async def _wait_for_admin(self, timeout: float = 15.0) -> bool:
        """Poll the admin UDS until Caddy accepts a connection (or timeout).

        Any HTTP response (any status) counts as "up" — the admin endpoint is
        listening. A transport-level failure (missing socket / refused —
        raised by httpx as :class:`~httpx.ConnectError`, but also a stalled
        peer raising a read *timeout* or protocol error) means not-up yet:
        every :class:`~httpx.HTTPError` is retried, not just the connect
        flavor — an uncaught one would kill the whole supervision task and
        leave a blank-config Caddy running unsupervised. Sleep and retry
        until the deadline, then return ``False``.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            try:
                transport = httpx.AsyncHTTPTransport(uds=self.admin_socket)
                async with httpx.AsyncClient(transport=transport) as c:
                    await c.get("http://localhost/config/")
                # Enforce owner-only mode on the admin UDS (#1559). We do NOT
                # use Caddy's |0600 address suffix for this — it's only honored
                # on Caddy >= 2.8, and on older Caddy it's folded into the
                # socket path, breaking the bind (#1709). os.chmod is
                # version-independent, and Caddy doesn't re-bind the admin on
                # /load with an unchanged address, so this one chmod per bind
                # persists across reloads. There's a brief window between
                # Caddy creating the socket (default permissive mode) and this
                # chmod (~one poll interval), acceptable for #1559's same-host
                # threat model.
                try:
                    os.chmod(self.admin_socket, 0o600)
                except OSError:
                    # Socket vanished (race) or a mocked test path with no
                    # real socket — don't let it mask the successful connect.
                    pass
                return True
            except (httpx.HTTPError, OSError):
                await asyncio.sleep(0.2)
        return False

    def _relay_line(self, line: str) -> None:  # pragma: no cover  – e2e
        """Log one Caddy stderr line; a bind failure marks the supervision
        loop fatal."""
        level, msg = classify_caddy_line(line)
        logger.log(level, "caddy: %s", msg)
        if level >= logging.ERROR and is_bind_error(line):
            self._bind_fatal = True

    async def _relay_stderr(
        self,
        stream: asyncio.StreamReader,
    ) -> None:  # pragma: no cover  – covered by the e2e suite
        """Read Caddy's stderr line-by-line and relay through Python logging.

        Errors are logged at ERROR; routine info/warn messages are logged at
        DEBUG so they're hidden by default but accessible via
        ``KLANGKD_LOG_LEVEL=DEBUG``.

        When a bind failure is detected (admin socket, ingress, or egress
        listener), sets ``_bind_fatal`` so the supervision loop exits
        instead of respawning (#1917).
        """
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._relay_line(line)

    def _log_listeners(self) -> None:
        """Log which addresses Caddy is serving after a successful config load."""
        s = self.app.state.settings
        if s.port is not None:
            if s.tls_hostname:
                flavor = (
                    "internal TLS"
                    if self._renderer._internal_tls()
                    else "automatic TLS"
                )
                scheme = f"https ({flavor}, {s.tls_hostname})"
            else:
                scheme = "http"
            logger.info(
                "caddy ingress listening on %s:%s [%s]",
                s.listen,
                s.port,
                scheme,
            )
        logger.info(
            "caddy egress listening on %s:%s", s.egress_listen, s.egress_port
        )

    async def _watch(
        self, bin_path: str
    ) -> None:  # pragma: no cover  – covered by the e2e suite
        """Spawn Caddy, wait for its admin UDS, push config; respawn on exit.

        Respawns with backoff;
        the only engine-specific step is re-pushing the Caddyfile over the
        admin API after each (re)start, since the in-memory config is lost
        when Caddy restarts.
        """
        backoff = 1.0
        env = dict(os.environ)
        # Belt-and-suspenders: honored on Caddy >= 2.7 (caddy#5317). The
        # bootstrap Caddyfile below is the authoritative source and works on
        # all versions — see CaddyRenderer._bootstrap_block (#1709).
        env["CADDY_ADMIN"] = self.admin_bind_address
        # Minimal initial config carrying only the admin global option, so
        # Caddy binds the admin UDS at bootstrap on ANY version — NOT
        # /dev/null, which falls back to localhost:2019 on Caddy < 2.7 (where
        # CADDY_ADMIN is unsupported) and collides with any other Caddy on
        # the host. An explicit --config also preserves the "no on-disk
        # source of truth" guarantee; the real config still arrives via
        # POST /load.
        bootstrap_cfg = (
            Path(self.app.state.settings.state_dir)
            / "caddy-bootstrap.Caddyfile"
        )
        bootstrap_cfg.write_text(
            self._renderer._bootstrap_block(self.admin_socket)
        )
        while not self._stopping:
            self._bind_fatal = False
            # A stale socket from a prior run blocks the bind.
            try:
                os.unlink(self.admin_socket)
            except FileNotFoundError:
                pass
            rc = await self._watch_once(bin_path, bootstrap_cfg, env)
            if self._stopping:
                return
            if self._bind_fatal:
                logger.error(
                    "caddy failed to bind a listener (rc=%d) — not "
                    "restarting. Check the admin socket (%s), ingress "
                    "port, and egress port, then restart klangkd.",
                    rc,
                    self.admin_socket,
                )
                return
            logger.warning(
                "caddy exited (rc=%d); restarting in %.1fs", rc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _watch_once(
        self, bin_path: str, bootstrap_cfg: Path, env: dict
    ) -> int:  # pragma: no cover – covered by the e2e suite
        """One spawn -> admin-wait -> config-push -> wait cycle; returns
        caddy's exit code."""
        self._proc = await asyncio.create_subprocess_exec(
            bin_path,
            "run",
            "--config",
            str(bootstrap_cfg),
            "--adapter",
            "caddyfile",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=_proxy_preexec,
        )
        stderr_task = asyncio.create_task(
            self._relay_stderr(self._proc.stderr)
        )
        logger.info(
            "caddy started (pid %d), admin UDS %s",
            self._proc.pid,
            self.admin_socket,
        )
        await self._load_or_kill()
        rc = await self._proc.wait()
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
        self._proc = None
        return rc

    def _terminate_live(self) -> None:  # pragma: no cover – e2e
        """SIGTERM Caddy when it is still up (no-op otherwise), so the
        backoff loop retries."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _load_or_kill(self) -> None:  # pragma: no cover – e2e
        """Push the real config over the admin API once the admin UDS is up;
        when that fails (or the UDS never came up), kill Caddy so the backoff
        loop retries. A failed /load leaves Caddy serving a *blank* config
        (no sites) — a healthy process doing nothing, which would never exit
        and so never respawn, mirroring nginx's fail-fast-on-bad-config
        behavior."""
        load_ok = False
        if await self._wait_for_admin():
            try:
                await self.load_config()
                load_ok = True
                self._log_listeners()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "caddy POST /load failed (killing for respawn): %s",
                    exc,
                )
        else:
            logger.error(
                "caddy admin UDS never came up at %s", self.admin_socket
            )
        if not load_ok:
            self._terminate_live()

    async def start(self) -> None:
        """Bootstrap Caddy (admin on a UDS, no config) and start the watchdog.

        Gated only by ``_KLANGKD_DISABLE_PROXY`` — the internal,
        non-user-facing test kill switch.
        """
        if os.environ.get("_KLANGKD_DISABLE_PROXY"):
            return
        bin_path = self.find_proxy_bin()
        # Probe whether the caddy binary supports the full global block
        # (persist_config + servers/trusted_proxies/strict). These postdate the
        # older system caddy a stock CI runner apt-installs (Ubuntu 24.04 →
        # 2.6.2); emitting them unconditionally makes that caddy reject the
        # whole config (#1709). klangkd must run on both the devenv's current
        # caddy and that older system caddy.
        self._full_global = caddy_supports_full_global_block(bin_path)
        # Automatic TLS (#3192) needs the full global block (email / storage /
        # the dropped auto_https off); on the older caddy the minimal fallback
        # would silently serve plain HTTP. Fail fast at boot instead — a
        # respawn loop over a config the binary can't load helps nobody.
        if self._renderer.auto_https_armed and not self._full_global:
            logger.error(
                "KLANGKD_TLS_HOSTNAME is set (automatic TLS) but the "
                "detected caddy (%s) is too old to load the required global "
                "options. Upgrade caddy, or unset KLANGKD_TLS_HOSTNAME "
                "and terminate TLS at an outer proxy "
                "(docs/deployment/behind-a-proxy.md).",
                bin_path,
            )
            raise AutoHttpsConfigError(
                f"automatic TLS (KLANGKD_TLS_HOSTNAME) requires a newer "
                f"caddy binary (detected: {bin_path})"
            )
        self._stopping = False
        self._task = asyncio.create_task(self._watch(bin_path))

    def _signal_group(self, proc, sig: int) -> None:
        """Signal Caddy's whole process group, falling back to the process
        alone when the group is gone or not ours."""
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()

    async def _stop_proxy_proc(self) -> None:
        """Terminate Caddy's whole process group — TERM, 5s wait, KILL — so
        no orphaned Caddy lingers after shutdown (#1533)."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._signal_group(proc, signal.SIGKILL)

    async def _cancel_watch_task(self) -> None:
        """Cancel (and await) the watchdog task."""
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def stop(self) -> None:
        """Stop Caddy and cancel the watchdog (cooperative: waits for exit).

        Kills the entire process group so no orphaned Caddy lingers after
        shutdown (#1533).
        """
        self._stopping = True
        await self._stop_proxy_proc()
        self._proc = None
        await self._cancel_watch_task()
