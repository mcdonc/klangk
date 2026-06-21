#!/usr/bin/env python3
"""Set up per-user Pi agent config at login time.

Called from bash.bashrc on each shell.  Creates $HOME/.pi/agent/ with
all the files Pi needs: settings.json, models.json, npm packages
(rsynced from image), and AGENTS.md.

Skips setup if $HOME/.pi/agent/settings.json already exists.
"""

import json
import os
import subprocess
import traceback
from pathlib import Path

IMAGE_DIR = Path("/opt/klangk/pi-agent")
SYSTEM_PROMPT_SRC = Path("/opt/klangk/system-prompt.md")
ERROR_LOG = Path("/tmp/setup_clankers_errors.log")


def _agent_dir():
    return Path(os.environ.get("HOME", "")) / ".pi" / "agent"


def setup_dirs():
    """Create agent directories."""
    agent = _agent_dir()
    for d in ("npm", "extensions"):
        (agent / d).mkdir(parents=True, exist_ok=True)


def sync_image_packages():
    """Rsync image npm packages into the user's agent dir.

    Pi resolves these by path (npm/node_modules/<pkg>).  Extensions
    are NOT copied — settings.json points directly at the image dir.
    """
    agent = _agent_dir()
    src = IMAGE_DIR / "npm"
    if src.is_dir():
        subprocess.run(
            ["rsync", "-a", f"{src}/", f"{agent / 'npm'}/"],
            check=True,
        )


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
    # Disable thinking by default — Ctrl+T toggle doesn't work in web
    # terminals because the browser captures it.
    image_settings["defaultThinkingLevel"] = "off"
    # Point at image extensions dir directly — Pi also auto-discovers
    # ~/.pi/agent/extensions/ for user-installed extensions.
    image_settings["extensions"] = [str(IMAGE_DIR / "extensions")]

    settings_path.write_text(json.dumps(image_settings, indent=2))


def write_models():
    """Write models.json with the llm-proxy provider."""
    agent = _agent_dir()
    proxy_url = os.environ.get("KLANGK_LLM_PROXY_URL", "")
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    workspace_token = "!klangk-workspace-token"

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


def build_system_prompt():
    """Write AGENTS.md to $HOME if not present."""
    home = Path(os.environ.get("HOME", ""))
    prompt_path = home / "AGENTS.md"
    if not prompt_path.exists() and SYSTEM_PROMPT_SRC.is_file():
        prompt_path.write_text(SYSTEM_PROMPT_SRC.read_text())


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
    _run_step("write_settings", write_settings)
    _run_step("write_models", write_models)
    _run_step("build_system_prompt", build_system_prompt)

    if ERROR_LOG.exists():
        print(f"setup_clankers: some steps failed, see {ERROR_LOG}")


if __name__ == "__main__":
    main()
