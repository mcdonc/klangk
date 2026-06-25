#!/bin/bash
# Create a symlink farm that maps nix playwright browser directories to the
# revision names the npm @playwright/test package expects.
#
# Nix backports browser security patches independently, so its revision
# numbers (e.g. firefox-1522) may differ from upstream npm (firefox-1511)
# even at the same playwright version.
#
# Sourced from enterShell — exports PLAYWRIGHT_BROWSERS_PATH.
# Requires: NIX_PLAYWRIGHT_BROWSERS, DEVENV_STATE, DEVENV_ROOT

_nix_pw="${NIX_PLAYWRIGHT_BROWSERS:-}"
_npm_json="$DEVENV_ROOT/src/frontend/e2e-tests/node_modules/playwright-core/browsers.json"

if [ -z "$_nix_pw" ] || [ ! -f "$_npm_json" ]; then
  export PLAYWRIGHT_BROWSERS_PATH="${_nix_pw:-}"
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

_farm="$DEVENV_STATE/playwright-browsers"
mkdir -p "$_farm"

for _dir in "$_nix_pw"/*/; do
  _nix_name=$(basename "$_dir")
  _prefix=${_nix_name%-*}
  # browsers.json uses dashes; nix dirs use underscores
  _json_prefix=${_prefix//_/-}

  _target=$(python3 -c "
import json, sys
with open(sys.argv[1]) as f:
    for b in json.load(f).get('browsers', []):
        if b['name'] == sys.argv[2]:
            print(b['name'].replace('-', '_') + '-' + b['revision'])
            break
" "$_npm_json" "$_json_prefix" 2>/dev/null)

  [ -n "$_target" ] && ln -sfn "$_dir" "$_farm/$_target"
done

export PLAYWRIGHT_BROWSERS_PATH="$_farm"
