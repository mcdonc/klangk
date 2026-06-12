#!/usr/bin/env python3
"""Set up per-user Pi agent config from shared defaults.

Called from bash.bashrc on first shell for each user.  Creates
$HOME/.pi/agent/ with symlinks to shared resources and copies of
files the user may customize.
"""

import json
import os
from pathlib import Path

SHARED_PI = Path("/home/.pi/agent")


def main():
    home = Path(os.environ.get("HOME", ""))
    if not home or home == Path("/home"):
        return  # don't init for the system user

    user_agent = home / ".pi" / "agent"
    if (user_agent / "settings.json").exists():
        return  # already initialized

    if not SHARED_PI.is_dir():
        return  # shared config not ready

    user_agent.mkdir(parents=True, exist_ok=True)

    # Symlink shared directories
    for d in ("extensions", "npm", "git", "bin", "skills"):
        shared = SHARED_PI / d
        local = user_agent / d
        if shared.is_dir() and not local.exists():
            local.symlink_to(shared)

    # Copy per-user files (contain tokens or user may customize)
    for f in ("models.json", "keybindings.json"):
        shared = SHARED_PI / f
        local = user_agent / f
        if shared.is_file() and not local.exists():
            local.write_text(shared.read_text())

    # Build settings.json with absolute paths to shared resources
    shared_settings_path = SHARED_PI / "settings.json"
    if shared_settings_path.is_file():
        shared = json.loads(shared_settings_path.read_text())
        user_settings = {
            "defaultProvider": shared.get("defaultProvider", ""),
            "defaultModel": shared.get("defaultModel", ""),
            "packages": shared.get("packages", []),
            "extensions": [str(SHARED_PI / "extensions")],
        }
        skills_dir = SHARED_PI / "skills"
        if skills_dir.is_dir():
            user_settings["skills"] = [str(skills_dir)]
        (user_agent / "settings.json").write_text(json.dumps(user_settings, indent=2))

    # Symlink shared AGENTS.md
    shared_agents = Path("/home/AGENTS.md")
    user_agents = home / "AGENTS.md"
    if shared_agents.is_file() and not user_agents.exists():
        user_agents.symlink_to(shared_agents)


if __name__ == "__main__":
    main()
