"""Build-pipeline integration test (#1666).

Exercises the real build pipeline — ``update_plugins.py`` →
``import_dart_plugins.py`` — against the **real** checked-in ``plugins.yaml``
and the real ``plugins/`` source trees, then asserts on the outputs. This is
the test the #1665 adversarial review flagged as missing: the runtime side
(``Plugins`` reading ``features.json``) is well-covered by ``test_plugins.py``,
but the build side — the code #1660/#1665 changed — had only isolated unit
tests per script.

What this catches that the per-script unit tests don't:

- A ``path:`` entry in ``plugins.yaml`` pointing at a missing dir.
- A plugin whose ``klangk/lib/plugin.dart`` lost its ``ToolPlugin`` subclass.
- Drift between the checked-in declaration and the plugin source trees.
- A generated Dart aggregator that references a class that doesn't exist.
- A ``features.json`` whose shape the runtime ``Plugins._read_manifest()``
  would reject (the manifest contract — see ``test_manifest_contract`` below).
- The 7-on-disk / 4-Dart asymmetry flipping silently (a plugin accidentally
  shipping without a ``klangk/`` dir, or gaining one).

Runs in ~1s (no flutter, no docker). The scripts tests are standalone — no
``klangk`` import — so this file mirrors the manifest shape contract inline
rather than importing ``klangk.plugins``; ``test_plugins.py`` covers the
runtime side with synthetic manifests, this file covers the build side with
the real one.
"""

import os
import re
import sys

# Make sure the scripts directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_dart_plugins
import update_plugins


# ────────────────────────────────────────────────────────────────────────────
# The contract: what the checked-in declaration + plugin source trees should
# produce today. Hard-coded so a drift is loud. Update these when you
# intentionally add/remove a plugin or change which ones ship Dart packages.
# ────────────────────────────────────────────────────────────────────────────

EXPECTED_FEATURE_NAMES = {
    "celebrate",
    "beep",
    "bobdobbs",
    "word-count",
    "browser-fetch",
    "boingball",
    "git-credential",
}

# Remote (git-sourced) plugins declared in plugins.yaml. These are compiled-in
# features too, but they're fetched over the network at build time — tests use
# `--local-only` to avoid the network, and a separate test synthesizes a
# soliplex-shaped tree to verify codegen picks remote plugins up correctly
# (#1664: the first compiled-in-but-dormant feature).
REMOTE_FEATURE_NAMES = {"soliplex"}

# Plugins with a klangk/ Dart package → class names emitted into the
# generated aggregator. Plugins without klangk/ (word-count, browser-fetch)
# are TS-only and must NOT appear in the Dart aggregator.
EXPECTED_DART_PLUGINS = {
    "celebrate": "CelebratePlugin",
    "beep": "BeepPlugin",
    "bobdobbs": "BobDobbsPlugin",
    "boingball": "BoingBallPlugin",
    "git-credential": "GitCredentialPlugin",
}

# The subset that appears in features.json's features[] list. import_dart_plugins
# only carries features with a klangk/ Dart package (the frontend-activatable
# set). TS-only plugins (word-count, browser-fetch) are baked into
# the workspace image and always-on — they never appear in features.json.
# This is the wheel/workspace activation asymmetry from #1655.
EXPECTED_DART_FEATURE_NAMES = set(EXPECTED_DART_PLUGINS)

# Config keys declared across all plugin package.json files, by scope.
EXPECTED_CONTAINER_ENV_KEYS = ["KLANGK_GITHUB_OAUTH_CLIENT_ID"]
EXPECTED_FRONTEND_CONFIG_KEYS = ["KLANGK_BOING_SPEED"]


def _run_codegen(payload_dir, tmp_path, monkeypatch):
    """Run import_dart_plugins.main() with outputs redirected into tmp_path.

    Both ``FEATURES_JSON`` (pre-computed at module load) and ``ROOT`` (used
    by ``write_overrides_and_symlink`` to locate ``src/frontend/``) are
    redirected — otherwise the test leaves a dangling
    ``src/frontend/pubspec_overrides.yaml`` symlink in the source tree after
    pytest reaps the tempdir. The tempdir's ``src/frontend/`` is created so
    the symlink has a parent to land in.
    """
    fake_frontend = tmp_path / "src" / "frontend"
    fake_frontend.mkdir(parents=True, exist_ok=True)
    features_json = fake_frontend / "build" / "web" / "features.json"
    monkeypatch.setattr(import_dart_plugins, "FEATURES_JSON", str(features_json))
    monkeypatch.setattr(import_dart_plugins, "ROOT", str(tmp_path))
    import_dart_plugins.main(["--payload-dir", str(payload_dir)])
    return features_json


