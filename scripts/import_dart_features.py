#!/usr/bin/env python3
"""Register Dart features as a generated package in <payload-dir>/.dart/.

Scans <payload-dir>/*/klangk/pubspec.yaml for features with Dart packages
and generates:

  <payload-dir>/.dart/pubspec.yaml         — package with path deps
  <payload-dir>/.dart/lib/klangk_features.dart — createAllFeatures()
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
to the generated overrides file, so Flutter resolves the klangk_features
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

# The default-on set: features a bare install gets when KLANGKD_FEATURES_ENABLE
# is unset (canonical activation — see #1655). This is the runtime default-on
# list; the build-time fetch list is the checked-in ``features.yaml`` at the
# repo root. The two are allowed to differ: a feature can ship dormant
# (compiled in but not in defaults). Today ``word-count`` and ``soliplex``
# (#1664, vendored local in #1686) are both compiled-in dormant local features
# — neither surfaced unless an operator opts in via KLANGKD_FEATURES_ENABLE (#1700).
DEFAULT_FEATURES = [
    "beep",
    "bobdobbs",
    "boingball",
    "browser-fetch",
    "celebrate",
    "git-credential",
]

FEATURE_API_DEP = {
    "klangk_plugin_api": {
        "git": {
            "url": "https://github.com/mcdonc/klangk-plugin-api.git",
            "ref": "v0.2.0",
        }
    }
}


def find_features(features_dir):
    """Scan features/*/klangk/ for Dart packages, return metadata."""
    features = []
    if not os.path.isdir(features_dir):
        return features

    for name in sorted(os.listdir(features_dir)):
        feature_dir = os.path.join(features_dir, name)
        dart_dir = os.path.join(feature_dir, "klangk")
        pubspec_file = os.path.join(dart_dir, "pubspec.yaml")
        feature_dart = os.path.join(dart_dir, "lib", "feature.dart")

        if not os.path.isfile(pubspec_file) or not os.path.isfile(feature_dart):
            continue

        with open(pubspec_file) as f:
            pubspec = yaml.safe_load(f)

        package_name = pubspec.get("name", f"klangk_feature_{name}")

        with open(feature_dart) as f:
            source = f.read()

        matches = re.findall(r"class\s+(\w+)\s+extends\s+ToolPlugin", source)
        if not matches:
            continue

        for class_name in matches:
            features.append(
                {
                    "name": name,
                    "package_name": package_name,
                    "dart_dir": dart_dir,
                    "class_name": class_name,
                }
            )

    return features


# Scopes that make a klangk.config key eligible for container-env injection.
# Mirrors VALID_SCOPES in src/klangk/klangk/features.py (the server's runtime
# resolver). "frontend" only is excluded — those go to the UI via /api/config,
# not into the container env.
_CONTAINER_SCOPES = {"container", "both"}

# Every klangk.config key a feature declares — regardless of scope — must
# start with this prefix. Server-side settings are all ``KLANGKD_<SETTING>``
# (no ``FEATURE_`` infix), so the prefix alone guarantees a feature can never
# declare a key that collides with a server secret, path, or infra field
# (``KLANGKD_JWT_SECRET``, ``KLANGKD_DATA_DIR``, …) — no denylist / reserved
# set needed, and nothing to keep in sync between this file and the runtime
# resolver (#1662). Non-KLANGKD_ environment poison (``PATH``, ``HOME``,
# ``LD_PRELOAD``, …) is rejected by the same rule. Mirrors
# ``_CONTAINER_ENV_KEY_PREFIX`` in ``src/klangk/klangk/features.py``.
_CONTAINER_ENV_KEY_PREFIX = "KLANGKWS_FEATURE_"


class InvalidFeatureConfigKey(RuntimeError):
    """A feature declared a klangk.config key without the KLANGKWS_FEATURE_ prefix.

    Raised at build time to abort the emit — distinct from the
    ``except (JSONDecodeError, ValueError, OSError)`` around the per-feature
    ``package.json`` read (which swallows a malformed feature's parse errors
    and continues the build). A prefix violation is a deliberate "stop the
    build, fix the declaration" condition, so this exception is a
    ``RuntimeError`` (not a ``ValueError`` subclass) and propagates.
    """


def _validate_feature_config_key(key, feature_name):
    """Reject a klangk.config key that lacks the KLANGKWS_FEATURE_ prefix.

    The prefix is the feature-config namespace (#1662): every server setting
    is ``KLANGKD_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone keeps
    feature-declared keys from ever colliding with a server secret / path /
    infra field — no reserved-set / denylist required. Applies to every scope
    (container, frontend, both): the declaration-side rule is uniform; how
    the value is surfaced to consumers differs (container env keeps the full
    ``KLANGKWS_FEATURE_*`` env var name; the frontend ``/api/config`` key is the
    lowercased suffix). Raises :class:`InvalidFeatureConfigKey` naming the
    feature + key so the feature author fixes the declaration before ship.
    """
    if not key.startswith(_CONTAINER_ENV_KEY_PREFIX):
        raise InvalidFeatureConfigKey(
            f"feature {feature_name!r} declares config key {key!r} — "
            f"must start with {_CONTAINER_ENV_KEY_PREFIX!r} "
            f"(the feature-config namespace; server settings are "
            f"KLANGKD_<SETTING> with no FEATURE_ infix, so the prefix alone "
            f"prevents collisions with server secrets/paths/infra)."
        )


def collect_feature_metadata(dart_features, features_dir):
    """Build the per-feature metadata + container_env_keys from package.json.

    Reads each compiled-in feature's ``package.json`` (sibling of ``klangk/``)
    for ``{version, description, klangk.config}`` — exactly the runtime read
    surface the old server-side ``Features.load()`` did via directory scan, now
    done at build time and collapsed into one file (#1655).

    ``dart_features`` is the output of :func:`find_features` — the Dart-compiled
    set, which is the registry of "what's available" (compiled in). Per-feature
    entries without a ``package.json`` are kept with empty metadata so the
    frontend still sees them as present.
    """
    features = []
    container_env_keys = []
    for p in dart_features:
        name = p["name"]
        pkg_json = os.path.join(features_dir, name, "package.json")
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
                        # Every declared key must carry the KLANGKWS_FEATURE_
                        # prefix, regardless of scope (#1662). Validate before
                        # emitting anything so a bad declaration aborts the
                        # build rather than shipping a manifest the runtime
                        # would silently skip.
                        _validate_feature_config_key(key, name)
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


def write_features_json(dart_features, features_dir):
    """Emit features.json next to the frontend's index.html (#1655).

    The frontend reads this sibling file for per-feature metadata + the
    defaults list (canonical KLANGKD_FEATURES_ENABLE activation). ``klangkd``
    reads one field — ``container_env_keys`` — to bridge the declared
    container-scope env vars into workspace containers (the server reads no
    on-disk feature trees; the build did the knowing). See #1655.
    """
    features, container_env_keys = collect_feature_metadata(dart_features, features_dir)
    manifest = {
        "features": features,
        "defaults": list(DEFAULT_FEATURES),
        "container_env_keys": container_env_keys,
    }
    os.makedirs(os.path.dirname(FEATURES_JSON), exist_ok=True)
    with open(FEATURES_JSON, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
        f.write("\n")


def write_pubspec(features, dart_pkg_dir):
    """Generate <payload-dir>/.dart/pubspec.yaml with feature path dependencies."""
    pubspec_path = os.path.join(dart_pkg_dir, "pubspec.yaml")
    deps = {
        "flutter": {"sdk": "flutter"},
    }
    deps.update(FEATURE_API_DEP)

    seen = set()
    for p in features:
        if p["package_name"] not in seen:
            deps[p["package_name"]] = {"path": p["dart_dir"]}
            seen.add(p["package_name"])

    pubspec = {
        "name": "klangk_features",
        "publish_to": "none",
        "version": "0.0.1",
        "environment": {"sdk": "^3.6.0", "flutter": "^3.27.0"},
        "dependencies": deps,
    }

    os.makedirs(dart_pkg_dir, exist_ok=True)
    with open(pubspec_path, "w") as f:
        yaml.dump(pubspec, f, default_flow_style=False, sort_keys=False)


def generate_dart(features):
    """Generate klangk_features.dart source as a string."""
    lines = [
        "// GENERATED by import_dart_features.py — do not edit.",
        "import 'package:klangk_plugin_api/klangk_plugin_api.dart';",
        "",
    ]
    for p in features:
        lines.append(f"import 'package:{p['package_name']}/feature.dart';")

    lines.append("")
    lines.append("List<ToolPlugin> createAllFeatures() {")
    lines.append("  return [")
    for p in features:
        lines.append(f"    {p['class_name']}(),")
    lines.append("  ];")
    lines.append("}")
    lines.append("")
    lines.append(
        "// ({name, feature}) records for the active-set filter in main.dart (#1655)."
    )
    lines.append("// `name` matches features.json features[].name / defaults[].")
    lines.append("List<({String name, ToolPlugin feature})> createAllNamedFeatures() {")
    lines.append("  return [")
    for p in features:
        lines.append(f"    (name: {p['name']!r}, feature: {p['class_name']}()),")
    lines.append("  ];")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_overrides_and_symlink(dart_pkg_dir):
    """Write pubspec_overrides.yaml at <payload-dir>/.dart/ and symlink it
    into the frontend directory so Flutter can find it."""
    overrides_content = (
        f"dependency_overrides:\n  klangk_features:\n    path: {dart_pkg_dir}\n"
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
    """Generate the Dart aggregator pubspec + createAllFeatures() + features.json.

    With ``--features-only``, skip the Dart codegen and just (re-)emit
    ``features.json`` — used by ``flutterbuildweb.sh`` *after* the Flutter
    build, because ``flutter build web`` may regenerate ``build/web/`` and
    wipe a manifest written before it (#1655).

    ``--payload-dir`` (required unless ``--features-only`` is given without
    having a prior payload to read) points at the materialized feature
    payload — the tempdir the build script owns (#1660).
    """
    parser = argparse.ArgumentParser(
        description="Generate the klangk_features Dart package + features.json manifest."
    )
    parser.add_argument(
        "--payload-dir",
        default=None,
        help=(
            "Directory holding the materialized feature trees (from "
            "update_features.py). Defaults to a fresh mktemp -d."
        ),
    )
    parser.add_argument(
        "--features-only",
        action="store_true",
        help="Skip Dart codegen; just (re-)emit features.json.",
    )
    args = parser.parse_args(argv)

    features_dir = args.payload_dir or tempfile.mkdtemp(prefix="klangk-features-")
    dart_pkg_dir = os.path.join(features_dir, ".dart")
    features = find_features(features_dir)

    if not args.features_only:
        write_pubspec(features, dart_pkg_dir)
        write_overrides_and_symlink(dart_pkg_dir)

    # Always (re-)emit the manifest. Pre-flutter-build, this creates
    # build/web/ so the dir exists; post-flutter-build (--features-only),
    # it restores the manifest if Flutter wiped/regenerated the dir.
    write_features_json(features, features_dir)

    if args.features_only:
        print(f"Regenerated feature manifest {FEATURES_JSON}")
        return

    output = generate_dart(features)
    output_path = os.path.join(dart_pkg_dir, "lib", "klangk_features.dart")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(output)

    names = [p["class_name"] for p in features]
    print(
        f"Generated Dart pubspec at {os.path.join(dart_pkg_dir, 'pubspec.yaml')} "
        f"with {len(features)} feature(s)"
    )
    print(f"Generated Dart {output_path}: {', '.join(names) or '(none)'}")
    print(f"Generated feature manifest {FEATURES_JSON}")


if __name__ == "__main__":
    main()
