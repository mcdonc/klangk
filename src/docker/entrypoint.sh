#!/bin/sh
# Minimal container entrypoint.
set -e

chown klangk:klangk /home/klangk /home/klangk/work

# Mark all directories as safe for git (bind mounts may have different ownership)
su -c "git config --global --add safe.directory '*'" klangk

# Allow klangk user to access the Docker socket (if mounted)
if [ -S /var/run/docker.sock ]; then
  chmod 666 /var/run/docker.sock
fi

# Wire up Claude Code skills — symlink enabled skill dirs into discovery path.
# KLANGK_SKILLS is a comma-separated list of skill directory names.
# Skills are expected at /opt/klangk/skills/<name>/ (user-mounted).
SKILLS_DIR="/opt/klangk/skills"
CC_SKILLS_DIR="/home/klangk/.claude/skills"
if [ -n "$KLANGK_SKILLS" ] && [ -d "$SKILLS_DIR" ]; then
  rm -rf "$CC_SKILLS_DIR"
  mkdir -p "$CC_SKILLS_DIR"
  echo "$KLANGK_SKILLS" | tr ',' '\n' | while read -r skill_name; do
    skill_name=$(echo "$skill_name" | tr -d ' ')
    [ -z "$skill_name" ] && continue
    if [ -d "$SKILLS_DIR/$skill_name" ]; then
      ln -sf "$SKILLS_DIR/$skill_name" "$CC_SKILLS_DIR/$skill_name"
    fi
  done
  chown -R klangk:klangk "$CC_SKILLS_DIR"
fi

# Signal that setup is complete. Terminal sessions (docker exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via docker exec.
exec sleep infinity
