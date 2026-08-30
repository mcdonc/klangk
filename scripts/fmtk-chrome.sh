#!/usr/bin/env bash
# CHROME_EXECUTABLE wrapper for `flutter run --debug -d chrome` under the
# fmtk harness (issue #2881, scripts/fmtk-up.sh).
#
# The frontend is same-origin only: `baseUrl` derives from the page origin
# (klangk-plugin-api backend_url), so the debug app served by the flutter
# dev server (127.0.0.1:8125) would call 127.0.0.1:8125/api/v1/... and get
# 404s. fmtk-up.sh fronts the debug run with an origin-splitting caddy on
# 127.0.0.1:8124 (API + /ws -> klangkd, everything else -> the dev server).
# This wrapper rewrites the URL flutter tells Chrome to open from the dev
# server origin to the proxy origin, so the app loads through the proxy and
# its same-origin API calls reach the backend.
#
# Do NOT add --remote-debugging-port here: flutter passes its own (it wins,
# last flag takes precedence) and fmtk-up.sh discovers that port from the
# running Chrome's command line instead.
set -euo pipefail

chrome_bin="${FMTK_CHROME:-}"
if [[ -z $chrome_bin ]]; then
  for candidate in google-chrome chromium google-chrome-stable chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      chrome_bin="$candidate"
      break
    fi
  done
fi
if [[ -z $chrome_bin ]]; then
  echo "fmtk-chrome.sh: no Chrome/Chromium found on PATH; set FMTK_CHROME" >&2
  exit 1
fi

args=()
for arg in "$@"; do
  case "$arg" in
  http://127.0.0.1:8125* | http://localhost:8125*) arg="http://127.0.0.1:8124/#/" ;;
  esac
  args+=("$arg")
done

exec "$chrome_bin" --no-first-run --no-default-browser-check "${args[@]}"
