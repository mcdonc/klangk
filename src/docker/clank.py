#!/usr/bin/env python3
"""Launch Pi. Takes args that Pi would take.

Agent config (extensions, settings, models, system prompt) is set up
by setup_pi.py at container init time via entrypoint.sh. This script
just builds the Pi command line and execs it.
"""

import os
import sys
from pathlib import Path

AGENT_DIR = Path.home() / ".pi" / "agent"
SKILLS_DIR = Path("/opt/klangk/skills")


def build_pi_args():
    """Build the Pi command line arguments."""
    args = [
        "pi",
        "--append-system-prompt",
        str(AGENT_DIR / "system-prompt.md"),
    ]

    # Skills from KLANGK_SKILLS env var
    skills = os.environ.get("KLANGK_SKILLS", "")
    if skills and SKILLS_DIR.is_dir():
        for name in skills.split(","):
            name = name.strip()
            if name and (SKILLS_DIR / name).is_dir():
                args.extend(["--skill", str(SKILLS_DIR / name)])

    # Pass through any extra arguments from the command line
    args.extend(sys.argv[1:])

    return args


def main():
    os.environ["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)
    args = build_pi_args()
    os.execvp("pi", args)


if __name__ == "__main__":
    main()
