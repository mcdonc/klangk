#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
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
# --no-cache and --force bypass the hash check.
FORCE_BUILD=false
for arg in "$@"; do
  case "$arg" in --no-cache | --force) FORCE_BUILD=true ;; esac
done
if ! $FORCE_BUILD && "$PODMAN" image exists "${KLANGK_IMAGE_NAME}" 2>/dev/null && [ -f "$STAMP" ]; then
  OLD_HASH=$(cat "$STAMP" 2>/dev/null || true)
  if [ "$CURRENT_HASH" = "$OLD_HASH" ]; then
    echo "Image ${KLANGK_IMAGE_NAME} is up to date, skipping build."
    exit 0
  fi
fi

# Materialize features into a build-owned tempdir (#1660): the declaration
# is checked in at features.yaml; the payload (symlinked trees + features.lock)
# is ephemeral. Cleaned up on exit.
#
# Git-sourced features are skipped by default — set KLANGK_BUILD_INCLUDE_REMOTE=1
# to fetch them. Keeps CI off the network and resilient to upstream failures
# (the policy dates to #1691). Every feature is a local path entry today
# (soliplex was vendored in #1686), so the skip is currently a no-op; the gate
# stays as the generic remote-feature policy for any future git entry.
PAYLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/klangk-features-XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR"' EXIT
UPDATE_FLAGS=(--payload-dir "$PAYLOAD_DIR")
if [ "${KLANGK_BUILD_INCLUDE_REMOTE:-0}" != "1" ]; then
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
for old_tag in $("$PODMAN" images --format '{{.Tag}}' --filter "reference=${KLANGK_IMAGE_NAME}" 2>/dev/null || true); do
  case "$old_tag" in
  latest | "$VERSION" | "<none>") ;;
  *) "$PODMAN" untag "${KLANGK_IMAGE_NAME}:${old_tag}" 2>/dev/null || true ;;
  esac
done
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --pull=newer \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  --build-context features="$STAGING/features" \
  -t "${KLANGK_IMAGE_NAME}:latest" \
  -t "${KLANGK_IMAGE_NAME}:${VERSION}" \
  "$@" src/containers/workspace/

echo "$CURRENT_HASH" >"$STAMP"
