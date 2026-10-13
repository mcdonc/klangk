#!/usr/bin/env bash
# Expose the klangk-jwt helper on PATH.
# Runs at image build time (Dockerfile RUN).
set -e
FEATURE_DIR="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$FEATURE_DIR/tools/klangk-jwt" /usr/local/bin/klangk-jwt
