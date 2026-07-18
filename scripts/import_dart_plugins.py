#!/usr/bin/env python3
"""Register Dart plugins as a generated package in <payload-dir>/.dart/.

Scans <payload-dir>/*/klangk/pubspec.yaml for plugins with Dart packages
and generates:

  <payload-dir>/.dart/pubspec.yaml         — package with path deps
  <payload-dir>/.dart/lib/klangk_plugins.dart — createAllPlugins()
  <payload-dir>/.dart/pubspec_overrides.yaml — real path override

``<payload-dir>`` is supplied by the calling build script (normally a fresh
``mktemp -d`` it owns and cleans up, #1660) via ``--payload-dir``.

Also emits ``features.json`` — the runtime feature manifest consumed by the
frontend (per-feature metadata + the default-on set) and by ``klangkd``
(the container-scope env keys to bridge into workspace containers). The
manifest lands in the frontend build-output dir (next to ``index.html``)
so the hatch build hook ships it inside the wheel at
``klangk/frontend/features.json`` with no extra include rule (#1655).

Also creates a symlink at src/frontend/pubspec_overrides.yaml pointing
to the generated overrides file, so Flutter resolves the klangk_plugins
placeholder dependency to the actual package. No committed source files
are modified.
"""

import argparse
import json
import os
import re
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEATURES_JSON = os.path.join(ROOT, "src", "frontend", "build", "web", "features.json")

# The default-on set: features a bare install gets when KLANGK_FEATURES_ENABLE
# is unset (canonical activation — see #1655). This is the runtime default-on
# list; the build-time fetch list is the checked-in ``plugins.yaml`` at the
# repo root. The two are equal today; they're allowed to differ when a feature
# ships dormant (compiled in but not in defaults, e.g. a single-client feature).
DEFAULT_FEATURES = [
    "celebrate",
    "beep",
    "pig-latin",
    "word-count",
    "browser-fetch",
    "boingball",
    "git-credential",
]

PLUGIN_API_DEP = {
    "klangk_plugin_api": {
        "git": {
            "url": "https://github.com/mcdonc/klangk-plugin-api.git",
            "ref": "v0.2.0",
        }
    }
}


def find_plugins(plugins_dir):
    """Scan plugins/*/klangk/ for Dart packages, return metadata."""
    plugins = []
    if not os.path.isdir(plugins_dir):
        return plugins

    for name in sorted(os.listdir(plugins_dir)):
        plugin_dir = os.path.join(plugins_dir, name)
        dart_dir = os.path.join(plugin_dir, "klangk")
        pubspec_file = os.path.join(dart_dir, "pubspec.yaml")
        plugin_dart = os.path.join(dart_dir, "lib", "plugin.dart")

        if not os.path.isfile(pubspec_file) or not os.path.isfile(plugin_dart):
            continue

        with open(pubspec_file) as f:
            pubspec = yaml.safe_load(f)

        package_name = pubspec.get("name", f"klangk_plugin_{name}")

        with open(plugin_dart) as f:
            source = f.read()

        matches = re.findall(r"class\s+(\w+)\s+extends\s+ToolPlugin", source)
        if not matches:
            continue

        for class_name in matches:
            plugins.append(
                {
                    "name": name,
                    "package_name": package_name,
                    "dart_dir": dart_dir,
                    "class_name": class_name,
                }
            )

    return plugins


# Scopes that make a klangk.config key eligible for container-env injection.
# Mirrors VALID_SCOPES in src/klangk/klangk/plugins.py (the server's runtime
# resolver). "frontend" only is excluded — those go to the UI via /api/config,
# not into the container env.
_CONTAINER_SCOPES = {"container", "both"}


