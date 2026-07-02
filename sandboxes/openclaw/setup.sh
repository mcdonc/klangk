#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/openclaw"

# Repoint HOME at the agent's home for the rest of this script so every
# home-relative path -- the ~/.profile exports below, the nvm/node install
# under NVM_DIR (which is $INSTALL_DIR/.nvm, unaffected), and any tool link
# -- resolves into the AGENT's home, the identity that runs the default
# command (#1133/#1158: the gateway runs in the agent's standalone
# `service` tmux session). The owner manages openclaw through the Service
# terminal tab, not their own shell, so nothing openclaw-related belongs in
# the owner's home (#1171). $KLANGK_AGENT_HOME is injected into the
# container env at bring-up and inherited by every podman exec (including
# the WS exec that runs this script); the `:-` fallback defends against an
# unset var. With HOME repointed, the existing ~/.profile appends below
# land in the agent's ~/.profile unchanged.
export HOME="${KLANGK_AGENT_HOME:-/home/clanker}"

# Persist every env export the service_command depends on to ~/.profile
# UP FRONT, before any long-running install step.
#
# Why ~/.profile: it's the POSIX file sourced by login shells -- here,
# the agent's `service` tmux session that runs the service command (HOME
# was repointed above to the agent's home). ~/.bashrc has an
# interactivity guard near its top (`case $- in *i*) ;; *) return`)
# that hides anything appended below it from non-interactive shells, so
# exports the service command needs cannot live there.
#
# NOTE: the workspace health check is NOT a reason to put these in
# ~/.profile. The check runs as a NON-login shell (`bash -c`) and
# sources nothing -- it uses the absolute-path /openclaw/bin/healthcheck.sh
# wrapper instead. Don't add exports here "for the health check".
#
# Why UP FRONT (#1039): a shell that sources the agent's ~/.profile while
# setup is still running (e.g. the service session created by an early
# terminal_start during setup -- see #1033) must see the complete
# pointer set from its very first spawn:
#   - NVM_DIR + nvm.sh source (so nvm/node load in new shells)
#   - /openclaw/bin on PATH (so the `openclaw` binary is found)
#   - OPENCLAW_HOME (so openclaw locates its config; the agent's
#     $HOME is /home/clanker, not /openclaw, so without this openclaw
#     looks in the wrong place and reports "Missing config" -- #1039)
# These used to be appended at three separate points in setup, so a
# mid-setup pane inherited PATH but not OPENCLAW_HOME. Each line is
# guarded so re-running setup never duplicates it.
# shellcheck disable=SC2016
if ! grep -q NVM_DIR ~/.profile 2>/dev/null; then
  cat >>~/.profile <<BASH
export NVM_DIR="$INSTALL_DIR/.nvm"
[ -s "\$NVM_DIR/nvm.sh" ] && . "\$NVM_DIR/nvm.sh"
BASH
fi
# shellcheck disable=SC2016
if ! grep -q "$INSTALL_DIR/bin" ~/.profile 2>/dev/null; then
  echo "export PATH=\"$INSTALL_DIR/bin:\$PATH\"" >>~/.profile
fi
# shellcheck disable=SC2016
if ! grep -q OPENCLAW_HOME ~/.profile 2>/dev/null; then
  echo "export OPENCLAW_HOME=\"$INSTALL_DIR\"" >>~/.profile
fi

# Test hook (no-op in production): the e2e test in sandboxes/tests/openclaw/
# drops a sentinel file on the mount to hold setup here, spawns a terminal
# mid-setup, and asserts the exports above are already in the spawned
# shell's environment. This guards the #1039 invariant: every ~/.profile
# export the service_command depends on must be written before any
# long-running step, so a shell spawned at any point sees the full set.
# The sentinel never exists outside that test.
while [ -f "$INSTALL_DIR/.klangk-test-pause" ]; do
  sleep 0.5
done

# Install Node.js 24 via nvm into /openclaw/.nvm.
export NVM_DIR="$INSTALL_DIR/.nvm"

if [ ! -d "$NVM_DIR" ]; then
  echo "Installing nvm..."
  mkdir -p "$NVM_DIR"
  curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
fi

# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh"

if ! nvm ls 24 &>/dev/null; then
  echo "Installing Node.js 24..."
  nvm install 24
fi
nvm use 24
nvm alias default 24

# Install openclaw globally (into nvm's prefix, no sudo needed).
if ! command -v openclaw &>/dev/null; then
  echo "Installing openclaw..."
  npm install -g openclaw@latest
else
  echo "openclaw already installed, skipping."
fi

# Install the klangk secret provider for dynamic workspace tokens.
mkdir -p "$INSTALL_DIR/bin"
cp "$SCRIPT_DIR/klangk-secret-provider.sh" "$INSTALL_DIR/bin/klangk-secret-provider"
chmod +x "$INSTALL_DIR/bin/klangk-secret-provider"