def _materialize(payload_dir):
    """Run update_plugins.main() against the real checked-in plugins.yaml.

    Uses ``--local-only`` so the test doesn't hit the network for the
    soliplex git entry — remote plugins are covered by a separate synthetic
    test (see ``TestRemotePluginCodegen``) that verifies codegen picks them
    up, and by the real build (``flutterbuildweb.sh``) which fetches them
    (#1664).
    """
    rc = update_plugins.main(["--payload-dir", str(payload_dir), "--local-only"])
    assert rc == 0, "update_plugins.py failed against the real plugins.yaml"


# ────────────────────────────────────────────────────────────────────────────
# Test 1: the build pipeline runs clean against the real declaration.
# ────────────────────────────────────────────────────────────────────────────


class TestPipelineRuns:
    """update_plugins + import_dart_plugins succeed against the real repo."""

    def test_update_plugins_materializes_all_declared(self, tmp_path):
        payload = tmp_path / "payload"
        payload.mkdir()
        # --local-only: skip git entries (soliplex) so the test doesn't hit
        # the network. The remote-plugin codegen path is exercised separately.
        rc = update_plugins.main(["--payload-dir", str(payload), "--local-only"])
        assert rc == 0

        # Every LOCAL plugin is symlinked into the payload dir. Git entries
        # (soliplex) are skipped without materializing — they appear only in
        # plugins.lock with sha: 'skipped'. Filter to directories —
        # plugins.lock (a file) also lives there.
        materialized = {
            p
            for p in os.listdir(payload)
            if (payload / p).is_dir() and not p.startswith(".")
        }
        assert materialized == EXPECTED_FEATURE_NAMES, (
            f"materialized set != declared local set — drift in plugins.yaml "
            f"or plugins/. materialized={sorted(materialized)}"
        )

        # plugins.lock lists EVERY declared plugin (local + remote-skipped).
        import yaml

        lock = yaml.safe_load((payload / "plugins.lock").read_text())
        lock_names = {e["name"] for e in lock["plugins"]}
        assert lock_names == EXPECTED_FEATURE_NAMES | REMOTE_FEATURE_NAMES, (
            f"plugins.lock names != all declared: {sorted(lock_names)}"
        )
        # Remote entries are recorded as skipped (not fetched).
        remote_entries = {
            e["name"]: e for e in lock["plugins"] if e["name"] in REMOTE_FEATURE_NAMES
        }
        for name, entry in remote_entries.items():
            assert entry.get("sha") == "skipped", (
                f"{name} should be sha='skipped' under --local-only, got "
                f"{entry.get('sha')!r}"
            )

    def test_import_dart_plugins_generates_aggregator(self, tmp_path, monkeypatch):
        payload = tmp_path / "payload"
        payload.mkdir()
        _materialize(payload)
        _run_codegen(payload, tmp_path, monkeypatch)

        # The aggregator is at <payload>/.dart/lib/klangk_plugins.dart.
        dart_file = payload / ".dart" / "lib" / "klangk_plugins.dart"
        assert dart_file.is_file(), "klangk_plugins.dart was not generated"
        source = dart_file.read_text()

        # Every Dart-bearing plugin's class is imported + instantiated.
        for name, cls in EXPECTED_DART_PLUGINS.items():
            pkg = f"klangk_plugin_{name.replace('-', '_')}"
            assert f"import 'package:{pkg}/plugin.dart';" in source, (
                f"{pkg} not imported by the aggregator"
            )
            assert f"{cls}()" in source, (
                f"{cls}() not instantiated in createAllPlugins/createAllNamedPlugins"
            )

        # Plugins WITHOUT a klangk/ dir must not appear in the Dart aggregator.
        non_dart = EXPECTED_FEATURE_NAMES - set(EXPECTED_DART_PLUGINS)
        for name in non_dart:
            pkg = f"klangk_plugin_{name.replace('-', '_')}"
            assert f"import 'package:{pkg}/" not in source, (
                f"{name} has no klangk/ dir but leaked into the Dart aggregator"
            )

    def test_named_aggregator_names_match_feature_names(self, tmp_path, monkeypatch):
        """createAllNamedPlugins() emits records whose `name` matches the
        feature name in plugins.yaml — the link the runtime's active-set
        filter in main.dart depends on (#1655)."""
        payload = tmp_path / "payload"
        payload.mkdir()
        _materialize(payload)
        _run_codegen(payload, tmp_path, monkeypatch)

        dart_file = payload / ".dart" / "lib" / "klangk_plugins.dart"
        source = dart_file.read_text()

        # Extract (name: '...', plugin: ...) records from createAllNamedPlugins.
        # The generator emits lines like:    (name: 'celebrate', plugin: CelebratePlugin()),
        named = re.findall(r"\(name:\s*'([^']+)',\s*plugin:\s*(\w+)\(\)\)", source)
        named_map = dict(named)

        # Every Dart-bearing plugin appears with the exact feature name.
        assert set(named_map) == set(EXPECTED_DART_PLUGINS), (
            f"named-plugin names don't match Dart plugin set: {sorted(named_map)}"
        )
        for name, cls in EXPECTED_DART_PLUGINS.items():
            assert named_map[name] == cls


