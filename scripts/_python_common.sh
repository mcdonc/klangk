# shellcheck shell=bash
# Shared helper: resolve the Python interpreter for scripts that need the
# project's dependencies. Sourced (not executed) by:
#   flutterbuildweb.sh, test-push.sh, _podman_common.sh
#
# Task and process exec environments (devenv tasks run / processes up) do
# NOT put the uv-managed venv (.devenv/state/venv) on PATH — ambient
# python3 resolves to the bare nix interpreter from languages.python,
# which carries none of the project's dependencies. On Python 3.13 that
# ambient interpreter happened to see a few propagated nix packages
# (pyyaml, via pyaml), masking the gap; the 3.14 package pin made it fail
# loudly ("PyYAML is required", #2864). Inside a devenv shell the venv IS
# on PATH, so this resolves to the same interpreter there.
#
# Sets KLANGK_PYTHON: the venv interpreter when the devenv venv exists,
# plain python3 otherwise (non-devenv contexts, e.g. release runners with
# their own interpreter).

# shellcheck disable=SC2034 # KLANGK_PYTHON is consumed by sourcing scripts

if [ -n "${DEVENV_ROOT:-}" ] && [ -x "${DEVENV_ROOT}/.devenv/state/venv/bin/python" ]; then
  KLANGK_PYTHON="${DEVENV_ROOT}/.devenv/state/venv/bin/python"
else
  # Fall back to the sibling checkout layout: this file lives in
  # <repo>/scripts/, the venv in <repo>/.devenv/state/venv.
  _klangk_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ -x "${_klangk_root}/.devenv/state/venv/bin/python" ]; then
    KLANGK_PYTHON="${_klangk_root}/.devenv/state/venv/bin/python"
  else
    KLANGK_PYTHON=python3
  fi
  unset _klangk_root
fi
