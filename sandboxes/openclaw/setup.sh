#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/openclaw"

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

# Ensure non-login shells get nvm too.
# shellcheck disable=SC2016
if ! grep -q NVM_DIR ~/.bashrc 2>/dev/null; then
  cat >>~/.bashrc <<BASH
export NVM_DIR="$INSTALL_DIR/.nvm"
[ -s "\$NVM_DIR/nvm.sh" ] && . "\$NVM_DIR/nvm.sh"
BASH
fi

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

# Ensure /openclaw/bin is on PATH for non-login shells.
# shellcheck disable=SC2016
if ! grep -q "$INSTALL_DIR/bin" ~/.bashrc 2>/dev/null; then
  echo "export PATH=\"$INSTALL_DIR/bin:\$PATH\"" >>~/.bashrc
fi
export PATH="$INSTALL_DIR/bin:$PATH"

# Ensure Pi's models.json and clanker config are up to date.
/opt/klangk/bin/klangk-setup-clankers

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

# Ensure OPENCLAW_HOME is set for non-login shells.
# shellcheck disable=SC2016
if ! grep -q OPENCLAW_HOME ~/.bashrc 2>/dev/null; then
  echo "export OPENCLAW_HOME=\"$INSTALL_DIR\"" >>~/.bashrc
fi

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
echo "The gateway will start automatically via default-command."
if [ -n "$host_port" ] && [ -n "$workspace_id" ]; then
  echo ""
  echo "Open the UI at:"
  echo "  ${proto}://${hostname}${base_path}/hosted/${workspace_id}/${host_port}/"
fi
