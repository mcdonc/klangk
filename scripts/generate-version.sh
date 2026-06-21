#!/usr/bin/env bash
# Generate version.json to stdout from git state.
#
# Priority:
#   1. Exact tag on HEAD (release builds)
#   2. Current branch name (dev builds)
#   3. Full commit SHA (detached HEAD)
set -euo pipefail

VERSION="$(git describe --tags --exact-match HEAD 2>/dev/null)" ||
  VERSION="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" ||
  VERSION="unknown"
if [ "$VERSION" = "HEAD" ]; then
  VERSION="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
fi
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"version":"%s","commit":"%s","built_at":"%s"}\n' "$VERSION" "$COMMIT" "$BUILT_AT"
