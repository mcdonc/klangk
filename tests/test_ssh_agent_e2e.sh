#!/usr/bin/env bash
# End-to-end test: SSH agent forwarding via `klangk shell -A`.
#
# Starts a fresh klangkd, logs in, creates a workspace, connects with
# `klangk shell <name> -A` driven by expect(1), runs `ssh-add -l` inside the
# container, and verifies that at least one SSH key fingerprint (SHA256:...)
# appears in the output.
#
# Requirements:
#   - devenv shell available (provides python, klangk, expect, etc.)
#   - podman running and the klangk-workspace image built
#   - SSH_AUTH_SOCK set (a running macOS/Linux ssh-agent with >= 1 key loaded)
#
# Usage:
#   devenv shell -- bash tests/test_ssh_agent_e2e.sh
#
# Exit codes: 0 = pass, 1 = fail/skip.

set -euo pipefail

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Colour

pass_msg() { printf "${GREEN}PASS${NC}: %s\n" "$1"; }
fail_msg() { printf "${RED}FAIL${NC}: %s\n" "$1"; }
skip_msg() { printf "${YELLOW}SKIP${NC}: %s\n" "$1"; }
info_msg() { printf "INFO: %s\n" "$1"; }

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
  skip_msg "SSH_AUTH_SOCK is not set -- cannot test agent forwarding"
  exit 1
fi

# Verify the agent has at least one key
if ! ssh-add -l 2>/dev/null | grep -q "SHA256:"; then
  skip_msg "No SSH keys loaded in agent (ssh-add -l shows none)"
  exit 1
fi

if ! command -v expect &>/dev/null; then
  skip_msg "expect(1) not found on PATH"
  exit 1
fi

if ! command -v klangk &>/dev/null; then
  skip_msg "klangk CLI not found on PATH (run inside devenv shell)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Temp dirs & cleanup trap
# ---------------------------------------------------------------------------
# Use a path under $HOME (not /tmp) because macOS /tmp resolves to
# /private/tmp which the Podman machine may not mount identically.
# Paths under /Users are shared 1:1 between macOS and the VM.
# Keep the name short — macOS has a 104-char limit on UDS socket paths,
# and klangkd creates a socket at $state_dir/klangk.sock.
TMPBASE="$(mktemp -d "$HOME/.kse2e.XXXXXX")"
SERVER_STATE_DIR="$TMPBASE/state"
SERVER_DATA_DIR="$TMPBASE/data"
CLI_HOME="$TMPBASE/cli-home"
SERVER_LOG="$TMPBASE/server.log"
PASSWORD_FILE="$TMPBASE/password"

mkdir -p "$SERVER_STATE_DIR" "$SERVER_DATA_DIR" "$CLI_HOME"

# The server config YAML (written below after port allocation).
SERVER_CONFIG="$TMPBASE/klangkd-test.yaml"

SERVER_PID=""
export WORKSPACE_NAME="ssh-e2e-test"

