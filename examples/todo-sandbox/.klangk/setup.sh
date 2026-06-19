#!/bin/bash
set -euo pipefail

echo "=== sandbox setup ==="
echo "  cwd: $(pwd)"
echo "  HOME: $HOME"
echo "  user: $(whoami)"

# Source env if available
if [ -f ~/.env ]; then
  echo "  .env: found, sourcing"
  # shellcheck source=/dev/null
  . ~/.env
else
  echo "  .env: not found"
fi

# Verify mounts
[ -d ~/.ssh ] && echo "  ~/.ssh: mounted" || echo "  ~/.ssh: not found"
[ -f ~/.gitconfig ] && echo "  ~/.gitconfig: copied" || echo "  ~/.gitconfig: not found"

# Test the todo app
echo ""
echo "Testing todo app..."
python3 todo.py add "Set up the sandbox"
python3 todo.py add "Run some tests"
python3 todo.py ls
python3 todo.py "done" 1
python3 todo.py ls
echo ""
echo "=== setup complete ==="
