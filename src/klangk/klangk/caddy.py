"""Python-owned reverse-proxy: Caddy engine (#1559).

This is the Caddy counterpart to :mod:`klangk.proxy` (the nginx engine). It
implements the same two responsibilities — render the proxy config from the
merged settings, and supervise the proxy child process — but for **Caddy**
instead of nginx, selected by ``KLANGK_PROXY_ENGINE=caddy``.

The two design choices that distinguish this from the nginx renderer (see
issue #1559):

- **Config is delivered over Caddy's admin API, not rendered to a file on
  disk.** :class:`CaddyRenderer` produces a **Caddyfile string**
  (eyeball-diffable with the nginx.conf), and :class:`CaddyWatchdog` pushes it
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
host-IP auto-detection probe the nginx renderer uses). It takes the upstream
dial target as a parameter so tests can pass a TCP address while production
passes a ``unix//<socket>`` address. The pure host-IP / loopback helpers are
imported from :mod:`klangk.proxy` rather than duplicated.

Out of scope here (tracked in #1559): the ``caddy-l4`` layer-4 plugin
(everything klangk proxies is HTTP), and per-route live JSON mutations on
``/config/.../routes`` (Phase 3 — Phase 1 uses full-config ``POST /load``).
"""

from __future__ import annotations

import asyncio
import logging
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

