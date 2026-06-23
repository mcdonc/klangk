#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Install Node.js 24 via nvm.  nvm keeps everything under ~/.nvm so
# no sudo is needed for global npm installs.
export NVM_DIR="$HOME/.nvm"

if [ ! -d "$NVM_DIR" ]; then
  echo "Installing nvm..."
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
  cat >>~/.bashrc <<'BASH'
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
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
mkdir -p ~/.local/bin
cp "$SCRIPT_DIR/klangk-secret-provider.sh" ~/.local/bin/klangk-secret-provider
chmod +x ~/.local/bin/klangk-secret-provider

# Ensure ~/.local/bin is on PATH for non-login shells.
# shellcheck disable=SC2016
if ! grep -q '\.local/bin' ~/.bashrc 2>/dev/null; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >>~/.bashrc
fi
export PATH="$HOME/.local/bin:$PATH"

# Write openclaw config pointing at the klangk LLM proxy.
mkdir -p ~/.openclaw
cat >~/.openclaw/openclaw.json <<JSON
{
  "models": {
    "providers": {
      "llm-proxy": {
        "baseUrl": "http://host.containers.internal:8995/llm-proxy",
        "api": "openai-completions",
        "apiKey": {
          "source": "exec",
          "provider": "klangk",
          "id": "workspace-token"
        },
        "models": [
          { "id": "$KLANGK_LLM_MODEL", "name": "$KLANGK_LLM_MODEL" }
        ]
      }
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "llm-proxy/$KLANGK_LLM_MODEL"
      }
    }
  },
  "gateway": {
    "port": 8000,
    "bind": "lan"
  },
  "secrets": {
    "providers": {
      "klangk": {
        "source": "exec",
        "command": "$HOME/.local/bin/klangk-secret-provider",
        "passEnv": ["PATH", "HOME"]
      }
    }
  }
}
JSON

# Run onboard non-interactively, skipping the model/auth provider
# prompt (we already configured the llm-proxy provider above).
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
echo "Start the gateway with:"
echo "  openclaw gateway"
if [ -n "$host_port" ] && [ -n "$workspace_id" ]; then
  echo ""
  echo "Then open the UI at:"
  echo "  ${proto}://${hostname}${base_path}/hosted/${workspace_id}/${host_port}/"
fi
