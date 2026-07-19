#!/usr/bin/env bash
# Build the klangk release wheel (#1656).
#
# Run AFTER scripts/flutterbuildweb.sh has produced src/frontend/build/web/
# (the hatch build hook force-includes it into the wheel at klangk/frontend/,
# and *requires* it for non-editable wheel builds — #1600). release.yml runs
# this as the "Build wheel" step with klangk:flutter-build as the devenv
# build-task, which runs flutterbuildweb.sh first.
#
# The ``build`` package isn't a declared dependency of the klangk venv (the
# venv is the app's runtime, not a build env). uv-sync runs at devenv shell
# entry and would wipe a transiently-installed build on the next shell, so
# this script installs it and builds in the same shell invocation.
#
# Usage: devenv shell -- bash scripts/build_wheel.sh
#   (or directly, inside a devenv shell)
# Produces: src/klangk/dist/klangk-<version>-py3-none-any.whl
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Install the PEP 517 build frontend transiently. uv-sync has already run by
# the time we're inside the shell; this install persists for this process.
uv pip install build

# Build the wheel from src/klangk (where pyproject.toml + the build hook live).
cd "$REPO_ROOT/src/klangk"
python3 -m build --wheel

# Report what we produced. The script cd'd into src/klangk before building,
# so dist/ is relative to that dir, not the caller's CWD — print absolute
# paths so the wheel can be located from anywhere.
echo "=== built wheels ==="
ls -lh dist/*.whl
for whl in dist/*.whl; do
  echo "$(cd "$(dirname "$whl")" && pwd)/$(basename "$whl")"
done
