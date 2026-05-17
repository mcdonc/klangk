#!/bin/bash
set -e

DEST="arctor.repoze.org:bark"

echo "Syncing Bark to $DEST..."

rsync -avz --delete \
  --exclude='.devenv/' \
  --exclude='.env' \
  --exclude='*.db' \
  --exclude='*.db-journal' \
  --exclude='*.db-wal' \
  --exclude='__pycache__/' \
  --exclude='.dart_tool/' \
  --exclude='.packages' \
  --exclude='.flutter-plugins' \
  --exclude='.flutter-plugins-dependencies' \
  --exclude='src/frontend/build/web/flutter_service_worker.js' \
  --exclude='.git/' \
  --exclude='.claude/' \
  --exclude='.venv/' \
  --exclude='devenv.lock' \
  --exclude='.devenv.flake.nix' \
  --exclude='pubspec.lock' \
  --exclude='uv.lock' \
  --exclude='.bark/' \
  --exclude='docs/screenshot.png' \
  ./ "$DEST/"

echo "Done. On arctor, run:"
echo "  cd ~/bark && devenv shell -- rebuild && devenv processes restart"
