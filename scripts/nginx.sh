#!/usr/bin/env bash
set -euo pipefail

# KLANGK_STATE_DIR is set in the host container; DEVENV_STATE is set by devenv
# in the dev environment. One of them must be set.
NGINX_STATE="${KLANGK_STATE_DIR:-${DEVENV_STATE:-}}/nginx"
if [ -z "${KLANGK_STATE_DIR:-}${DEVENV_STATE:-}" ]; then
  echo "error: KLANGK_STATE_DIR or DEVENV_STATE must be set" >&2
  exit 1
fi
mkdir -p "$NGINX_STATE"

# Derive nginx client_max_body_size from KLANGK_FILE_UPLOAD_SIZE_MAX (bytes).
# Default 500 MB. Convert to MB for nginx (minimum 1m).
_upload_bytes="${KLANGK_FILE_UPLOAD_SIZE_MAX:-524288000}"
_upload_mb=$((_upload_bytes / 1048576))
[ "$_upload_mb" -lt 1 ] && _upload_mb=1
KLANGK_NGINX_CLIENT_MAX_BODY_SIZE="${_upload_mb}m"

# nginx resolver needs space-separated IPs; env var may use commas.
# Default: parse /etc/resolv.conf for nameservers (works on host and
# in Docker). Fall back to 8.8.8.8 if resolv.conf has no entries.
if [ -n "${KLANGK_DNS_SERVERS:-}" ]; then
  DNS_RESOLVERS="${KLANGK_DNS_SERVERS//,/ }"
else
  # Wrap IPv6 addresses in brackets for nginx resolver directive.
  DNS_RESOLVERS=$(awk '/^nameserver/{
    addr = $2
    if (addr ~ /:/) addr = "[" addr "]"
    printf "%s ", addr
  }' /etc/resolv.conf)
  DNS_RESOLVERS="${DNS_RESOLVERS:-8.8.8.8}"
fi

# Container source-IP set and the two ACLs derived from it.
#
# Podman uses pasta networking (rootless default): containers share the
# host's network via userspace NAT, so traffic to host.containers.internal
# arrives from the host's own IP (e.g. 192.168.1.112), not a virtual bridge
# subnet. We auto-detect the host's IPv4 addresses as the "container source
# set". Override with KLANGK_CONTAINER_SUBNETS (comma-separated CIDRs).
#
# Two complementary ACLs are built from this one set (#1376):
#
#   CONTAINER_ACL  — allowlist on the three explicit container endpoints
#     (^/llm-proxy/, /api/v1/browser-delegate, post-chat-message). Allows the
#     container source IPs; denies everyone else. Container traffic still has
#     to pass the workspace-token auth_request after the ACL.
#
#   CONTAINER_DENY  — blocklist on the catch-all `location /`. Denies the
#     container source IPs so a container can reach ONLY the three explicit
#     endpoints, not the whole /api/v1/* tree. This inverts nginx's container
#     model to deny-by-default: safety no longer relies on every backend
#     endpoint remembering its Depends(auth) — a forgotten dependency is
#     refused at nginx before the backend sees it. The realistic container
#     escalation path is API brute-forcing for an endpoint that forgot its
#     auth dependency; this caps that surface structurally.
#
# Loopback (127.0.0.0/8, ::1) is ALWAYS excluded from CONTAINER_DENY: local
# browsers connect via loopback and must reach the full UI/API. Container NAT
# traffic appears as the host's *non-loopback* IP, which is what we deny here.
# (A browser running on the server host that connects via the host's LAN IP
# rather than loopback would share the container source IP and be denied —
# use loopback for local browsing.)
#
# 127.0.0.1 is NOT implicitly added to CONTAINER_ACL with an explicit
# KLANGK_CONTAINER_SUBNETS override; include it in the list if needed.

_explicit_override=false
if [ -n "${KLANGK_CONTAINER_SUBNETS:-}" ]; then
  # Explicit override — use exactly what the operator specified.
  IFS=',' read -ra _subnets <<<"$KLANGK_CONTAINER_SUBNETS"
  _explicit_override=true
