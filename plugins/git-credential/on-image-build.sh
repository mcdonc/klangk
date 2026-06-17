#!/usr/bin/env bash
# Configure git to use the klangk credential helper system-wide.
# Runs at image build time (Dockerfile RUN).
set -e
git config --system credential.helper klangk