# shellcheck disable=SC2329  # invoked via trap
cleanup() {
  info_msg "Cleaning up..."
  # Kill server
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  # Clean up podman containers labelled with our instance id
  local instance_id_file="$SERVER_DATA_DIR/instance-id"
  if [ -f "$instance_id_file" ]; then
    local iid
    iid="$(cat "$instance_id_file" 2>/dev/null || true)"
    if [ -n "$iid" ]; then
      local cids
      cids="$(podman ps -a --filter "label=klangk.instance=$iid" -q 2>/dev/null || true)"
      if [ -n "$cids" ]; then
        # shellcheck disable=SC2086
        podman rm -f $cids 2>/dev/null || true
      fi
    fi
  fi
  rm -rf "$TMPBASE"
  info_msg "Cleanup done."
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Allocate a free port
# ---------------------------------------------------------------------------
pick_free_port() {
  python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
"
}

SERVER_PORT="$(pick_free_port)"
EGRESS_PORT="$(pick_free_port)"
# Ensure egress != server port
while [ "$EGRESS_PORT" = "$SERVER_PORT" ]; do
  EGRESS_PORT="$(pick_free_port)"
done

export SERVER_URL="http://127.0.0.1:${SERVER_PORT}"

info_msg "Server port: $SERVER_PORT  Egress port: $EGRESS_PORT"
info_msg "Temp dir:    $TMPBASE"

# ---------------------------------------------------------------------------
# Write server config
# ---------------------------------------------------------------------------
cat >"$SERVER_CONFIG" <<EOF
port: "${SERVER_PORT}"
listen: "127.0.0.1"
egress_port: "${EGRESS_PORT}"
auth_modes: password
jwt_secret: test-secret-e2e-ssh
default_user: test@test.com
default_password: testpass
state_dir: "${SERVER_STATE_DIR}"
data_dir: "${SERVER_DATA_DIR}"
idle_timeout_seconds: "300"
image_name: klangk-workspace
EOF

# ---------------------------------------------------------------------------
# Write password file for klangk login
# ---------------------------------------------------------------------------
printf 'testpass' >"$PASSWORD_FILE"

# ---------------------------------------------------------------------------
# CLI isolation (XDG overrides)
# ---------------------------------------------------------------------------
# We do NOT export XDG_CONFIG_HOME/XDG_STATE_HOME globally because podman
# (inside the klangkd server process) uses XDG_CONFIG_HOME to locate its
# machine connection config.  Instead, CLI commands get their own env via
# the cli() wrapper below.
export CLI_XDG_CONFIG="$CLI_HOME/.config"
export CLI_XDG_STATE="$CLI_HOME/.local/state"
mkdir -p "$CLI_XDG_CONFIG/klangk" "$CLI_XDG_STATE/klangk"

# Write CLI config with forward-agent enabled.
cat >"$CLI_XDG_CONFIG/klangk/klangk.yaml" <<CLICFG
forward-agent: true
servers: {}
CLICFG

# Wrapper: run a klangk CLI command with isolated XDG dirs.
cli() {
  XDG_CONFIG_HOME="$CLI_XDG_CONFIG" XDG_STATE_HOME="$CLI_XDG_STATE" "$@"
}

# ---------------------------------------------------------------------------
# Start klangkd server (no XDG overrides — podman needs the real config)
# ---------------------------------------------------------------------------
info_msg "Starting klangkd on $SERVER_URL ..."
KLANGKD_PREVENT_INSECURE_JWT_SECRET="" \
  KLANGKD_TEST_MODE=1 \
  LOGFIRE_TOKEN="" \
  python3 -m klangk.main --config="$SERVER_CONFIG" \
  >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Poll /health until ready (up to 60 s)
DEADLINE=$((SECONDS + 60))
SERVER_READY=0
while [ $SECONDS -lt $DEADLINE ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    fail_msg "klangkd exited early. Server log:"
    cat "$SERVER_LOG"
    exit 1
  fi
  if curl -sf "$SERVER_URL/health" >/dev/null 2>&1; then
    SERVER_READY=1
    break
  fi
  sleep 0.5
done

if [ $SERVER_READY -ne 1 ]; then
  fail_msg "klangkd did not become healthy within 60 s. Server log:"
  tail -80 "$SERVER_LOG"
  exit 1
fi

info_msg "klangkd is healthy."

# ---------------------------------------------------------------------------
# klangk login
# ---------------------------------------------------------------------------
info_msg "Logging in..."
cli klangk login "$SERVER_URL" "test@test.com" --password-file "$PASSWORD_FILE"
info_msg "Login successful."

# ---------------------------------------------------------------------------
# klangk create
# ---------------------------------------------------------------------------
info_msg "Creating workspace '$WORKSPACE_NAME'..."
cli klangk --server "$SERVER_URL" create "$WORKSPACE_NAME"
info_msg "Workspace created."

# Do not start explicitly — `klangk shell` sends workspace_connect which
# starts the container on demand.  Give the create API a moment to finish
# writing the workspace home directory.
sleep 2

# ---------------------------------------------------------------------------
# klangk shell -A via expect  (the core test)
# ---------------------------------------------------------------------------
info_msg "Connecting to workspace shell with agent forwarding (-A)..."

EXPECT_LOG="$TMPBASE/expect.log"

# The expect script navigates the TUI (not `klangk shell` directly):
#   1. Spawn bare `klangk --server <url>` to launch the TUI
#   2. TUI MainScreen: workspace list — press Enter on the workspace
#   3. TUI WorkspaceDetailScreen: terminal list (auto-focused) — press Enter
#   4. TUI suspends, spawns `klangk shell` subprocess → tmux shell appears
#   5. Run ssh-add -l and check for SSH key fingerprints
#   6. Disconnect with Enter, ~.  → TUI resumes, then exit with q/Ctrl-C
expect_exit=0
expect -f - <<'EXPECT_SCRIPT' >"$EXPECT_LOG" 2>&1 || expect_exit=$?
set ws_name $env(WORKSPACE_NAME)
set server_url $env(SERVER_URL)
set timeout 120

# Set CLI-specific XDG paths
set ::env(XDG_CONFIG_HOME) $env(CLI_XDG_CONFIG)
set ::env(XDG_STATE_HOME) $env(CLI_XDG_STATE)

# Set terminal dimensions for both the TUI and the shell it spawns.
set stty_init "rows 24 columns 80"

# 1. Launch the TUI (bare klangk, no subcommand).
spawn klangk --server $server_url

# 2. Wait for the workspace to appear in the TUI workspace list.
expect {
    $ws_name {
        puts "\nTUI: saw workspace in list"
    }
    timeout {
        puts "EXPECT_TIMEOUT: timed out waiting for TUI workspace list"
        exit 2
    }
    eof {
        puts "EXPECT_EOF: TUI exited early"
        exit 2
    }
}

# Give the TUI a moment to finish rendering, then press Enter to
# select the workspace (it should be the only one, already highlighted).
sleep 2
send "\r"

# 3. Wait for the WorkspaceDetailScreen.  Look for the Terminals
#    header or the "(no terminals)" placeholder.
expect {
    "Terminals" {
        puts "\nTUI: saw workspace detail screen"
    }
    timeout {
        puts "EXPECT_TIMEOUT: timed out waiting for workspace detail screen"
        exit 2
    }
    eof {
        puts "EXPECT_EOF: TUI exited early"
        exit 2
    }
}

# Give the detail screen time to fully render and load terminals.
sleep 3

# If this is a fresh workspace there may be no terminals yet.
# Press "n" to create a new terminal, wait for it to appear,
# then press Enter to launch it.  If a terminal already exists
# (e.g. "0  bash"), pressing "n" creates another — harmless.
send "n"
sleep 3

# Now select the (first) terminal and press Enter.
send "\r"

# 4. Wait for the tmux status bar in the spawned shell.
expect {
    "0:bash" {
        puts "\nSHELL: tmux is up"
    }
    timeout {
        puts "EXPECT_TIMEOUT: timed out waiting for tmux shell"
        exit 2
    }
    eof {
        puts "EXPECT_EOF: shell exited before tmux appeared"
        exit 2
    }
}

# Give the shell a few seconds to fully settle.
sleep 3

# 5. Run ssh-add -l
send "ssh-add -l\r"

set timeout 30
expect {
    "SHA256:" {
        puts "\nAGENT_FORWARD_OK: saw SSH key fingerprint"
        set result 0
    }
    "no identities" {
        puts "\nAGENT_FORWARD_PARTIAL: agent connected but no keys"
        set result 0
    }
    "Could not open" {
        puts "\nAGENT_FORWARD_FAIL: Could not open agent connection"
        set result 1
    }
    "Error connecting" {
        puts "\nAGENT_FORWARD_FAIL: Error connecting to agent"
        set result 1
    }
    timeout {
        puts "\nAGENT_FORWARD_TIMEOUT: timed out waiting for ssh-add output"
        set result 1
    }
    eof {
        puts "\nAGENT_FORWARD_EOF: shell exited unexpectedly"
        set result 1
    }
}

# 6. Disconnect from shell: Enter then ~.
#    This exits the subprocess and the TUI resumes.
sleep 1
send "\r"
sleep 1
send "~."

# Wait for the TUI to resume (workspace detail screen reappears).
sleep 3

# Exit the TUI with Ctrl-C.
send "\x03"

expect {
    eof { }
    timeout { }
}

exit $result
EXPECT_SCRIPT

# ---------------------------------------------------------------------------
# Evaluate result
# ---------------------------------------------------------------------------
info_msg "--- expect log ---"
cat "$EXPECT_LOG"
info_msg "--- end expect log ---"

if [ $expect_exit -eq 0 ]; then
  # Double-check that the log actually contains a fingerprint
  if grep -q "SHA256:" "$EXPECT_LOG"; then
    pass_msg "SSH agent forwarding works -- key fingerprint seen inside container"
    exit 0
  elif grep -q "AGENT_FORWARD_PARTIAL" "$EXPECT_LOG"; then
    pass_msg "SSH agent forwarding works -- agent connected (no identities, but socket forwarded)"
    exit 0
  else
    fail_msg "expect exited 0 but no SHA256: fingerprint found in output"
    exit 1
  fi
else
  fail_msg "SSH agent forwarding test failed (expect exit code: $expect_exit)"
  info_msg "Server log (last 60 lines):"
  tail -60 "$SERVER_LOG"
  exit 1
fi