# ────────────────────────────────────────────────────────────────────────────
# Test 2: the manifest contract — features.json has the shape the runtime
# Plugins._read_manifest() expects. Mirrors the validation in
# src/klangk/klangk/plugins.py; if the runtime's expectations change, both
# this test (real manifest) and test_plugins.py (synthetic) must update.
# ────────────────────────────────────────────────────────────────────────────


class TestManifestContract:
    """The real features.json satisfies the runtime's shape contract."""

    def _build_manifest(self, tmp_path, monkeypatch):
        payload = tmp_path / "payload"
        payload.mkdir()
        _materialize(payload)
        features_json = _run_codegen(payload, tmp_path, monkeypatch)
        import json

        return json.loads(features_json.read_text())

    def test_top_level_shape(self, tmp_path, monkeypatch):
        manifest = self._build_manifest(tmp_path, monkeypatch)
        assert set(manifest) == {"features", "defaults", "container_env_keys"}

    def test_every_feature_has_required_keys(self, tmp_path, monkeypatch):
        manifest = self._build_manifest(tmp_path, monkeypatch)
        feature_names = set()
        for f in manifest["features"]:
            assert isinstance(f, dict)
            for key in ("name", "version", "description", "config"):
                assert key in f, f"feature {f.get('name')} missing {key}"
            assert isinstance(f["name"], str) and f["name"]
            assert isinstance(f["version"], str)
            assert isinstance(f["description"], str)
            assert isinstance(f["config"], dict)
            feature_names.add(f["name"])
        # features[] carries only Dart plugins — TS-only plugins are absent
        # (wheel/workspace activation asymmetry, #1655).
        assert feature_names == EXPECTED_DART_FEATURE_NAMES, (
            f"features[] names drifted from the Dart plugin set: "
            f"{sorted(feature_names)}"
        )

    def test_every_config_key_has_valid_shape_and_scope(self, tmp_path, monkeypatch):
        manifest = self._build_manifest(tmp_path, monkeypatch)
        valid_scopes = {"container", "frontend", "both"}
        all_keys = {}
        for f in manifest["features"]:
            for key, spec in f["config"].items():
                assert isinstance(spec, dict), (
                    f"feature {f['name']} config {key} is not a dict"
                )
                for subkey in ("description", "default", "scope"):
                    assert subkey in spec, (
                        f"feature {f['name']} config {key} missing {subkey}"
                    )
                assert spec["scope"] in valid_scopes, (
                    f"feature {f['name']} config {key} has invalid scope "
                    f"{spec['scope']!r}"
                )
                all_keys[key] = spec["scope"]
        # Spot-check the two keys actually declared today.
        assert all_keys == {
            "KLANGK_BOING_SPEED": "frontend",
            "KLANGK_GITHUB_OAUTH_CLIENT_ID": "container",
        }

    def test_defaults_are_default_features_constant(self, tmp_path, monkeypatch):
        """The manifest's defaults list == DEFAULT_FEATURES in
        import_dart_plugins.py — the build-time constant. This is the full
        conceptual default-on set (6 today), a SUPERSET of the default-on Dart
        features (5): the extra name is the TS-only browser-fetch, always-on in
        the workspace image and harmlessly ignored by the frontend's Dart-only
        active-set filter (#1655 asymmetry)."""
        manifest = self._build_manifest(tmp_path, monkeypatch)
        assert manifest["defaults"] == list(import_dart_plugins.DEFAULT_FEATURES)

    def test_dart_defaults_relationship(self, tmp_path, monkeypatch):
        """The defaults list is a SUPERSET of the default-on Dart features
        and excludes dormant ones.

        Today every stock Dart feature is default-on. Soliplex (#1664) is the
        first exception: it's compiled-in (appears in features[]) but dormant
        (NOT in defaults) — operators opt in with KLANGK_FEATURES_ENABLE.
        This is the canonical "compiled-in ⊋ defaults" case from #1655."""
        manifest = self._build_manifest(tmp_path, monkeypatch)
        feature_names = {f["name"] for f in manifest["features"]}
        defaults = set(manifest["defaults"])
        dormant = feature_names - defaults
        # Every Dart feature is either default-on or explicitly dormant.
        assert dormant <= REMOTE_FEATURE_NAMES, (
            f"Unexpected dormant features (not in defaults and not declared "
            f"dormant): {dormant - REMOTE_FEATURE_NAMES}"
        )
        # The default-on Dart features are exactly the stock set (the 5 local
        # Dart plugins — celebrate, beep, boingball, git-credential, bobdobbs).
        default_on_dart = feature_names & defaults
        assert default_on_dart == set(EXPECTED_DART_PLUGINS), (
            f"Default-on Dart features drifted: {sorted(default_on_dart)}"
        )

    def test_container_env_keys_are_declared_container_scope(
        self, tmp_path, monkeypatch
    ):
        """Every container_env_key is declared in some feature's config with
        scope container/both — the bridge Plugins.container_env() depends on."""
        manifest = self._build_manifest(tmp_path, monkeypatch)
        declared_container_keys = set()
        for f in manifest["features"]:
            for key, spec in f["config"].items():
                if spec["scope"] in {"container", "both"}:
                    declared_container_keys.add(key)
        assert set(manifest["container_env_keys"]) <= declared_container_keys, (
            f"container_env_keys names keys not declared with container/both scope: "
            f"{set(manifest['container_env_keys']) - declared_container_keys}"
        )
        # Spot-check the one key actually declared today.
        assert manifest["container_env_keys"] == EXPECTED_CONTAINER_ENV_KEYS


