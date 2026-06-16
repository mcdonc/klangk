#!/usr/bin/env python3
"""Collect plugin configuration declarations from package.json manifests.

Scans $KLANGK_PLUGINS_DIR/*/package.json for ``klangk.config`` entries and
writes a merged summary to $KLANGK_PLUGINS_DIR/.plugin_config.json.

Each config entry declares an environment variable that the plugin needs,
along with its scope (where the value should be delivered):

  - "container" — injected as an env var into workspace containers
  - "frontend"  — included in the GET /api/config response
  - "both"      — both of the above

Example package.json::

    {
      "name": "@klangk/my-plugin",
      "klangk": {
        "config": {
          "MY_PLUGIN_URL": {
            "description": "URL for the my-plugin backend",
            "default": "",
            "scope": "frontend"
          }
        }
      }
    }
"""

import json
import os

PLUGINS_DIR = os.environ.get("KLANGK_PLUGINS_DIR") or os.path.join(
    os.path.expanduser("~"), ".klangk", "plugins"
)
OUTPUT = os.path.join(PLUGINS_DIR, ".plugin_config.json")

VALID_SCOPES = {"container", "frontend", "both"}


def collect():
    """Scan plugin manifests and return merged config dict."""
    result = {}
    if not os.path.isdir(PLUGINS_DIR):
        return result

    for name in sorted(os.listdir(PLUGINS_DIR)):
        pkg_json = os.path.join(PLUGINS_DIR, name, "package.json")
        if not os.path.isfile(pkg_json):
            continue

        with open(pkg_json) as f:
            try:
                manifest = json.load(f)
            except (json.JSONDecodeError, ValueError):
                continue

        klangk_section = manifest.get("klangk", {})
        config = klangk_section.get("config", {})
        if not isinstance(config, dict):
            continue

        for key, spec in config.items():
            if not isinstance(spec, dict):
                continue
            scope = spec.get("scope", "container")
            if scope not in VALID_SCOPES:
                scope = "container"
            result[key] = {
                "plugin": name,
                "description": spec.get("description", ""),
                "default": spec.get("default", ""),
                "scope": scope,
            }

    return result


def main():
    config = collect()
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Collected {len(config)} plugin config key(s) → {OUTPUT}")


if __name__ == "__main__":
    main()
