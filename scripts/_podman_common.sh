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

# shellcheck disable=SC1091,SC2034 # sourced helper; its vars (incl. KLANGK_PYTHON) are consumed by sourcing scripts
source "$(dirname "${BASH_SOURCE[0]}")/_python_common.sh"

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
  "${KLANGK_PYTHON}" scripts/update_features.py "${flags[@]}"
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

# --- Rootless reexec retry guard (#3168) ----------------------------------
#
# "devenv tasks run klangk:build-workspace-image klangk:build-network-sidecar"
# runs both tasks in parallel, and on a fresh machine (stock CI runners, first
# "devenv up") those are the machine's first two rootless podman invocations.
# Concurrent first-time user-namespace/storage initialization intermittently
# kills one of them ~1.4s in with
#
#     failed to reexec: Permission denied
#
# before any test runs, reding the whole job (run 33888969754). The failure is
# environmental and transient: by the time a retry runs, the sibling build has
# finished initializing podman's state. klangk::run_podman wraps a podman
# invocation so that exactly this signature is retried once (after capturing
# diagnostics, so a persistent occurrence is attributable); every other
# failure passes through untouched with its original exit code — the same
# policy as scripts/retry-on-invalid-path.sh for the devenv eval UAF (#2775).

# Fixed-string signature (grep -F): any "failed to reexec:" is the rootless
# bootstrap (userns reexec) failing, which is runner-environment level — never
# a klangk code path or a Dockerfile problem.
REEXEC_SIGNATURE="failed to reexec:"

# Seconds to let the sibling podman's first-time initialization finish before
# the retry attempt. KLANGKBUILD_PODMAN_RETRY_SLEEP overrides (tests zero it).
PODMAN_RETRY_SLEEP="${KLANGKBUILD_PODMAN_RETRY_SLEEP:-5}"

# klangk::reexec_diagnostics — probes named in #3168 (podman info rc,
# "podman unshare true" rc) plus the standard userns checklist (sysctls,
# subid ranges). Best-effort: nothing here may fail the caller. Probes run
# under timeout(1): a probe contending a storage flock held by the sibling
# build must degrade to a "FAILED rc=124" line, not stall the retry.
klangk::reexec_diagnostics() {
  local podman_bin="${KLANGKD_PODMAN_BIN:-podman}" f
  echo "--- podman reexec diagnostics (#3168) ---" >&2
  if timeout 30 "$podman_bin" info >/dev/null 2>&1; then
    echo "podman info: ok" >&2
  else
    echo "podman info: FAILED rc=$?" >&2
  fi
  if timeout 30 "$podman_bin" unshare true >/dev/null 2>&1; then
    echo "podman unshare true: ok (userns creatable)" >&2
  else
    echo "podman unshare true: FAILED rc=$?" >&2
  fi
  for f in /proc/sys/user/max_user_namespaces \
    /proc/sys/kernel/unprivileged_userns_clone; do
    [ -r "$f" ] || continue
    echo "$f = $(cat "$f")" >&2
  done
  echo "subuid: $(grep "^$(id -un):" /etc/subuid 2>/dev/null || echo '<none>')" >&2
  echo "subgid: $(grep "^$(id -un):" /etc/subgid 2>/dev/null || echo '<none>')" >&2
  echo "------------------------------------------" >&2
}

# klangk::run_podman <args...> — run "${KLANGKD_PODMAN_BIN:-podman}" "$@"
# with the #3168 reexec retry. The command's combined output streams live on
# stderr (and into a temp log for the signature match); stdout stays clean so
# callers may capture it. Returns the podman exit status: after one retry when
# the reexec signature was seen, immediately otherwise.
klangk::run_podman() {
  local errlog attempt rc
  for attempt in 1 2; do
    rc=0
    errlog="$(mktemp "${TMPDIR:-/tmp}/klangk-podman-XXXXXX")"
    "${KLANGKD_PODMAN_BIN:-podman}" "$@" 2>&1 | tee "$errlog" >&2 ||
      rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
      rm -f "$errlog"
      return 0
    fi
    if grep -Fq "$REEXEC_SIGNATURE" "$errlog"; then
      rm -f "$errlog"
      klangk::reexec_diagnostics
      if [ "$attempt" -eq 1 ]; then
        echo "::warning::podman rootless reexec failure (attempt 1/2) — retrying once (#3168)" >&2
        sleep "$PODMAN_RETRY_SLEEP"
        continue
      fi
      echo "::error::podman rootless reexec failure persisted after retry (#3168)" >&2
    else
      rm -f "$errlog"
    fi
    return "$rc"
  done
}
