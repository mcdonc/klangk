#!/usr/bin/env bash
# Strict wrapper for the xenon complexity gate (#3415).
#
# xenon exits 0 — success — even when its parser cannot read a graded
# file: it logs a "cannot parse" WARNING and silently drops that file
# from grading. That bit for real when ruff 0.15's PEP 758 rewrite
# (``except A, B:`` on the py3.14 target) met nixpkgs' python3.13-built
# xenon: every converted file left the gate un-graded with no signal
# anywhere (#3415). The parser gap itself is fixed (devenv builds xenon
# against python3.14), but the skip is still silent — the next syntax
# the gate cannot parse would again un-gate files invisibly.
#
# This wrapper makes any skip a hard failure and owns the gate
# invocation: the pre-commit hook and the ``klangk:xenon`` devenv task
# both run this script, so the thresholds and the graded file set have
# exactly one definition and cannot drift apart.
#
# Usage: xenon-gate.sh [FILE...]
#   No arguments: grade the full gate file set (git ls-files, run from
#   the repo root). With arguments: grade exactly those files (the
#   scripts/tests contract tests use this for targeted runs).
set -euo pipefail

thresholds=(--max-absolute A --max-modules A --max-average A)

if [ "$#" -gt 0 ]; then
  set -- "${thresholds[@]}" "$@"
else
  # Graded set (#2828): the klangk package (server + CLI), the network
  # sidecar, and the build scripts. bash 3.2 compatible (no mapfile):
  # the macOS CI runner still ships bash 3.2 as /bin/bash.
  files=()
  while IFS= read -r f; do
    files+=("$f")
  done < <(git ls-files \
    'src/klangk/klangk/*.py' \
    'src/klangksidecar/klangksidecar/*.py' \
    'scripts/*.py')
  if [ "${#files[@]}" -eq 0 ]; then
    echo "xenon-gate: no graded .py files found — run from the repo root" >&2
    exit 1
  fi
  set -- "${thresholds[@]}" "${files[@]}"
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT

status=0
xenon "$@" >"$log" 2>&1 || status=$?
cat "$log"

# xenon's skip warning (logger "xenon", level WARNING): the file was
# dropped from grading while the exit code stays 0.
if grep -q 'WARNING:xenon:cannot parse' "$log"; then
  cat >&2 <<'MSG'
xenon-gate: FAIL — xenon skipped at least one graded file it cannot
parse (on its own this exits 0). The file(s) above left the complexity
gate un-graded; the gate's parser is probably older than the code's
syntax. See #3415 (PEP 758 vs a python3.13-built xenon) for the last
occurrence and its fix.
MSG
  exit 1
fi

exit "$status"
