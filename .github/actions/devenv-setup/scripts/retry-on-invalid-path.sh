#!/usr/bin/env bash
# Retry-once wrapper for devenv-evaluating steps in the devenv-setup action.
#
# devenv 2.2.x evaluates through an embedded Nix whose libexpr-c has a
# readOnlyMode use-after-free (upstream cachix/devenv#3064, analyzed and
# reproduced deterministically in #2774). The failure is a per-eval coin
# flip inside the devenv binary and matches exactly:
#
#     error: path '/nix/store/<hash>-<name>' is not valid
#
# A single retry converts it to noise. Only this signature is retried;
# every other failure passes through with its original exit code.
#
# Usage: retry-on-invalid-path.sh <command> [args...]
#
# Remove this script once the bootstrap pins a devenv release embedding the
# fixed nix 2.35 line (tracked in #2774).
set -uo pipefail

signature_re="error: path '/nix/store/[^']*' is not valid"
retry_annotation="devenv eval flake (#2774 / cachix/devenv#3064): 'path ... is not valid' - retrying once"

strip_ansi() {
  # nix colorizes output when it thinks stderr is a terminal; strip SGR
  # sequences so the match does not depend on that.
  sed 's/\x1b\[[0-9;]*m//g' "$1"
}

attempt=1
while true; do
  log=$(mktemp)
  "$@" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}

  if [ "$rc" -eq 0 ]; then
    rm -f "$log"
    exit 0
  fi

  if [ "$attempt" -eq 1 ] && strip_ansi "$log" | grep -Eq "$signature_re"; then
    echo "::warning::${retry_annotation}"
    attempt=2
    rm -f "$log"
    continue
  fi

  rm -f "$log"
  exit "$rc"
done
