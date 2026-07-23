#!/bin/bash
# service-command wrapper for the hermes sandbox.
#
# Refreshes the workspace token into $HERMES_HOME/.env, then execs the
# foreground gateway. Sandboxes have no on-shell-init hook, so this runs on
# every container start -- right before the long-running gateway reads the
# token -- keeping it fresh (the JWT rotates on every container restart).
#
# Runs in a login shell (the service-command pane sources ~/.profile), so
# `hermes` resolves from ~/.local/bin and HERMES_HOME is set.
set -euo pipefail

# If the llm-proxy is disabled, just start the gateway unconfigured.
if [ "${KLANGKWS_HERMES_USE_LLM_PROXY:-true}" != "true" ] &&
  [ "${KLANGKWS_HERMES_USE_LLM_PROXY:-}" != "1" ]; then
  exec hermes gateway run
fi
[ -n "${KLANGKWS_LLM_PROXY_URL:-}" ] || exec hermes gateway run

hermes_home="${HERMES_HOME:-$HOME/.hermes}"
token="$(/opt/klangk/bin/klangk-workspace-token 2>/dev/null || true)"

if [ -n "$token" ]; then
  env_file="$hermes_home/.env"
  mkdir -p "$hermes_home"
  touch "$env_file"
  # Refresh only the two keys we manage; preserve anything else the user set.
  sed -i '/^OPENAI_BASE_URL=/d;/^OPENAI_API_KEY=/d' "$env_file"
  cat >>"$env_file" <<EOF
OPENAI_BASE_URL=${KLANGKWS_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
EOF
fi

# `gateway run` is the foreground command recommended for Docker/WSL/Termux.
# With no messaging platforms configured it idles for cron job execution
# rather than exiting, so the health check still reports healthy.
exec hermes gateway run
