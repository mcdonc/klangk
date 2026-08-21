#!/usr/bin/env bash
# Build the FIPS workspace image variant (#2570, #2591).
#
# Wraps Dockerfile.fips the same way build-workspace-image.sh wraps the
# stock Dockerfile: hash-stamped incremental build, feature payload
# staged into a tempdir, signature-policy/security args, deterministic
# version tags. The base for the variant is the *stock* workspace image
# (KLANGKD_IMAGE_NAME), so run klangk:build-workspace-image first (the
# devenv task ordering enforces this).
#
# Result tag: klangk-workspace-fips:latest (+ :<calver>-<commit>). Point
# a server at it with KLANGKD_IMAGE_NAME=klangk-workspace-fips — the
# same override mechanism devenv documents for variant images.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

STAMP="$DEVENV_STATE/klangk/.fips-image-hash"
FIPS_IMAGE="${KLANGKBUILD_FIPS_IMAGE_NAME:-klangk-workspace-fips}"
BASE_IMAGE="${KLANGKD_IMAGE_NAME:-klangk-workspace}"
# Optional compile-parallelism cap (#2631): empty (default) = all cores;
# constrained environments (e2e on the shared CI host) pass e.g. 2 so the
# OpenSSL compile cannot starve sibling suites.
FIPS_BUILD_JOBS="${KLANGKBUILD_FIPS_BUILD_JOBS:-}"

# Only this script and Dockerfile.fips affect the FIPS layer (the base
# image is an ARG; its own task rebuilds when IT changes).
CURRENT_HASH=$(find \
  scripts/build-fips-image.sh \
  src/containers/workspace/Dockerfile.fips \
  -type f -print0 2>/dev/null |
  sort -z |
  xargs -0 sha256sum 2>/dev/null |
  sha256sum | cut -d' ' -f1)

klangk::parse_build_flags "$@"
if ! $FORCE_BUILD &&
  "$PODMAN" image exists "${FIPS_IMAGE}" 2>/dev/null &&
  klangk::stamp_matches "$STAMP" "$CURRENT_HASH"; then
  echo "Image ${FIPS_IMAGE} is up to date, skipping build."
  exit 0
fi

if ! "$PODMAN" image exists "${BASE_IMAGE}" 2>/dev/null; then
  echo "Base workspace image ${BASE_IMAGE} not found — building it first." >&2
  bash "$SCRIPT_DIR/build-workspace-image.sh"
fi
if [ "${FIPS_IMAGE}" = "${BASE_IMAGE}" ]; then
  echo "FIPS image and base image are both '${FIPS_IMAGE}' — set" >&2
  echo "KLANGKBUILD_FIPS_IMAGE_NAME (or KLANGKD_IMAGE_NAME) so the variant" >&2
  echo "layers onto the stock image instead of onto itself." >&2
  exit 2
fi

klangk::prune_old_tags "${FIPS_IMAGE}"
echo "Building FIPS workspace image ${FIPS_IMAGE} (base ${BASE_IMAGE}) ..."
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  "${BUILD_SECURITY_ARGS[@]}" \
  --pull=newer \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  --build-arg WORKSPACE_IMAGE="${BASE_IMAGE}:latest" \
  --build-arg FIPS_BUILD_JOBS="${FIPS_BUILD_JOBS}" \
  -t "${FIPS_IMAGE}:latest" \
  -t "${FIPS_IMAGE}:${KLANGK_IMAGE_VERSION}" \
  "${PASSTHROUGH_ARGS[@]}" \
  -f src/containers/workspace/Dockerfile.fips \
  src/containers/workspace/

klangk::write_stamp "$STAMP" "$CURRENT_HASH"
