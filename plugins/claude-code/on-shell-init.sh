#!/bin/bash
# Symlink enabled skills into Claude Code's discovery path.
# Skills are shared with Pi — both read from /opt/klangk/pi-agent/skills/.

if [ -z "$KLANGK_SKILLS" ] || [ ! -d /opt/klangk/pi-agent/skills ]; then
  exit 0
fi

CC_SKILLS_DIR="$HOME/.claude/skills"
mkdir -p "$CC_SKILLS_DIR"

IFS=',' read -ra SKILLS <<<"$KLANGK_SKILLS"
for skill in "${SKILLS[@]}"; do
  skill=$(echo "$skill" | xargs) # trim whitespace
  src="/opt/klangk/pi-agent/skills/$skill"
  dst="$CC_SKILLS_DIR/$skill"
  if [ -n "$skill" ] && [ -d "$src" ] && [ ! -e "$dst" ]; then
    ln -s "$src" "$dst"
  fi
done