def collect_feature_metadata(dart_plugins, plugins_dir):
    """Build the per-feature metadata + container_env_keys from package.json.

    Reads each compiled-in feature's ``package.json`` (sibling of ``klangk/``)
    for ``{version, description, klangk.config}`` — exactly the runtime read
    surface the old server-side ``Plugins.load()`` did via directory scan, now
    done at build time and collapsed into one file (#1655).

    ``dart_plugins`` is the output of :func:`find_plugins` — the Dart-compiled
    set, which is the registry of "what's available" (compiled in). Per-feature
    entries without a ``package.json`` are kept with empty metadata so the
    frontend still sees them as present.
    """
    features = []
    container_env_keys = []
    for p in dart_plugins:
        name = p["name"]
        pkg_json = os.path.join(plugins_dir, name, "package.json")
        version = ""
        description = ""
        config = {}
        if os.path.isfile(pkg_json):
            try:
                with open(pkg_json) as f:
                    manifest = json.load(f)
                version = manifest.get("version", "")
                description = manifest.get("description", "")
                cfg = manifest.get("klangk", {}).get("config", {})
                if isinstance(cfg, dict):
                    # Carry only the JSON-serializable, runtime-relevant shape
                    # per key: {description, default, scope}. Unknown scopes
                    # default to "container" (same as the old server resolver).
                    for key, spec in cfg.items():
                        if not isinstance(spec, dict):
                            continue
                        scope = spec.get("scope", "container")
                        if scope not in {"container", "frontend", "both"}:
                            scope = "container"
                        config[key] = {
                            "description": spec.get("description", ""),
                            "default": spec.get("default", ""),
                            "scope": scope,
                        }
                        if scope in _CONTAINER_SCOPES:
                            container_env_keys.append(key)
            except (json.JSONDecodeError, ValueError, OSError):
                pass
        features.append(
            {
                "name": name,
                "version": version,
                "description": description,
                "config": config,
            }
        )
    return features, container_env_keys


