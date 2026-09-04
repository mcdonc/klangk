#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

# Build the klangksidecar wheel (#2450) here in the dev env — git is present,
# so hatch-vcs derives the real version — and hand it to the image build as a
# named context (``--build-context sidecar=``). The Dockerfile pip-installs the
# wheel's [nfqueue] extra instead of COPYing proxy.py by hand: the package will
# grow multifile, and a wheel install is what keeps working then. The staging
# dir holds only the built wheel and is cleaned up on exit.
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/klangk-sidecar-XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
uv build --package klangksidecar --wheel --out-dir "$STAGING"

echo "Building network sidecar image ..."
# Through klangk::run_podman: this task runs in parallel with
# klangk:build-workspace-image, and the concurrent first-time rootless
# podman init race needs the #3168 reexec retry + diagnostics.
klangk::run_podman build -t klangk-network-sidecar \
  "${SIG_POLICY_ARGS[@]}" \
  "${BUILD_SECURITY_ARGS[@]}" \
  --build-context sidecar="$STAGING" \
  -f src/containers/network/Dockerfile src/containers/network
