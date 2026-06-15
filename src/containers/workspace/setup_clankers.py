#!/usr/bin/env python3
"""Set up per-user Pi agent config at login time.

Called from bash.bashrc on each shell.  Creates $HOME/.pi/agent/ with
all the files Pi needs: settings.json, models.json, keybindings.json,
extensions (rsynced from image), npm/git packages, bin tools, skills,
AGENTS.md, and Claude Code skills.

Skips setup if $HOME/.pi/agent/settings.json already exists.
"""

import json
import os
import subprocess
import traceback
from pathlib import Path

IMAGE_DIR = Path("/opt/klangk/pi-agent")
SKILLS_DIR = IMAGE_DIR / "skills"
SYSTEM_PROMPT_SRC = Path("/opt/klangk/system-prompt.md")
ERROR_LOG = Path("/tmp/setup_clankers_errors.log")


def _agent_dir():
    return Path(os.environ.get("HOME", "")) / ".pi" / "agent"


def setup_dirs():
    """Create agent directories."""
    agent = _agent_dir()
    for d in ("bin", "npm", "git", "extensions", "skills"):
        (agent / d).mkdir(parents=True, exist_ok=True)


def sync_image_packages():
    """Rsync image npm and git packages into the user's agent dir.

    Pi resolves these by path (npm/node_modules/<pkg>).  Extensions
    and skills are NOT copied — settings.json points directly at the
    image dirs for those.
    """
    agent = _agent_dir()
    for subdir in ("npm", "git"):
        src = IMAGE_DIR / subdir
        if src.is_dir():
            subprocess.run(
                ["rsync", "-a", f"{src}/", f"{agent / subdir}/"],
                check=True,
            )


def setup_bin():
    """Symlink system fd/rg into Pi's bin dir."""
    agent = _agent_dir()
    for tool in ("fd", "rg"):
        link = agent / "bin" / tool
        target = Path(f"/usr/bin/{tool}")
        if target.exists() and not link.exists():
            link.symlink_to(target)


def write_settings():
    """Write settings.json with image packages and LLM config."""
    agent = _agent_dir()
    settings_path = agent / "settings.json"
    if settings_path.exists():
        return  # don't overwrite user's settings

    image_settings = json.loads((IMAGE_DIR / "settings.json").read_text())
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    image_settings["defaultProvider"] = "llm-proxy"
    image_settings["defaultModel"] = model
    # Point at image extensions dir directly — Pi also auto-discovers
    # ~/.pi/agent/extensions/ for user-installed extensions.
    image_settings["extensions"] = [str(IMAGE_DIR / "extensions")]
    if SKILLS_DIR.is_dir():
        image_settings["skills"] = [str(SKILLS_DIR)]

    settings_path.write_text(json.dumps(image_settings, indent=2))


def write_models():
    """Write models.json with the llm-proxy provider."""
    agent = _agent_dir()
    proxy_url = os.environ.get("KLANGK_LLM_PROXY_URL", "")
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    workspace_token = os.environ.get("KLANGK_WORKSPACE_TOKEN", "proxy")

    models = {
        "providers": {
            "llm-proxy": {
                "baseUrl": proxy_url,
                "api": "openai-completions",
                "apiKey": workspace_token,
                "models": (
                    [{"id": model, "input": ["text", "image"]}] if model else []
                ),
            }
        }
    }
    (agent / "models.json").write_text(json.dumps(models, indent=2))


def write_keybindings():
    """Write default keybindings.json if not present."""
    agent = _agent_dir()
    kb_path = agent / "keybindings.json"
    if kb_path.exists():
        return  # don't overwrite user's keybindings

    keybindings = {
        "tui.editor.cursorLeft": ["left"],
        "tui.editor.cursorRight": ["right"],
    }
    kb_path.write_text(json.dumps(keybindings, indent=2))


def build_system_prompt():
    """Write AGENTS.md to $HOME if not present."""
    home = Path(os.environ.get("HOME", ""))
    prompt_path = home / "AGENTS.md"
    if not prompt_path.exists() and SYSTEM_PROMPT_SRC.is_file():
        prompt_path.write_text(SYSTEM_PROMPT_SRC.read_text())


def setup_pi_skills():
    """Symlink enabled skill dirs into Pi's skills dir."""
    skills_env = os.environ.get("KLANGK_SKILLS", "")
    if not skills_env or not SKILLS_DIR.is_dir():
        return

    agent = _agent_dir()
    pi_skills_dir = agent / "skills"

    for name in skills_env.split(","):
        name = name.strip()
        link = pi_skills_dir / name
        if name and (SKILLS_DIR / name).is_dir() and not link.exists():
            link.symlink_to(SKILLS_DIR / name)


def setup_claude_code_skills():
    """Symlink enabled skill dirs into Claude Code's discovery path."""
    skills_env = os.environ.get("KLANGK_SKILLS", "")
    if not skills_env or not SKILLS_DIR.is_dir():
        return

    home = Path(os.environ.get("HOME", ""))
    cc_skills_dir = home / ".claude" / "skills"
    cc_skills_dir.mkdir(parents=True, exist_ok=True)

    for name in skills_env.split(","):
        name = name.strip()
        link = cc_skills_dir / name
        if name and (SKILLS_DIR / name).is_dir() and not link.exists():
            link.symlink_to(SKILLS_DIR / name)


def _run_step(name, fn):
    """Run a setup step, logging errors to a tempfile and continuing."""
    try:
        fn()
    except Exception:
        with open(ERROR_LOG, "a") as f:
            f.write(f"=== {name} failed ===\n")
            traceback.print_exc(file=f)
            f.write("\n")


def main():
    home = Path(os.environ.get("HOME", ""))
    if not home or home == Path("/home"):
        return  # don't init for the system user

    agent = _agent_dir()
    if (agent / "settings.json").exists():
        # Already initialized — just refresh models.json (token may
        # have changed on container restart).
        _run_step("write_models", write_models)
        return

    ERROR_LOG.unlink(missing_ok=True)

    _run_step("setup_dirs", setup_dirs)
    _run_step("sync_image_packages", sync_image_packages)
    _run_step("setup_bin", setup_bin)
    _run_step("write_settings", write_settings)
    _run_step("write_models", write_models)
    _run_step("write_keybindings", write_keybindings)
    _run_step("build_system_prompt", build_system_prompt)
    _run_step("setup_pi_skills", setup_pi_skills)
    _run_step("setup_claude_code_skills", setup_claude_code_skills)

    if ERROR_LOG.exists():
        print(f"setup_clankers: some steps failed, see {ERROR_LOG}")


if __name__ == "__main__":
    main()
