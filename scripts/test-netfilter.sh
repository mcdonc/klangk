#!/usr/bin/env bash
# Manual integration test for the workspace netfilter (allowed_domains).
#
# Prerequisites:
#   - klangkd running locally (default: http://localhost:8997)
#   - admin/admin credentials seeded (devenv default)
#   - SSH key loaded in the local agent (ssh-add -l shows a key)
#   - forward-agent: true in klangkd.yaml (devenv default)
#   - expect(1) available (NixOS: in system packages; Debian: apt install expect)
#
# Usage:
#   scripts/test-netfilter.sh                    # default server
#   scripts/test-netfilter.sh http://localhost:9000   # custom server
#
# The script creates ephemeral workspaces, runs tests, and cleans up.
# It is NOT run in CI — it requires a live klangkd + container runtime.

set -uo pipefail
# Not using set -e — we handle errors per-test and report them in the summary.

SERVER="${1:-http://localhost:8997}"
USER="admin"
PASS="admin"
CLONE_REPO="git@github.com:mcdonc/klangk.git"
CLONE_OPTS="--depth 1" # shallow clone for speed
SHELL_SETTLE=8         # seconds to wait for klangk shell to be ready
CLONE_TIMEOUT=90       # seconds before declaring a git-clone hung

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[0;33m'
RESET=$'\033[0m'

pass=0
fail=0
skip=0

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

log() { echo "${GREEN}[+]${RESET} $*"; }
warn() { echo "${YELLOW}[!]${RESET} $*"; }
err() { echo "${RED}[-]${RESET} $*"; }

cleanup_ws() {
  local ws_id="$1"
  # Stop then delete — stop is idempotent, delete fails if running.
  curl -s -X POST "$SERVER/api/v1/workspaces/$ws_id/stop" \
    -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
  sleep 1
  curl -s -X DELETE "$SERVER/api/v1/workspaces/$ws_id" \
    -H "Authorization: Bearer $TOKEN" >/dev/null 2>&1 || true
}

api_login() {
  local resp
  resp=$(curl -s -X POST "$SERVER/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"identifier\":\"$USER\",\"password\":\"$PASS\"}")
  TOKEN=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  if [[ -z $TOKEN || $TOKEN == "None" ]]; then
    err "Login failed: $resp"
    exit 1
  fi
}

create_ws() {
  # $1 = name, $2 = JSON allowed_domains (e.g. '["github.com:22"]' or 'null')
  local name="$1" domains="$2"
  local resp
  resp=$(curl -s -X POST "$SERVER/api/v1/workspaces" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"allowed_domains\":$domains}")
  local ws_id
  ws_id=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
  if [[ -z $ws_id ]]; then
    err "Failed to create workspace '$name': $resp"
    return 1
  fi
  echo "$ws_id"
}

start_ws() {
  curl -s -X POST "$SERVER/api/v1/workspaces/$1/start" \
    -H "Authorization: Bearer $TOKEN" >/dev/null
  # Wait for the container to be running.
  local _i
  for _i in $(seq 1 15); do
    local running
    running=$(curl -s "$SERVER/api/v1/workspaces/$1/status" \
      -H "Authorization: Bearer $TOKEN" |
      python3 -c "import sys,json; print(json.load(sys.stdin).get('running',False))")
    [[ $running == "True" ]] && return 0
    sleep 1
  done
  err "workspace $1 did not start within 15s"
  return 1
}

# Run a command inside a klangk shell via expect.
# $1 = workspace name, $2 = command string
# Prints the raw expect output; caller greps for result markers.
shell_exec() {
  local ws_name="$1" cmd="$2"
  expect -f - <<EXPECT_SCRIPT 2>&1
set timeout $CLONE_TIMEOUT
spawn bash -c "devenv shell -- python -m klangk.cli.main --server $SERVER shell $ws_name 0"
sleep $SHELL_SETTLE
send "\r"
sleep 1
send "$cmd; echo __MARKER_RC=\\\$?\r"
expect {
    -re {__MARKER_RC=(\d+)} {
        set code \$expect_out(1,string)
        puts "\n__RESULT=\$code"
    }
    timeout {
        puts "\n__RESULT=EXPECT_TIMEOUT"
    }
}
send "exit\r"
sleep 1
send "q"
expect eof
EXPECT_SCRIPT
}

# Extract the __RESULT= value from shell_exec output.
# Uses sed instead of grep -oP for macOS (BSD grep) portability.
extract_result() {
  sed -n 's/.*__RESULT=\([^[:space:]]*\).*/\1/p' <<<"$1" | tail -1
}

record() {
  local label="$1" expected="$2" actual="$3"
  if [[ $actual == "$expected" ]]; then
    log "PASS: $label (got $actual)"
    ((pass++))
  else
    err "FAIL: $label — expected $expected, got $actual"
    ((fail++))
  fi
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

log "Server: $SERVER"

if ! command -v expect &>/dev/null; then
  err "expect(1) not found — install it first"
  exit 1
fi

if ! ssh-add -l &>/dev/null; then
  err "No SSH keys in agent — load one first (ssh-add)"
  exit 1
fi

if ! curl -sf "$SERVER/api/v1/config" >/dev/null 2>&1; then
  err "Cannot reach $SERVER — is klangkd running?"
  exit 1
fi

api_login
log "Logged in as $USER (token ${TOKEN:0:20}…)"

# Clean up any leftover test workspaces from previous runs.
cleanup_stale() {
  local ws_list
  ws_list=$(curl -s "$SERVER/api/v1/workspaces?limit=100" \
    -H "Authorization: Bearer $TOKEN" |
    python3 -c "
import sys, json
for ws in json.load(sys.stdin).get('items', []):
    if ws['name'].startswith('nf-test-'):
        print(ws['id'], ws['name'])
" 2>/dev/null)
  while IFS=' ' read -r ws_id ws_name; do
    [[ -z $ws_id ]] && continue
    warn "Cleaning up stale workspace $ws_name ($ws_id)"
    cleanup_ws "$ws_id"
  done <<<"$ws_list"
}
cleanup_stale

# ---------------------------------------------------------------------------
# Test 1: git clone over SSH works WITHOUT a netfilter
# ---------------------------------------------------------------------------

log "--- Test 1: git clone SSH without netfilter ---"

WS1_ID=$(create_ws "nf-test-open" "null")
log "Created workspace nf-test-open ($WS1_ID)"
start_ws "$WS1_ID"
log "Workspace started"

out=$(shell_exec "nf-test-open" "rm -rf /tmp/nf-clone && git clone $CLONE_OPTS $CLONE_REPO /tmp/nf-clone 2>&1")
rc=$(extract_result "$out")
record "git clone SSH (no netfilter)" "0" "$rc"

cleanup_ws "$WS1_ID"
log "Cleaned up nf-test-open"

# ---------------------------------------------------------------------------
# Test 2: git clone over SSH works WITH github.com:22 netfilter
# ---------------------------------------------------------------------------

log "--- Test 2: git clone SSH with github.com:22 netfilter ---"

WS2_ID=$(create_ws "nf-test-ssh" '["github.com:22"]')
log "Created workspace nf-test-ssh ($WS2_ID) — allowed_domains=[github.com:22]"
start_ws "$WS2_ID"
log "Workspace started"

out=$(shell_exec "nf-test-ssh" "rm -rf /tmp/nf-clone && git clone $CLONE_OPTS $CLONE_REPO /tmp/nf-clone 2>&1")
rc=$(extract_result "$out")
record "git clone SSH (github.com:22)" "0" "$rc"

cleanup_ws "$WS2_ID"
log "Cleaned up nf-test-ssh"

# ---------------------------------------------------------------------------
# Test 3: git clone over SSH BLOCKED with wrong netfilter (example.com:22)
# ---------------------------------------------------------------------------

log "--- Test 3: git clone SSH blocked by wrong netfilter ---"

WS3_ID=$(create_ws "nf-test-blocked" '["example.com:22"]')
log "Created workspace nf-test-blocked ($WS3_ID) — allowed_domains=[example.com:22]"
start_ws "$WS3_ID"
log "Workspace started"

out=$(shell_exec "nf-test-blocked" "rm -rf /tmp/nf-clone && git clone $CLONE_OPTS $CLONE_REPO /tmp/nf-clone 2>&1")
rc=$(extract_result "$out")
# Should NOT be 0 — clone should fail (timeout or connection refused).
if [[ $rc != "0" ]]; then
  log "PASS: git clone blocked by wrong netfilter (got $rc)"
  ((pass++))
else
  err "FAIL: git clone should have been blocked but succeeded"
  ((fail++))
fi

cleanup_ws "$WS3_ID"
log "Cleaned up nf-test-blocked"

# ---------------------------------------------------------------------------
# Test 4: HTTPS blocked when only SSH allowed
# ---------------------------------------------------------------------------

log "--- Test 4: HTTPS blocked when only github.com:22 allowed ---"

WS4_ID=$(create_ws "nf-test-no-https" '["github.com:22"]')
log "Created workspace nf-test-no-https ($WS4_ID) — allowed_domains=[github.com:22]"
start_ws "$WS4_ID"
log "Workspace started"

out=$(shell_exec "nf-test-no-https" "timeout 10 curl -sf https://github.com >/dev/null 2>&1")
rc=$(extract_result "$out")
if [[ $rc != "0" ]]; then
  log "PASS: HTTPS to github.com blocked when only :22 allowed (got $rc)"
  ((pass++))
else
  err "FAIL: HTTPS should have been blocked but succeeded"
  ((fail++))
fi

cleanup_ws "$WS4_ID"
log "Cleaned up nf-test-no-https"

# ---------------------------------------------------------------------------
# Test 5: bare domain (no port) allows all traffic to that host
# ---------------------------------------------------------------------------

log "--- Test 5: bare domain allows all ports ---"

WS5_ID=$(create_ws "nf-test-bare" '["github.com"]')
log "Created workspace nf-test-bare ($WS5_ID) — allowed_domains=[github.com]"
start_ws "$WS5_ID"
log "Workspace started"

out=$(shell_exec "nf-test-bare" "rm -rf /tmp/nf-clone && git clone $CLONE_OPTS $CLONE_REPO /tmp/nf-clone 2>&1")
rc=$(extract_result "$out")
record "git clone SSH (bare github.com — all ports)" "0" "$rc"

cleanup_ws "$WS5_ID"
log "Cleaned up nf-test-bare"

# ---------------------------------------------------------------------------
# Test 6: apt install works with Debian netfilter domains
# ---------------------------------------------------------------------------

log "--- Test 6: apt install with Debian netfilter ---"

DEBIAN_DOMAINS='["deb.debian.org:80","deb.debian.org:443","security.debian.org:80","security.debian.org:443","cdn-fastly.deb.debian.org:80","cdn-fastly.deb.debian.org:443","cdn-aws.deb.debian.org:443"]'
WS6_ID=$(create_ws "nf-test-apt" "$DEBIAN_DOMAINS")
log "Created workspace nf-test-apt ($WS6_ID) — Debian netfilter"
start_ws "$WS6_ID"
log "Workspace started"

out=$(shell_exec "nf-test-apt" "sudo apt-get update -qq 2>&1 && sudo apt-get install -y -qq sl 2>&1 && sl --version 2>&1 || which sl")
rc=$(extract_result "$out")
record "apt install with Debian netfilter" "0" "$rc"

cleanup_ws "$WS6_ID"
log "Cleaned up nf-test-apt"

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

echo ""
echo "=============================="
echo "  Results: ${GREEN}${pass} passed${RESET}, ${RED}${fail} failed${RESET}, ${YELLOW}${skip} skipped${RESET}"
echo "=============================="

[[ $fail -eq 0 ]] && exit 0 || exit 1
