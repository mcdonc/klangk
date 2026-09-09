#!/usr/bin/env bash
# dart format verify wrapper for the pre-commit hook (#3376).
#
# dart format resolves a file's language version — and therefore its short
# vs tall output style — from the enclosing package's
# .dart_tool/package_config.json. Without it (a fresh worktree, before the
# first flutter pub get) the toolchain's latest language version wins and
# the same file formats differently, so verifying in that state would flag
# every file. Files whose package is not yet resolved are skipped with a
# notice; CI runs the same check after pub get and is the authoritative
# gate.
set -euo pipefail

resolved=()
for f in "$@"; do
  pkg_dir=$(dirname "$f")
  while [ "$pkg_dir" != "/" ] && [ ! -f "$pkg_dir/pubspec.yaml" ]; do
    pkg_dir=$(dirname "$pkg_dir")
  done
  if [ -f "$pkg_dir/.dart_tool/package_config.json" ]; then
    resolved+=("$f")
  else
    echo "dart-format: skipping $f ($pkg_dir not resolved; run flutter pub get there)"
  fi
done

if [ "${#resolved[@]}" -gt 0 ]; then
  dart format --output=none --set-exit-if-changed "${resolved[@]}"
fi