# Stable symlink so the health check can invoke openclaw by absolute
# path. The host monitor runs the check via `bash -c` (NON-login), so
# it does not source ~/.profile / nvm -- bare `openclaw` would not
# resolve. npm rewrote openclaw's shebang to an absolute node path, so
# this symlink runs standalone without node on PATH.
ln -sf "$(command -v openclaw)" "$INSTALL_DIR/bin/openclaw"

# Copy the health-check wrapper to a stable absolute path (same reason:
# the NON-login check cannot rely on PATH). It sets OPENCLAW_HOME and
# execs /openclaw/bin/openclaw health.
cp "$SCRIPT_DIR/healthcheck.sh" "$INSTALL_DIR/bin/healthcheck.sh"
chmod +x "$INSTALL_DIR/bin/healthcheck.sh"

export PATH="$INSTALL_DIR/bin:$PATH"

# Ensure Pi's models.json and clanker config are up to date.
/opt/klangk/bin/klangk-setup-pi

# Run onboard non-interactively first — it creates initial config
# and sets up auth tokens. We overwrite the config afterward so
# onboard doesn't clobber our settings (bind, http endpoints, etc.).
export OPENCLAW_HOME="$INSTALL_DIR"
openclaw onboard --non-interactive \
  --accept-risk \
  --mode local \
  --flow quickstart \
  --auth-choice skip \
  --skip-channels \
  --skip-skills \
  --skip-search \
  --skip-health \
  --skip-ui

# Write openclaw config on top of onboard's output.
# We preserve gateway.auth (written by onboard) and merge our settings.
python3 -c "
import json, os
cfg_path = '$INSTALL_DIR/.openclaw/openclaw.json'
with open(cfg_path) as f:
    cfg = json.load(f)
cfg['models'] = {
    'providers': {
        'llm-proxy': {
            'baseUrl': 'http://host.containers.internal:8995/llm-proxy',
            'api': 'openai-completions',
            'apiKey': {
                'source': 'exec',
                'provider': 'klangk',
                'id': 'workspace-token'
            },
            'models': [
                {'id': '\$KLANGK_LLM_MODEL', 'name': '\$KLANGK_LLM_MODEL'}
            ],
        }
    }
}
cfg['agents'] = cfg.get('agents', {})
cfg['agents']['defaults'] = cfg['agents'].get('defaults', {})
cfg['agents']['defaults']['model'] = {'primary': 'llm-proxy/\$KLANGK_LLM_MODEL'}
cfg['gateway']['port'] = 8000
# Allow the gateway to reach the LLM proxy on the host's private
# network and listen on all interfaces so Klangk's hosted app
# proxy can reach it.
cfg['models']['providers']['llm-proxy']['request'] = {'allowPrivateNetwork': True}
cfg['gateway']['bind'] = 'lan'
cfg['gateway']['http'] = {'endpoints': {'chatCompletions': {'enabled': True}}}
# Trust the Klangk reverse proxy and allow the hosted origin for
# WebSocket connections (required since openclaw v2026.2.26).
hosting_proto = os.environ.get('KLANGK_HOSTING_PROTO', 'http')
hosting_hostname = os.environ.get('KLANGK_HOSTING_HOSTNAME', 'localhost:8995')
hosted_origin = f'{hosting_proto}://{hosting_hostname}'
cfg['gateway']['controlUi'] = cfg.get('gateway', {}).get('controlUi', {})
cfg['gateway']['controlUi']['allowedOrigins'] = [
    hosted_origin,
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]
cfg['gateway']['trustedProxies'] = ['0.0.0.0/0']
cfg['secrets'] = {
    'providers': {
        'klangk': {
            'source': 'exec',
            'command': '$INSTALL_DIR/bin/klangk-secret-provider',
            'passEnv': ['PATH', 'HOME']
        }
    }
}
with open(cfg_path, 'w') as f:
    json.dump(cfg, f, indent=2)
"

# Derive the hosted app URL from Klangk env vars.
# Container port 8000 maps to the first host port in KLANGK_PORT_MAPPINGS.
host_port=""
if [ -n "${KLANGK_PORT_MAPPINGS:-}" ]; then
  # Format: 8000:9000,8001:9001,...
  host_port=$(echo "$KLANGK_PORT_MAPPINGS" | cut -d, -f1 | cut -d: -f2)
fi
proto="${KLANGK_HOSTING_PROTO:-http}"
hostname="${KLANGK_HOSTING_HOSTNAME:-localhost:8995}"
base_path="${KLANGK_HOSTING_BASE_PATH:-}"
workspace_id="${KLANGK_WORKSPACE_ID:-}"

echo ""
echo "node: $(node -v)"
echo "openclaw: $(openclaw --version)"
echo ""
echo "Setup complete."
echo "The gateway will start automatically via service-command."
if [ -n "$host_port" ] && [ -n "$workspace_id" ]; then
  echo ""
  echo "Open the UI at:"
  echo "  ${proto}://${hostname}${base_path}/hosted/${workspace_id}/${host_port}/"
fi