def write_features_json(dart_plugins, plugins_dir):
    """Emit features.json next to the frontend's index.html (#1655).

    The frontend reads this sibling file for per-feature metadata + the
    defaults list (canonical KLANGK_FEATURES_ENABLE activation). ``klangkd``
    reads one field — ``container_env_keys`` — to bridge the declared
    container-scope env vars into workspace containers (the server reads no
    on-disk plugin trees; the build did the knowing). See #1655.
    """
    features, container_env_keys = collect_feature_metadata(dart_plugins, plugins_dir)
    manifest = {
        "features": features,
        "defaults": list(DEFAULT_FEATURES),
        "container_env_keys": container_env_keys,
    }
    os.makedirs(os.path.dirname(FEATURES_JSON), exist_ok=True)
    with open(FEATURES_JSON, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
        f.write("\n")


def write_pubspec(plugins, dart_pkg_dir):
    """Generate <payload-dir>/.dart/pubspec.yaml with plugin path dependencies."""
    pubspec_path = os.path.join(dart_pkg_dir, "pubspec.yaml")
    deps = {
        "flutter": {"sdk": "flutter"},
    }
    deps.update(PLUGIN_API_DEP)

    seen = set()
    for p in plugins:
        if p["package_name"] not in seen:
            deps[p["package_name"]] = {"path": p["dart_dir"]}
            seen.add(p["package_name"])

    pubspec = {
        "name": "klangk_plugins",
        "publish_to": "none",
        "version": "0.0.1",
        "environment": {"sdk": "^3.6.0", "flutter": "^3.27.0"},
        "dependencies": deps,
    }

    os.makedirs(dart_pkg_dir, exist_ok=True)
    with open(pubspec_path, "w") as f:
        yaml.dump(pubspec, f, default_flow_style=False, sort_keys=False)


def generate_dart(plugins):
    """Generate klangk_plugins.dart source as a string."""
    lines = [
        "// GENERATED by import_dart_plugins.py — do not edit.",
        "import 'package:klangk_plugin_api/klangk_plugin_api.dart';",
        "",
    ]
    for p in plugins:
        lines.append(f"import 'package:{p['package_name']}/plugin.dart';")

    lines.append("")
    lines.append("List<ToolPlugin> createAllPlugins() {")
    lines.append("  return [")
    for p in plugins:
        lines.append(f"    {p['class_name']}(),")
    lines.append("  ];")
    lines.append("}")
    lines.append("")
    lines.append(
        "// ({name, plugin}) records for the active-set filter in main.dart (#1655)."
    )
    lines.append("// `name` matches features.json features[].name / defaults[].")
    lines.append("List<({String name, ToolPlugin plugin})> createAllNamedPlugins() {")
    lines.append("  return [")
    for p in plugins:
        lines.append(f"    (name: {p['name']!r}, plugin: {p['class_name']}()),")
    lines.append("  ];")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_overrides_and_symlink(dart_pkg_dir):
    """Write pubspec_overrides.yaml at <payload-dir>/.dart/ and symlink it
    into the frontend directory so Flutter can find it."""
    overrides_content = (
        f"dependency_overrides:\n  klangk_plugins:\n    path: {dart_pkg_dir}\n"
    )
    overrides_path = os.path.join(dart_pkg_dir, "pubspec_overrides.yaml")
    with open(overrides_path, "w") as f:
        f.write(overrides_content)

    # Symlink into the frontend directory
    frontend_dir = os.path.join(ROOT, "src", "frontend")
    symlink_path = os.path.join(frontend_dir, "pubspec_overrides.yaml")
    if os.path.islink(symlink_path) or os.path.exists(symlink_path):
        os.remove(symlink_path)
    os.symlink(overrides_path, symlink_path)


def main(argv=None):
    """Generate the Dart aggregator pubspec + createAllPlugins() + features.json.

    With ``--features-only``, skip the Dart codegen and just (re-)emit
    ``features.json`` — used by ``flutterbuildweb.sh`` *after* the Flutter
    build, because ``flutter build web`` may regenerate ``build/web/`` and
    wipe a manifest written before it (#1655).

    ``--payload-dir`` (required unless ``--features-only`` is given without
    having a prior payload to read) points at the materialized plugin
    payload — the tempdir the build script owns (#1660).
    """
    parser = argparse.ArgumentParser(
        description="Generate the klangk_plugins Dart package + features.json manifest."
    )
    parser.add_argument(
        "--payload-dir",
        default=None,
        help=(
            "Directory holding the materialized plugin trees (from "
            "update_plugins.py). Defaults to a fresh mktemp -d."
        ),
    )
    parser.add_argument(
        "--features-only",
        action="store_true",
        help="Skip Dart codegen; just (re-)emit features.json.",
    )
    args = parser.parse_args(argv)

    plugins_dir = args.payload_dir or tempfile.mkdtemp(prefix="klangk-plugins-")
    dart_pkg_dir = os.path.join(plugins_dir, ".dart")
    plugins = find_plugins(plugins_dir)

    if not args.features_only:
        write_pubspec(plugins, dart_pkg_dir)
        write_overrides_and_symlink(dart_pkg_dir)

    # Always (re-)emit the manifest. Pre-flutter-build, this creates
    # build/web/ so the dir exists; post-flutter-build (--features-only),
    # it restores the manifest if Flutter wiped/regenerated the dir.
    write_features_json(plugins, plugins_dir)

    if args.features_only:
        print(f"Regenerated feature manifest {FEATURES_JSON}")
        return

    output = generate_dart(plugins)
    output_path = os.path.join(dart_pkg_dir, "lib", "klangk_plugins.dart")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(output)

    names = [p["class_name"] for p in plugins]
    print(
        f"Generated Dart pubspec at {os.path.join(dart_pkg_dir, 'pubspec.yaml')} "
        f"with {len(plugins)} plugin(s)"
    )
    print(f"Generated Dart {output_path}: {', '.join(names) or '(none)'}")
    print(f"Generated feature manifest {FEATURES_JSON}")


if __name__ == "__main__":
    main()
