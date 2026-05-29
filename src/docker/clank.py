#!/usr/bin/env python3
"""Launch Pi with session resume. Intended as a workspace default command.

Sets up Pi agent config, merges settings.json (preserving user-installed
packages), builds system prompt, and execs Pi with appropriate flags.
"""

import glob
import json
import os
import re
import subprocess
from pathlib import Path

IMAGE_DIR = Path("/opt/klangk/pi-agent")
AGENT_DIR = Path.home() / ".pi" / "agent"
SESSION_DIR = Path.home() / ".pi" / "sessions"
SKILLS_DIR = Path("/opt/klangk/skills")
SYSTEM_PROMPT_SRC = Path("/opt/klangk/system-prompt.md")
SIDECAR = AGENT_DIR / ".image-packages"


def setup_dirs():
    """Create agent directories and clean up stale symlinks."""
    for name in ("extensions", "npm"):
        p = AGENT_DIR / name
        if p.is_symlink():
            p.unlink()
    (AGENT_DIR / "bin").mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "npm").mkdir(parents=True, exist_ok=True)


def sync_npm():
    """Rsync image npm packages into the writable agent dir."""
    src = IMAGE_DIR / "npm"
    if src.is_dir():
        subprocess.run(
            ["rsync", "-a", f"{src}/", f"{AGENT_DIR / 'npm'}/"],
            check=True,
        )


def setup_bin():
    """Symlink system fd/rg into Pi's bin dir."""
    for tool in ("fd", "rg"):
        link = AGENT_DIR / "bin" / tool
        target = Path(f"/usr/bin/{tool}")
        if target.exists():
            link.unlink(missing_ok=True)
            link.symlink_to(target)


def write_models_json():
    """Write models.json with proxy URL (no real API key)."""
    proxy_url = os.environ.get("KLANGK_LLM_PROXY_URL", "")
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    models = {
        "providers": {
            "llm-proxy": {
                "baseUrl": proxy_url,
                "api": "openai-completions",
                "apiKey": "proxy",
                "models": [{"id": model}],
            }
        }
    }
    (AGENT_DIR / "models.json").write_text(json.dumps(models, indent=2))


def merge_settings():
    """Merge image settings.json with user settings, preserving user packages.

    Image-managed package names are tracked in a sidecar file. On each start:
    - Packages in the old sidecar but not in the current image are removed
    - Current image packages are added/updated
    - User-installed packages (never in any sidecar) are preserved
    """
    image_settings = json.loads((IMAGE_DIR / "settings.json").read_text())
    image_pkgs = image_settings.get("packages", [])
    image_pkg_names = {p["name"] for p in image_pkgs if "name" in p}

    # Read previous sidecar (what the image managed last time)
    old_image_names = set()
    if SIDECAR.exists():
        old_image_names = {
            n.strip() for n in SIDECAR.read_text().splitlines() if n.strip()
        }

    user_settings_path = AGENT_DIR / "settings.json"
    if user_settings_path.exists():
        settings = json.loads(user_settings_path.read_text())
        existing_pkgs = settings.get("packages", [])

        # Remove packages that were image-managed but are no longer in image
        dropped = old_image_names - image_pkg_names
        existing_pkgs = [p for p in existing_pkgs if p.get("name") not in dropped]

        # Remove existing image packages (will be re-added from current image)
        existing_pkgs = [
            p for p in existing_pkgs if p.get("name") not in image_pkg_names
        ]

        # Add current image packages
        settings["packages"] = existing_pkgs + image_pkgs
    else:
        settings = image_settings

    # Set LLM config
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    settings["defaultProvider"] = "llm-proxy"
    settings["defaultModel"] = model

    user_settings_path.write_text(json.dumps(settings, indent=2))

    # Write sidecar
    SIDECAR.write_text("\n".join(sorted(image_pkg_names)) + "\n")


def build_system_prompt():
    """Build system prompt from template + image extension tool descriptions."""
    prompt = SYSTEM_PROMPT_SRC.read_text()

    ext_dir = IMAGE_DIR / "extensions"
    tools = []
    if ext_dir.is_dir():
        for ext in sorted(ext_dir.glob("*.ts")):
            text = ext.read_text()
            name_m = re.search(r'^\s+name:\s*"([^"]+)"', text, re.MULTILINE)
            desc_m = re.search(r'^\s+description:\s*"([^"]+)"', text, re.MULTILINE)
            if name_m and desc_m:
                tools.append((name_m.group(1), desc_m.group(1)))

    if tools:
        prompt += "\n\nRegistered extension tools (use these instead of bash when appropriate):\n"
        for name, desc in tools:
            prompt += f"- `{name}`: {desc}\n"

    prompt_path = AGENT_DIR / "system-prompt.md"
    prompt_path.write_text(prompt)
    return prompt_path


def build_pi_args(system_prompt_path):
    """Build the Pi command line arguments."""
    args = [
        "pi",
        "--no-context-files",
        "--session-dir",
        str(SESSION_DIR),
        "--append-system-prompt",
        str(system_prompt_path),
    ]

    # Image-provided .ts extensions via --extension flags
    ext_dir = IMAGE_DIR / "extensions"
    if ext_dir.is_dir():
        for ext in sorted(ext_dir.glob("*.ts")):
            args.extend(["--extension", str(ext)])

    # Skills from KLANGK_SKILLS env var
    skills = os.environ.get("KLANGK_SKILLS", "")
    if skills and SKILLS_DIR.is_dir():
        for name in skills.split(","):
            name = name.strip()
            if name and (SKILLS_DIR / name).is_dir():
                args.extend(["--skill", str(SKILLS_DIR / name)])

    # Resume most recent session
    sessions = sorted(glob.glob(str(SESSION_DIR / "*.jsonl")))
    if sessions:
        args.extend(["--session", sessions[-1]])

    return args


def main():
    # Git safe directory
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "/home/klangk/work"],
        capture_output=True,
    )

    os.environ["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)

    setup_dirs()
    sync_npm()
    setup_bin()
    write_models_json()
    merge_settings()
    prompt_path = build_system_prompt()
    args = build_pi_args(prompt_path)

    os.execvp("pi", args)


if __name__ == "__main__":
    main()
