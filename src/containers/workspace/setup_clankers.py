#!/usr/bin/env python3
"""Set up Pi agent config at container init time.

Called from entrypoint.sh. Syncs image extensions/npm, merges
settings.json (preserving user packages), writes models.json,
and builds the system prompt.
"""

import json
import os
import subprocess
import traceback
from pathlib import Path

IMAGE_DIR = Path("/opt/klangk/pi-agent")
AGENT_DIR = Path.home() / ".pi" / "agent"
SYSTEM_PROMPT_SRC = Path("/opt/klangk/system-prompt.md")
ERROR_LOG = Path("/tmp/setup_clankers_errors.log")


def setup_dirs():
    """Create agent directories."""
    (AGENT_DIR / "bin").mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "npm").mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "git").mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "extensions").mkdir(parents=True, exist_ok=True)


def sync_image_files():
    """Rsync image npm packages and extensions into the writable agent dir.

    Image-managed extensions are tracked via a sidecar so we can remove
    ones that were dropped from the image without touching user-installed files.
    """
    sidecar = AGENT_DIR / ".image-extensions"

    # Remove extensions that were image-managed but no longer in the image
    old_names = set()
    if sidecar.exists():
        old_names = {n.strip() for n in sidecar.read_text().splitlines() if n.strip()}

    current_names = set()
    ext_src = IMAGE_DIR / "extensions"
    if ext_src.is_dir():
        current_names = {f.name for f in ext_src.iterdir()}

    for dropped in old_names - current_names:
        target = AGENT_DIR / "extensions" / dropped
        if target.exists() or target.is_symlink():
            if target.is_dir():
                import shutil

                shutil.rmtree(target)
            else:
                target.unlink()

    # Rsync image files into writable dirs
    for subdir in ("npm", "extensions", "git"):
        src = IMAGE_DIR / subdir
        if src.is_dir():
            subprocess.run(
                ["rsync", "-a", f"{src}/", f"{AGENT_DIR / subdir}/"],
                check=True,
            )

    # Write sidecar with current image extension names
    sidecar.write_text("\n".join(sorted(current_names)) + "\n")


def setup_bin():
    """Symlink system fd/rg into Pi's bin dir."""
    for tool in ("fd", "rg"):
        link = AGENT_DIR / "bin" / tool
        target = Path(f"/usr/bin/{tool}")
        if target.exists() and not link.exists():
            link.symlink_to(target)


def merge_settings():
    """Merge image settings.json with user settings, preserving user packages.

    Image-managed package names are tracked in a sidecar file. On each start:
    - Packages in the old sidecar but not in the current image are removed
    - Current image packages are added/updated
    - User-installed packages (never in any sidecar) are preserved
    """

    def pkg_name(p):
        return p["name"] if isinstance(p, dict) else str(p)

    sidecar = AGENT_DIR / ".image-packages"
    image_settings = json.loads((IMAGE_DIR / "settings.json").read_text())
    image_pkgs = image_settings.get("packages", [])
    image_pkg_names = {pkg_name(p) for p in image_pkgs}

    old_image_names = set()
    if sidecar.exists():
        old_image_names = {
            n.strip() for n in sidecar.read_text().splitlines() if n.strip()
        }

    user_settings_path = AGENT_DIR / "settings.json"
    if user_settings_path.exists():
        settings = json.loads(user_settings_path.read_text())
        existing_pkgs = settings.get("packages", [])

        dropped = old_image_names - image_pkg_names
        existing_pkgs = [p for p in existing_pkgs if pkg_name(p) not in dropped]
        existing_pkgs = [p for p in existing_pkgs if pkg_name(p) not in image_pkg_names]

        settings["packages"] = existing_pkgs + image_pkgs
    else:
        settings = image_settings

    model = os.environ.get("KLANGK_LLM_MODEL", "")
    settings["defaultProvider"] = "llm-proxy"
    settings["defaultModel"] = model

    user_settings_path.write_text(json.dumps(settings, indent=2))
    sidecar.write_text("\n".join(sorted(image_pkg_names)) + "\n")


