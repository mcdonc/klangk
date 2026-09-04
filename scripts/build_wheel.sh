#!/usr/bin/env bash
# Build the klangk release wheel (#1656).
#
# Run AFTER scripts/flutterbuildweb.sh has produced src/frontend/build/web/
# (the hatch build hook force-includes it into the wheel at klangk/frontend/,
# and *requires* it for non-editable wheel builds — #1600). release.yml runs
# this as the "Build wheel" step with klangk:flutter-build as the devenv
# build-task, which runs flutterbuildweb.sh first.
#
# The wheel is built with `uv build` (#3143): uv resolves hatchling/hatch-vcs
# into its own cached, isolated build environment and never touches the
# shared devenv venv. The pre-#3143 approach (`uv pip install build` +
# `python -m build`) transiently polluted .devenv/state/venv, which raced
# with a concurrent uv-sync at another devenv shell entry — the sync wiped
# pyproject-hooks mid-build and the backend died with "can't open ...
# _in_process.py". `--no-isolation` variants have the same problem (they
# would require installing the build deps into the shared venv); `uv build`
# avoids the shared venv entirely, same as scripts/build-network-sidecar.sh
# (#2450).
#
# Usage: devenv shell -- bash scripts/build_wheel.sh
#   (or directly, inside a devenv shell)
# Produces: src/klangk/dist/klangk-<version>-py3-none-any.whl
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Build the klangk workspace member's wheel from the repo root (the hatch
# build hook resolves the frontend artifact via absolute paths, so CWD
# doesn't matter; --out-dir keeps the wheel where the Dockerfile glob and
# release.yml's pypi-publish step expect it).
cd "$REPO_ROOT"
uv build --package klangk --wheel --out-dir src/klangk/dist

# Report what we produced, with absolute paths so the wheel can be located
# from anywhere.
echo "=== built wheels ==="
ls -lh src/klangk/dist/*.whl
for whl in src/klangk/dist/*.whl; do
  echo "$(cd "$(dirname "$whl")" && pwd)/$(basename "$whl")"
done
