#!/usr/bin/env bash
# Build the FIPS variant of the docker host container image (#2628).
#
# Wraps src/containers/host/Dockerfile.fips the same way
# build-fips-image.sh wraps the workspace variant: layers the validated
# OpenSSL 3.1.2 FIPS provider onto the STOCK host image and swaps the
# embedded workspace tarball for the FIPS workspace image's (a FIPS host
# must ship a FIPS workspace — KLANGKD_FIPS_MODE fails closed on a
# stock workspace at start).
#
# Prerequisites (run via devenv tasks, which order them):
#   klangk:build-host-image   → klangk-host (stock host image)
#   klangk:build-fips-image   → klangk-workspace-fips
#
# Result tag: klangk-host-fips:latest (+ :<version>, same versioning as
# the stock host image). Run it with KLANGKD_FIPS_MODE enabled — inside
# it, klangkd's containerized-backend boot gate demands the FIPS
# provider this image provides.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
HOST_IMAGE="${KLANGKBUILD_HOST_IMAGE:-klangk-host}"
FIPS_HOST_IMAGE="${KLANGKBUILD_FIPS_HOST_IMAGE_NAME:-klangk-host-fips}"
# The workspace tarball this image embeds comes from the FIPS workspace
# image (not KLANGKD_IMAGE_NAME — the stock name would embed a stock
# workspace that the FIPS start gate then refuses).
FIPS_WORKSPACE_IMAGE="${KLANGKBUILD_FIPS_IMAGE_NAME:-klangk-workspace-fips}"

if [ "${FIPS_HOST_IMAGE}" = "${HOST_IMAGE}" ]; then
  echo "FIPS host image and base host image are both '${FIPS_HOST_IMAGE}'" \
    >&2
  echo "— set KLANGKBUILD_FIPS_HOST_IMAGE_NAME so the variant layers onto" \
    >&2
  echo "the stock host image instead of onto itself." >&2
  exit 2
fi

for img in "${HOST_IMAGE}" "${FIPS_WORKSPACE_IMAGE}"; do
  if ! "$PODMAN" image exists "$img" 2>/dev/null; then
    echo "Required image '${img}' not found — build it first" >&2
    echo "(klangk:build-host-image, klangk:build-fips-image)." >&2
    exit 2
  fi
done

VERSION="$(jq -r .version "$KLANGKD_VERSION_FILE")"

# Stage the FIPS workspace tarball (exported from podman; docker build
# consumes it via the named build context). The sidecar tar is already
# inside the stock host image — this layer only replaces the workspace
# one.
WORKSPACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-fips-host-ws-XXXXXX")
trap 'rm -rf "$WORKSPACE_DIR"' EXIT
echo "Exporting FIPS workspace image ${FIPS_WORKSPACE_IMAGE} from podman ..."
"$PODMAN" save -o "$WORKSPACE_DIR/workspace-fips.tar" \
  "${FIPS_WORKSPACE_IMAGE}:latest"

echo "Building ${FIPS_HOST_IMAGE} ${VERSION} (base ${HOST_IMAGE}) ..."
# Optional compile-parallelism cap (#2631): empty (default) = all cores.
FIPS_BUILD_JOBS="${KLANGKBUILD_FIPS_BUILD_JOBS:-}"

docker build \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  -f src/containers/host/Dockerfile.fips \
  --build-arg HOST_IMAGE="${HOST_IMAGE}:latest" \
  --build-arg FIPS_BUILD_JOBS="${FIPS_BUILD_JOBS}" \
  --build-context "fips-workspace-image=$WORKSPACE_DIR" \
  -t "${FIPS_HOST_IMAGE}:latest" \
  -t "${FIPS_HOST_IMAGE}:${VERSION}" \
  "$@" \
  .

echo "Done. Image: ${FIPS_HOST_IMAGE}:${VERSION}"
docker images "${FIPS_HOST_IMAGE}" --format "  {{.Tag}}\t{{.Size}}"
