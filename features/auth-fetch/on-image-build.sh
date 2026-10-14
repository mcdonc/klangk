#!/usr/bin/env bash
# Symlink the auth-fetch CLI into PATH at image build time (Dockerfile RUN).
set -e
FEATURE_DIR="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$FEATURE_DIR/tools/klangk-auth-fetch" /usr/local/bin/klangk-auth-fetch
