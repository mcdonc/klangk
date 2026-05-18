#!/usr/bin/env python3
"""Fetch external plugins declared in plugins/plugins.yaml.

If plugins/ doesn't exist, creates it with a template plugins.yaml.
If plugins/plugins.yaml exists, fetches listed plugins and writes plugins.lock.
"""

import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.environ.get("BARK_PLUGINS_DIR") or os.path.join(
    os.path.expanduser("~"), ".bark", "plugins"
)
YAML_PATH = os.path.join(PLUGINS_DIR, "plugins.yaml")
LOCK_PATH = os.path.join(PLUGINS_DIR, "plugins.lock")

DEFAULT_TEMPLATE = """\
# Bark plugins configuration
# Run 'update-plugins' to fetch plugins listed here.
# Each entry requires: name, git. Optional: path, ref.
plugins:
  # Default plugins (from the Bark repo)
  - name: celebrate
    git: git@github.com:mcdonc/bark.git
    path: plugins/celebrate
    ref: main
  - name: beep
    git: git@github.com:mcdonc/bark.git
    path: plugins/beep
    ref: main
  - name: pig-latin
    git: git@github.com:mcdonc/bark.git
    path: plugins/pig-latin
    ref: main
  - name: word-count
    git: git@github.com:mcdonc/bark.git
    path: plugins/word-count
    ref: main
  # Add more plugins:
  # - name: my-plugin
  #   git: git@github.com:user/repo.git
  #   path: subdir              # optional: subdirectory within the repo
  #   ref: main                 # branch, tag, or commit SHA
  #
  # Plugin structure:
  #   extension.ts              # required: Pi extension (TypeScript)
  #   dart/                     # optional: Dart package for client-side tools
  #     pubspec.yaml            #   depends on bark_plugin_api
  #     lib/
  #       plugin.dart           #   class extending ToolPlugin
  #   tools/                    # optional: server-side scripts
"""


def resolve_ref(git_url, ref):
    """Resolve a git ref (branch, tag, or SHA) to a commit SHA."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", git_url, ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            sha, _name = line.split("\t", 1)
            return sha
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # If ls-remote didn't match, assume ref is already a SHA
    return ref


def fetch_plugin(plugin, plugins_dir):
    """Fetch a single plugin from a git repo into plugins_dir."""
    git_url = plugin["git"]
    ref = plugin.get("ref", "main")
    subpath = plugin.get("path", "")

    name = plugin["name"]

    dest = os.path.join(plugins_dir, name)

    # Resolve ref to SHA
    sha = resolve_ref(git_url, ref)
    if not sha:
        print(f"  ERROR: Could not resolve ref '{ref}' for {git_url}", file=sys.stderr)
        return None

    print(f"  {name}: {git_url} @ {ref} -> {sha[:12]}")

    # Clone into temp dir, then copy the subpath
    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dir = os.path.join(tmpdir, "repo")
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch", ref, git_url, clone_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            # ref might be a SHA, try full clone
            result = subprocess.run(
                ["git", "clone", git_url, clone_dir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(
                    f"  ERROR: git clone failed: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                return None
            subprocess.run(
                ["git", "checkout", ref],
                cwd=clone_dir,
                capture_output=True,
                text=True,
            )

        source = os.path.join(clone_dir, subpath) if subpath else clone_dir

        if not os.path.isdir(source):
            print(f"  ERROR: path '{subpath}' not found in {git_url}", file=sys.stderr)
            return None

        # Remove old version and copy
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(source, dest)

        # Remove .git if it was copied
        git_dir = os.path.join(dest, ".git")
        if os.path.exists(git_dir):
            shutil.rmtree(git_dir)

    return {"name": name, "git": git_url, "path": subpath, "ref": ref, "sha": sha}


def write_lock(entries, lock_path):
    """Write the lockfile."""
    with open(lock_path, "w") as f:
        yaml.dump({"plugins": entries}, f, default_flow_style=False, sort_keys=False)


def plugin_name(plugin):
    """Get the plugin name from a plugins.yaml entry. Name is required."""
    name = plugin.get("name")
    if not name:
        raise ValueError(f"Plugin entry missing required 'name' field: {plugin}")
    return name


def read_lock():
    """Read existing lock entries as a dict keyed by name."""
    if not os.path.exists(LOCK_PATH):
        return {}
    with open(LOCK_PATH) as f:
        data = yaml.safe_load(f)
    return {e["name"]: e for e in (data or {}).get("plugins", [])}


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None

    # Create plugins/ with template if it doesn't exist
    if not os.path.exists(YAML_PATH):
        os.makedirs(PLUGINS_DIR, exist_ok=True)
        with open(YAML_PATH, "w") as f:
            f.write(DEFAULT_TEMPLATE)
        print(f"Created template {YAML_PATH}")
        print("Edit it to add plugins, then run 'update-plugins' again.")
        return

    # Read plugins.yaml
    with open(YAML_PATH) as f:
        config = yaml.safe_load(f)

    plugins = config.get("plugins", [])
    if not plugins:
        print("No plugins listed in plugins.yaml")
        return

    # Filter to a single plugin if requested
    if only:
        matched = [p for p in plugins if plugin_name(p) == only]
        if not matched:
            print(f"Plugin '{only}' not found in plugins.yaml", file=sys.stderr)
            sys.exit(1)
        plugins = matched

    print(f"Fetching {len(plugins)} plugin{'s' if len(plugins) != 1 else ''}...")

    # Preserve existing lock entries when updating a single plugin
    old_lock = read_lock()
    lock_map = dict(old_lock)

    for plugin in plugins:
        if "git" not in plugin:
            print(f"  SKIP: entry missing 'git' key: {plugin}", file=sys.stderr)
            continue
        entry = fetch_plugin(plugin, PLUGINS_DIR)
        if entry:
            lock_map[entry["name"]] = entry

    # Remove plugins that were in the old lockfile but dropped from plugins.yaml
    if not only:
        yaml_names = {plugin_name(p) for p in config.get("plugins", []) if "git" in p}
        for name in list(lock_map):
            if name not in yaml_names:
                plugin_dir = os.path.join(PLUGINS_DIR, name)
                if os.path.isdir(plugin_dir):
                    shutil.rmtree(plugin_dir)
                    print(f"  Removed {name} (no longer in plugins.yaml)")
                del lock_map[name]

    write_lock(list(lock_map.values()), LOCK_PATH)
    print(f"Wrote {LOCK_PATH} with {len(lock_map)} plugins")


if __name__ == "__main__":
    main()
