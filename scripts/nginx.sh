#!/usr/bin/env bash
set -euo pipefail

NGINX_STATE="${KLANGK_STATE_DIR:-${DEVENV_STATE:-}}/nginx"
if [ -z "${KLANGK_STATE_DIR:-}${DEVENV_STATE:-}" ]; then
  echo "error: KLANGK_STATE_DIR or DEVENV_STATE must be set" >&2
  exit 1
fi
mkdir -p "$NGINX_STATE"

# Build LLM proxy block only if the URL is configured
LLM_BLOCK=""
if [ -n "${KLANGK_LLM_BASE_URL:-}" ]; then
  # nginx resolver needs space-separated IPs; env var may use commas.
  # Default: parse /etc/resolv.conf for nameservers (works on host and
  # in Docker). Fall back to 8.8.8.8 if resolv.conf has no entries.
  if [ -n "${KLANGK_DNS_SERVERS:-}" ]; then
    DNS_RESOLVERS="${KLANGK_DNS_SERVERS//,/ }"
  else
    DNS_RESOLVERS=$(awk '/^nameserver/{printf "%s ", $2}' /etc/resolv.conf)
    DNS_RESOLVERS="${DNS_RESOLVERS:-8.8.8.8}"
  fi
  LLM_BLOCK="
    # LLM proxy: forward to the real LLM endpoint with API key injected.
    # Containers hit this instead of the real endpoint, so they never
    # see the API key. Restricted to Docker subnets and localhost only.
    location ~ ^/llm-proxy/(.*)\$ {
      allow 172.16.0.0/12;
      allow 192.168.0.0/16;
      allow 10.0.0.0/8;
      allow 127.0.0.1;
      deny all;
      # Use a variable so nginx resolves the upstream at request time,
      # not at config load time (avoids crash on unresolvable hosts).
      resolver ${DNS_RESOLVERS} valid=30s;
      set \$llm_backend ${KLANGK_LLM_BASE_URL}/\$1;
      proxy_pass \$llm_backend;
      proxy_set_header Authorization \"Bearer ${KLANGK_LLM_API_KEY:-}\";
      proxy_set_header Host \$proxy_host;
      proxy_http_version 1.1;
      proxy_set_header Connection \"\";
      # SSE streaming support
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

    # Hosted app proxy: extract port from URL and proxy directly to container
    location ~ ^/hosted/[^/]+/(\d+)/(.*)\$ {
      proxy_pass http://127.0.0.1:\$1/\$2\$is_args\$args;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto \$scheme;
      proxy_http_version 1.1;
    }
${LLM_BLOCK}
    location / {
      proxy_pass http://127.0.0.1:${KLANGK_PORT}/;
      proxy_set_header Host \$http_host;
      proxy_set_header X-Real-IP \$remote_addr;
      proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
      # Pass through X-Forwarded-* from outer proxy, or set defaults for direct access
      set \$fwd_proto \$http_x_forwarded_proto;
      if (\$fwd_proto = "") { set \$fwd_proto \$scheme; }
      proxy_set_header X-Forwarded-Proto \$fwd_proto;
      proxy_set_header X-Forwarded-Host \$http_x_forwarded_host;
      proxy_set_header X-Forwarded-Prefix \$http_x_forwarded_prefix;
      proxy_http_version 1.1;
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
