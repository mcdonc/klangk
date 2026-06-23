#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/haiku-rag/repo"

# Clone the repo via SSH if not already present.
if [ ! -d "$REPO_DIR" ]; then
  echo "Cloning ggozad/haiku.rag..."
  git clone git@github.com:ggozad/haiku.rag.git "$REPO_DIR"
else
  echo "Repo already cloned, skipping."
fi

# Create virtualenv and install the package.
if [ ! -d "$REPO_DIR/.venv" ]; then
  echo "Creating virtualenv..."
  uv venv "$REPO_DIR/.venv"
fi

echo "Installing haiku.rag..."
uv pip install --python "$REPO_DIR/.venv/bin/python" -e "$REPO_DIR"

echo "Setup complete."
