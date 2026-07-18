#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

STAMP="$DEVENV_STATE/klangk/.backend-image-hash"

# Compute a hash of all files that affect the workspace image. The plugin
# payload is a build-owned tempdir now (#1660), so hash the *source* — the
# checked-in declaration (plugins.yaml) + the plugin trees under plugins/ —
# rather than the ephemeral materialized dir.
CURRENT_HASH=$(find \
  scripts/build-workspace-image.sh \
  src/containers/workspace/ \
  plugins.yaml \
  plugins/ \
  -type f 2>/dev/null | sort | xargs sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)

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

# Materialize plugins into a build-owned tempdir (#1660): the declaration
# is checked in at plugins.yaml; the payload (symlinked trees + plugins.lock)
# is ephemeral. Cleaned up on exit.
PAYLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/klangk-plugins-XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR"' EXIT
python3 scripts/update_plugins.py --payload-dir "$PAYLOAD_DIR"

# Stage full plugin directories outside the source tree
STAGING="$PAYLOAD_DIR/.docker"
rm -rf "$STAGING"
mkdir -p "$STAGING/plugins"
for d in "$PAYLOAD_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  cp -r "$d" "$STAGING/plugins/$name"
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
  --build-context plugins="$STAGING/plugins" \
  -t "${KLANGK_IMAGE_NAME}:latest" \
  -t "${KLANGK_IMAGE_NAME}:${VERSION}" \
  "$@" src/containers/workspace/

echo "$CURRENT_HASH" >"$STAMP"
