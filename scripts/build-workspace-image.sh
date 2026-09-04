#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

STAMP="$DEVENV_STATE/klangk/.backend-image-hash"

# Compute a hash of all files that affect the workspace image. The feature
# payload is a build-owned tempdir now (#1660), so hash the *source* — the
# checked-in declaration (features.yaml) + the feature trees under features/ —
# rather than the ephemeral materialized dir. Use -print0 / -0 so feature
# names with spaces don't corrupt the hash (silently landing on a malformed
# value that never matches the stamp → needless rebuilds).
# Dockerfile.fips is excluded: it layers the FIPS *variant* and has no
# effect on the stock image (hashing it would needlessly invalidate this
# stamp on FIPS-only edits).
CURRENT_HASH=$(find \
  scripts/build-workspace-image.sh \
  src/containers/workspace/ \
  features.yaml \
  features/ \
  -type f ! -name Dockerfile.fips -print0 2>/dev/null |
  sort -z |
  xargs -0 sha256sum 2>/dev/null |
  sha256sum | cut -d' ' -f1)

# Skip rebuild if the image exists and the hash hasn't changed. The
# stamp's input set (this script, the workspace image dir, features.yaml,
# and the feature trees) covers every file that affects the image, so a
# matching stamp means the image is up to date.
klangk::parse_build_flags "$@"
# podman calls go through klangk::run_podman: this task runs in parallel
# with klangk:build-network-sidecar, and the concurrent first-time rootless
# podman init race needs the #3168 reexec retry + diagnostics (stderr stays
# visible — "podman image exists" is silent on its normal rc-1 path).
if ! $FORCE_BUILD &&
  klangk::run_podman image exists "${KLANGKD_IMAGE_NAME}" &&
  klangk::stamp_matches "$STAMP" "$CURRENT_HASH"; then
  echo "Image ${KLANGKD_IMAGE_NAME} is up to date, skipping build."
  exit 0
fi

# Materialize features into a build-owned tempdir (#1660): the declaration
# is checked in at features.yaml; the payload (symlinked trees + features.lock)
# is ephemeral. Cleaned up on exit.
klangk::stage_features
trap 'rm -rf "$FEATURES_PAYLOAD_DIR"' EXIT

# Build workspace image on top of the base. Tag with both :latest (used
# by the backend at runtime) and a deterministic version tag (date +
# commit hash), pruning stale version tags from previous builds.
klangk::prune_old_tags "${KLANGKD_IMAGE_NAME}"
klangk::run_podman build \
  "${SIG_POLICY_ARGS[@]}" \
  "${BUILD_SECURITY_ARGS[@]}" \
  --pull=newer \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  --build-context features="$FEATURES_STAGING/features" \
  -t "${KLANGKD_IMAGE_NAME}:latest" \
  -t "${KLANGKD_IMAGE_NAME}:${KLANGK_IMAGE_VERSION}" \
  "${PASSTHROUGH_ARGS[@]}" src/containers/workspace/

klangk::write_stamp "$STAMP" "$CURRENT_HASH"