else
  # Auto-detect: pasta NAT makes container traffic appear as the host's own
  # addresses. Collect both IPv4 and IPv6 so a container that reaches nginx
  # over IPv6 (podman configured for IPv6) is allowed on the container
  # endpoints instead of hitting deny all (#1385).
  #
  # `ip -4` includes 127.0.0.1 and `ip -6` includes ::1 (the lo interface);
  # loopback is wanted for CONTAINER_ACL (loopback reaches container
  # endpoints in dev) but is filtered out of CONTAINER_DENY below so local
  # browsers are not blocked from the catch-all. IPv6 link-local (fe80::/10)
  # is skipped — it needs a zone id nginx can't express and isn't how
  # container traffic sources.
  _subnets=()
  while IFS= read -r addr; do
    [ -n "$addr" ] && _subnets+=("$addr")
  done < <(ip -4 addr show 2>/dev/null | awk '/inet /{sub(/\/.*/, "", $2); print $2}')
  while IFS= read -r addr; do
    [ -n "$addr" ] && _subnets+=("$addr")
  done < <(ip -6 addr show 2>/dev/null | awk '/inet6/{sub(/\/.*/, "", $2); print $2}' | grep -v '^fe80')
fi

# is_loopback — true for any address in 127.0.0.0/8 or ::1. Used to keep
# loopback out of CONTAINER_DENY (local browsers depend on the catch-all).
_is_loopback() {
  case "$1" in
  127.* | ::1*) return 0 ;;
  *) return 1 ;;
  esac
}

