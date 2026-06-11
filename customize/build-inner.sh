#!/usr/bin/env bash
# Inner build script — runs inside the devenv shell.
# Called by build.sh; not intended to be run directly.
#
# Usage: build-inner.sh <plugins-dir> <workspace-tar-dir> <ssl-cert-dir>
set -euo pipefail

PLUGINS_DIR="$1"
WORKSPACE_TAR_DIR="$2"
SSL_CERT_DIR="$3"

HAVE_CUSTOM_CERTS=false
if ls "$SSL_CERT_DIR"/*.pem 2>/dev/null || ls "$SSL_CERT_DIR"/*.crt 2>/dev/null; then
  HAVE_CUSTOM_CERTS=true
fi

WORKSPACE_IMAGE="${KLANGK_IMAGE_NAME:-klangk-workspace}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
POLICY_ARGS=()
if [ -n "${KLANGK_SIGNATURE_POLICY:-}" ]; then
  POLICY_ARGS+=(--signature-policy "${KLANGK_SIGNATURE_POLICY}")
fi

export KLANGK_PLUGINS_DIR="$PLUGINS_DIR"

# Fetch plugins
echo '--- Fetching plugins ---'
python3 scripts/update_plugins.py

# Build Flutter web (imports Dart plugins, rebuilds frontend)
echo '--- Building Flutter web ---'
bash scripts/flutterbuildweb.sh

# Build workspace image (stages extensions/tools, builds image)
echo '--- Building workspace image ---'
bash scripts/build-workspace-image.sh

# Layer custom CA certs onto the workspace image if present
if [ "$HAVE_CUSTOM_CERTS" = true ]; then
  echo '--- Layering custom CA certs onto workspace image ---'

  WS_CERT_DIR=$(mktemp -d)
  trap 'rm -rf "$WS_CERT_DIR"' EXIT

  cp "$SSL_CERT_DIR"/*.pem "$WS_CERT_DIR/" 2>/dev/null || true
  cp "$SSL_CERT_DIR"/*.crt "$WS_CERT_DIR/" 2>/dev/null || true

  cat >"$WS_CERT_DIR/Dockerfile" <<'CERTDF'
ARG BASE
FROM $BASE
COPY *.pem *.crt /tmp/ssl/
USER root
RUN cp /tmp/ssl/*.pem /usr/local/share/ca-certificates/ 2>/dev/null; \
    cp /tmp/ssl/*.crt /usr/local/share/ca-certificates/ 2>/dev/null; \
    for f in /usr/local/share/ca-certificates/*.pem; do \
      [ -f "$f" ] && mv "$f" "${f%.pem}.crt"; \
    done; \
    update-ca-certificates && \
    rm -rf /tmp/ssl
USER klangk
CERTDF

  "$PODMAN" build "${POLICY_ARGS[@]}" \
    --build-arg BASE="$WORKSPACE_IMAGE" \
    -t "$WORKSPACE_IMAGE:latest" \
    "$WS_CERT_DIR"
fi

# Export workspace image as tarball
echo '--- Exporting workspace image ---'
"$PODMAN" save "${POLICY_ARGS[@]}" -o "$WORKSPACE_TAR_DIR/workspace.tar" "$WORKSPACE_IMAGE"
