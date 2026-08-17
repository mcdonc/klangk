# shellcheck shell=bash
# Shared helpers for podman build/pull scripts. Sourced (not executed) by:
#   build-workspace-image.sh, build-base-image.sh, pull-base-image.sh
#
# Sets SIG_POLICY_ARGS, expanded into every `podman build` / `podman pull`
# invocation via "${SIG_POLICY_ARGS[@]}".
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