if [ ${#_subnets[@]} -gt 0 ]; then
  CONTAINER_ACL=$'\n'
  CONTAINER_DENY=$'\n'
  _deny_count=0
  for cidr in "${_subnets[@]}"; do
    CONTAINER_ACL+="      allow ${cidr};"$'\n'
    # Never deny loopback on the catch-all — local browsers rely on it.
    if ! _is_loopback "$cidr"; then
      CONTAINER_DENY+="      deny ${cidr};"$'\n'
      _deny_count=$((_deny_count + 1))
    fi
  done
  CONTAINER_ACL+="      deny all;"
  CONTAINER_DENY+="      allow all;"
  if [ "$_deny_count" -eq 0 ]; then
    echo "nginx: WARNING: container source set has no non-loopback entries — catch-all location / denies nothing (deny-by-default inactive)" >&2
  fi
  echo "nginx container ACL: ${_subnets[*]}${_explicit_override:+ (explicit)}" >&2
else
  # Fallback: broad RFC1918 ranges covering typical container subnets.
  # 192.168.0.0/16 is intentionally excluded — it is the most common
  # LAN range and allowing it would expose the LLM proxy to LAN peers.
  CONTAINER_ACL="
      allow 172.16.0.0/12;
      allow 10.0.0.0/8;
      allow 127.0.0.1;
      allow ::1;
      deny all;"
  # Inverse of the allowlist minus loopback (127.0.0.1 stays allowed so
  # local browsers reach the full UI/API).
  CONTAINER_DENY="
      deny 172.16.0.0/12;
      deny 10.0.0.0/8;
      allow all;"
  echo "nginx container ACL: subnet detection failed, using fallback RFC1918 ranges" >&2
fi

# LLM proxy block: only included if KLANGK_LLM_BASE_URL is configured.
# Containers hit this instead of the real endpoint, so they never see the
# API key. Uses a variable so nginx resolves the upstream at request time,
# not at config load time (avoids crash on unresolvable hosts).
# NOTE: nginx resolver doesn't support search domains, so KLANGK_LLM_BASE_URL
# must use a FQDN or IP address — bare hostnames won't resolve.
#
# These vars are consumed via bash expansion (never the Python resolver),
# so resolve any file:/cmd: prefix here first — otherwise the prefix would
# be emitted verbatim into nginx.conf (e.g. "Bearer cmd:..."). The
# klangk-resolve-value console script shares the single source of truth
# in klangk_backend.util.resolve_file_value (no shell reimplementation).
KLANGK_LLM_BASE_URL="$(klangk-resolve-value "${KLANGK_LLM_BASE_URL:-}")"
KLANGK_LLM_API_KEY="$(klangk-resolve-value "${KLANGK_LLM_API_KEY:-}")"
LLM_BLOCK=""
if [ -n "${KLANGK_LLM_BASE_URL:-}" ]; then
  LLM_BLOCK="
    location ~ ^/llm-proxy/(.*)\$ {
${CONTAINER_ACL}
      auth_request /api/v1/auth/verify-workspace-token;
      auth_request_set \$auth_token_error \$upstream_http_x_token_error;
      error_page 401 = @token_auth_failed;
      resolver ${DNS_RESOLVERS} valid=30s;
      set \$llm_backend ${KLANGK_LLM_BASE_URL}/\$1;
      proxy_pass \$llm_backend;
      proxy_set_header Authorization \"Bearer ${KLANGK_LLM_API_KEY:-}\";
      proxy_set_header Host \$proxy_host;
      proxy_ssl_server_name on;
      proxy_http_version 1.1;
      proxy_set_header Connection \"\";
      proxy_buffering off;
      proxy_cache off;
      chunked_transfer_encoding on;
    }
"
fi

# Security: by default klangk's nginx OVERWRITES client-supplied
# X-Forwarded-Host/-Proto with authoritative values ($http_host / $scheme)
# so an attacker hitting nginx cannot poison the verification/reset/OIDC
# links the backend generates. Set KLANGK_TRUST_OUTER_PROXY=1 (or true)
# ONLY when a trusted outer proxy sits in front of klangk's nginx and you
# need its X-Forwarded-* values to survive (and that outer proxy itself
# overwrites, not passes through, these headers).
TRUST_OUTER_PROXY=0
case "${KLANGK_TRUST_OUTER_PROXY:-}" in
1 | true | TRUE | yes | YES) TRUST_OUTER_PROXY=1 ;;
esac

# Hosted-app serving is disabled entirely when the per-workspace port cap
# (KLANGK_HOSTED_PORTS_PER_WORKSPACE) is exactly 0 — mirrors the backend's
# ports_per_workspace_cap(). Any other value leaves the proxy enabled; the
# backend clamps non-int to the default 5, and the proxy only needs the
# boolean "is hosting turned off". #1237
HOSTED_PORTS_PER_WS="${KLANGK_HOSTED_PORTS_PER_WORKSPACE:-5}"
HOSTED_BLOCK=""
if [ "$HOSTED_PORTS_PER_WS" = "0" ]; then
  HOSTED_BLOCK="
    # Hosted-app serving is disabled (KLANGK_HOSTED_PORTS_PER_WORKSPACE=0).
    location ^~ /hosted/ {
      return 404;
    }
"
else
  HOSTED_BLOCK="
    # A hosted URL without a trailing slash (e.g. .../9001) can't match the
    # proxy location below. Proxying it to the app root wouldn't help either:
    # hosted apps emit relative asset paths (./assets/...) that resolve against
    # the browser's base URL, so without the slash every asset 404s. Redirect to
    # the canonical trailing-slash form so the base URL is correct.
    # Named capture (not \$1): the \$hosted_is_ws regex map clobbers positional
    # captures, which would leave proxy_pass with an empty port.
    location ~ ^/hosted/[^/]+/(?<hosted_port>\d+)\$ {
      # Non-WebSocket: redirect to the trailing-slash form so relative assets
      # (./assets/...) resolve. Only \`return\` inside \`if\` -> \"if is evil\" N/A.
      if (\$hosted_is_ws = 0) {
        return 308 \$uri/\$is_args\$args;
      }
      # WebSocket clients cannot follow a 308 (RFC 6455 4.1); some apps open
      # their socket at this slash-less root. Proxy instead. \$is_args\$args:
      # a variable in proxy_pass drops the query (e.g. auth token) otherwise.
      proxy_pass http://127.0.0.1:\$hosted_port/\$is_args\$args;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_http_version 1.1;
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
    }

    # Hosted app proxy: extract port from URL and proxy directly to container
    location ~ ^/hosted/[^/]+/(\d+)/(.*)\$ {
      proxy_pass http://127.0.0.1:\$1/\$2\$is_args\$args;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_http_version 1.1;
      # Hosted apps (marimo, jupyter, vite, ...) talk to their backends over
      # websockets. Without these the WS handshake never upgrades and the app
      # reports things like \"kernel not found\".
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
    }
"
fi

# IPv6 ingress listener (opt-in). Disabled by default: a `listen [::]:`
# directive crashes nginx at startup on kernels with IPv6 compiled out
# (ipv6.disable=1 on the kernel cmdline makes socket() return EAFNOSUPPORT —
# see nginx trac #1320), which would take the whole app down. Set
# KLANGK_NGINX_ENABLE_IPV6=1 to add `listen [::]:${KLANGK_NGINX_PORT}` for
# dual-stack ingress (nginx sets ipv6only=on by default, so the IPv4 and
# IPv6 listens don't conflict). This is the primary IPv6 win: an IPv6-only
# client can reach the app. nginx->uvicorn stays IPv4 loopback internally
# (clients never see that hop), so this works independently of the backend
# bind address (#1385).
IPV6_LISTEN_LINE=""
if [ "${KLANGK_NGINX_ENABLE_IPV6:-}" = "1" ]; then
  IPV6_LISTEN_LINE="    listen [::]:${KLANGK_NGINX_PORT};"
fi

cat >"$NGINX_STATE/nginx.conf" <<EOF_SECURE
daemon off;
pid /tmp/nginx.pid;
error_log stderr;
events { worker_connections 1024; }
http {
  access_log /dev/stdout;
  client_body_temp_path /tmp/nginx_client_body;
  proxy_temp_path /tmp/nginx_proxy;
  fastcgi_temp_path /tmp/nginx_fastcgi;
  uwsgi_temp_path /tmp/nginx_uwsgi;
  scgi_temp_path /tmp/nginx_scgi;

  map \$http_upgrade \$connection_upgrade {
    default upgrade;
    "" close;
  }

  # WebSocket upgrade specifically (not h2c/other Upgrade tokens); gates the
  # slash-less hosted location's redirect-vs-proxy choice below.
  map \$http_upgrade \$hosted_is_ws {
    default 0;
    "~*^websocket\$" 1;
  }

  client_max_body_size ${KLANGK_NGINX_CLIENT_MAX_BODY_SIZE};

  server {
    listen ${KLANGK_NGINX_PORT};
${IPV6_LISTEN_LINE}

${HOSTED_BLOCK}
${LLM_BLOCK}
    # Browser-delegate bridge: Pi extensions delegate long-running actions
    # (soliplex RAG + LLM) through here. The read timeout must accommodate
    # the git-credential device flow (up to 15 min) as well as streaming
    # RAG/LLM responses, so it exceeds the backend's max bridge timeout.
    location /api/v1/browser-delegate {
${CONTAINER_ACL}
      auth_request /api/v1/auth/verify-workspace-token;
      auth_request_set \$auth_token_error \$upstream_http_x_token_error;
      error_page 401 = @token_auth_failed;
      proxy_pass http://127.0.0.1:${KLANGK_PORT};
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_http_version 1.1;
      proxy_read_timeout 920s;
      proxy_send_timeout 920s;
      proxy_buffering off;
    }

    # Container-to-chat API: containers post chat messages via workspace JWT.
    location = /api/v1/workspaces/post-chat-message {
${CONTAINER_ACL}
      auth_request /api/v1/auth/verify-workspace-token;
      auth_request_set \$auth_token_error \$upstream_http_x_token_error;
      error_page 401 = @token_auth_failed;
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/api/v1/workspaces/post-chat-message;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_http_version 1.1;
    }

    # Workspace token verification subrequest (nginx auth_request target).
    # X-Token-Error is captured so the 401 error page can return a
    # meaningful JSON body to containers (expired vs invalid vs missing).
    location = /api/v1/auth/verify-workspace-token {
      internal;
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/api/v1/auth/verify-workspace-token;
      proxy_pass_request_body off;
      proxy_set_header Content-Length "";
      proxy_set_header Authorization \$http_authorization;
    }

    # JSON 401 error page for auth_request failures.  The \$auth_token_error
    # variable is set from the X-Token-Error header of the auth subrequest.
    location @token_auth_failed {
      internal;
      default_type application/json;
      return 401 '{\"error\":\"\$auth_token_error\",\"detail\":\"Workspace token \$auth_token_error\"}';
    }

    location / {
${CONTAINER_DENY}
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_http_version 1.1;
      # Security: X-Forwarded-Host/Proto are derived from trusted values
      # (the Host nginx received and nginx's own scheme), NOT passed
      # through from client-supplied headers. Previously this forwarded
      # \$http_x_forwarded_host verbatim, so an attacker hitting nginx
      # could set X-Forwarded-Host: evil.com and poison the
      # verification/reset/OIDC links the backend generates (the backend
      # trusts these headers by default; see util.derive_hosting_info).
      #
      # KLANGK_TRUST_OUTER_PROXY=1 (or true/yes): opt-in for a TRUSTED
      # outer proxy in front of klangk's nginx whose X-Forwarded-* values
      # must survive. Only set this if that outer proxy itself overwrites,
      # not passes through, these headers.
EOF_SECURE
if [ "$TRUST_OUTER_PROXY" = "1" ]; then
  cat >>"$NGINX_STATE/nginx.conf" <<NGINX
      proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;
      proxy_set_header X-Forwarded-Host \$http_x_forwarded_host;
      proxy_set_header X-Forwarded-Prefix \$http_x_forwarded_prefix;
NGINX
else
  cat >>"$NGINX_STATE/nginx.conf" <<NGINX
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_set_header X-Forwarded-Host \$http_host;
NGINX
fi
cat >>"$NGINX_STATE/nginx.conf" <<NGINX
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
    }
  }
}
NGINX

echo "nginx listening on port $KLANGK_NGINX_PORT" >&2
# Find the real nginx binary, excluding $HOME/bin (which contains this script
# in the host container) to avoid infinite recursion.
NGINX_BIN=$(PATH="${PATH//$HOME\/bin:/}" command -v nginx 2>/dev/null || echo /usr/sbin/nginx)
exec "$NGINX_BIN" -e stderr -c "$NGINX_STATE/nginx.conf"
