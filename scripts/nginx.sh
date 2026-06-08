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
# browser-delegate bridge). Restricts access to the actual container
# subnet(s) and localhost so that only our own containers can reach the
# LLM API key and browser-delegate bridge — the backend also validates
# tokens, but rejecting at the network level avoids unnecessary
# round-trips.
#
# We auto-detect the container subnet by inspecting the default podman
# network. This is much tighter than allowing broad RFC1918 ranges
# (which would let any LAN peer use the LLM API key). If detection
# fails we fall back to 172.16.0.0/12 + 10.0.0.0/8 which cover the
# typical Docker/Podman defaults.
#
# Override: set KLANGK_CONTAINER_SUBNETS (comma-separated CIDRs) to
# bypass auto-detection entirely.
#
# Further mitigations for even tighter lockdown:
#   • Add token-based auth to the LLM proxy endpoint so that even
#     allowed networks must present a per-session secret.
#   • Bind nginx to localhost only and front it with a separate reverse
#     proxy that handles external access and authentication.
PODMAN_BIN="${KLANGK_PODMAN_BIN:-podman}"

if [ -n "${KLANGK_CONTAINER_SUBNETS:-}" ]; then
  # Explicit override — use exactly what the operator specified.
  IFS=',' read -ra _subnets <<<"$KLANGK_CONTAINER_SUBNETS"
else
  # Auto-detect from the default podman/docker network.
  _subnets=()
  if _raw=$("$PODMAN_BIN" network inspect podman 2>/dev/null); then
    while IFS= read -r cidr; do
      [ -n "$cidr" ] && _subnets+=("$cidr")
    done < <(echo "$_raw" | jq -r '.[0].subnets[].subnet' 2>/dev/null)
  fi
  # Docker fallback: try `docker network inspect bridge`.
  if [ ${#_subnets[@]} -eq 0 ] && command -v docker &>/dev/null; then
    while IFS= read -r cidr; do
      [ -n "$cidr" ] && _subnets+=("$cidr")
    done < <(docker network inspect bridge 2>/dev/null |
      jq -r '.[0].IPAM.Config[].Subnet' 2>/dev/null)
  fi
fi

if [ ${#_subnets[@]} -gt 0 ]; then
  CONTAINER_ACL=$'\n'
  for cidr in "${_subnets[@]}"; do
    CONTAINER_ACL+="      allow ${cidr};"$'\n'
  done
  CONTAINER_ACL+="      allow 127.0.0.1;"$'\n'
  CONTAINER_ACL+="      deny all;"
  echo "nginx container ACL: detected subnets: ${_subnets[*]}" >&2
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
      resolver ${DNS_RESOLVERS} valid=30s;
      set \$llm_backend ${KLANGK_LLM_BASE_URL}/\$1;
      proxy_pass \$llm_backend;
      proxy_set_header Authorization \"Bearer ${KLANGK_LLM_API_KEY:-}\";
      proxy_set_header Host \$proxy_host;
      proxy_http_version 1.1;
      proxy_set_header Connection \"\";
      proxy_buffering off;
      proxy_cache off;
      chunked_transfer_encoding on;
    }
"
fi

cat >"$NGINX_STATE/nginx.conf" <<NGINX
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

  client_max_body_size 500m;

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
    # (soliplex RAG + LLM) through here. The read timeout must comfortably
    # exceed the backend's per-chunk KLANGK_BRIDGE_TIMEOUT_SECONDS (default
    # 30s) — well above nginx's 60s default — so nginx never cuts the request
    # before the backend's own idle timeout fires.
    location = /api/browser-delegate {
${CONTAINER_ACL}
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/api/browser-delegate;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_http_version 1.1;
      proxy_read_timeout 300s;
      proxy_send_timeout 300s;
      proxy_buffering off;
    }

    location / {
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_http_version 1.1;
      # Pass through X-Forwarded-* from outer proxy, or set defaults for direct access
      set \$fwd_proto \$http_x_forwarded_proto;
      if (\$fwd_proto = "") { set \$fwd_proto \$scheme; }
      proxy_set_header X-Forwarded-Proto \$fwd_proto;
      proxy_set_header X-Forwarded-Host \$http_x_forwarded_host;
      proxy_set_header X-Forwarded-Prefix \$http_x_forwarded_prefix;
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
