"""Python-owned nginx config renderer + process manager (#1396).

Replaces ``scripts/nginx.sh`` (the bash heredoc that generated ``nginx.conf``)
and the ``/home/klangk/bin/nginx`` shim it was copied into for the host
container. ``klangkd`` calls :func:`render_config` to write the config, then
:func:`start_nginx` / :func:`stop_nginx` (an async watchdog in the lifespan
owns the child process).

The renderer is a pure function of the merged config (settings + env probes
for host-IP / DNS auto-detection). It takes the upstream proxy target as a
parameter so it serves both the production UDS bind
(:func:`uds_upstream`) and the TCP bind tests use
(:func:`tcp_upstream`) — only the ``proxy_pass`` base differs.

See #1392 (design record) and #1396 (this chunk).

The settings-driven rendering logic lives on :class:`NginxRenderer`, an
owned instance constructed with ``app_state`` (``self._settings =
app_state.settings``) per the composition-root refactor (#1426, #1469).
Pure helpers (upstream constructors, host-IP auto-detection, the minimal-
template auth-location formatter) stay module-level — they don't read
settings.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .settings import (
    KlangkSettings,
    listen_is_socket,
)

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


def _minimal_auth_locations(upstream: str) -> str:
    """The workspace-token ``auth_request`` subrequest target + JSON 401 page.

    Minimal-template counterpart of the inline blocks in the full template.
    Extracted because the minimal server block emits them adjacent to the
    ``/llm-proxy`` location that gates on them; the full template interleaves
    them with the browser/auth-local locations, so it keeps them inline.
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


