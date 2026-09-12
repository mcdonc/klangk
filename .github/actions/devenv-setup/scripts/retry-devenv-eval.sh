#!/usr/bin/env bash
# Retry-once wrapper for devenv-evaluating steps in the devenv-setup action.
#
# devenv 2.2.x evaluates through an embedded Nix whose libexpr-c has a
# readOnlyMode use-after-free (upstream cachix/devenv#3064, analyzed and
# reproduced deterministically in #2774). The failure is a per-eval coin
# flip inside the devenv binary and matches one of two signatures:
#
#     error: path '/nix/store/<hash>-<name>' is not valid
#
#     × Failed to get shell attribute:
#       … while evaluating the attribute 'assertion'
#
# The second signature surfaced in #3442: the shell-attribute evaluation
# died on a task assertion ("The 'exports' option for a task can only be
# set when 'package' is a bash package") whose inputs were identical to
# green sibling runs minutes earlier on the same commit — the same pinned
# lock, the same CLI, the same tarball refs — so the assertion result was
# corrupted by the same eval flake wearing a different hat. Assertions are
# pure functions of the pinned inputs; a failure that sibling runs do not
# reproduce is a coin flip, not a config error.
#
# A single retry converts each signature to noise. Only these signatures
# are retried; every other failure passes through with its original exit
# code. A genuinely broken config retried under the second signature just
# evaluates twice and still fails.
#
# Usage: retry-devenv-eval.sh <command> [args...]
#
# Remove this script once the bootstrap pins a devenv release embedding the
# fixed nix 2.35 line (tracked in #2774).
set -uo pipefail

signature_invalid_path="error: path '/nix/store/[^']*' is not valid"
annotation_invalid_path="devenv eval flake (#2774 / cachix/devenv#3064): 'path ... is not valid' - retrying once"

signature_shell_attribute="Failed to get shell attribute"
annotation_shell_attribute="devenv eval flake (#2774 / cachix/devenv#3064, seen in #3442): 'Failed to get shell attribute' - retrying once"

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

  if [ "$attempt" -eq 1 ] && strip_ansi "$log" | grep -Eq "$signature_invalid_path"; then
    echo "::warning::${annotation_invalid_path}"
    attempt=2
    rm -f "$log"
    continue
  fi

  if [ "$attempt" -eq 1 ] && strip_ansi "$log" | grep -Fq "$signature_shell_attribute"; then
    echo "::warning::${annotation_shell_attribute}"
    attempt=2
    rm -f "$log"
    continue
  fi

  rm -f "$log"
  exit "$rc"
done
