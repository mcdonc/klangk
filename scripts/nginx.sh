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

# Shared allow/deny rules for container-only endpoints (LLM proxy,
# browser-delegate bridge). Restricts access so that only our own
# containers can reach the LLM API key and browser-delegate bridge.
# The backend also validates tokens, but rejecting at the network
# level avoids unnecessary round-trips.
#
# Podman uses pasta networking (rootless default): containers share
# the host's network via userspace NAT, so traffic to
# host.containers.internal arrives from the host's own IP (e.g.,
# 192.168.1.112), not from a virtual bridge subnet. We auto-detect
# the host's IPv4 addresses and allow those.
#
# Override: set KLANGK_CONTAINER_SUBNETS (comma-separated CIDRs) to
# bypass auto-detection entirely. 127.0.0.1 is NOT added implicitly
# with an explicit override; include it in the list if needed.
#

_explicit_override=false
if [ -n "${KLANGK_CONTAINER_SUBNETS:-}" ]; then
  # Explicit override — use exactly what the operator specified.
  # 127.0.0.1 is NOT added implicitly; include it in the list if needed.
  IFS=',' read -ra _subnets <<<"$KLANGK_CONTAINER_SUBNETS"
  _explicit_override=true
else
  # Auto-detect: podman uses pasta networking (rootless default), so
  # containers share the host's network via userspace NAT. Traffic to
  # host.containers.internal arrives from the host's own IP (e.g.,
  # 192.168.1.112), not from a virtual bridge subnet. We allow the
  # host's own IPv4 addresses.
  _subnets=()
  while IFS= read -r addr; do
    [ -n "$addr" ] && _subnets+=("$addr")
  done < <(ip -4 addr show 2>/dev/null | awk '/inet /{sub(/\/.*/, "", $2); print $2}')
fi

if [ ${#_subnets[@]} -gt 0 ]; then
  CONTAINER_ACL=$'\n'
  for cidr in "${_subnets[@]}"; do
    CONTAINER_ACL+="      allow ${cidr};"$'\n'
  done
  CONTAINER_ACL+="      deny all;"
  echo "nginx container ACL: ${_subnets[*]}${_explicit_override:+ (explicit)}" >&2
else
  # Fallback: broad RFC1918 ranges covering typical container subnets.
  # 192.168.0.0/16 is intentionally excluded — it is the most common
  # LAN range and allowing it would expose the LLM proxy to LAN peers.
  CONTAINER_ACL="
      allow 172.16.0.0/12;
      allow 10.0.0.0/8;
      allow 127.0.0.1;
      deny all;"
  echo "nginx container ACL: subnet detection failed, using fallback RFC1918 ranges" >&2
fi

# LLM proxy block: only included if KLANGK_LLM_BASE_URL is configured.
# Containers hit this instead of the real endpoint, so they never see the
# API key. Uses a variable so nginx resolves the upstream at request time,
# not at config load time (avoids crash on unresolvable hosts).
# NOTE: nginx resolver doesn't support search domains, so KLANGK_LLM_BASE_URL
# must use a FQDN or IP address — bare hostnames won't resolve.
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

cat >"$NGINX_STATE/nginx.conf" <<EOF_SECURE
daemon off;
pid /tmp/nginx.pid;
error_log stderr;
events { worker_connections 64; }
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

  client_max_body_size ${KLANGK_NGINX_CLIENT_MAX_BODY_SIZE};

  server {
    listen ${KLANGK_NGINX_PORT};

    # A hosted URL without a trailing slash (e.g. .../9001) can't match the
    # proxy location below. Proxying it to the app root wouldn't help either:
    # hosted apps emit relative asset paths (./assets/...) that resolve against
    # the browser's base URL, so without the slash every asset 404s. Redirect to
    # the canonical trailing-slash form so the base URL is correct.
    location ~ ^/hosted/[^/]+/\d+\$ {
      return 308 \$uri/\$is_args\$args;
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
      # reports things like "kernel not found".
      proxy_set_header Upgrade \$http_upgrade;
      proxy_set_header Connection \$connection_upgrade;
    }
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
