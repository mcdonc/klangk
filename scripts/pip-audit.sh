#!/usr/bin/env bash
# pip-audit the locked Python dependency resolution (#2856).
#
# Audits the exact resolution recorded in uv.lock — not whatever happens
# to be installed in the current environment — for each workspace member
# that ships to users:
#   * klangk        — the `pip install klangk` wheel (klangkd server +
#                     klangk CLI; its metadata also drives what the
#                     container images install)
#   * klangksidecar — the network-sidecar wheel (installed by its image)
#
# `uv export --frozen --all-extras` pins each member's full transitive
# closure (runtime + test/nfqueue extras); `--no-deps` audits exactly
# those pins without installing anything (netfilterqueue, for one, cannot
# build on a stock runner). Vulnerabilities accepted permanently-or-pending
# live in pip-audit-ignore.txt, next to this script; any finding not
# listed there fails the run.
#
# Usage: devenv shell -- bash scripts/pip-audit.sh
#   (or directly, with uv on PATH — how CI runs it)
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)

# Keep in sync with the uv pin in .github/workflows/python-deps-audit.yml.
PIP_AUDIT_VERSION=2.10.1
ALLOWLIST="$SCRIPT_DIR/pip-audit-ignore.txt"

# Expand the committed allowlist into --ignore-vuln flags: one advisory ID
# per non-comment line (the justification lives in the # comment above it).
ignore_flags=()
while IFS= read -r vuln_id; do
  ignore_flags+=(--ignore-vuln "$vuln_id")
done < <(grep -vE '^[[:space:]]*(#|$)' "$ALLOWLIST")

audit_member() {
  local label="$1" member_dir="$2"
  local reqs
  reqs=$(mktemp --suffix=.requirements.txt)
  echo "=== pip-audit: $label ==="
  (
    cd "$member_dir"
    uv export --frozen --all-extras --no-hashes --no-emit-workspace \
      --format requirements-txt -o "$reqs"
  )
  uvx "pip-audit@$PIP_AUDIT_VERSION" -r "$reqs" --no-deps --desc on \
    "${ignore_flags[@]}"
}

audit_member "klangk (klangkd server + klangk CLI wheel)" \
  "$REPO_ROOT/src/klangk"
audit_member "klangksidecar (network sidecar wheel)" \
  "$REPO_ROOT/src/klangksidecar"
