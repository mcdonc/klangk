"""Build-pipeline integration test (#1666).

Exercises the real build pipeline — ``update_features.py`` →
``import_dart_features.py`` — against the **real** checked-in ``features.yaml``
and the real ``features/`` source trees, then asserts on the outputs. This is
the test the #1665 adversarial review flagged as missing: the runtime side
(``Features`` reading ``features.json``) is well-covered by ``test_features.py``,
but the build side — the code #1660/#1665 changed — had only isolated unit
tests per script.

What this catches that the per-script unit tests don't:

- A ``path:`` entry in ``features.yaml`` pointing at a missing dir.
- A feature whose ``klangk/lib/feature.dart`` lost its ``ToolPlugin`` subclass.
- Drift between the checked-in declaration and the feature source trees.
- A generated Dart aggregator that references a class that doesn't exist.
- A ``features.json`` whose shape the runtime ``Features._read_manifest()``
  would reject (the manifest contract — see ``test_manifest_contract`` below).
- A feature accidentally shipping without (or gaining) a ``klangk/`` Dart dir
  — the on-disk-vs-Dart-feature asymmetry flipping silently.

Runs in ~1s (no flutter, no docker). The scripts tests are standalone — no
``klangk`` import — so this file mirrors the manifest shape contract inline
rather than importing ``klangk.features``; ``test_features.py`` covers the
runtime side with synthetic manifests, this file covers the build side with
the real one.
"""

import os
import re
import sys

# Make sure the scripts directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_dart_features
import update_features


# ────────────────────────────────────────────────────────────────────────────
# The contract: what the checked-in declaration + feature source trees should
# produce today. Hard-coded so a drift is loud. Update these when you
# intentionally add/remove a feature or change which ones ship Dart packages.
# ────────────────────────────────────────────────────────────────────────────

EXPECTED_FEATURE_NAMES = {
    "celebrate",
    "beep",
    "bobdobbs",
    "word-count",
    "browser-fetch",
    "boingball",
    "git-credential",
    "soliplex",  # vendored local in #1686 (was a remote git: entry in #1664)
}

# Compiled-in Dart features that are NOT in DEFAULT_FEATURES — dormant unless
# an operator opts in via KLANGKD_FEATURES_ENABLE. Soliplex (#1664, vendored
# local in #1686) is the canonical "compiled-in ⊋ defaults" case.
DORMANT_FEATURE_NAMES = {"soliplex"}

# Features with a klangk/ Dart package → class names emitted into the
# generated aggregator. Features without klangk/ (word-count, browser-fetch)
# are TS-only and must NOT appear in the Dart aggregator.
EXPECTED_DART_FEATURES = {
    "celebrate": "CelebrateFeature",
    "beep": "BeepFeature",
    "bobdobbs": "BobDobbsFeature",
    "boingball": "BoingBallFeature",
    "git-credential": "GitCredentialFeature",
    "soliplex": "SoliplexFeature",
}

# The subset that appears in features.json's features[] list. import_dart_features
# only carries features with a klangk/ Dart package (the frontend-activatable
# set). TS-only features (word-count, browser-fetch) are baked into
# the workspace image and always-on — they never appear in features.json.
# This is the wheel/workspace activation asymmetry from #1655.
EXPECTED_DART_FEATURE_NAMES = set(EXPECTED_DART_FEATURES)

# Config keys declared across all feature package.json files, by scope.
# All carry the KLANGKWS_FEATURE_ prefix (the feature-config namespace, #1662):
# server settings are KLANGKD_<SETTING> (no FEATURE_ infix), so the prefix
# alone keeps feature keys from colliding with server secrets/paths/infra.
# Soliplex's KLANGKWS_FEATURE_SOLIPLEX_URL was renamed from SOLIPLEX_URL when it
# was vendored (#1686) — the build guard from #1662 requires the prefix.
EXPECTED_CONTAINER_ENV_KEYS = ["KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID"]


