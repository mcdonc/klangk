#!/usr/bin/env bash
# Pull the workspace base image into the local podman store.
#
# Pulls exactly the reference pinned by WORKSPACE_BASE_IMAGE in the workspace
# Dockerfile — an immutable digest since #2063 — never a mutable :latest, so
# the local cache always matches what a workspace build consumes. The pinned
# reference is multi-arch; podman selects the variant for the host platform.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

DOCKERFILE="${DEVENV_ROOT:-$SCRIPT_DIR/..}/src/containers/workspace/Dockerfile"
BASE_REF="$(sed -n 's/^ARG WORKSPACE_BASE_IMAGE=//p' "$DOCKERFILE")"
if [ -z "$BASE_REF" ]; then
  echo "error: WORKSPACE_BASE_IMAGE not found in $DOCKERFILE" >&2
  exit 1
fi
"$PODMAN" pull \
  "${SIG_POLICY_ARGS[@]}" \
  "$BASE_REF"
