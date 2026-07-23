#!/bin/sh
# klangk-hosted-url — print the hosted-app browser URL for a container port.
#
# Single source of truth for hosted-URL construction inside workspace
# containers. The Pi `get_hosted_url` tool (builtin-extensions/port-map.ts)
# delegates to this script so the shell and the agent share one implementation.
#
# Reads KLANGKWS_PORT_MAPPINGS ("8000:9000,8001:9001,...") to resolve the host
# port for the given container port, then combines it with the
# KLANGKWS_HOSTING_* / KLANGKWS_WORKSPACE_ID env vars (which the backend injects
# when the container starts) to print:
#
#   {proto}://{hostname}{base_path}/hosted/{workspace_id}/{host_port}/
#
# Exit codes:
#   0  success (URL on stdout)
#   1  bad args, port not mapped, or missing KLANGKWS_PORT_MAPPINGS
set -eu

usage() {
  echo "Usage: klangk-hosted-url <container_port>" >&2
  echo "Print the browser URL for a web app running on a container port." >&2
}

if [ $# -ne 1 ]; then
  usage
  exit 1
fi

container_port=$1

mappings=${KLANGKWS_PORT_MAPPINGS:-}
if [ -z "$mappings" ]; then
  echo "klangk-hosted-url: KLANGKWS_PORT_MAPPINGS is not set" >&2
  echo "(this script must run inside a Klangk workspace container)" >&2
  exit 1
fi

# Walk the CSV mapping, recording the matching host port (if any) and the
# full list of valid container ports for the error case. POSIX-sh parsing:
# peel off one "container:host" pair at a time, then split it on ":".
host_port=""
valid_ports=""
rest=$mappings
while [ -n "$rest" ]; do
  pair=${rest%%,*}
  case "$rest" in
  *,*) rest=${rest#*,} ;;
  *) rest= ;;
  esac
  c=${pair%%:*}
  h=${pair##*:}
  [ "$c" = "$container_port" ] && host_port=$h
  if [ -z "$valid_ports" ]; then
    valid_ports=$c
  else
    valid_ports="$valid_ports, $c"
  fi
done

if [ -z "$host_port" ]; then
  echo "klangk-hosted-url: container port $container_port is not in KLANGKWS_PORT_MAPPINGS" >&2
  echo "Mapped container ports: $valid_ports" >&2
  exit 1
fi

proto=${KLANGKWS_HOSTING_PROTO:-http}
hostname=${KLANGKWS_HOSTING_HOSTNAME:-localhost}
base_path=${KLANGKWS_HOSTING_BASE_PATH:-}
workspace_id=${KLANGKWS_WORKSPACE_ID:-}

printf '%s://%s%s/hosted/%s/%s/\n' \
  "$proto" "$hostname" "$base_path" "$workspace_id" "$host_port"