def merge_models_json():
    """Merge the llm-proxy provider into models.json without overwriting.

    Preserves any providers the user or Pi may have added.
    """
    proxy_url = os.environ.get("KLANGK_LLM_PROXY_URL", "")
    model = os.environ.get("KLANGK_LLM_MODEL", "")
    models_path = AGENT_DIR / "models.json"

    if models_path.exists():
        models = json.loads(models_path.read_text())
    else:
        models = {}

    providers = models.setdefault("providers", {})
    providers["llm-proxy"] = {
        "baseUrl": proxy_url,
        "api": "openai-completions",
        "apiKey": "proxy",
        "models": [{"id": model, "input": ["text", "image"]}],
    }

    models_path.write_text(json.dumps(models, indent=2))


def merge_keybindings():
    """Merge default keybindings into ~/.pi/agent/keybindings.json.

    Removes built-in cursor shortcuts from ctrl+b/ctrl+f/alt+f/ctrl+e
    so extensions can use those keys without conflicts. Existing user
    keybindings are preserved — only missing keys are added.
    """
    defaults = {
        "tui.editor.cursorLeft": ["left"],
        "tui.editor.cursorRight": ["right"],
    }

    kb_path = AGENT_DIR / "keybindings.json"
    if kb_path.exists():
        keybindings = json.loads(kb_path.read_text())
    else:
        keybindings = {}

    for key, value in defaults.items():
        if key not in keybindings:
            keybindings[key] = value

    kb_path.write_text(json.dumps(keybindings, indent=2))


def build_system_prompt():
    """Write system prompt template to ~/AGENTS.md if it doesn't exist.

    Pi auto-discovers AGENTS.md. Users can edit it freely — it won't
    be overwritten on subsequent container starts.
    """
    prompt_path = Path.home() / "AGENTS.md"
    if not prompt_path.exists():
        prompt_path.write_text(SYSTEM_PROMPT_SRC.read_text())


def setup_claude_code_skills():
    """Symlink enabled skill dirs into Claude Code's discovery path.

    KLANGK_SKILLS is a comma-separated list of skill directory names.
    Skills are expected at /opt/klangk/skills/<name>/ (user-mounted).
    """
    skills_env = os.environ.get("KLANGK_SKILLS", "")
    skills_dir = Path("/opt/klangk/skills")
    cc_skills_dir = Path.home() / ".claude" / "skills"

    if not skills_env or not skills_dir.is_dir():
        return

    # Clean and recreate
    if cc_skills_dir.exists():
        import shutil

        shutil.rmtree(cc_skills_dir)
    cc_skills_dir.mkdir(parents=True, exist_ok=True)

    for name in skills_env.split(","):
        name = name.strip()
        if name and (skills_dir / name).is_dir():
            (cc_skills_dir / name).symlink_to(skills_dir / name)


def setup_pi_skills():
    """Symlink enabled skill dirs into Pi's discovery path.

    KLANGK_SKILLS is a comma-separated list of skill directory names.
    Skills are expected at /opt/klangk/skills/<name>/ (user-mounted).
    Pi auto-discovers skills from ~/.pi/agent/skills/.
    """
    skills_env = os.environ.get("KLANGK_SKILLS", "")
    skills_dir = Path("/opt/klangk/skills")
    pi_skills_dir = AGENT_DIR / "skills"

    if not skills_env or not skills_dir.is_dir():
        return

    # Clean and recreate
    if pi_skills_dir.exists():
        import shutil

        shutil.rmtree(pi_skills_dir)
    pi_skills_dir.mkdir(parents=True, exist_ok=True)

    for name in skills_env.split(","):
        name = name.strip()
        if name and (skills_dir / name).is_dir():
            (pi_skills_dir / name).symlink_to(skills_dir / name)


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
    os.environ["PI_CODING_AGENT_DIR"] = str(AGENT_DIR)

    # Clear previous error log
    ERROR_LOG.unlink(missing_ok=True)

    _run_step("setup_dirs", setup_dirs)
    _run_step("sync_image_files", sync_image_files)
    _run_step("setup_bin", setup_bin)
    _run_step("merge_settings", merge_settings)
    _run_step("merge_models_json", merge_models_json)
    _run_step("merge_keybindings", merge_keybindings)
    _run_step("build_system_prompt", build_system_prompt)
    _run_step("setup_claude_code_skills", setup_claude_code_skills)
    _run_step("setup_pi_skills", setup_pi_skills)

    if ERROR_LOG.exists():
        print(f"setup_clankers: some steps failed, see {ERROR_LOG}")


if __name__ == "__main__":
    main()
