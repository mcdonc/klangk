"""Build-layer tests for the KLANGK_FEATURE_ prefix guard in
import_dart_plugins.py (#1662).

The build emitter refuses to write a ``klangk.config`` key into
``features.json`` when the key doesn't start with ``KLANGK_FEATURE_``. That
prefix is the plugin-config namespace: every server setting is
``KLANGK_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone keeps
plugin-declared keys from ever colliding with a server secret / path / infra
field — no reserved set / denylist needed. Generic env poison (``PATH``,
``HOME``, ``LD_PRELOAD``, …) is rejected by the same rule.

A violation raises :class:`InvalidFeatureConfigKey` (a ``RuntimeError``, not
a ``ValueError`` — it must escape the per-plugin
``except (JSONDecodeError, ValueError, OSError)`` that swallows malformed
package.json parse errors) naming the plugin + key so the plugin author
fixes the declaration before ship.

The runtime resolver in ``klangk.plugins`` enforces the same prefix on read;
those tests live in ``src/klangk/klangkd-tests/tests/test_plugins.py``.

This file mirrors ``_CONTAINER_ENV_KEY_PREFIX`` against the runtime copy in
``klangk.plugins`` to catch drift between the two (the build script
deliberately does not import ``klangk``, #1666 — duplication is intentional
and this test is the drift detector).
"""

import json
import os
import sys

import pytest

# Make the scripts dir importable (matches the pattern in
# test_build_pipeline.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import import_dart_plugins  # noqa: E402


# Convenience alias — the build script raises this distinct type (a
# RuntimeError, not a ValueError) so it escapes the per-plugin
# ``except (JSONDecodeError, ValueError, OSError)`` and aborts the build.
_BadKeyError = import_dart_plugins.InvalidFeatureConfigKey


def _make_plugin(plugins_dir, name, config):
    """Write a minimal plugin tree with the given klangk.config block.

    ``config`` is the ``klangk.config`` dict written verbatim into
    ``package.json`` — each entry's ``scope`` controls whether the key is
    a candidate for ``container_env_keys``.
    """
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "package.json").write_text(
        json.dumps(
            {
                "name": f"@test/{name}",
                "version": "1.0.0",
                "description": "test plugin",
                "klangk": {"config": config},
            }
        )
    )
    return {"name": name, "path": str(plugin_dir)}


class TestPrefixGuard:
    """A klangk.config key without the KLANGK_FEATURE_ prefix fails the build.

    Applies to every scope (container, frontend, both) — the declaration-side
    rule is uniform; how the value is surfaced to consumers differs (container
    env keeps the full name; /api/config strips + lowercases).
    """

    def test_unprefixed_klangk_key_raises(self, tmp_path):
        # KLANGK_JWT_SECRET looks superficially namespaced but lacks FEATURE_.
        # This is the canonical "would leak a server secret" example — the
        # prefix rule alone catches it without any denylist.
        p = _make_plugin(
            tmp_path,
            "leaky",
            {"KLANGK_JWT_SECRET": {"scope": "container", "default": ""}},
        )
        with pytest.raises(_BadKeyError) as ei:
            import_dart_plugins.collect_feature_metadata([p], str(tmp_path))
        msg = str(ei.value)
        assert "leaky" in msg
        assert "KLANGK_JWT_SECRET" in msg
        assert "KLANGK_FEATURE_" in msg

    def test_bare_unprefixed_key_raises(self, tmp_path):
        # SOLIPLEX_URL (no KLANGK_ at all) — the pre-vendoring soliplex
        # declaration form. Same failure; the rule is "must start with
        # KLANGK_FEATURE_", nothing narrower.
        p = _make_plugin(
            tmp_path,
            "soliplex-old",
            {"SOLIPLEX_URL": {"scope": "frontend", "default": ""}},
        )
        with pytest.raises(_BadKeyError) as ei:
            import_dart_plugins.collect_feature_metadata([p], str(tmp_path))
        assert "SOLIPLEX_URL" in str(ei.value)

    @pytest.mark.parametrize(
        "key",
        [
            "PATH",
            "HOME",
            "USER",
            "LD_PRELOAD",
            "PYTHONPATH",
            "NODE_PATH",
            "KLANGK_JWT_SECRET",
            "KLANGK_DATA_DIR",
            "KLANGK_SOCKET",
            "KLANGK_BOING_SPEED",  # the OLD pre-FEATURE_ name — must rename
        ],
    )
    def test_each_non_prefixed_key_raises(self, tmp_path, key):
        p = _make_plugin(tmp_path, "p", {key: {"scope": "container", "default": ""}})
        with pytest.raises(_BadKeyError) as ei:
            import_dart_plugins.collect_feature_metadata([p], str(tmp_path))
        assert key in str(ei.value)

    @pytest.mark.parametrize("scope", ["container", "frontend", "both"])
    def test_guard_fires_for_every_scope(self, tmp_path, scope):
        # The prefix rule applies to every scope — a frontend-scope leak is
        # just as much a build error as a container-scope one.
        p = _make_plugin(
            tmp_path,
            "p",
            {"KLANGK_JWT_SECRET": {"scope": scope, "default": ""}},
        )
        with pytest.raises(_BadKeyError):
            import_dart_plugins.collect_feature_metadata([p], str(tmp_path))