def _run_codegen(payload_dir, tmp_path, monkeypatch):
    """Run import_dart_features.main() with outputs redirected into tmp_path.

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
    monkeypatch.setattr(import_dart_features, "FEATURES_JSON", str(features_json))
    monkeypatch.setattr(import_dart_features, "ROOT", str(tmp_path))
    import_dart_features.main(["--payload-dir", str(payload_dir)])
    return features_json


def _materialize(payload_dir):
    """Run update_features.main() against the real checked-in features.yaml.

    ``--local-only`` skips any git-sourced feature so the test never hits the
    network — there are none declared today (soliplex was vendored local in
    #1686), so the flag is a no-op safety net. The git-skip path itself is
    covered by ``test_update_features.py::TestLocalOnlyFlag``.
    """
    rc = update_features.main(["--payload-dir", str(payload_dir), "--local-only"])
    assert rc == 0, "update_features.py failed against the real features.yaml"


# ────────────────────────────────────────────────────────────────────────────
# Test 1: the build pipeline runs clean against the real declaration.
# ────────────────────────────────────────────────────────────────────────────


class TestPipelineRuns:
    """update_features + import_dart_features succeed against the real repo."""

    def test_update_features_materializes_all_declared(self, tmp_path):
        payload = tmp_path / "payload"
        payload.mkdir()
        # --local-only skips any git-sourced feature so the test doesn't hit
        # the network. No git entries are declared today (soliplex was
        # vendored local in #1686), so every declared feature materializes.
        rc = update_features.main(["--payload-dir", str(payload), "--local-only"])
        assert rc == 0

        # Every declared feature is symlinked into the payload dir. Filter to
        # directories — features.lock (a file) also lives there.
        materialized = {
            p
            for p in os.listdir(payload)
            if (payload / p).is_dir() and not p.startswith(".")
        }
        assert materialized == EXPECTED_FEATURE_NAMES, (
            f"materialized set != declared set — drift in features.yaml "
            f"or features/. materialized={sorted(materialized)}"
        )

        # features.lock lists EVERY declared feature.
        import yaml

        lock = yaml.safe_load((payload / "features.lock").read_text())
        lock_names = {e["name"] for e in lock["features"]}
        assert lock_names == EXPECTED_FEATURE_NAMES, (
            f"features.lock names != all declared: {sorted(lock_names)}"
        )

    def test_import_dart_features_generates_aggregator(self, tmp_path, monkeypatch):
        payload = tmp_path / "payload"
        payload.mkdir()
        _materialize(payload)
        _run_codegen(payload, tmp_path, monkeypatch)

        # The aggregator is at <payload>/.dart/lib/klangk_features.dart.
        dart_file = payload / ".dart" / "lib" / "klangk_features.dart"
        assert dart_file.is_file(), "klangk_features.dart was not generated"
        source = dart_file.read_text()

        # Every Dart-bearing feature's class is imported + instantiated.
        for name, cls in EXPECTED_DART_FEATURES.items():
            pkg = f"klangk_feature_{name.replace('-', '_')}"
            assert f"import 'package:{pkg}/feature.dart';" in source, (
                f"{pkg} not imported by the aggregator"
            )
            assert f"{cls}()" in source, (
                f"{cls}() not instantiated in createAllFeatures/createAllNamedFeatures"
            )

        # Features WITHOUT a klangk/ dir must not appear in the Dart aggregator.
        non_dart = EXPECTED_FEATURE_NAMES - set(EXPECTED_DART_FEATURES)
        for name in non_dart:
            pkg = f"klangk_feature_{name.replace('-', '_')}"
            assert f"import 'package:{pkg}/" not in source, (
                f"{name} has no klangk/ dir but leaked into the Dart aggregator"
            )

    def test_named_aggregator_names_match_feature_names(self, tmp_path, monkeypatch):
        """createAllNamedFeatures() emits records whose `name` matches the
        feature name in features.yaml — the link the runtime's active-set
        filter in main.dart depends on (#1655)."""
        payload = tmp_path / "payload"
        payload.mkdir()
        _materialize(payload)
        _run_codegen(payload, tmp_path, monkeypatch)

        dart_file = payload / ".dart" / "lib" / "klangk_features.dart"
        source = dart_file.read_text()

        # Extract (name: '...', feature: ...) records from createAllNamedFeatures.
        # The generator emits lines like:    (name: 'celebrate', feature: CelebrateFeature()),
        named = re.findall(r"\(name:\s*'([^']+)',\s*feature:\s*(\w+)\(\)\)", source)
        named_map = dict(named)

        # Every Dart-bearing feature appears with the exact feature name.
        assert set(named_map) == set(EXPECTED_DART_FEATURES), (
            f"named-feature names don't match Dart feature set: {sorted(named_map)}"
        )
        for name, cls in EXPECTED_DART_FEATURES.items():
            assert named_map[name] == cls


# ────────────────────────────────────────────────────────────────────────────
# Test 2: the manifest contract — features.json has the shape the runtime
# Features._read_manifest() expects. Mirrors the validation in
# src/klangk/klangk/features.py; if the runtime's expectations change, both
# this test (real manifest) and test_features.py (synthetic) must update.
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
        # features[] carries only Dart features — TS-only features are absent
        # (wheel/workspace activation asymmetry, #1655).
        assert feature_names == EXPECTED_DART_FEATURE_NAMES, (
            f"features[] names drifted from the Dart feature set: "
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
        # Spot-check the three keys actually declared today.
        assert all_keys == {
            "KLANGKWS_FEATURE_BOING_SPEED": "frontend",
            "KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID": "container",
            "KLANGKWS_FEATURE_SOLIPLEX_URL": "frontend",
        }

    def test_defaults_are_default_features_constant(self, tmp_path, monkeypatch):
        """The manifest's defaults list == DEFAULT_FEATURES in
        import_dart_features.py — the build-time constant. This is the full
        conceptual default-on set (6 today), a SUPERSET of the default-on Dart
        features (5): the extra name is the TS-only browser-fetch, always-on in
        the workspace image and harmlessly ignored by the frontend's Dart-only
        active-set filter (#1655 asymmetry)."""
        manifest = self._build_manifest(tmp_path, monkeypatch)
        assert manifest["defaults"] == list(import_dart_features.DEFAULT_FEATURES)

    def test_dart_defaults_relationship(self, tmp_path, monkeypatch):
        """The defaults list is a SUPERSET of the default-on Dart features
        and excludes dormant ones.

        Every stock Dart feature is default-on except soliplex (#1664,
        vendored local in #1686): it's compiled-in (appears in features[])
        but dormant (NOT in defaults) — operators opt in with
        KLANGKD_FEATURES_ENABLE. This is the canonical "compiled-in ⊋
        defaults" case from #1655."""
        manifest = self._build_manifest(tmp_path, monkeypatch)
        feature_names = {f["name"] for f in manifest["features"]}
        defaults = set(manifest["defaults"])
        dormant = feature_names - defaults
        # Soliplex is the only dormant Dart feature today.
        assert dormant == DORMANT_FEATURE_NAMES, (
            f"Dormant Dart features drifted (expected only "
            f"{sorted(DORMANT_FEATURE_NAMES)}): {sorted(dormant)}"
        )
        # The default-on Dart features are the stock set minus the dormant one.
        default_on_dart = feature_names & defaults
        assert default_on_dart == set(EXPECTED_DART_FEATURES) - DORMANT_FEATURE_NAMES, (
            f"Default-on Dart features drifted: {sorted(default_on_dart)}"
        )

    def test_container_env_keys_are_declared_container_scope(
        self, tmp_path, monkeypatch
    ):
        """Every container_env_key is declared in some feature's config with
        scope container/both — the bridge Features.container_env() depends on."""
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
