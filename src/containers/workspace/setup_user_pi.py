#!/usr/bin/env python3
"""Set up per-user Pi agent config from the skel.

Called from bash.bashrc on each shell.  Symlinks most of the Pi agent
skel (built by setup_clankers at container start) into the user's home
directory.  Writable directories (bin, extensions, git, npm) are created
as real dirs so per-user installs don't affect other users.
"""

import os
import shutil
from pathlib import Path

SKEL_DIR = Path("/opt/klangk/pi-skel")

# Directories created as real (writable) dirs for user installs.
# Contents of skel dirs are symlinked into the user's copy so
# image-provided resources are available alongside user additions.
WRITABLE_DIRS_WITH_CHILDREN = {"bin"}

# Writable dirs that start empty — Pi finds shared content via
# the extensions/skills arrays in settings.json pointing at the skel.
WRITABLE_DIRS_EMPTY = {"extensions", "skills"}

# Files that must be copied (not symlinked) because Pi writes to them.
COPIED_FILES = {"settings.json"}

# Skel entries to skip — Pi manages these itself when the user installs
# packages, and image content is found via settings.json paths.
SKIP = {"git", "npm"}


def main():
    home = Path(os.environ.get("HOME", ""))
    if not home or home == Path("/home"):
        return  # don't init for the system user

    if not SKEL_DIR.is_dir():
        return  # skel not ready

    # Set up .pi/agent/ — symlink files, create writable dirs
    skel_agent = SKEL_DIR / ".pi" / "agent"
    user_agent = home / ".pi" / "agent"
    if skel_agent.is_dir() and not user_agent.exists():
        user_agent.mkdir(parents=True)
        for entry in skel_agent.iterdir():
            if entry.name in SKIP:
                continue
            target = user_agent / entry.name
            if entry.name in WRITABLE_DIRS_WITH_CHILDREN:
                # Real dir with symlinks to skel children (bin/fd, npm/node_modules)
                target.mkdir(exist_ok=True)
                if entry.is_dir():
                    for child in entry.iterdir():
                        child_target = target / child.name
                        if not child_target.exists():
                            child_target.symlink_to(child)
            elif entry.name in WRITABLE_DIRS_EMPTY:
                # Empty writable dir — Pi finds shared content via settings.json
                target.mkdir(exist_ok=True)
            elif entry.name in COPIED_FILES:
                # Copy files that Pi writes to (e.g. settings.json)
                shutil.copy2(entry, target)
            else:
                # Symlink everything else.  models.json in particular must
                # be a symlink so KLANGK_WORKSPACE_TOKEN refreshes on
                # container restart propagate to all users automatically.
                target.symlink_to(entry)

    # Symlink AGENTS.md if not present
    user_agents = home / "AGENTS.md"
    skel_agents = SKEL_DIR / "AGENTS.md"
    if not user_agents.exists() and skel_agents.is_file():
        user_agents.symlink_to(skel_agents)

    # Symlink .claude/ tree if not present (skills symlinks)
    user_claude = home / ".claude"
    skel_claude = SKEL_DIR / ".claude"
    if not user_claude.exists() and skel_claude.is_dir():
        shutil.copytree(skel_claude, user_claude, symlinks=True)


if __name__ == "__main__":
    main()