class TestPrefixedKeysStillEmit:
    """The intended pattern (KLANGK_FEATURE_<NAME>) still emits — the guard
    doesn't overreach."""

    def test_container_scope_emits_to_env_keys(self, tmp_path):
        # The git-credential plugin's renamed key
        # (KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID, scope=container) is the
        # canonical 'good' container example.
        p = _make_plugin(
            tmp_path,
            "git-credential",
            {
                "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": {
                    "scope": "container",
                    "default": "",
                    "description": "GitHub OAuth client ID",
                }
            },
        )
        features, env_keys = import_dart_plugins.collect_feature_metadata(
            [p], str(tmp_path)
        )
        assert env_keys == ["KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID"]
        assert features[0]["name"] == "git-credential"
        # The full prefixed key is carried into the feature's config block
        # too — the runtime resolver strips + lowercases for /api/config.
        assert "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID" in features[0]["config"]

    def test_frontend_scope_emits_to_config_block(self, tmp_path):
        # The boingball plugin's renamed key (KLANGK_FEATURE_BOING_SPEED,
        # scope=frontend) is the canonical 'good' frontend example.
        p = _make_plugin(
            tmp_path,
            "boingball",
            {
                "KLANGK_FEATURE_BOING_SPEED": {
                    "scope": "frontend",
                    "default": "1.0",
                    "description": "Animation speed",
                }
            },
        )
        features, env_keys = import_dart_plugins.collect_feature_metadata(
            [p], str(tmp_path)
        )
        # Frontend-scope keys don't enter container_env_keys.
        assert env_keys == []
        assert "KLANGK_FEATURE_BOING_SPEED" in features[0]["config"]

    def test_both_scope_emits_to_both_surfaces(self, tmp_path):
        p = _make_plugin(
            tmp_path,
            "shared",
            {"KLANGK_FEATURE_SHARED_URL": {"scope": "both", "default": ""}},
        )
        features, env_keys = import_dart_plugins.collect_feature_metadata(
            [p], str(tmp_path)
        )
        assert env_keys == ["KLANGK_FEATURE_SHARED_URL"]
        assert "KLANGK_FEATURE_SHARED_URL" in features[0]["config"]


class TestPrefixConstantDrift:
    """The build script's prefix constant must match the runtime copy in
    klangk.plugins — the two are deliberately duplicated (the build script
    doesn't import klangk, #1666), so this test is the drift detector."""

    def test_prefix_constants_match(self):
        # Import the runtime copy. The klangk package is installed in the
        # pytest venv that runs the scripts/tests suite (CI runs this after
        # the package wheel is built), so this import is safe here even
        # though import_dart_plugins itself must not take it.
        from klangk.plugins import _CONTAINER_ENV_KEY_PREFIX

        assert (
            import_dart_plugins._CONTAINER_ENV_KEY_PREFIX
            == _CONTAINER_ENV_KEY_PREFIX
            == "KLANGK_FEATURE_"
        )