# Reuse the pure host-IP / loopback probes from the nginx renderer — both
# engines auto-detect the pasta-NAT container source set the same way.
from klangk.proxy import (
    _FALLBACK_ACL_SUBNETS,
    _FALLBACK_DENY_SUBNETS,
    _is_loopback,
    detect_host_ipv4s,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Upstream constructors (pure — no settings)
# ---------------------------------------------------------------------------


def uds_upstream(socket_path: str) -> str:
    """The Caddy ``reverse_proxy`` dial target for a UDS upstream (production).

    Caddy's UDS dial address is ``unix//path/to/sock`` — a literal ``unix//``
    prefix (two slashes) followed by the absolute socket path. (nginx's form
    is ``http://unix:/path:``; Caddy's is different, hence a separate helper
    rather than reusing :func:`klangk.proxy.uds_upstream`.)
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
# Renderer (settings-driven — owned instance, parallel to ProxyRenderer)
# ---------------------------------------------------------------------------


class CaddyRenderer:
    """Settings-driven Caddy **Caddyfile** renderer (#1559).

    Parallel to :class:`klangk.proxy.ProxyRenderer`: constructed with
    ``app`` per the composition-root pattern, settings read live via
    ``self.app.state.settings`` (#1608). :meth:`render_config` returns a
    Caddyfile string covering the same surface as the nginx renderer — two
    listeners, ``forward_auth`` token gate, IP matchers, ``request_body``
    max-size, UDS upstream, injected ``Authorization``. The Caddyfile maps
    almost 1:1 onto the nginx.conf so the two can be eyeball-diffed.
    """

    def __init__(self, app) -> None:
        self.app = app

    def reconfigure(self, app) -> None:
        self.app = app

    # -- shared computation (mirrors ProxyRenderer, Caddy-shaped output) ---

    def _container_source_entries(self) -> tuple[list[str], list[str]]:
        """Resolve the container source IP/CIDR set → ``(acl_entries, deny_entries)``.

        Identical policy to :meth:`klangk.proxy.ProxyRenderer._container_source_entries`
        (both engines gate on the same set):

        - ``acl_entries``: every source, loopback included — drives the egress
          allowlist (containers connect from these IPs).
        - ``deny_entries``: non-loopback sources only — drives the browser
          catch-all guard. Loopback is excluded so a local browser keeps full
          UI/API access.
        """
        explicit = self.app.state.settings.container_subnets
        if explicit:
            entries = [
                s.strip() for s in str(explicit).split(",") if s.strip()
            ]
            deny_entries = [s for s in entries if not _is_loopback(s)]
            if not deny_entries:
                logger.warning(
                    "container source set has no non-loopback entries — "
                    "catch-all location / denies nothing (deny-by-default inactive)"
                )
            return entries, deny_entries
        addrs = detect_host_ipv4s()
        if addrs:
            return addrs, [a for a in addrs if not _is_loopback(a)]
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
        """Caddy ``request_body`` ``max_size`` from ``KLANGK_FILE_UPLOAD_SIZE_MAX``.

        The setting is bytes (default 500 MB); Caddy's ``max_size`` accepts a
        size with a unit (``500MB``). Minimum 1MB.
        """
        raw = self.app.state.settings.file_upload_size_max
        try:
            bytes_ = int(str(raw))
        except (TypeError, ValueError):
            bytes_ = 524288000
        mb = max(1, bytes_ // 1048576)
        return f"{mb}MB"

    def _reject_proxy_headers(self) -> bool:
        """True if KLANGK_REJECT_PROXY_HEADERS is set (hard trust-off)."""
        raw = self.app.state.settings.reject_proxy_headers
        return bool(raw and str(raw).strip().lower() in ("1", "true", "yes"))

    def _trusted_proxy_cidrs(self) -> list[str]:
        """Validated KLANGK_TRUSTED_PROXY_CIDRS entries (loopback if empty/invalid)."""
        raw = self.app.state.settings.trusted_proxy_cidrs
        entries: list[str] = []
        for token in (raw or "").split(","):
            token = token.strip()
            if not token:
                continue
            entries.append(token)
        if not entries:
            entries = ["127.0.0.1", "::1"]
        return entries

    # -- global options ----------------------------------------------------

    def _global_block(self, admin_socket: str, *, full_global: bool = True) -> str:
        """The global options block: admin UDS, no HTTPS, no on-disk persistence.

        - ``admin unix//...`` re-declares the admin endpoint on the
          klangkd-owned UDS so it survives every ``POST /load``.
        - ``auto_https off`` — klangk serves plain HTTP (it terminates TLS at
          an outer proxy or not at all); without this Caddy would spawn a
          certificate-automation server and HTTPS redirect.
        - ``persist_config off`` — the admin API is the source of truth, not
          disk (mirrors the no-on-disk-config decision).
        - ``servers { trusted_proxies ... }`` when proxy-header trust is on —
          this is what makes ``{client_ip}`` resolve the real client from
          ``X-Forwarded-For`` (#1558), the Caddy equivalent of nginx's
          ``set_real_ip_from`` / ``real_ip_header`` realip directives.
          Suppressed entirely under ``KLANGK_REJECT_PROXY_HEADERS`` (hard
          trust-off), in which case ``{client_ip}`` falls back to the
          immediate peer — matching nginx with no realip directives.
        """
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
            "	auto_https off",
        ]
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

        The ``trust_outer_proxy`` X-Forwarded-Host/Prefix client-passthrough
        (nginx's ``$http_x_forwarded_host`` branch) is deferred to Phase 2:
        Caddy's derived defaults already match nginx's *non-trust* path (the
        common case); the trust-on passthrough is a Phase 2 refinement.
        """
        return (
            "\t\t\theader_up Host {host}\n"
            "\t\t\theader_up X-Real-IP {client_ip}"
        )

    # -- egress locations --------------------------------------------------

    def _build_llm_block(self, upstream: str, guard: str) -> str:
        """The ``/llm-proxy/*`` location, only when ``KLANGK_LLM_BASE_URL`` is set.

        Containers hit this instead of the real endpoint so they never see the
        API key. ``file:``/``cmd:`` prefixes on the URL and key are resolved
        by the settings layer (Python's resolver) before rendering, so the
        key lands here already resolved — and because the config never touches
        disk (admin API payload), the secret stays in memory only.

        Path-bearing ``llm_base_url`` values (e.g. z.ai's
        ``https://api.z.ai/api/coding/paas/v4``, OpenRouter's
        ``https://openrouter.ai/api/v1``) are split into a host-only upstream
        for ``reverse_proxy`` plus a ``rewrite`` that re-attaches the path:
        caddy rejects upstream URLs that include a path (``for now, URLs for
        proxy upstreams only support scheme, host, and port components``),
        which crashed the LLM block for every provider whose base URL isn't
        host-root — the regression surfaced when caddy became the default
        engine in #1643 (#1681). nginx's ``proxy_pass $llm_backend`` resolves
        at request time and never structural-validates the URL, so the same
        base_url works there without splitting.

        No explicit ``header_up Host`` is emitted: caddy ≥2.8 auto-sets
        ``Host: {upstream_hostport}`` when the transport has TLS
        (caddyserver/caddy#7454), which covers every HTTPS LLM provider
        (z.ai, OpenRouter, OpenAI, Anthropic, …). For a plain-HTTP upstream
        (e.g. local Ollama ``http://127.0.0.1:11434``) caddy passes the
        original request's Host through — the upstream sees ``Host:
        host.containers.internal:8995`` rather than ``127.0.0.1:11434``.
        This is a real but narrow parity gap with nginx (which sets ``Host
        $proxy_host`` for both schemes); most HTTP upstreams ignore Host, so
        it's left as-is rather than complicating the block with scheme
        detection. An earlier attempt to set ``header_up Host
        {upstream.hostport}`` unconditionally reintroduced the ``http2:
        invalid Host header`` 502 against HTTPS upstreams — caddy's
        placeholder substitution in the header context isn't reliable enough
        to override what it would auto-set. See review of #1681.
        """
        base_url = self.app.state.settings.llm_base_url
        if not base_url:
            return ""
        api_key = self.app.state.settings.llm_api_key
        # Split scheme://host[:port] from path and query. Trailing slash is
        # stripped from the path so concatenation with {http.request.uri.path}
        # (which begins with /) doesn't double it: base_path "" + path "/x"
        # -> "/x"; base_path "/v4" + path "/chat" -> "/v4/chat".
        parts = urlsplit(base_url)
        upstream_url = f"{parts.scheme}://{parts.netloc}"
        base_path = parts.path.rstrip("/")
        base_query = parts.query
        # ``handle_path /llm-proxy/*`` is the caddy directive that both
        # matches the path AND strips the prefix atomically before any
        # subsequent directive in the same block reads {uri}. A plain
        # ``uri strip_prefix`` followed by ``rewrite`` does NOT work —
        # caddy's Caddyfile adapter reorders the two rewrite-family
        # handlers, so the rewrite sees the un-stripped {uri} and emits
        # /api/coding/paas/v4/llm-proxy/chat. nginx's regex capture
        # (``location ~ ^/llm-proxy/(.*)$``) does strip+substitute in one
        # directive; handle_path is the caddy equivalent.
        #
        # The rewrite uses {http.request.uri.path} (path only), NOT {uri}
        # (path + query) — the base URL is trusted operator config and is
        # the only source of upstream query params; the container user's
        # per-request query is untrusted and is dropped (#1687). The base
        # query, if present, is re-attached after the path (Gemini-style
        # ?key=... auth, documented but discouraged by Google on security
        # grounds; the OpenAI Python client also preserves hardcoded
        # query params on base_url, openai/openai-python@73ea2f7).
        #
        # CRITICAL: the rewrite target MUST carry a query component
        # (even an empty one) so caddy treats it as query-REPLACING rather
        # than query-PRESERVING. A bare ``rewrite * {path}`` with no ``?``
        # leaves the incoming request's query intact — verifiable live:
        # POST /llm-proxy/chat?user=evil with a no-base-query config
        # forwards ``/chat?user=evil`` to the upstream, leaking the
        # container user's query. Appending ``?{base_query}`` (which
        # expands to ``?`` when base_query is empty) drops the user query
        # in both cases. (Discovered in review of #1696.)
        target = f"{base_path}{{http.request.uri.path}}?{base_query}"
        path_fix = f"		rewrite * {target}\n"
        return (
            "	handle_path /llm-proxy/* {\n"
            f"{guard}"
            f"{path_fix}"
            f"		reverse_proxy {upstream_url} {{\n"
            f'			header_up Authorization "Bearer {api_key}"\n'
            "		}\n"
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
        # nginx uses ``location =`` (exact) for this endpoint; mirror
        # it with a ``path`` matcher (exact by default) so e.g.
        # /api/v1/workspaces/post-chat-message/other does not match.
        post_chat = (
            "	@postchat path /api/v1/workspaces/post-chat-message\n"
            "	handle @postchat {\n"
            f"{guard}"
            f"		reverse_proxy {upstream}\n"
            "	}\n"
        )
        return not_src_matcher + llm + delegate + post_chat

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
            f"	forward_auth {upstream} {{\n"
            f"		uri /api/v1/auth/verify-workspace-token\n"
            f"	}}\n"
            f"{locations}}}\n"
        )

    # -- browser-only locations -------------------------------------------

    def _build_hosted_block(self) -> str:
        """The ``/hosted/<ws>/<port>/`` proxy (or nothing when disabled).

        Disabled entirely when ``KLANGK_HOSTED_PORTS_PER_WORKSPACE`` is
        exactly 0 (mirrors the backend's ``ports_per_workspace_cap()``,
        #1237): a bare ``respond 404`` catch for ``^/hosted/``.

        Otherwise two matchers (mirroring the nginx ``location`` pair):

        - slash-less ``/hosted/<ws>/<port>`` → ``308`` redirect to the
          canonical trailing-slash form (so relative asset paths resolve);
        - ``/hosted/<ws>/<port>/<rest...>`` → strip the prefix and proxy to
          ``127.0.0.1:<port>`` (WebSocket upgrade is automatic in
          ``reverse_proxy``).
        """
        raw = self.app.state.settings.hosted_ports_per_workspace
        if str(raw).strip() == "0":
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
        """The browser-listener site block (full mode only)."""
        listen_addr = self.app.state.settings.listen
        port = self.app.state.settings.port
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
            f"http://:{port} {{\n"
            f"	bind {listen_addr}\n"
            f"	request_body {{\n"
            f"		max_size {self._max_body_size()}\n"
            f"	}}\n"
            f"{deny_matcher}"
            f"{hosted}"
            f"{auth_local}"
            f"{catch_all}}}\n"
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
        reloads. Template selection keys off ``KLANGK_PORT`` (#1542):
        **unset** ⇒ headless (egress listener only); **set** ⇒ full (browser
        + egress listeners). All other values come from the merged settings
        plus the host-IP auto-detection probe.
        """
        acl_entries, deny_entries = self._container_source_entries()
        global_block = self._global_block(admin_socket, full_global=full_global)
        egress = self._egress_site(upstream, " ".join(acl_entries))
        if self.app.state.settings.port is None:
            return global_block + egress
        browser = self._browser_site(upstream, " ".join(deny_entries))
        return global_block + egress + browser

    # -- binary location ---------------------------------------------------

    def find_proxy_bin(self) -> str:
        """Locate the caddy binary: KLANGK_PROXY_BIN > PATH > /usr/bin/caddy.

        ``KLANGK_PROXY_BIN`` overrides for both engines; the Caddy fallbacks
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
# Process supervision (parallel to ProxyWatchdog)
# ---------------------------------------------------------------------------


# The preexec body (new session for killpg + PR_SET_PDEATHSIG) is identical for
# every proxy engine — reuse the nginx watchdog's rather than duplicate it.
from klangk.proxy import _proxy_preexec as _caddy_preexec  # noqa: E402


def _caddy_supports_full_global_block(bin_path: str) -> bool:
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
            [bin_path, "adapt", "--adapter", "caddyfile", "--config", probe_path],
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

    Parallel to :class:`klangk.proxy.ProxyWatchdog` but for Caddy. Instead of
    rendering a config file and pointing Caddy at it with ``-c``, this:

    1. bootstraps Caddy with ``CADDY_ADMIN=unix//<sock>`` (empty config, no
       file) — the admin endpoint comes up on a klangkd-owned UDS;
    2. waits for the admin UDS to accept a connection;
    3. pushes the rendered Caddyfile via :func:`post_load` (``POST /load``,
       ``text/caddyfile``) — full-config replace.

    On every respawn the Caddyfile is re-applied (config lives only in memory
    until ``/load`` runs). Constructed with ``app``; settings read live via
    ``self.app.state.settings`` (#1608). Stored on ``app.state.proxy_watchdog``
    (selected in :func:`klangk.main.build_app` when
    ``KLANGK_PROXY_ENGINE=caddy``); the lifespan calls ``.start()`` /
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
        # _caddy_supports_full_global_block (#1709).
        self._full_global: bool = True
        # Flagged by reconfigure() on a SIGHUP settings swap; applied
        # async by apply_pending_reload() (POST /load can't run in the
        # sync reconfigure loop). #1559 Phase 1: a settings change is a
        # fresh POST /load, not stale-until-restart.
        self._pending_reload = False

    def reconfigure(self, app) -> None:
        self.app = app
        self._renderer.reconfigure(app)
        # The SIGHUP path swapped in new settings; flag that the running
        # Caddy needs a fresh POST /load (applied async after the sync
        # reconfigure loop). No-op when the watchdog never started
        # (_KLANGK_DISABLE_PROXY) — the flag is just never applied.
        self._pending_reload = True

    async def apply_pending_reload(self) -> None:
        """Push the re-rendered Caddyfile if reconfigure() flagged one.

        Mirrors :meth:`klangk.main.Lifecycle.apply_pending_reseed`: the
        sync ``reconfigure()`` can't ``POST /load`` (it runs inside the
        SIGHUP subsystem loop, not a coroutine), so it flags and this
        async method — called by ``_apply_reloaded_settings`` after the
        loop — does the push. No-op when the watchdog didn't start
        (``_KLANGK_DISABLE_PROXY``) or nothing flagged. A push failure
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
            logger.warning(
                "caddy SIGHUP reload failed (running config unchanged): %s",
                exc,
            )

    # -- paths / config ----------------------------------------------------

    @property
    def admin_socket(self) -> str:
        """The admin UDS **path** (bare filesystem path; what httpx dials).

        Read live from ``settings.caddy_admin_socket`` (default
        ``<state_dir>/caddy-admin.sock``, overridable via
        ``KLANGK_CADDY_ADMIN_SOCKET`` for environments where the default would
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
            uds_upstream(uds_path), self.admin_socket, full_global=self._full_global
        )

    def find_proxy_bin(self) -> str:
        return self._renderer.find_proxy_bin()

    async def load_config(
        self,
        caddyfile: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """Render (if omitted) and ``POST /load`` the Caddyfile to running Caddy."""
        if caddyfile is None:
            caddyfile = self._render_caddyfile()
        return await post_load(self.admin_socket, caddyfile, client=client)

    # -- supervision -------------------------------------------------------

    async def _wait_for_admin(self, timeout: float = 15.0) -> bool:
        """Poll the admin UDS until Caddy accepts a connection (or timeout).

        Any HTTP response (any status) counts as "up" — the admin endpoint is
        listening. A connection failure (missing socket / refused — raised by
        httpx as :class:`~httpx.ConnectError`) means not-up yet: sleep and
        retry until the deadline, then return ``False``.
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
            except (httpx.ConnectError, OSError):
                await asyncio.sleep(0.2)
        return False

    async def _watch(
        self, bin_path: str
    ) -> None:  # pragma: no cover  – covered by the e2e suite
        """Spawn Caddy, wait for its admin UDS, push config; respawn on exit.

        Respawn-with-backoff mirrors :meth:`klangk.proxy.ProxyWatchdog._watch`;
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
            Path(self.app.state.settings.state_dir) / "caddy-bootstrap.Caddyfile"
        )
        bootstrap_cfg.write_text(self._renderer._bootstrap_block(self.admin_socket))
        while not self._stopping:
            # A stale socket from a prior run blocks the bind.
            try:
                os.unlink(self.admin_socket)
            except FileNotFoundError:
                pass
            self._proc = await asyncio.create_subprocess_exec(
                bin_path,
                "run",
                "--config",
                str(bootstrap_cfg),
                "--adapter",
                "caddyfile",
                stdout=None,
                stderr=None,
                env=env,
                preexec_fn=_caddy_preexec,
            )
            logger.info(
                "caddy started (pid %d), admin UDS %s",
                self._proc.pid,
                self.admin_socket,
            )
            load_ok = False
            if await self._wait_for_admin():
                try:
                    await self.load_config()
                    load_ok = True
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "caddy POST /load failed (killing for respawn): %s",
                        exc,
                    )
            else:
                logger.error(
                    "caddy admin UDS never came up at %s", self.admin_socket
                )
            if not load_ok and self._proc and self._proc.returncode is None:
                # A failed /load leaves Caddy serving a *blank* config (no
                # sites) — a healthy process doing nothing, which would never
                # exit and so never respawn. Kill it so the backoff loop
                # retries, mirroring nginx's fail-fast-on-bad-config behavior.
                try:
                    self._proc.terminate()
                except ProcessLookupError:
                    pass
            rc = await self._proc.wait()
            self._proc = None
            if self._stopping:
                return
            logger.warning(
                "caddy exited (rc=%d); restarting in %.1fs", rc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def start(self) -> None:
        """Bootstrap Caddy (admin on a UDS, no config) and start the watchdog.

        Gated only by ``_KLANGK_DISABLE_PROXY`` — the same internal,
        non-user-facing test kill switch the nginx watchdog uses.
        """
        if os.environ.get("_KLANGK_DISABLE_PROXY"):
            return
        bin_path = self.find_proxy_bin()
        # Probe whether the caddy binary supports the full global block
        # (persist_config + servers/trusted_proxies/strict). These postdate the
        # older system caddy a stock CI runner apt-installs (Ubuntu 24.04 →
        # 2.6.2); emitting them unconditionally makes that caddy reject the
        # whole config (#1709). klangkd must run on both the devenv's current
        # caddy and that older system caddy.
        self._full_global = _caddy_supports_full_global_block(bin_path)
        self._stopping = False
        self._task = asyncio.create_task(self._watch(bin_path))

    async def stop(self) -> None:
        """Stop Caddy and cancel the watchdog (cooperative: waits for exit).

        Kills the entire process group so no orphaned Caddy lingers after
        shutdown (#1533).
        """
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        self._proc = None
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
