"""Python-owned reverse-proxy config renderer + process manager (#1396).

The proxy fronting the backend is currently implemented with nginx: this
module renders an ``nginx.conf`` from the merged settings and supervises the
nginx child process. It replaces ``scripts/nginx.sh`` (the bash heredoc that
generated ``nginx.conf``) and the ``/home/klangk/bin/nginx`` shim it was
copied into for the host container. ``klangkd`` calls :meth:`ProxyRenderer.render_config`
to write the config, then :meth:`ProxyWatchdog.start` / :meth:`ProxyWatchdog.stop`
(an async watchdog in the lifespan owns the child process).

The renderer is a pure function of the merged config (settings + env probes
for host-IP / DNS auto-detection). It takes the upstream proxy target as a
parameter so it serves both the production UDS bind
(:func:`uds_upstream`) and the TCP bind tests use
(:func:`tcp_upstream`) — only the ``proxy_pass`` base differs.

See #1392 (design record) and #1396 (this chunk).

The settings-driven rendering logic lives on :class:`ProxyRenderer`, an
owned instance constructed with ``app_state`` per the composition-root
refactor (#1426, #1469).  Settings are read live via
``self.app.state.settings`` (#1608).
Pure helpers (upstream constructors, host-IP auto-detection, the minimal-
template auth-location formatter) stay module-level — they don't read
settings.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import ipaddress
import logging
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

# Loopback ranges excluded from the catch-all deny (CONTAINER_DENY) — local
# browsers connect via loopback and must reach the full UI/API. Matches the
# ``_is_loopback`` helper in the old nginx.sh (127.0.0.0/8 + ::1).
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


# ---------------------------------------------------------------------------
# Upstream constructors (pure — no settings)
# ---------------------------------------------------------------------------


def tcp_upstream(host: str, port: str | int) -> str:
    """The ``proxy_pass`` base for a TCP upstream (tests)."""
    return f"http://{host}:{port}"


def uds_upstream(socket_path: str) -> str:
    """The ``proxy_pass`` base for a UDS upstream (production).

    nginx's UDS ``proxy_pass`` is ``http://unix:/path/to/sock:`` — the path
    goes between ``unix:`` and a trailing ``:``, and any URI suffix appends
    after that trailing colon.
    """
    return f"http://unix:{socket_path}:"


# ---------------------------------------------------------------------------
# Environment probes (auto-detection, not settings)
# ---------------------------------------------------------------------------


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


# Fallback subnets when auto-detection yields nothing (mirrors nginx.sh):
# 172.16/12 + 10/8 (common container ranges), explicitly NOT 192.168/16
# (most common LAN range — allowing it would expose the LLM proxy to peers).
_FALLBACK_ACL_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8", "127.0.0.1"]
_FALLBACK_DENY_SUBNETS = ["172.16.0.0/12", "10.0.0.0/8"]


# ---------------------------------------------------------------------------
# Minimal-template auth-location formatter (pure — no settings)
# ---------------------------------------------------------------------------


def _egress_auth_locations(upstream: str) -> str:
    """The workspace-token ``auth_request`` subrequest target + JSON 401 page.

    Shared by the egress server block in both headless and full modes: the
    container-egress locations (``/llm-proxy``, ``/api/v1/browser-delegate``,
    ``/api/v1/workspaces/post-chat-message``) all gate on an ``auth_request``
    subrequest, whose target (``verify-workspace-token``) and failure page
    (``@token_auth_failed``) must live in the same server block.
    """
    return (
        "    # Workspace token verification subrequest"
        " (nginx auth_request target).\n"
        "    location = /api/v1/auth/verify-workspace-token {\n"
        "      internal;\n"
        f"      proxy_pass {upstream}/api/v1/auth/verify-workspace-token;\n"
        "      proxy_pass_request_body off;\n"
        '      proxy_set_header Content-Length "";\n'
        "      proxy_set_header Authorization $http_authorization;\n"
        "    }\n"
        "\n"
        "    # JSON 401 error page for auth_request failures.\n"
        "    location @token_auth_failed {\n"
        "      internal;\n"
        "      default_type application/json;\n"
        '      return 401 \'{"error":"$auth_token_error",'
        '"detail":"Workspace token $auth_token_error"}\';\n'
        "    }\n"
    )


# ---------------------------------------------------------------------------
# Renderer (settings-driven — owned instance, #1469)
# ---------------------------------------------------------------------------


class ProxyRenderer:
    """Settings-driven reverse-proxy (``nginx.conf``) renderer (#1396, #1469).

    Constructed with ``app_state`` per the composition-root pattern; settings
    are read live via ``self.app.state.settings`` (#1608). The renderer is a pure function of the
    merged config (settings + env probes); it does not touch podman.
    ``ProxyWatchdog`` owns an instance and calls :meth:`render_config` /
    :meth:`find_proxy_bin` / :meth:`write_config` from its ``_prepare`` step.
    """

    def __init__(self, app) -> None:
        self._app = app

    def reconfigure(self, app) -> None:
        self._app = app

    # -- DNS / ACL / size computation --------------------------------------

    def detect_dns_resolvers(self) -> str:
        """Space-separated nameservers for nginx's ``resolver`` directive.

        From ``KLANGK_DNS_SERVERS`` (comma→space) if set, else parsed from
        ``/etc/resolv.conf`` (IPv6 bracketed for nginx), else ``8.8.8.8``.
        """
        raw = self._app.state.settings.dns_servers
        if raw:
            servers = []
            for token in str(raw).split(","):
                token = token.strip()
                if not token:
                    continue
                if ":" in token and not token.startswith("["):
                    servers.append(f"[{token}]")
                else:
                    servers.append(token)
            return " ".join(servers) or "8.8.8.8"
        # Parse /etc/resolv.conf.
        servers: list[str] = []
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    addr = parts[1]
                    if ":" in addr:
                        servers.append(f"[{addr}]")
                    else:
                        servers.append(addr)
        except OSError:
            pass
        return " ".join(servers) or "8.8.8.8"

    def _container_source_entries(self) -> tuple[list[str], list[str]]:
        """Resolve the container source IP/CIDR set → ``(acl_entries, deny_entries)``.

        - ``acl_entries``: every source, loopback included — drives the egress
          CONTAINER_ACL allowlist (containers connect from these IPs).
        - ``deny_entries``: non-loopback sources only — drives the browser
          catch-all guard. Loopback is excluded so a local browser keeps full
          UI/API access.

        Source: explicit ``KLANGK_CONTAINER_SUBNETS`` if set (used verbatim,
        127.0.0.1 NOT implicitly added), else auto-detected host IPv4s, else
        the RFC1918 fallback.
        """
        explicit = self._app.state.settings.container_subnets
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

    def compute_container_acls(self) -> tuple[str, str]:
        """Build the CONTAINER_ACL (egress allowlist) and browser deny guard.

        Returns ``(acl, deny_guard)``:

        - ``acl``: ``allow <src>;`` lines + ``deny all;`` for the three
          container-egress locations (llm-proxy, browser-delegate,
          post-chat-message). Keyed on nginx's ``$remote_addr`` — fine because
          egress is reached *directly* by containers (pasta NAT), with no proxy
          in front to rewrite it.
        - ``deny_guard``: an ``if ($container_source) { return 403; }`` line
          for the browser catch-all ``location /``. ``$container_source`` is a
          flag set by :meth:`compute_container_geo`.
        """
        acl_entries, _deny_entries = self._container_source_entries()
        acl = "\n".join(f"      allow {e};" for e in acl_entries)
        acl = acl + "\n      deny all;"
        deny_guard = "      if ($container_source) { return 403; }\n"
        return acl, deny_guard

    def compute_container_geo(self) -> str:
        """The ``geo`` block that flags container-source peers (http scope).

        Plain-English version of what this does and why:

        **What.** Builds a lookup table: for each incoming request, nginx takes
        the address the connection came from and sets ``$container_source`` to
        ``1`` if that address is a workspace-container source, ``0`` otherwise.
        The browser catch-all ``location /`` then does
        ``if ($container_source) { return 403; }`` — a container trying to
        reach the browser UI is refused, everyone else gets through. This is
        the brute-force cap from #1376 (a container can hammer only its own
        token-gated egress endpoints, not ``/api/v1/auth/login`` etc.).

        **Why ``$realip_remote_addr`` and not ``$remote_addr``.**
        ``$remote_addr`` is the *effective* client: #1560's realip directives
        rewrite it to the real browser IP (from ``X-Forwarded-For``) whenever
        the immediate peer is a trusted proxy. Keying the guard on that would
        mean: when a trusted proxy forwards a request whose real client is one
        of klangk's own host interface IPs (e.g. a proxy co-located on the same
        host, #1546), nginx rewrites ``$remote_addr`` to that host IP, the guard
        matches a container source, and every proxied browser request returns
        403. ``$realip_remote_addr`` is the address the realip module *started
        with* — the actual TCP peer, before any rewrite. So:

          * request through a trusted proxy → peer is the *proxy's* IP (not a
            container source) → allowed, and ``$remote_addr`` still carries the
            real client for the backend's IP-trust checks;
          * container connecting directly (pasta NAT) → peer is a host IP that
            *is* a container source → denied (brute-force cap intact);
          * LAN browser connecting directly → peer is its LAN IP → allowed.

        This needs the realip module compiled into nginx (it provides the
        ``$realip_remote_addr`` variable) — already required by #1560's
        ``set_real_ip_from`` directives. When proxy-header trust is off
        (``KLANGK_REJECT_PROXY_HEADERS``), no realip directives fire,
        ``$remote_addr`` is never rewritten, and ``$realip_remote_addr`` simply
        equals it — correct either way.

        Loopback is never listed (it maps to the ``default 0`` → allowed), so
        local browsers are unaffected. With no non-loopback container sources
        at all, the block still declares the variable (default 0) so the
        ``location /`` guard references a defined variable.
        """
        _acl_entries, deny_entries = self._container_source_entries()
        body = (
            "\n".join(f"    {e} 1;" for e in deny_entries)
            if deny_entries
            else ""
        )
        body = f"    default 0;\n{body}" if body else "    default 0;"
        return (
            f"  geo $realip_remote_addr $container_source {{\n{body}\n  }}\n"
        )

    def compute_client_max_body_size(self) -> str:
        """Derive nginx ``client_max_body_size`` from ``KLANGK_FILE_UPLOAD_SIZE_MAX``.

        The setting is in bytes (default 500 MB); nginx wants ``Nm``. Minimum 1m.
        """
        raw = self._app.state.settings.file_upload_size_max
        try:
            bytes_ = int(str(raw))
        except (TypeError, ValueError):
            bytes_ = 524288000
        mb = max(1, bytes_ // 1048576)
        return f"{mb}m"

    # -- Section builders --------------------------------------------------

    def _build_hosted_block(self) -> str:
        """The /hosted/ proxy locations (or a 404 block when disabled).

        Disabled entirely when ``KLANGK_HOSTED_PORTS_PER_WORKSPACE`` is exactly 0
        — mirrors the backend's ``ports_per_workspace_cap()`` (#1237).
        """
        raw = self._app.state.settings.hosted_ports_per_workspace
        if str(raw).strip() == "0":
            return (
                "    # Hosted-app serving is disabled "
                "(KLANGK_HOSTED_PORTS_PER_WORKSPACE=0).\n"
                "    location ^~ /hosted/ {\n"
                "      return 404;\n"
                "    }\n"
            )
        return (
            "    # A hosted URL without a trailing slash (e.g. .../9001) can't match the\n"
            "    # proxy location below. Redirect to the canonical trailing-slash form so\n"
            "    # relative asset paths resolve.\n"
            "    location ~ ^/hosted/[^/]+/(?<hosted_port>\\d+)$ {\n"
            "      if ($hosted_is_ws = 0) {\n"
            "        return 308 $uri/$is_args$args;\n"
            "      }\n"
            "      # WebSocket clients cannot follow a 308; proxy instead.\n"
            "      proxy_pass http://127.0.0.1:$hosted_port/$is_args$args;\n"
            "      proxy_set_header Host $http_host;\n"
            "      proxy_set_header X-Real-IP $remote_addr;\n"
            "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "      proxy_set_header X-Forwarded-Proto $scheme;\n"
            "      proxy_http_version 1.1;\n"
            "      proxy_set_header Upgrade $http_upgrade;\n"
            "      proxy_set_header Connection $connection_upgrade;\n"
            "    }\n"
            "\n"
            "    # Hosted app proxy: extract port from URL and proxy to container.\n"
            "    location ~ ^/hosted/[^/]+/(\\d+)/(.*)$ {\n"
            "      proxy_pass http://127.0.0.1:$1/$2$is_args$args;\n"
            "      proxy_set_header Host $http_host;\n"
            "      proxy_set_header X-Real-IP $remote_addr;\n"
            "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "      proxy_set_header X-Forwarded-Proto $scheme;\n"
            "      proxy_http_version 1.1;\n"
            "      proxy_set_header Upgrade $http_upgrade;\n"
            "      proxy_set_header Connection $connection_upgrade;\n"
            "    }\n"
        )

    def _build_llm_block(self, acl: str, resolvers: str) -> str:
        """The /llm-proxy/ location, only when ``KLANGK_LLM_BASE_URL`` is set.

        Containers hit this instead of the real endpoint, so they never see the
        API key. Uses an nginx variable so the upstream resolves at request time
        (avoids crash on unresolvable hosts). ``file:``/``cmd:`` prefixes on the
        URL and key are resolved here (Python's resolver, not the retired
        ``klangk-resolve-value`` console script).

        Two buffering/resolution knobs that matter for LLM traffic:

        - ``proxy_request_buffering off`` (#1682): stream the request body
          straight to the upstream instead of spilling it to
          ``client_body_temp_path``. With the default ``on``, any body larger
          than ``client_body_buffer_size`` (nginx's compiled default — 16 KB on
          64-bit, 8 KB on 32-bit) is written to that temp dir, which in the
          keep-id userns is owned by a different uid than the nginx worker →
          EACCES → 500. Streaming sidesteps the temp dir entirely (matching
          caddy's ``reverse_proxy``, which never buffers requests to disk) and
          lets the upstream begin processing as the body arrives. Safe with
          ``auth_request``: the token-check subrequest runs in the preaccess
          phase, before ``proxy_pass`` reads the body, so a 401 short-circuits
          with nothing streamed. The same directive is on the other
          container-egress locations (``browser-delegate``, ``post-chat-message``)
          for the same reason — see ``_egress_locations``.
        - ``resolver ... ipv6=off``: don't resolve AAAA records for the
          upstream. LLM providers are dual-stack with IPv4 always available;
          on hosts with no IPv6 egress, attempting the AAAA address produces
          ``connect() ... failed (Network is unreachable)`` noise (and a
          failed first attempt before the A fallback) (#1682).

        The location regex ``^/llm-proxy/(.*)$`` matches against ``$uri``
        (path only), so ``$1`` is the path the container user requested —
        the incoming request's query string never reaches ``$1``. The
        load-bearing reason the user query is dropped, though, is that
        ``proxy_pass`` with a *variable* argument (``$llm_backend``) does
        not auto-append ``$args`` — a fixed ``proxy_pass`` would. The
        regex capture being path-only is necessary but not sufficient.
        Together they implement the trust-boundary rule: the base URL is
        operator config and is the only source of upstream query params
        (#1687). A base query (Gemini-style ``?key=...`` auth, documented
        but discouraged by Google; the OpenAI Python client also preserves
        hardcoded query params on ``base_url`` — openai/openai-python@73ea2f7)
        is reassembled AFTER ``$1`` so the final upstream URL is
        ``{scheme}://{host}{path}/$1?{query}``.
        """
        base_url = self._app.state.settings.llm_base_url
        if not base_url:
            return ""
        api_key = self._app.state.settings.llm_api_key
        # Reassemble so a base query (if any) lands AFTER $1 rather than
        # being strung into the path. ``urlsplit(base_url).geturl()`` would
        # round-trip the query mid-URL; instead, strip the query here and
        # append it explicitly after ``$1``.
        parts = urlsplit(base_url)
        base_without_query = parts._replace(query="").geturl()
        if parts.query:
            set_value = f"{base_without_query}/$1?{parts.query}"
        else:
            set_value = f"{base_without_query}/$1"
        return (
            f"    location ~ ^/llm-proxy/(.*)$ {{\n"
            f"{acl}\n"
            "      auth_request /api/v1/auth/verify-workspace-token;\n"
            "      auth_request_set $auth_token_error $upstream_http_x_token_error;\n"
            "      error_page 401 = @token_auth_failed;\n"
            f"      resolver {resolvers} valid=30s ipv6=off;\n"
            f"      set $llm_backend {set_value};\n"
            "      proxy_pass $llm_backend;\n"
            f'      proxy_set_header Authorization "Bearer {api_key}";\n'
            "      proxy_set_header Host $proxy_host;\n"
            "      proxy_ssl_server_name on;\n"
            "      proxy_http_version 1.1;\n"
            '      proxy_set_header Connection "";\n'
            "      proxy_request_buffering off;\n"
            "      proxy_buffering off;\n"
            "      proxy_cache off;\n"
            "      chunked_transfer_encoding on;\n"
            "    }\n"
        )

    def _reject_proxy_headers(self) -> bool:
        """True if KLANGK_REJECT_PROXY_HEADERS is set (hard trust-off)."""
        raw = self._app.state.settings.reject_proxy_headers
        return bool(raw and str(raw).strip().lower() in ("1", "true", "yes"))

    def _realip_block(self) -> str:
        """Emit nginx realip directives so ``$remote_addr`` is the real client.

        Without the realip module ``$remote_addr`` is always the immediate
        peer (the outer reverse proxy), and ``proxy_set_header X-Real-IP
        $remote_addr`` clobbers the real client IP the proxy forwarded — so
        the backend's ``client_is_loopback`` / ``derive_hosting_info`` resolve
        the proxy's IP, not the browser's (#1558, regression from
        stable/1.0 where the customer proxy hit uvicorn directly). With
        these directives nginx rewrites ``$remote_addr`` from
        ``X-Forwarded-For``, but **only when the immediate peer is in the
        trusted set** (``set_real_ip_from``): a direct, non-proxy connection
        cannot spoof XFF.

        Reuses ``KLANGK_TRUSTED_PROXY_CIDRS`` — the same trust set the
        Python ``peer_trusted()`` / ``client_is_loopback()`` helpers consult
        — so nginx and the backend agree on which peers are trusted.
        Suppressed entirely when ``KLANGK_REJECT_PROXY_HEADERS`` is set (hard
        trust-off), preserving the fail-closed posture.
        """
        if self._reject_proxy_headers():
            return ""
        raw = self._app.state.settings.trusted_proxy_cidrs
        entries: list[str] = []
        for token in (raw or "").split(","):
            token = token.strip()
            if not token:
                continue
            # Validate so an invalid entry doesn't crash nginx at start
            # (mirrors util.trusted_proxy_cidrs()'s skip-and-log).
            try:
                ipaddress.ip_address(token)
            except ValueError:
                try:
                    ipaddress.ip_network(token, strict=False)
                except ValueError:
                    logger.warning(
                        "Ignoring an invalid KLANGK_TRUSTED_PROXY_CIDRS "
                        "entry in nginx realip block"
                    )
                    continue
            entries.append(token)
        if not entries:
            entries = ["127.0.0.1", "::1"]
        lines = "\n".join(f"  set_real_ip_from {e};" for e in entries)
        return (
            f"{lines}\n"
            "  real_ip_header X-Forwarded-For;\n"
            "  real_ip_recursive on;\n"
        )

    def _trust_outer_proxy(self) -> bool:
        raw = self._app.state.settings.trust_outer_proxy
        return str(raw).strip().lower() in ("1", "true", "yes")

    # -- Main renderer -----------------------------------------------------

    def render_config(self, upstream: str) -> str:
        """Render ``nginx.conf`` as a string.

        Template selection keys off ``KLANGK_PORT`` (#1542): **unset** ⇒
        headless (a single container-egress listener on ``KLANGK_EGRESS_PORT``);
        **set** ⇒ full (a browser listener on ``{listen}:{port}`` plus a
        separate container-egress listener). ``upstream`` is the ``proxy_pass``
        base (:func:`uds_upstream` for the production socket bind,
        :func:`tcp_upstream` for tests). All other values come from the merged
        settings (env > config file > defaults) plus the host-IP / DNS
        auto-detection probes.
        """
        if self._app.state.settings.port is None:
            return self._render_headless_config(upstream)
        return self._render_full_config(upstream)

    def _egress_locations(
        self, upstream: str, acl: str, resolvers: str
    ) -> str:
        """The container-egress locations, shared by headless and full modes.

        Served on the ``KLANGK_EGRESS_PORT`` listener (container → backend via
        ``host.containers.internal``): the LLM proxy (when configured), the
        browser-delegate bridge, container-posted chat messages, and the
        workspace-token ``auth_request`` subrequest + JSON 401 page that gate
        them. All carry CONTAINER_ACL (allow container source IPs, deny all).
        ``upstream`` is the UDS ``proxy_pass`` base.
        """
        llm_block = self._build_llm_block(acl, resolvers)
        common_headers = (
            "      proxy_set_header Host $http_host;\n"
            "      proxy_set_header X-Real-IP $remote_addr;\n"
            "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "      proxy_http_version 1.1;\n"
        )
        delegate = (
            "    # Browser-delegate bridge: Pi extensions delegate long-running\n"
            "    # actions through here. Read timeout accommodates streaming + git-credential.\n"
            "    location /api/v1/browser-delegate {\n"
            f"{acl}\n"
            "      auth_request /api/v1/auth/verify-workspace-token;\n"
            "      auth_request_set $auth_token_error $upstream_http_x_token_error;\n"
            "      error_page 401 = @token_auth_failed;\n"
            f"      proxy_pass {upstream};\n"
            f"{common_headers}"
            "      proxy_read_timeout 920s;\n"
            "      proxy_send_timeout 920s;\n"
            "      # Stream the request body (#1682): a delegated action can carry\n"
            "      # a payload over client_body_buffer_size, which would otherwise\n"
            "      # spill to client_body_temp_path (EACCES under keep-id userns).\n"
            "      proxy_request_buffering off;\n"
            "      proxy_buffering off;\n"
            "    }\n"
        )
        post_chat = (
            "    # Container-to-chat API: containers post chat messages via workspace JWT.\n"
            "    location = /api/v1/workspaces/post-chat-message {\n"
            f"{acl}\n"
            "      auth_request /api/v1/auth/verify-workspace-token;\n"
            "      auth_request_set $auth_token_error $upstream_http_x_token_error;\n"
            "      error_page 401 = @token_auth_failed;\n"
            f"      proxy_pass {upstream}/api/v1/workspaces/post-chat-message;\n"
            f"{common_headers}"
            "      # Stream the request body (#1682): a chat message with a large\n"
            "      # tool result can exceed client_body_buffer_size; without this\n"
            "      # it spills to client_body_temp_path (EACCES under keep-id userns).\n"
            "      proxy_request_buffering off;\n"
            "    }\n"
        )
        return f"{llm_block}{delegate}{post_chat}{_egress_auth_locations(upstream)}"

    def _render_headless_config(self, upstream: str) -> str:
        """Render the headless ``nginx.conf`` — egress listener only (#1542).

        ``KLANGK_PORT`` is unset ⇒ headless: no browser listener is rendered.
        the proxy listens on ``KLANGK_EGRESS_PORT`` for container → backend egress
        (``/llm-proxy``, ``/api/v1/browser-delegate``,
        ``/api/v1/workspaces/post-chat-message``). The browser UI, ``/hosted/``,
        ``/auth/local``, and the catch-all ``location /`` are all absent — the
        only served surface is container egress (plus same-uid UDS access to
        the backend, which bypasses the proxy entirely).
        """
        egress_port = self._app.state.settings.egress_port
        egress_listen = self._app.state.settings.egress_listen
        client_max_body_size = self.compute_client_max_body_size()
        resolvers = self.detect_dns_resolvers()
        acl, _ = self.compute_container_acls()
        egress_locations = self._egress_locations(upstream, acl, resolvers)
        realip = self._realip_block()
        return f"""daemon off;
pid /tmp/nginx.pid;
error_log stderr;
events {{ worker_connections 1024; }}
http {{
  access_log /dev/stdout;
  client_body_temp_path /tmp/nginx_client_body;
  proxy_temp_path /tmp/nginx_proxy;
  fastcgi_temp_path /tmp/nginx_fastcgi;
  uwsgi_temp_path /tmp/nginx_uwsgi;
  scgi_temp_path /tmp/nginx_scgi;

  client_max_body_size {client_max_body_size};
{realip}
  server {{
    listen {egress_listen}:{egress_port};
{egress_locations}  }}
}}
"""

    def _render_full_config(self, upstream: str) -> str:
        """Render the full (browser) ``nginx.conf`` — two listeners (#1542).

        ``KLANGK_PORT`` is set ⇒ full/browser mode. Two server blocks:

        - **Egress listener** (``listen {egress_listen}:{egress_port};``): container → backend
          egress (shared with headless via :meth:`_egress_locations`).
        - **Browser listener** (``listen {listen}:{port};``): the browser UI,
          ``/hosted/``, ``/auth/local``, and the catch-all ``location /``.

        Splitting browser ingress from container egress onto separate ports
        lets operators firewall them independently. The browser block carries
        no ``auth_request`` infra (it has no token-gated locations); all of
        that lives in the egress block.
        """
        listen_addr = self._app.state.settings.listen
        port = self._app.state.settings.port
        egress_port = self._app.state.settings.egress_port
        egress_listen = self._app.state.settings.egress_listen
        client_max_body_size = self.compute_client_max_body_size()
        resolvers = self.detect_dns_resolvers()
        acl, deny = self.compute_container_acls()

        hosted_block = self._build_hosted_block()
        egress_locations = self._egress_locations(upstream, acl, resolvers)
        realip = self._realip_block()
        # The container-source flag used by the browser catch-all's
        # ``if ($container_source) { return 403; }`` guard (the deny value
        # above). http-scope ``geo`` keyed on the pre-realip peer — see
        # compute_container_geo().
        geo = self.compute_container_geo()
        trust = self._trust_outer_proxy()
        if trust:
            forwarded_headers = (
                "      proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;\n"
                "      proxy_set_header X-Forwarded-Host $http_x_forwarded_host;\n"
                "      proxy_set_header X-Forwarded-Prefix $http_x_forwarded_prefix;\n"
            )
        else:
            forwarded_headers = (
                "      proxy_set_header X-Forwarded-Proto $scheme;\n"
                "      proxy_set_header X-Forwarded-Host $http_host;\n"
            )

        # The three proxy-header lines every proxied location shares.
        common_headers = (
            "      proxy_set_header Host $http_host;\n"
            "      proxy_set_header X-Real-IP $remote_addr;\n"
            "      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "      proxy_http_version 1.1;\n"
        )

        conf = f"""daemon off;
pid /tmp/nginx.pid;
error_log stderr;
events {{ worker_connections 1024; }}
http {{
  access_log /dev/stdout;
  client_body_temp_path /tmp/nginx_client_body;
  proxy_temp_path /tmp/nginx_proxy;
  fastcgi_temp_path /tmp/nginx_fastcgi;
  uwsgi_temp_path /tmp/nginx_uwsgi;
  scgi_temp_path /tmp/nginx_scgi;

  map $http_upgrade $connection_upgrade {{
    default upgrade;
    "" close;
  }}

  # WebSocket upgrade specifically (not h2c/other); gates the slash-less
  # hosted location's redirect-vs-proxy choice.
  map $http_upgrade $hosted_is_ws {{
    default 0;
    "~*^websocket$" 1;
  }}

  client_max_body_size {client_max_body_size};
{realip}
{geo}  # --- Container-egress listener (container → backend via host.containers.internal) ---
  server {{
    listen {egress_listen}:{egress_port};
{egress_locations}  }}

  # --- Browser listener (browser UI + API + hosted apps) ---
  server {{
    listen {listen_addr}:{port};

{hosted_block}    # No-auth single-user token handout (POST /api/v1/auth/local, #1374).
    # In none mode this freely issues an admin token, so it must be reachable
    # from the operator's browser (loopback) but NOT from workspace containers.
    location = /api/v1/auth/local {{
      allow 127.0.0.1;
      allow ::1;
      deny all;
      proxy_pass {upstream};
{common_headers}    }}

    location / {{
{deny}
      proxy_pass {upstream}/;
{common_headers}{forwarded_headers}      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
    }}
  }}
}}
"""
        return conf

    # -- I/O + binary location --------------------------------------------

    def write_config(self, upstream: str, conf_path: str | Path) -> str:
        """Render the config and write it to ``conf_path`` (returns the text).

        Written mode ``0600`` because the rendered config may embed secrets
        (notably the LLM API key, which nginx needs in the ``/llm-proxy/`` block
        to set the ``Authorization`` header — there is no way to proxy that
        header without nginx knowing it). The file lives under the same
        same-uid-only ``state_dir`` as the UDS, so the restrictive mode matches
        the existing trust boundary.
        """
        text = self.render_config(upstream)
        path = Path(conf_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The LLM API key is necessarily in this config (nginx adds the
        # Authorization header on the proxied upstream); 0600 keeps it private
        # to the klangk user. lgtm[py/clear-text-storage-of-sensitive-data]
        # — the storage is intentional and unavoidable for nginx-based proxying.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return text

    def find_proxy_bin(self) -> str:
        """Locate the nginx binary: KLANGK_PROXY_BIN > PATH > /usr/sbin/nginx."""
        configured = self._app.state.settings.proxy_bin
        if configured:
            return str(configured)
        found = shutil.which("nginx")
        if found:
            return found
        return "/usr/sbin/nginx"


_PR_SET_PDEATHSIG = 1
_HAS_PDEATHSIG = sys.platform == "linux"
if _HAS_PDEATHSIG:
    _libc = ctypes.CDLL(
        ctypes.util.find_library("c") or "libc.so.6", use_errno=True
    )


def _proxy_preexec() -> None:  # pragma: no cover  – runs in forked child
    """New session (for killpg) + auto-SIGTERM when parent dies (#1533).

    ``os.setsid()`` puts nginx in its own process group so ``stop()`` can
    ``os.killpg`` the entire tree on clean shutdown.

    On Linux, ``prctl(PR_SET_PDEATHSIG, SIGTERM)`` asks the kernel to send
    SIGTERM to the nginx master if klangkd dies without calling ``stop()``
    (e.g. SIGKILL).  nginx handles SIGTERM by forwarding SIGQUIT to its
    workers, so the whole tree exits.  macOS has no equivalent; on unclean
    shutdown, orphaned nginx processes must be cleaned up externally.
    """
    os.setsid()
    if _HAS_PDEATHSIG:
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


class ProxyWatchdog:
    """Owns the proxy child process and its supervision task (#1463).

    The proxy is currently the nginx child process rendered by
    :class:`ProxyRenderer`. Constructed with ``app_state`` and owns a
    :class:`ProxyRenderer` instance for config rendering (#1469). Settings
    are read live via ``self.app.state.settings`` (#1608).
    Stored on ``app.state.proxy_watchdog``; the lifespan calls
    ``.start()`` / ``.stop()``.
    """

    def __init__(self, app) -> None:
        self._app = app
        self._renderer = ProxyRenderer(app)
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def reconfigure(self, app) -> None:
        self._app = app
        self._renderer.reconfigure(app)

    async def _watch(
        self, bin_path: str, conf_path: str
    ) -> None:  # pragma: no cover
        """Spawn nginx and respawn it on unexpected exit (with backoff).

        Exits cleanly when ``_stopping`` is set (a cooperative shutdown).
        ``_proxy_preexec`` puts nginx in its own session (for ``os.killpg``
        on clean shutdown) and sets ``PR_SET_PDEATHSIG(SIGTERM)`` so the
        kernel auto-signals nginx if klangkd dies uncleanly (#1533).
        """
        backoff = 1.0
        while not self._stopping:
            self._proc = await asyncio.create_subprocess_exec(
                bin_path,
                "-e",
                "stderr",
                "-c",
                conf_path,
                stdout=None,
                stderr=None,
                preexec_fn=_proxy_preexec,
            )
            logger.info(
                "nginx started (pid %d) with %s", self._proc.pid, conf_path
            )
            rc = await self._proc.wait()
            self._proc = None
            if self._stopping:
                return
            logger.warning(
                "nginx exited (rc=%d); restarting in %.1fs", rc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _prepare(self) -> tuple[str, str]:
        """Render nginx.conf and return ``(bin_path, conf_path)``.

        uvicorn always binds the UDS at ``settings.socket`` (default
        ``<state_dir>/klangk.sock``, overridable via ``KLANGK_SOCKET``); the proxy
        proxies to that socket regardless of deployment shape. ``KLANGK_PORT``
        selects headless (unset) vs full (set) templates and the nginx listen
        directives; the upstream is always the UDS.
        """
        uds_path = self._app.state.settings.socket
        conf_path = os.path.join(
            self._app.state.settings.state_dir, "nginx.conf"
        )
        bin_path = self._renderer.find_proxy_bin()
        self._renderer.write_config(uds_upstream(uds_path), conf_path)
        return bin_path, conf_path

    async def start(self) -> None:
        """Render the proxy config and start the proxy watchdog.

        Gated only by ``_KLANGK_DISABLE_PROXY`` — an **internal,
        non-user-facing** env var the test suite sets to suppress nginx spawn
        (tests boot the app via the lifespan and don't want a real nginx
        process). Not a documented config knob; no operator-facing name.
        """
        if os.environ.get("_KLANGK_DISABLE_PROXY"):
            return
        bin_path, conf_path = self._prepare()
        self._stopping = False
        # The real watchdog (nginx spawn + respawn loop) is covered by the
        # e2e ACL suite; here create_task just schedules the coroutine.
        self._task = asyncio.create_task(self._watch(bin_path, conf_path))

    async def stop(self) -> None:
        """Stop the proxy and cancel the watchdog (cooperative: waits for exit).

        Kills the entire process group (master + workers) so no orphaned
        workers linger after shutdown (#1533).
        """
        self._stopping = True
        proc = self._proc
        # The proc-kill branch is only reached when nginx was spawned (UDS
        # mode); covered via TestStopWatchdog + the e2e ACL teardown.
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
        # Same: only when a watchdog task was created (UDS mode).
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