# ────────────────────────────────────────────────────────────────────────────
# Remote (git-sourced) plugin codegen — #1664.
#
# update_plugins.py materializes a git-sourced plugin (soliplex) by cloning +
# copying its tree into the payload dir, stripping .git. The codegen step
# (import_dart_plugins.py) then discovers it exactly like a local plugin:
# `find_plugins()` scans <payload>/<name>/klangk/lib/plugin.dart.
#
# We don't hit the network here. Instead we synthesize a minimal soliplex-
# shaped tree (pubspec.yaml + plugin.dart + package.json) in the payload dir
# — the same shape `fetch_plugin()` would have produced — and verify codegen
# picks it up. This proves the materialize → codegen handoff works for
# remote plugins without coupling CI to the soliplex repo's availability.
# ────────────────────────────────────────────────────────────────────────────


class TestRemotePluginCodegen:
    """A materialized remote plugin is picked up by codegen (#1664)."""

    def _synthesize_soliplex(self, payload_dir):
        """Write a minimal soliplex-shaped tree into the payload dir.

        Mirrors what `update_plugins.py::fetch_plugin()` produces for a real
        clone: <payload>/soliplex/{package.json, klangk/{pubspec.yaml,
        lib/plugin.dart}}. The Dart class name + the package.json's klangk.config
        key match what the real soliplex v0.4 plugin declares.
        """
        plugin_dir = payload_dir / "soliplex"
        dart_dir = plugin_dir / "klangk"
        (dart_dir / "lib").mkdir(parents=True)
        (dart_dir / "lib" / "plugin.dart").write_text(
            "import 'package:klangk_plugin_api/klangk_plugin_api.dart';\n"
            "class SoliplexPlugin extends ToolPlugin {}\n"
        )
        (dart_dir / "pubspec.yaml").write_text("name: klangk_plugin_soliplex\n")
        (plugin_dir / "package.json").write_text(
            '{"name": "@soliplex/klangk-plugin-soliplex", '
            '"version": "0.2.0", '
            '"description": "Soliplex KB plugin", '
            '"klangk": {"config": {"SOLIPLEX_URL": '
            '{"description": "Soliplex RAG API endpoint URL", '
            '"default": "", "scope": "frontend"}}}}'
        )

    def test_remote_plugin_appears_in_features(self, tmp_path, monkeypatch):
        """A materialized remote plugin lands in features.json::features[] with
        its declared config keys (the SOLIPLEX_URL frontend-scope key)."""
        payload = tmp_path / "payload"
        payload.mkdir()
        # Local plugins materialized normally (no network).
        update_plugins.main(["--payload-dir", str(payload), "--local-only"])
        # Soliplex synthesized in the shape fetch_plugin would have produced.
        self._synthesize_soliplex(payload)

        features_json = _run_codegen(payload, tmp_path, monkeypatch)
        import json

        manifest = json.loads(features_json.read_text())
        features = {f["name"]: f for f in manifest["features"]}

        # Soliplex is compiled in.
        assert "soliplex" in features, (
            f"soliplex missing from features[] — codegen didn't pick up the "
            f"synthesized remote-plugin tree. features={sorted(features)}"
        )
        # Its config key is carried through with the frontend scope.
        soliplex_config = features["soliplex"]["config"]
        assert "SOLIPLEX_URL" in soliplex_config, (
            f"SOLIPLEX_URL missing from soliplex config: {soliplex_config}"
        )
        assert soliplex_config["SOLIPLEX_URL"]["scope"] == "frontend"

    def test_remote_plugin_is_dormant(self, tmp_path, monkeypatch):
        """Soliplex is compiled-in but NOT in defaults — the dormant pattern.

        This is the canonical acceptance criterion from #1664: a bare install
        compiles soliplex in (so an operator can opt in) but doesn't surface
        it. KLANGK_FEATURES_ENABLE=soliplex activates it at runtime."""
        payload = tmp_path / "payload"
        payload.mkdir()
        update_plugins.main(["--payload-dir", str(payload), "--local-only"])
        self._synthesize_soliplex(payload)

        features_json = _run_codegen(payload, tmp_path, monkeypatch)
        import json

        manifest = json.loads(features_json.read_text())
        assert "soliplex" not in manifest["defaults"], (
            f"soliplex leaked into defaults — a bare install would surface a "
            f"feature that needs a Soliplex server to be useful. "
            f"defaults={manifest['defaults']}"
        )
        # Defaults stay exactly DEFAULT_FEATURES (the stock 6) — soliplex is
        # dormant, and word-count is now dormant too (#1700), so defaults is no
        # longer == EXPECTED_FEATURE_NAMES.
        assert set(manifest["defaults"]) == set(import_dart_plugins.DEFAULT_FEATURES)

    def test_remote_plugin_dart_aggregator_record(self, tmp_path, monkeypatch):
        """createAllNamedPlugins() emits a (name, plugin) record for soliplex.

        The runtime's active-set filter (main.dart) gates on the `name` field
        matching KLANGK_FEATURES_ENABLE; the record must exist for the filter
        to find it when an operator opts in."""
        payload = tmp_path / "payload"
        payload.mkdir()
        update_plugins.main(["--payload-dir", str(payload), "--local-only"])
        self._synthesize_soliplex(payload)

        _run_codegen(payload, tmp_path, monkeypatch)
        dart_file = payload / ".dart" / "lib" / "klangk_plugins.dart"
        source = dart_file.read_text()

        # The aggregator imports the synthesized package + instantiates the
        # class, and emits the (name: 'soliplex', plugin: SoliplexPlugin())
        # record the runtime filter depends on.
        assert "import 'package:klangk_plugin_soliplex/plugin.dart';" in source
        assert "(name: 'soliplex', plugin: SoliplexPlugin())," in source, (
            f"soliplex record missing from createAllNamedPlugins — the "
            f"runtime active-set filter won't find it when an operator sets "
            f"KLANGK_FEATURES_ENABLE=soliplex. source:\n{source}"
        )
