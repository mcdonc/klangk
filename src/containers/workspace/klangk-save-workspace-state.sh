#!/bin/sh
# Atomically write stdin to a target file via mktemp + rename.
# Usage: klangk-save-workspace-state <path>
set -e
target="${1:-/home/.workspace-state.json}"
t=$(mktemp "${target}.XXXXXX")
trap 'rm -f "$t"' EXIT
cat >"$t" && mv "$t" "$target"
trap - EXIT
