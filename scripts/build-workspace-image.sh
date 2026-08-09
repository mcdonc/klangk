#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

STAMP="$DEVENV_STATE/klangk/.backend-image-hash"

# Compute a hash of all files that affect the workspace image. The feature
# payload is a build-owned tempdir now (#1660), so hash the *source* — the
# checked-in declaration (features.yaml) + the feature trees under features/ —
# rather than the ephemeral materialized dir. Use -print0 / -0 so feature
# names with spaces don't corrupt the hash (silently landing on a malformed
# value that never matches the stamp → needless rebuilds).
CURRENT_HASH=$(find \
  scripts/build-workspace-image.sh \
  src/containers/workspace/ \
  features.yaml \
  features/ \
  -type f -print0 2>/dev/null |
  sort -z |
  xargs -0 sha256sum 2>/dev/null |
  sha256sum | cut -d' ' -f1)

# Skip rebuild if the image exists and the hash hasn't changed.
# --no-cache and --force bypass the hash check.  --force is consumed
# here and stripped from the passthrough args so it doesn't leak to
# podman build (which doesn't recognise it).
FORCE_BUILD=false
PASSTHROUGH_ARGS=()
for arg in "$@"; do
  case "$arg" in
  --force) FORCE_BUILD=true ;;
  --no-cache)
    FORCE_BUILD=true
    PASSTHROUGH_ARGS+=("$arg")
    ;;
  *) PASSTHROUGH_ARGS+=("$arg") ;;
  esac
done
if ! $FORCE_BUILD && "$PODMAN" image exists "${KLANGKD_IMAGE_NAME}" 2>/dev/null && [ -f "$STAMP" ]; then
  OLD_HASH=$(cat "$STAMP" 2>/dev/null || true)
  if [ "$CURRENT_HASH" = "$OLD_HASH" ]; then
    # Stamp matches source — trust it and skip the build. The stamp hash
    # already covers every file that affects the image (this script, the
    # workspace image dir, features.yaml, and the feature trees), so a
    # matching stamp means the image is up to date. The image-creation-time
    # "newer than every source file" check that lived here was unreliable
    # (podman inspect timestamps don't reflect storage-layer caching / layer
    # reuse) and rebuilt the image on every server restart (#2273).
    echo "Image ${KLANGKD_IMAGE_NAME} is up to date, skipping build."
    exit 0
  fi
fi

# Materialize features into a build-owned tempdir (#1660): the declaration
# is checked in at features.yaml; the payload (symlinked trees + features.lock)
# is ephemeral. Cleaned up on exit.
#
# Git-sourced features are skipped by default — set KLANGKBUILD_BUILD_INCLUDE_REMOTE=1
# to fetch them. Keeps CI off the network and resilient to upstream failures
# (the policy dates to #1691). Every feature is a local path entry today
# (soliplex was vendored in #1686), so the skip is currently a no-op; the gate
# stays as the generic remote-feature policy for any future git entry.
PAYLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/klangk-features-XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR"' EXIT
UPDATE_FLAGS=(--payload-dir "$PAYLOAD_DIR")
if [ "${KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0}" != "1" ]; then
  UPDATE_FLAGS+=(--local-only)
fi
python3 scripts/update_features.py "${UPDATE_FLAGS[@]}"

# Stage full feature directories outside the source tree
STAGING="$PAYLOAD_DIR/.docker"
rm -rf "$STAGING"
mkdir -p "$STAGING/features"
for d in "$PAYLOAD_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  cp -r "$d" "$STAGING/features/$name"
done

# Build workspace image on top of the base.
# Tag with both :latest (used by the backend at runtime) and a
# deterministic version tag (date + commit hash).  Remove stale
# version tags from previous builds so they don't accumulate.
COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
# Remove old version tags (but not :latest — podman build will update it).
for old_tag in $("$PODMAN" images --format '{{.Tag}}' --filter "reference=${KLANGKD_IMAGE_NAME}" 2>/dev/null || true); do
  case "$old_tag" in
  latest | "$VERSION" | "<none>") ;;
  *) "$PODMAN" untag "${KLANGKD_IMAGE_NAME}:${old_tag}" 2>/dev/null || true ;;
  esac
done
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --pull=newer \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  --build-context features="$STAGING/features" \
  -t "${KLANGKD_IMAGE_NAME}:latest" \
  -t "${KLANGKD_IMAGE_NAME}:${VERSION}" \
  "${PASSTHROUGH_ARGS[@]}" src/containers/workspace/

echo "$CURRENT_HASH" >"$STAMP"
