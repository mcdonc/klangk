#!/bin/bash
# Hermes sandbox setup — installs the NousResearch Hermes agent at runtime
# (per-workspace) and configures it to route inference through klangk's
# llm-proxy. Mirrors sandboxes/openclaw/setup.sh.
#
# Why a sandbox and not a plugin (#1109): hermes's installer spawns
# `bash -i` ONLY in its root/FHS-layout branch to probe PATH. A sandbox runs
# setup as the non-root klangk user, so that branch is never taken -- which
# makes the /tmp/.klangk-image-build bailout in bash.bashrc dead code (deleted
# in this change). Sandboxes also let each workspace install/configure hermes
# independently without rebuilding the image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/hermes"

# Hermes release branch -- single source of truth for the installed version.
# The human version (e.g. 0.17.x) tracks this but isn't asserted here.
HERMES_VERSION=v2026.6.19

# --- Persist env exports to ~/.profile UP FRONT, before the slow install. ---
# Why ~/.profile (#1087): it's the POSIX file sourced by ALL login shells --
# the default-command pane (interactive login shell), the health check
# (bash -lc), and `klangkc exec`. ~/.bashrc has an interactivity guard that
# hides its body from non-interactive shells, so these exports cannot live
# there. Written before the install so a shell spawned mid-setup already sees
# a complete PATH/HERMES_HOME.
# shellcheck disable=SC2016
if ! grep -q 'HERMES_HOME' ~/.profile 2>/dev/null; then
  echo "export HERMES_HOME=\"$INSTALL_DIR\"" >>~/.profile
fi
# shellcheck disable=SC2016
if ! grep -q 'klangk-hermes-path' ~/.profile 2>/dev/null; then
  {
    echo "# klangk-hermes-path"
    echo 'export PATH="$HOME/.local/bin:'"$INSTALL_DIR"'/bin:$PATH"'
  } >>~/.profile
fi

# set the env for THIS script's own commands (it runs non-login via bash -c).
export HERMES_HOME="$INSTALL_DIR"
export PATH="$HOME/.local/bin:$INSTALL_DIR/bin:$PATH"
mkdir -p "$INSTALL_DIR/bin"

# --- Install hermes (skip if already present). ---
# Non-root install layout: repo+venv at $HERMES_HOME/hermes-agent, binary link
# at ~/.local/bin/hermes, config/data at $HERMES_HOME. The root/FHS branch
# (which spawns `bash -i` to probe PATH) is never taken -- we run as klangk.
# ffmpeg/ripgrep: ripgrep is in the base image; ffmpeg is an optional runtime
# dep that the installer soft-fails on (non-interactive, no sudo) -- TTS voice
# features are limited without it but the install succeeds.
if ! command -v hermes >/dev/null 2>&1; then
  echo "Downloading hermes installer..."
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh
  echo "Installing hermes ${HERMES_VERSION}..."
  bash /tmp/hermes-install.sh \
    --hermes-home "$INSTALL_DIR" \
    --branch "$HERMES_VERSION" \
    --non-interactive \
    --skip-setup \
    --skip-browser \
    --no-skills </dev/null
  rm -f /tmp/hermes-install.sh
else
  echo "hermes already installed, skipping."
fi

# --- Configure klangk llm-proxy (idempotent). ---
# Sets OPENAI_BASE_URL + OPENAI_API_KEY in .env and writes config.yaml so
# hermes routes inference through klangk's proxy. Do NOT set
# HERMES_INFERENCE_MODEL -- it triggers provider auto-detection that bypasses
# the custom endpoint; the model goes in config.yaml instead.
use_proxy=false
case "${KLANGK_HERMES_USE_LLM_PROXY:-true}" in
true | 1) use_proxy=true ;;
esac
if [ "$use_proxy" = true ] && [ -n "${KLANGK_LLM_PROXY_URL:-}" ]; then
  token="$(/opt/klangk/bin/klangk-workspace-token 2>/dev/null || true)"

  # config.yaml -- provider + model (overwritten; they don't change).
  cat >"$INSTALL_DIR/config.yaml" <<EOF
model:
  provider: klangk-proxy
  model: "${KLANGK_LLM_MODEL}"
custom_providers:
  - name: klangk-proxy
    base_url: "${KLANGK_LLM_PROXY_URL}"
    key_env: OPENAI_API_KEY
EOF

  # .env -- refresh OPENAI_BASE_URL + OPENAI_API_KEY without clobbering
  # other keys a user may add later. This is the initial value for the
  # current container; the gateway wrapper (default-command) refreshes the
  # token before every gateway start, since the JWT rotates on restart.
  env_file="$INSTALL_DIR/.env"
  touch "$env_file"
  sed -i '/^OPENAI_BASE_URL=/d;/^OPENAI_API_KEY=/d' "$env_file"
  cat >>"$env_file" <<EOF
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
EOF
fi

# --- Install the gateway wrapper (default-command). ---
# Refreshes the workspace token into .env, then execs the foreground gateway.
# Copied (not bind-used directly) so it lands on PATH at $INSTALL_DIR/bin/.
cp "$SCRIPT_DIR/klangk-hermes-gateway.sh" "$INSTALL_DIR/bin/klangk-hermes-gateway"
chmod +x "$INSTALL_DIR/bin/klangk-hermes-gateway"

# Refresh Pi agent config (extensions, settings, models, skills).
/opt/klangk/bin/klangk-setup-clankers

echo ""
echo "hermes: $(hermes --version 2>&1 | head -1)"
echo ""
echo "Setup complete."
echo "The gateway starts automatically via default-command (klangk-hermes-gateway)."
