#!/bin/sh
# Minimal container entrypoint.
#
# With --userns=keep-id:uid=1000,gid=1000 the host user maps to
# klangk (UID 1000) inside the container.  The entrypoint runs as
# klangk — no root privileges needed.
set -e

# Run feature on-entrypoint hooks (alphabetical by feature name).
# These run once per container start, as root (inside userns).
for f in /opt/klangk/features/*/on-entrypoint.sh; do
  [ -x "$f" ] || continue
  feature=$(basename "$(dirname "$f")")
  label=" $feature (on-entrypoint) "
  pad=$(((60 - ${#label}) / 2))
  line=$(printf '%*s' "$pad" '' | tr ' ' '-')
  printf '\033[33m%s%s%s\033[0m\n' "$line" "$label" "$line"
  "$f" || true
  printf '\033[33m%s\033[0m\n' "$(printf '%*s' 60 '' | tr ' ' '-')"
done

# Create the workspace token directory.
mkdir -p /tmp/klangk

# Build the CA bundle from mounted deployer certs (runtime trust
# injection, #1181). The backend mounts KLANGK_SSL_CERT_DIR read-only
# at /opt/klangk/ssl when it contains .pem/.crt CAs, and sets
# SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE /
# NODE_EXTRA_CA_CERTS to point at this bundle. Those vars REPLACE the
# default trust store, so the bundle must contain the system CAs too --
# a custom-only bundle would break public-internet TLS (npm/pip/git).
# Concatenate system bundle first, then custom certs, on the writable
# /tmp tmpfs (the entrypoint runs as non-root UID 1000). Built BEFORE
# the readiness sentinel so every later process -- shells, podman exec,
# the agent subprocess -- finds it present. POSIX-sh under `set -e`:
# use an explicit `if` so an unmatched glob doesn't abort the script.
if [ -d /opt/klangk/ssl ]; then
  bundle=/tmp/klangk/ca-bundle.crt
  : >"$bundle"
  # System CAs first (preserve public-internet trust).
  if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    cat /etc/ssl/certs/ca-certificates.crt >>"$bundle"
  fi
  for f in /opt/klangk/ssl/*.pem /opt/klangk/ssl/*.crt; do
    if [ -f "$f" ]; then
      cat "$f" >>"$bundle"
    fi
  done
  if [ ! -s "$bundle" ]; then
    rm -f "$bundle"
  fi
fi

# Signal that the entrypoint's one-time setup is done. The backend polls
# this sentinel (podman.wait_for_container_ready) before reporting the
# container as ready, so every consumer — terminals, exec, agent, health
# check — gets the guarantee regardless of shell. /tmp is a tmpfs, so the
# sentinel is cleared on every container start and recreated here.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
