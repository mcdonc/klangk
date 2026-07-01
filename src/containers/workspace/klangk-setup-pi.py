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
import sys
import traceback
from pathlib import Path

IMAGE_DIR = Path("/opt/klangk/pi-agent")
AGENT_CONTEXT_SRC = Path("/opt/klangk/agent-context.md")
ERROR_LOG = Path("/tmp/klangk-setup-pi-errors.log")


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
    # Point at image dirs directly — Pi also auto-discovers
    # ~/.pi/agent/{extensions,skills,prompts}/ for user-installed ones.
    image_settings["extensions"] = [str(IMAGE_DIR / "extensions")]
    image_settings["skills"] = [str(IMAGE_DIR / "skills")]
    image_settings["prompts"] = [str(IMAGE_DIR / "prompts")]

    settings_path.write_text(json.dumps(image_settings, indent=2))


def ensure_settings_keys():
    """Backfill new settings keys into an existing settings.json."""
    agent = _agent_dir()
    settings_path = agent / "settings.json"
    if not settings_path.exists():
        return
    settings = json.loads(settings_path.read_text())
    defaults = {
        "extensions": [str(IMAGE_DIR / "extensions")],
        "skills": [str(IMAGE_DIR / "skills")],
        "prompts": [str(IMAGE_DIR / "prompts")],
    }
    changed = False
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = value
            changed = True
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2))


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


def build_agent_context():
    """Write AGENTS.md to ~/.pi/agent/ (Pi's global context-file slot).

    Pi loads ``~/.pi/agent/AGENTS.md`` as a *context file* on every run,
    regardless of cwd (its always-loaded global slot). The prior behavior wrote
    ``$HOME/AGENTS.md`` (home root) to interop with Claude, which is no longer
    shipped in the base image; Pi reads the global context file from
    ``~/.pi/agent/`` instead. This serves both the chat agent (``pi --mode rpc``)
    and any human running ``pi`` directly.
    """
    agent = _agent_dir()
    agent.mkdir(parents=True, exist_ok=True)
    prompt_path = agent / "AGENTS.md"
    if not prompt_path.exists() and AGENT_CONTEXT_SRC.is_file():
        prompt_path.write_text(AGENT_CONTEXT_SRC.read_text())


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

    force = "--force" in sys.argv

    agent = _agent_dir()
    if force:
        (agent / "settings.json").unlink(missing_ok=True)

    if (agent / "settings.json").exists():
        # Already initialized — refresh models.json (token may have
        # changed on container restart) and backfill any new keys.
        _run_step("ensure_settings_keys", ensure_settings_keys)
        _run_step("write_models", write_models)
        return

    ERROR_LOG.unlink(missing_ok=True)

    _run_step("setup_dirs", setup_dirs)
    _run_step("sync_image_packages", sync_image_packages)
    _run_step("write_settings", write_settings)
    _run_step("write_models", write_models)
    _run_step("build_agent_context", build_agent_context)

    if ERROR_LOG.exists():
        print(f"klangk-setup-pi: some steps failed, see {ERROR_LOG}")


if __name__ == "__main__":
    main()
