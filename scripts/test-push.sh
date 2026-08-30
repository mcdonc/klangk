#!/usr/bin/env bash
# Scoped pre-push test gate (#2727).
#
# Diffs the working tree (committed + uncommitted) against the merge-base
# with the default branch and runs only the suites whose area changed:
#
#   src/klangk/**        -> testmon over both unit suites (changed-line
#                           selection; same caveats as the `testmon` task)
#   scripts/** or
#   src/containers/**    -> scripts/tests (build-pipeline contract suite)
#   src/klangksidecar/** -> sidecar unit suite
#   src/frontend/**      -> flutter unit tests (no coverage; CI runs that)
#
# This is a fast-fail gate, not a replacement for CI: CI still runs the
# full suites with the coverage gates (backend-tests.yml,
# frontend-tests.yml). The merge gate is CI green against the latest
# push; a local full-suite run is optional, not required.
#
# Override the base ref with TEST_PUSH_BASE=<ref> (default origin/main).
# The ref is used as-is, without a network fetch; rebase onto the latest
# main before pushing anyway (see AGENTS.md / the workon flow).

set -uo pipefail
# shellcheck disable=SC1091 # sourced sibling helper
source "$(dirname "${BASH_SOURCE[0]}")/_python_common.sh"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

BASE="${TEST_PUSH_BASE:-origin/main}"
if ! git rev-parse --verify --quiet "$BASE" >/dev/null; then
  echo "test-push: base ref '$BASE' not found — run 'git fetch origin main' first" >&2
  exit 2
fi
MB="$(git merge-base HEAD "$BASE")" || {
  echo "test-push: no merge-base with '$BASE'" >&2
  exit 2
}

# Changed = diff (working tree vs merge-base, so uncommitted edits count)
# plus untracked files (git diff alone never lists them — a brand-new
# uncommitted test file must still select its area).
CHANGED="$(
  {
    git diff --name-only "$MB"
    git ls-files --others --exclude-standard
  } | sort -u
)"

backend=0
build=0
sidecar=0
frontend=0
infra_notes=()
while IFS= read -r f; do
  [ -n "$f" ] || continue
  case "$f" in
  src/klangk/*) backend=1 ;;
  src/klangksidecar/*) sidecar=1 ;;
  src/frontend/*) frontend=1 ;;
  scripts/* | src/containers/*) build=1 ;;
  devenv.nix | flake.nix | .github/*) infra_notes+=("$f") ;;
  *) ;; # docs, README, etc. — no test area
  esac
done <<<"$CHANGED"

if [ "$backend$build$sidecar$frontend" = "0000" ]; then
  echo "test-push: no changes vs $BASE touch a test area; nothing to run."
  exit 0
fi

echo "test-push: changed areas vs $BASE ($(echo "$CHANGED" | grep -c .) files):"
[ "$backend" = 1 ] && echo "  backend   (src/klangk/)           -> testmon unit suites"
[ "$build" = 1 ] && echo "  build     (scripts/, src/containers/) -> scripts/tests contract suite"
[ "$sidecar" = 1 ] && echo "  sidecar   (src/klangksidecar/)    -> sidecar unit suite"
[ "$frontend" = 1 ] && echo "  frontend  (src/frontend/)         -> flutter test"

status=0

if [ "$backend" = 1 ]; then
  echo
  echo "=== backend unit (testmon; first run in a fresh worktree baselines, ~test-unit cost) ==="
  "${KLANGK_PYTHON}" -m pytest src/klangk/klangkd-tests/tests src/klangk/klangkc-tests/tests \
    -v -n auto --no-cov --testmon || status=1
fi

if [ "$build" = 1 ]; then
  echo
  echo "=== build-pipeline contract tests (scripts/tests) ==="
  "${KLANGK_PYTHON}" -m pytest scripts/tests -v || status=1
fi

if [ "$sidecar" = 1 ]; then
  echo
  echo "=== sidecar unit ==="
  "${KLANGK_PYTHON}" -m pytest src/klangksidecar/tests -v -n auto || status=1
fi

if [ "$frontend" = 1 ]; then
  echo
  echo "=== frontend unit (flutter test) ==="
  # macOS xcrun shim for the objective_c FFI — parity with the
  # test-frontend task (see the comment there for the full story).
  if [ "$(uname -s)" = "Darwin" ] && [ -x /usr/bin/xcrun ]; then
    export PATH="$REPO_ROOT/scripts/xcrun-shim:$PATH"
  fi
  (cd src/frontend && flutter test) || status=1
fi

echo
if [ "${#infra_notes[@]}" -gt 0 ]; then
  echo "note: infra files changed (${infra_notes[*]}); these gate on CI only."
fi
if [ "$status" = 0 ]; then
  echo "test-push: selected areas green. CI runs the authoritative full suites."
else
  echo "test-push: FAILURES in selected areas."
fi
exit "$status"
