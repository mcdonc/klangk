#!/usr/bin/env bash
# Configure git to use the klangk credential helper system-wide.
# Runs at image build time (Dockerfile RUN).
set -e
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$PLUGIN_DIR/tools/git-credential-klangk" /usr/local/bin/git-credential-klangk
git config --system credential.helper klangk
