#!/usr/bin/env bash
# Retry-once wrapper for devenv-evaluating steps in the devenv-setup action.
#
# devenv 2.2.x evaluates through an embedded Nix whose libexpr-c has a
# readOnlyMode use-after-free (upstream cachix/devenv#3064, analyzed and
# reproduced deterministically in #2774). The failure is a per-eval coin
# flip inside the devenv binary and surfaces as a store path the eval
# references being reported missing, in two wordings:
#
#     error: path '/nix/store/<hash>-<name>' is not valid
#     error: getting attributes of path "/nix/store/<hash>-<name>": No such file or directory
#
# The first is the database-miss shape the wrapper was built for; the
# second is the stat-failure shape (stdenv's builder.pl at eval time,
# first seen in CI run 40320711358, #3594). Both are the same coin
# flip; a single retry converts either to noise. Only these signatures
# are retried; every other failure passes through with its original
# exit code.
#
# Usage: retry-on-invalid-path.sh <command> [args...]
#
# Remove this script once the bootstrap pins a devenv release embedding the
# fixed nix 2.35 line (tracked in #2774).
set -uo pipefail

# Quote style varies by nix version (single vs double); tolerate both.
_store_path="['\"]?/nix/store/[^'\"]*['\"]?"
signature_res=(
  "error: path ${_store_path} is not valid"
  "error: getting attributes of path ${_store_path}: No such file or directory"
)
retry_annotation="devenv eval flake (#2774 / cachix/devenv#3064): phantom missing store path ('is not valid' / 'getting attributes') - retrying once"

strip_ansi() {
  # nix colorizes output when it thinks stderr is a terminal; strip SGR
  # sequences so the match does not depend on that. printf-built ESC keeps
  # the pattern portable across GNU and BSD sed.
  sed "s/$(printf '\033')\\[[0-9;]*m//g" "$1"
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

  stripped=$(strip_ansi "$log")
  if [ "$attempt" -eq 1 ] && grep -Eq "${signature_res[0]}|${signature_res[1]}" <<<"$stripped"; then
    echo "::warning::${retry_annotation}"
    attempt=2
    rm -f "$log"
    continue
  fi

  rm -f "$log"
  exit "$rc"
done
