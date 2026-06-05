#!/bin/bash
# Source this to add OTEL env vars for Logfire tracing to the current shell.
#
# Usage (inside a container):
#   . /opt/klangk/otel.sh
#
# Requires LOGFIRE_TOKEN to be set in the container environment
# (via workspace --env LOGFIRE_TOKEN=...).

if [ -z "$LOGFIRE_TOKEN" ]; then
  echo "Error: LOGFIRE_TOKEN is not set" >&2
else
  export OTEL_EXPORTER_OTLP_ENDPOINT="${LOGFIRE_BASE_URL:-https://logfire-api.pydantic.dev}"
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $LOGFIRE_TOKEN"
  export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-klangk-pi-agent}"

  if [ -n "$LOGFIRE_ENVIRONMENT" ]; then
    export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=$LOGFIRE_ENVIRONMENT"
  fi
fi
