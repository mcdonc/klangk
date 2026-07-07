#!/usr/bin/env bash
# Generate version.json to stdout from git state.
#
# Priority:
#   1. Exact tag on HEAD (release builds)
#   2. Current branch name (dev builds)
#   3. Full commit SHA (detached HEAD)
#
# If KLANGK_VARIANT is set (non-empty), a "variant" field carrying that
# product-identity string is emitted between "version" and "commit". When
# unset/empty the output is byte-identical to stock klangk — the field is
# omitted entirely, not emitted as null (see #1358).
set -euo pipefail

VERSION="$(git describe --tags --exact-match HEAD 2>/dev/null)" ||
  VERSION="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" ||
  VERSION="unknown"
if [ "$VERSION" = "HEAD" ]; then
  VERSION="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
fi
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
VARIANT="${KLANGK_VARIANT:-}"
if [ -n "$VARIANT" ]; then
  printf '{"version":"%s","variant":"%s","commit":"%s","built_at":"%s"}\n' \
    "$VERSION" "$VARIANT" "$COMMIT" "$BUILT_AT"
else
  printf '{"version":"%s","commit":"%s","built_at":"%s"}\n' \
    "$VERSION" "$COMMIT" "$BUILT_AT"
fi
