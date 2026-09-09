#!/usr/bin/env bash
# Real Gitea instance for E2E testing (#3385).
#
#   devenv --quiet shell -- gitea-e2e up      # run the instance (foreground)
#   devenv --quiet shell -- gitea-e2e ready   # block until the API answers
#   devenv --quiet shell -- gitea-e2e seed    # idempotent fixture seed
#   devenv --quiet shell -- gitea-e2e info    # URL, admin creds, OAuth client
#   devenv --quiet shell -- gitea-e2e stop    # stop a running instance
#   devenv --quiet shell -- gitea-e2e reset   # stop + wipe all state
#
# Boots the nixpkgs Gitea binary (a real instance, not a mock) on
# 127.0.0.1:8993 with sqlite storage under $DEVENV_STATE/gitea — a fresh
# database, so it never touches the dev stack's data or ports
# (8997/8995), nor the fmtk scratch stack (8998/8996/8124/8125; the
# OAuth redirect defaults to the fmtk proxy origin because that is where
# the SPA callback lives). The seed creates:
#
#   - admin user gitea-admin / gitea-e2e-admin (registration is closed;
#     every fixture account comes from the seed)
#   - an API token (scopes: all), reused across seeds, kept in token.txt
#   - the public repo gitea-admin/e2e-repo (auto-initialized)
#   - the OAuth2 application "klangk-e2e" — a PUBLIC client, so PKCE
#     works (confidential clients fail the PKCE challenge on Gitea >=
#     1.22, go-gitea/gitea#33956). Its redirect URI defaults to
#     http://127.0.0.1:8124/oauth/callback and is overridable per seed:
#         gitea-e2e seed --redirect-uri https://my-klangk/oauth/callback
#
# Overridable: GITEA_E2E_PORT (8993), GITEA_E2E_STATE.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVENV_ROOT="${DEVENV_ROOT:-$REPO_ROOT}"
DEVENV_STATE="${DEVENV_STATE:-$DEVENV_ROOT/.devenv/state}"
GITEA_PORT="${GITEA_E2E_PORT:-8993}"
STATE_DIR="${GITEA_E2E_STATE:-$DEVENV_STATE/gitea}"
APP_INI="$STATE_DIR/app.ini"
BASE_URL="http://127.0.0.1:$GITEA_PORT"
# The default OAuth redirect: the fmtk origin-splitting proxy, because
# the frontend is same-origin only and that is the origin the SPA
# callback route will live on in the browser-relayed flow (#3385).
DEFAULT_REDIRECT_URI="http://127.0.0.1:8124/"
READY_TIMEOUT="${GITEA_E2E_READY_TIMEOUT:-60}"

log() { printf '\033[1m[gitea-e2e]\033[0m %s\n' "$*"; }
die() {
  log "ERROR: $*"
  exit 1
}

for tool in gitea git curl python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not on PATH (run inside devenv shell)"
done

write_app_ini() {
  mkdir -p "$STATE_DIR" "$STATE_DIR/home"
  [ -f "$APP_INI" ] && return 0
  cat >"$APP_INI" <<EOF
APP_NAME = klangk E2E Gitea
RUN_MODE = prod

[server]
PROTOCOL = http
HTTP_ADDR = 127.0.0.1
HTTP_PORT = $GITEA_PORT
DOMAIN = 127.0.0.1
ROOT_URL = $BASE_URL/
DISABLE_SSH = true
OFFLINE_MODE = true
LFS_START_SERVER = false

[security]
INSTALL_LOCK = true

[database]
DB_TYPE = sqlite3
PATH = $STATE_DIR/gitea.db

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = false
DEFAULT_KEEP_EMAIL_PRIVATE = true

[actions]
ENABLED = false

[mailer]
ENABLED = false

[indexer]
REPO_INDEXER_ENABLED = false

[log]
MODE = console
LEVEL = warn

[other]
SHOW_FOOTER_VERSION = false
EOF
  chmod 600 "$APP_INI"
}

# Run a gitea CLI subcommand against this instance's config. HOME points
# at a state-local dir so git never touches (or needs) the user's.
gitea_cli() {
  HOME="$STATE_DIR/home" gitea --config "$APP_INI" --work-path "$STATE_DIR" "$@"
}

cmd_up() {
  write_app_ini
  log "starting Gitea on $BASE_URL (state: $STATE_DIR)"
  log "first boot writes SECRET_KEY/INTERNAL_TOKEN into app.ini and may take a few seconds"
  # exec (not the gitea_cli wrapper — exec needs a binary) so gitea is
  # the foreground process and takes signals directly.
  export HOME="$STATE_DIR/home"
  exec gitea --config "$APP_INI" --work-path "$STATE_DIR" web
}

wait_ready() {
  local deadline=$((SECONDS + READY_TIMEOUT))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS -o /dev/null --max-time 2 "$BASE_URL/api/v1/version" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cmd_ready() {
  log "waiting up to ${READY_TIMEOUT}s for $BASE_URL/api/v1/version"
  if wait_ready; then
    log "ready"
  else
    die "Gitea did not become ready in ${READY_TIMEOUT}s (is it running?)"
  fi
}

cmd_seed() {
  write_app_ini
  local redirect="$DEFAULT_REDIRECT_URI"
  if [ "${1:-}" = "--redirect-uri" ] && [ -n "${2:-}" ]; then
    redirect="$2"
  elif [ -n "${1:-}" ]; then
    die "unknown seed flag: $1 (only --redirect-uri URI)"
  fi
  cmd_ready
  exec python3 "$REPO_ROOT/scripts/gitea-e2e-seed.py" \
    --state "$STATE_DIR" --port "$GITEA_PORT" --redirect-uri "$redirect"
}

cmd_info() {
  cat "$STATE_DIR/credentials" 2>/dev/null ||
    die "not seeded yet (run: gitea-e2e seed)"
}

cmd_stop() {
  # Match only our instance: the config path is unique to this state dir.
  if pkill -f "gitea --config $APP_INI"; then
    log "stopped"
  else
    log "no running instance found"
  fi
}

cmd_reset() {
  cmd_stop
  # Gitea shuts down asynchronously after TERM; wait it out so a dying
  # process cannot recreate state files under the rm -rf below.
  for _ in $(seq 1 50); do
    pgrep -f "gitea --config $APP_INI" >/dev/null || break
    sleep 0.2
  done
  rm -rf "$STATE_DIR"
  log "wiped $STATE_DIR"
}

case "${1:-}" in
up)
  shift
  cmd_up "$@"
  ;;
ready)
  shift
  cmd_ready "$@"
  ;;
seed)
  shift
  cmd_seed "$@"
  ;;
info)
  shift
  cmd_info "$@"
  ;;
stop)
  shift
  cmd_stop "$@"
  ;;
reset)
  shift
  cmd_reset "$@"
  ;;
*) die "usage: gitea-e2e {up|ready|seed [--redirect-uri URI]|info|stop|reset}" ;;
esac
