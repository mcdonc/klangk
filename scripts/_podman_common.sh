# shellcheck shell=bash
# Shared helpers for podman build/pull scripts. Sourced (not executed) by:
#   build-workspace-image.sh, build-base-image.sh, pull-base-image.sh
#
# Sets SIG_POLICY_ARGS, expanded into every `podman build` / `podman pull`
# invocation via "${SIG_POLICY_ARGS[@]}", and ensures the registries.conf
# named by CONTAINERS_REGISTRIES_CONF exists (#286) so short image names
# resolve.
#
# Rootless podman from Nix ships no default /etc/containers/policy.json, so a
# build/pull that verifies image signatures fails in a fresh environment
# (#1230). devenv sets CONTAINERS_SIGNATURE_POLICY to a project-managed
# policy on Linux (and leaves it empty on macOS, where podman runs in a VM
# with its own policy). We pass --signature-policy only when that var is set
# and non-empty, and ensure the file exists on first run as a safety net for
# direct script invocation (enterShell normally generates it).
#
# We deliberately do NOT fall back to /etc/containers/policy.json: that file
# is not guaranteed to exist and is the wrong place on Nix/rootless setups.

# shellcheck disable=SC2034 # SIG_POLICY_ARGS / BUILD_SECURITY_ARGS consumed by sourcing script
SIG_POLICY_ARGS=()

# Rootless podman masks /proc paths inside build containers (OCI
# maskedPaths/readonlyPaths). In a nested user namespace (rootless
# podman inside a systemd service) the kernel's mnt_already_visible()
# sees those overmounts and rejects a fresh proc mount in the child
# pidns as "VFS: Mount too revealing". unmask=ALL prevents the
# overmounts, letting concurrent rootless builds succeed.
# Only set when CONTAINERS_STORAGE_CONF is present (nix CI runner);
# older podman on ubuntu-latest doesn't support this option.
BUILD_SECURITY_ARGS=()
if [ -n "${CONTAINERS_STORAGE_CONF:-}" ]; then
  BUILD_SECURITY_ARGS=(--security-opt unmask=ALL)
fi
if [ -n "${CONTAINERS_SIGNATURE_POLICY:-}" ]; then
  if [ ! -f "$CONTAINERS_SIGNATURE_POLICY" ]; then
    mkdir -p "$(dirname "$CONTAINERS_SIGNATURE_POLICY")"
    # Same permissive policy as enterShell generates. A permissive default is
    # correct for dev: images are pulled from our own GHCR or built locally.
    echo '{"default": [{"type": "insecureAcceptAnything"}]}' \
      >"$CONTAINERS_SIGNATURE_POLICY"
  fi
  SIG_POLICY_ARGS=(--signature-policy "$CONTAINERS_SIGNATURE_POLICY")
fi

# Rootless podman from nix ships no registries.conf either, so short image
# names in our Dockerfiles (alpine:3.21, python:3.14-slim, …) fail to resolve
# (#286). Unlike the policy, podman reads CONTAINERS_REGISTRIES_CONF
# directly, so no flag is passed — every podman call in the environment
# inherits it. The scripts only ensure the file exists: podman hard-fails
# with "loading registries configuration ... no such file or directory" when
# the env var points at a missing path.
if [ -n "${CONTAINERS_REGISTRIES_CONF:-}" ] &&
  [ ! -f "$CONTAINERS_REGISTRIES_CONF" ]; then
  mkdir -p "$(dirname "$CONTAINERS_REGISTRIES_CONF")"
  # Same config as enterShell generates: every short name our Dockerfiles
  # use lives on docker.io.
  echo 'unqualified-search-registries = ["docker.io"]' \
    >"$CONTAINERS_REGISTRIES_CONF"
fi

# --- Shared image-build helpers (build-workspace-image.sh,
# build-fips-image.sh) --------------------------------------------------
#
# Each helper communicates via documented globals (bash arrays and
# multiple outputs don't compose cleanly as return values):

# klangk::parse_build_flags "$@" — consume --force / --no-cache.
# Sets FORCE_BUILD=true when a rebuild must happen (--force is consumed;
# --no-cache also forces and is kept for the podman passthrough) and
# PASSTHROUGH_ARGS to the remaining flags for `podman build`.
FORCE_BUILD=false
PASSTHROUGH_ARGS=()
klangk::parse_build_flags() {
  FORCE_BUILD=false
  PASSTHROUGH_ARGS=()
  local arg
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
}

# klangk::stamp_matches <stamp_file> <current_hash> — 0 when the image
# build can be skipped: the stamp file exists and holds current_hash.
# (The caller separately checks `podman image exists`.)
klangk::stamp_matches() {
  local stamp_file="$1" current_hash="$2"
  [ -f "$stamp_file" ] && [ "$(cat "$stamp_file" 2>/dev/null || true)" = "$current_hash" ]
}

# klangk::write_stamp <stamp_file> <current_hash>
klangk::write_stamp() {
  mkdir -p "$(dirname "$1")"
  echo "$2" >"$1"
}

# klangk::stage_features — materialize the feature payload for a build
# context. Sets FEATURES_STAGING to the dir containing features/<name>/
# trees (pass it as the features build-context) and FEATURES_PAYLOAD_DIR
# to its parent tempdir. The caller owns cleanup:
#   trap 'rm -rf "$FEATURES_PAYLOAD_DIR"' EXIT
# Respects KLANGKBUILD_BUILD_INCLUDE_REMOTE (default: local-only,
# keeping CI off the network — the #1691 policy).
FEATURES_STAGING=""
FEATURES_PAYLOAD_DIR=""
klangk::stage_features() {
  local payload_dir staging d name
  payload_dir="$(mktemp -d "${TMPDIR:-/tmp}/klangk-features-XXXXXX")"
  local flags=(--payload-dir "$payload_dir")
  if [ "${KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0}" != "1" ]; then
    flags+=(--local-only)
  fi
  python3 scripts/update_features.py "${flags[@]}"
  staging="$payload_dir/.docker"
  rm -rf "$staging"
  mkdir -p "$staging/features"
  for d in "$payload_dir"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    cp -r "$d" "$staging/features/$name"
  done
  FEATURES_STAGING="$staging"
  FEATURES_PAYLOAD_DIR="$payload_dir"
}

# klangk::prune_old_tags <image> — drop version tags other than latest
# and the current one, so repeated builds don't accumulate. Sets
# KLANGK_IMAGE_VERSION to the deterministic "<calver>-<commit>" tag.
KLANGK_IMAGE_VERSION=""
klangk::prune_old_tags() {
  local image="$1" podman_bin="${KLANGKD_PODMAN_BIN:-podman}" old_tag
  KLANGK_IMAGE_VERSION="$(date -u +%Y.%m.%d)-$(git rev-parse --short HEAD)"
  for old_tag in $("$podman_bin" images --format '{{.Tag}}' --filter "reference=${image}" 2>/dev/null || true); do
    case "$old_tag" in
    latest | "$KLANGK_IMAGE_VERSION" | "<none>") ;;
    *) "$podman_bin" untag "${image}:${old_tag}" 2>/dev/null || true ;;
    esac
  done
}