class NginxRenderer:
    """Settings-driven ``nginx.conf`` renderer (#1396, #1469).

    Constructed with ``app_state`` (``self._settings = app_state.settings``)
    per the composition-root pattern. The renderer is a pure function of the
    merged config (settings + env probes); it does not touch podman.
    ``NginxWatchdog`` owns an instance and calls :meth:`render_config` /
    :meth:`find_nginx_bin` / :meth:`write_config` from its ``_prepare`` step.
    """

    def __init__(self, app_state) -> None:
        self._app_state = app_state
        self._settings: KlangkSettings = app_state.settings

    # -- DNS / ACL / size computation --------------------------------------

    def detect_dns_resolvers(self) -> str:
        """Space-separated nameservers for nginx's ``resolver`` directive.

        From ``KLANGK_DNS_SERVERS`` (comma→space) if set, else parsed from
        ``/etc/resolv.conf`` (IPv6 bracketed for nginx), else ``8.8.8.8``.
        """
        raw = self._settings.dns_servers
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

    def compute_container_acls(self) -> tuple[str, str]:
        """Build the CONTAINER_ACL (allowlist) and CONTAINER_DENY (blocklist) text.

        Returns ``(acl_block, deny_block)`` where each is the indented
        ``allow``/``deny`` directives (no leading/trailing newline). Two
        complementary ACLs from one source set (#1376):

        - CONTAINER_ACL: allowlist on the three container endpoints
          (llm-proxy, browser-delegate, post-chat-message). Allows the container
          source IPs, denies everyone else.
        - CONTAINER_DENY: blocklist on the catch-all ``location /``. Denies the
          container source IPs so a container reaches ONLY those three endpoints,
          not the whole /api/v1/* tree. Loopback is ALWAYS excluded (local
          browsers need the full UI/API).

        With an explicit ``KLANGK_CONTAINER_SUBNETS`` override, 127.0.0.1 is NOT
        implicitly added to CONTAINER_ACL (the operator's list is used verbatim).
        """
        explicit = self._settings.container_subnets
        if explicit:
            subnets = [
                s.strip() for s in str(explicit).split(",") if s.strip()
            ]
            acl_lines = [f"      allow {s};" for s in subnets]
            deny_lines = [
                f"      deny {s};" for s in subnets if not _is_loopback(s)
            ]
            if not deny_lines:
                logger.warning(
                    "container source set has no non-loopback entries — "
                    "catch-all location / denies nothing (deny-by-default inactive)"
                )
        else:
            addrs = detect_host_ipv4s()
            if addrs:
                acl_lines = [f"      allow {a};" for a in addrs]
                deny_lines = [
                    f"      deny {a};" for a in addrs if not _is_loopback(a)
                ]
            else:
                logger.warning(
                    "container subnet detection failed, using fallback RFC1918 ranges"
                )
                acl_lines = [
                    f"      allow {s};" for s in _FALLBACK_ACL_SUBNETS
                ]
                deny_lines = [
                    f"      deny {s};" for s in _FALLBACK_DENY_SUBNETS
                ]
        acl = "\n".join(acl_lines) + "\n      deny all;"
        deny = "\n".join(deny_lines) + "\n      allow all;"
        return acl, deny

    def compute_client_max_body_size(self) -> str:
        """Derive nginx ``client_max_body_size`` from ``KLANGK_FILE_UPLOAD_SIZE_MAX``.

        The setting is in bytes (default 500 MB); nginx wants ``Nm``. Minimum 1m.
        """
        raw = self._settings.file_upload_size_max
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
        raw = self._settings.hosted_ports_per_workspace
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
        """
        base_url = self._settings.llm_base_url
        if not base_url:
            return ""
        api_key = self._settings.llm_api_key or ""
        return (
            f"    location ~ ^/llm-proxy/(.*)$ {{\n"
            f"{acl}\n"
            "      auth_request /api/v1/auth/verify-workspace-token;\n"
            "      auth_request_set $auth_token_error $upstream_http_x_token_error;\n"
            "      error_page 401 = @token_auth_failed;\n"
            f"      resolver {resolvers} valid=30s;\n"
            f"      set $llm_backend {base_url}/$1;\n"
            "      proxy_pass $llm_backend;\n"
            f'      proxy_set_header Authorization "Bearer {api_key}";\n'
            "      proxy_set_header Host $proxy_host;\n"
            "      proxy_ssl_server_name on;\n"
            "      proxy_http_version 1.1;\n"
            '      proxy_set_header Connection "";\n'
            "      proxy_buffering off;\n"
            "      proxy_cache off;\n"
            "      chunked_transfer_encoding on;\n"
            "    }\n"
        )

    def _trust_outer_proxy(self) -> bool:
        raw = self._settings.trust_outer_proxy or ""
        return str(raw).strip().lower() in ("1", "true", "yes")

    # -- Main renderer -----------------------------------------------------

    def render_config(self, upstream: str) -> str:
        """Render ``nginx.conf`` as a string.

        Template selection keys off ``KLANGK_LISTEN``'s shape only (#1398): a
        socket path ⇒ the minimal (headless) template; TCP ⇒ the full (browser)
        template. The AUTH value does not participate in template selection —
        only the bind does. ``upstream`` is the ``proxy_pass`` base
        (:func:`uds_upstream` for the production socket bind, :func:`tcp_upstream`
        for tests). All other values come from the merged settings
        (env > config file > defaults) plus the host-IP / DNS auto-detection
        probes.
        """
        if listen_is_socket(self._settings.listen):
            return self._render_minimal_config(upstream)
        return self._render_full_config(upstream)

    def _render_minimal_config(self, upstream: str) -> str:
        """Render the minimal (headless) ``nginx.conf`` — socket bind only (#1398).

        Emitted when ``KLANGK_LISTEN`` is a socket path: a browser can't reach a
        UDS and uvicorn exposes no browser-facing TCP, so no browser UI is
        serviceable. The only served surface is the container-egress
        ``/llm-proxy`` location (with its workspace-token ``auth_request`` gate +
        CONTAINER_ACL) on the single container-egress listener. No ``location /``,
        no ``/api/v1/*``, no static UI, no ``/auth/local`` — the attack surface is
        two channels (operator→UDS, container→llm-proxy) and nothing else.

        The ``auth_request`` subrequest target + JSON 401 page are emitted only
        when an ``/llm-proxy`` location exists to gate on them (i.e. when
        ``KLANGK_LLM_BASE_URL`` is set); with no LLM configured the server block
        serves nothing.
        """
        nginx_port = self._settings.nginx_port
        client_max_body_size = self.compute_client_max_body_size()
        resolvers = self.detect_dns_resolvers()
        acl, _deny = self.compute_container_acls()
        llm_block = self._build_llm_block(acl, resolvers)
        # The auth_request infrastructure is only reachable via the /llm-proxy
        # location's auth_request; omit it entirely when there's no LLM proxy.
        auth_locations = _minimal_auth_locations(upstream) if llm_block else ""
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

  server {{
    listen {nginx_port};
{llm_block}{auth_locations}  }}
}}
"""

    def _render_full_config(self, upstream: str) -> str:
        """Render the full (browser) ``nginx.conf`` — TCP bind (#1396, #1398).

        Emitted when ``KLANGK_LISTEN`` is TCP: the browser UI + every API path +
        static files + the no-auth ``/auth/local`` handout are all serviceable.
        This is the template the renderer shipped before #1398's socket/minimal
        branch; it is kept verbatim so the TCP path is a strict regression guard.
        """
        nginx_port = self._settings.nginx_port
        client_max_body_size = self.compute_client_max_body_size()
        resolvers = self.detect_dns_resolvers()
        acl, deny = self.compute_container_acls()

        hosted_block = self._build_hosted_block()
        llm_block = self._build_llm_block(acl, resolvers)
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

  server {{
    listen {nginx_port};

{hosted_block}{llm_block}    # Browser-delegate bridge: Pi extensions delegate long-running
    # actions through here. Read timeout accommodates streaming + git-credential.
    location /api/v1/browser-delegate {{
{acl}
      auth_request /api/v1/auth/verify-workspace-token;
      auth_request_set $auth_token_error $upstream_http_x_token_error;
      error_page 401 = @token_auth_failed;
      proxy_pass {upstream};
{common_headers}      proxy_read_timeout 920s;
      proxy_send_timeout 920s;
      proxy_buffering off;
    }}

    # Container-to-chat API: containers post chat messages via workspace JWT.
    location = /api/v1/workspaces/post-chat-message {{
{acl}
      auth_request /api/v1/auth/verify-workspace-token;
      auth_request_set $auth_token_error $upstream_http_x_token_error;
      error_page 401 = @token_auth_failed;
      proxy_pass {upstream}/api/v1/workspaces/post-chat-message;
{common_headers}    }}

    # Workspace token verification subrequest (nginx auth_request target).
    location = /api/v1/auth/verify-workspace-token {{
      internal;
      proxy_pass {upstream}/api/v1/auth/verify-workspace-token;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Authorization $http_authorization;
    }}

    # No-auth single-user token handout (POST /api/v1/auth/local, #1374).
    # In none mode this freely issues an admin token, so it must be reachable
    # from the operator's browser (loopback) but NOT from workspace containers.
    location = /api/v1/auth/local {{
      allow 127.0.0.1;
      allow ::1;
      deny all;
      proxy_pass {upstream};
{common_headers}    }}

    # JSON 401 error page for auth_request failures.
    location @token_auth_failed {{
      internal;
      default_type application/json;
      return 401 '{{"error":"$auth_token_error","detail":"Workspace token $auth_token_error"}}';
    }}

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

    def find_nginx_bin(self) -> str:
        """Locate the nginx binary: KLANGK_NGINX_BIN > PATH > /usr/sbin/nginx."""
        configured = self._settings.nginx_bin
        if configured:
            return str(configured)
        found = shutil.which("nginx")
        if found:
            return found
        return "/usr/sbin/nginx"
