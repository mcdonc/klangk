#!/usr/bin/env bash
# Build a custom klangk-host image with plugins baked in.
#
# Clones klangk at a pinned ref, fetches the plugins listed in plugins.yaml,
# then builds the host image from source so the Dart (UI) and TypeScript
# (workspace) plugins are compiled straight into the image. The upstream
# build-host-image.sh already embeds the Flutter web build, the workspace
# tarball, and the plugin directories — so a single source build is enough;
# no separate overlay / base-image pass is needed.
#
# Prerequisites:
#   - Nix with devenv installed (or run klangk's ./bootstrap)
#   - Docker
#   - SSH key with access to git repos in plugins.yaml
#
# Usage (run from the customize/ directory):
#   ./build/build.sh
#
# Optional:
#   KLANGK_REF=v1.0.1 ./build/build.sh          # build from a tagged release (default: main)
#   KLANGK_VARIANT="Custom 1.0.0" ./build/build.sh  # identify the build (default: "custom")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

KLANGK_REF="${KLANGK_REF:-main}"
KLANGK_REPO="${KLANGK_REPO:-https://github.com/mcdonc/klangk.git}"
KLANGK_DIR="$SCRIPT_DIR/.klangk"
PLUGINS_DIR="$SCRIPT_DIR/.plugins"

# Image tag. Override with KLANGK_HOST_IMAGE to publish elsewhere.
IMAGE="${KLANGK_HOST_IMAGE:-ghcr.io/mcdonc/klangk/klangk-host-custom}"

# Variant — identifies this custom build in version.json, surfaced at
# GET /api/v1/version and the debug pane's "Variant" row. Independent of the
# upstream klangk version (which stays in version.json["version"]). Override
# via the environment, or just edit this default. Empty = stock klangk (no
# variant reported), but a non-empty default is recommended so a copied
# template never impersonates upstream.
VARIANT="${KLANGK_VARIANT:-custom}"

DEVENV_CMD=(devenv --quiet -O dotenv.enable:bool false shell --)

# 1. Clone or update klangk repo
echo "=== Cloning klangk ($KLANGK_REF) ==="
if [ -d "$KLANGK_DIR/.git" ]; then
  git -C "$KLANGK_DIR" reset --hard HEAD
  git -C "$KLANGK_DIR" clean -fd
  git -C "$KLANGK_DIR" fetch origin --tags
  git -C "$KLANGK_DIR" checkout "$KLANGK_REF"
  # Pull only if on a branch (not a detached tag/SHA)
  if git -C "$KLANGK_DIR" symbolic-ref -q HEAD >/dev/null 2>&1; then
    git -C "$KLANGK_DIR" pull --ff-only || true
  fi
else
  git clone "$KLANGK_REPO" "$KLANGK_DIR"
  git -C "$KLANGK_DIR" checkout "$KLANGK_REF"
fi

# 2. Stage plugin config into the plugins dir
echo "=== Fetching plugins ==="
rm -rf "$PLUGINS_DIR"
mkdir -p "$PLUGINS_DIR"
cp "$SCRIPT_DIR/plugins.yaml" "$PLUGINS_DIR/plugins.yaml"

# 3. Fetch plugins and build the host image from source, with plugins baked in.
#    devenv's profile defaults KLANGK_PLUGINS_DIR to an empty dir inside the
#    clone, so KLANGK_PLUGINS_DIR / KLANGK_HOST_IMAGE / KLANGK_VARIANT must be
#    overridden *inside* the shell — exporting before `devenv shell` is
#    clobbered by the profile.
cd "$KLANGK_DIR"
"${DEVENV_CMD[@]}" bash -c "
  export KLANGK_PLUGINS_DIR='$PLUGINS_DIR'
  export KLANGK_HOST_IMAGE='$IMAGE'
  export KLANGK_VARIANT='$VARIANT'
  python3 scripts/update_plugins.py
  bash scripts/build-host-image.sh
"

echo "=== Done. Image: $IMAGE (variant: $VARIANT) ==="
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
