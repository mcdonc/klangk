"""Tests for features module (feature-manifest model, #1655).

The runtime no longer scans ``KLANGK_PLUGINS_DIR`` for per-feature
``package.json`` files — that presumed materialized source trees on the
klangkd host, which pip/uv installs never have. Instead the build emits one
``features.json`` into the frontend bundle dir, and ``Features`` reads it at
construction. These tests cover the new model: manifest parsing, the
container-env key bridge, frontend-scope config values, the feature list for
``/api/version``, and the ``features_enable`` knob forwarding.
"""

import json
import types

import pytest

from klangk import features
from _helpers import make_settings


def _write_manifest(frontend_dir, manifest):
    """Write features.json at <frontend_dir>/features.json (#1655)."""
    frontend_dir.mkdir(parents=True, exist_ok=True)
    (frontend_dir / "features.json").write_text(json.dumps(manifest))


def _features(frontend_dir, env=None):
    """Build a fresh Features instance whose frontend_dir is *frontend_dir*."""
    settings_env = {"KLANGK_FRONTEND_DIR": str(frontend_dir)}
    if env:
        settings_env.update(env)
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings(settings_env))
    )
    return features.Features(app_state)


class TestFeatureList:
    """feature_list() backs /api/version's `features` field — the full set of
    features possible to use on this install (regardless of activation)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        # No features.json at the frontend dir → empty feature list.
        p = _features(tmp_path)
        assert p.feature_list() == []

    def test_missing_manifest_file(self, tmp_path):
        p = _features(tmp_path / "nonexistent")
        assert p.feature_list() == []

    def test_returns_metadata(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "celebrate",
                        "version": "1.0.0",
                        "description": "A feature",
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        result = p.feature_list()
        assert result == [
            {
                "name": "celebrate",
                "version": "1.0.0",
                "description": "A feature",
            }
        ]

    def test_missing_fields_default_empty(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [{"name": "minimal"}],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.feature_list() == [
            {"name": "minimal", "version": "", "description": ""}
        ]

    def test_invalid_json_returns_empty(self, tmp_path):
        (tmp_path / "features.json").write_text("not json")
        p = _features(tmp_path)
        assert p.feature_list() == []

    def test_non_dict_manifest_returns_empty(self, tmp_path):
        (tmp_path / "features.json").write_text('["not", "a", "dict"]')
        p = _features(tmp_path)
        assert p.feature_list() == []

    def test_non_dict_feature_entry_skipped(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": ["not-a-dict", {"name": "ok"}],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.feature_list() == [
            {"name": "ok", "version": "", "description": ""}
        ]


class TestContainerEnv:
    """container_env() reads the build-emitted container_env_keys list and
    resolves each from the server env (the bridge into workspace containers)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        p = _features(tmp_path)
        assert p.container_env() == {}

    def test_no_keys_returns_empty(self, tmp_path):
        _write_manifest(
            tmp_path,
            {"features": [], "defaults": [], "container_env_keys": []},
        )
        p = _features(tmp_path)
        assert p.container_env() == {}

    def test_resolves_keys_from_env(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": [
                    "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID"
                ],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID", "abc123")
        p = _features(tmp_path)
        assert p.container_env() == {
            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc123"
        }

    def test_unset_key_resolves_empty(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_UNSET_KEY"],
            },
        )
        monkeypatch.delenv("KLANGK_FEATURE_UNSET_KEY", raising=False)
        p = _features(tmp_path)
        # No default carried in the key-list (only the names); unresolved → "".
        assert p.container_env() == {"KLANGK_FEATURE_UNSET_KEY": ""}

    def test_multiple_keys(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": [
                    "KLANGK_FEATURE_A_KEY",
                    "KLANGK_FEATURE_B_KEY",
                ],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_A_KEY", "a-val")
        monkeypatch.setenv("KLANGK_FEATURE_B_KEY", "b-val")
        p = _features(tmp_path)
        assert p.container_env() == {
            "KLANGK_FEATURE_A_KEY": "a-val",
            "KLANGK_FEATURE_B_KEY": "b-val",
        }

    def test_non_string_key_skipped(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_OK_KEY", 42, None],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_OK_KEY", "ok")
        p = _features(tmp_path)
        assert p.container_env() == {"KLANGK_FEATURE_OK_KEY": "ok"}


class TestContainerEnvPrefixGuard:
    """container_env() refuses to resolve keys without the KLANGK_FEATURE_
    prefix, even if a stale or buggy manifest lists them (#1662). The build
    layer refuses to emit them; this runtime guard is belt-and-suspenders
    against an older manifest shipping with a newer server. The prefix is
    the whole protection — every server setting is KLANGK_<SETTING> (no
    FEATURE_ infix), so KLANGK_FEATURE_* can never collide with one."""

    def test_server_secret_key_skipped(self, tmp_path, monkeypatch):
        # KLANGK_JWT_SECRET is the canonical example — a server secret that
        # lacks the FEATURE_ infix and so must never leak into a container.
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": [
                    "KLANGK_JWT_SECRET",
                    "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID",
                ],
            },
        )
        monkeypatch.setenv("KLANGK_JWT_SECRET", "server-secret-do-not-leak")
        monkeypatch.setenv("KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID", "abc")
        p = _features(tmp_path)
        # Unprefixed server key dropped; prefixed-bearing feature key resolves.
        assert p.container_env() == {
            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc"
        }

    @pytest.mark.parametrize(
        "key",
        [
            "KLANGK_JWT_SECRET",  # server secret — no FEATURE_ infix
            "KLANGK_DATA_DIR",  # server path
            "KLANGK_SOCKET",  # server infra
            "KLANGK_BOING_SPEED",  # the OLD pre-FEATURE_ feature name
            "PATH",  # generic env poison
            "HOME",
            "LD_PRELOAD",
            "PYTHONPATH",
            "MADE_UP_KEY",  # any non-prefixed key
        ],
    )
    def test_each_non_prefixed_key_skipped(self, tmp_path, monkeypatch, key):
        # The prefix rule is structural — it catches server secrets, generic
        # env poison, AND the old pre-FEATURE_ feature names in one check,
        # with no denylist to maintain.
        _write_manifest(
            tmp_path,
            {"features": [], "defaults": [], "container_env_keys": [key]},
        )
        monkeypatch.setenv(key, "leaked-value")
        p = _features(tmp_path)
        assert p.container_env() == {}

    def test_non_prefixed_key_logs_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        # The skip is visible at warning level so a misbuilt manifest is
        # diagnosable without crashing a running server.
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_JWT_SECRET"],
            },
        )
        monkeypatch.setenv("KLANGK_JWT_SECRET", "x")
        p = _features(tmp_path)
        with caplog.at_level("WARNING", logger="klangk.features"):
            assert p.container_env() == {}
        assert any(
            "KLANGK_JWT_SECRET" in r.message and "KLANGK_FEATURE_" in r.message
            for r in caplog.records
        )


class TestManifestSizeCap:
    """_read_manifest caps file size as defense-in-depth against a buggy
    build emitting a runaway features.json (#1662). Oversize → empty dict,
    same degradation as a missing/bad manifest."""

    def test_oversize_manifest_treated_as_empty(self, tmp_path):
        # Write a manifest > 1MB (mostly padding). Reading it must not parse —
        # degrades to empty feature/env lists.
        import klangk.features as features_mod

        cap = features_mod._MAX_MANIFEST_BYTES
        manifest = {
            "features": [{"name": "x", "version": "1.0.0", "description": ""}],
            "defaults": [],
            "container_env_keys": ["KLANGK_FEATURE_X_KEY"],
        }
        body = json.dumps(manifest)
        padding = "x" * (cap - len(body) + 1024)
        manifest["features"][0]["description"] = padding
        (tmp_path / "features.json").write_text(json.dumps(manifest))
        assert (tmp_path / "features.json").stat().st_size > cap
        p = _features(tmp_path)
        assert p.feature_list() == []
        assert p.container_env() == {}

    def test_normal_manifest_under_cap_unaffected(self, tmp_path):
        # A normal (~1KB) manifest parses fine — the cap doesn't trip.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {"name": "ok", "version": "1.0.0", "description": ""}
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.feature_list() == [
            {"name": "ok", "version": "1.0.0", "description": ""}
        ]


class TestPrefixHelper:
    """Direct unit tests for is_valid_container_env_key — the prefix check
    shared between the runtime resolver (here) and the build emitter
    (import_dart_features.py). No denylist: the prefix alone is the contract."""

    def test_prefixed_key_passes(self):
        from klangk.features import is_valid_container_env_key

        assert is_valid_container_env_key(
            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID"
        )
        assert is_valid_container_env_key("KLANGK_FEATURE_BOING_SPEED")
        assert is_valid_container_env_key("KLANGK_FEATURE_ANY_TOKEN")

    @pytest.mark.parametrize(
        "key",
        [
            "KLANGK_JWT_SECRET",  # server secret — no FEATURE_ infix
            "KLANGK_DATA_DIR",  # server path
            "KLANGK_BOING_SPEED",  # the OLD pre-FEATURE_ feature name
            "PATH",  # generic env poison
            "HOME",
            "LD_PRELOAD",
            "MADE_UP_KEY",  # any non-prefixed key
        ],
    )
    def test_non_prefixed_key_rejected(self, key):
        from klangk.features import is_valid_container_env_key

        assert not is_valid_container_env_key(key)


class TestSettingsCollisionInvariant:
    """The prefix rule is the whole security boundary, and it rests on a
    convention: no ``KlangkSettings`` field's env-var form starts with
    ``KLANGK_FEATURE_`` (server settings are all ``KLANGK_<SETTING>`` with no
    ``FEATURE_`` infix, so the feature-config namespace ``KLANGK_FEATURE_*``
    can't collide with one). True today, but nothing pins it — a future
    ``feature_default_set`` field (env ``KLANGK_FEATURE_DEFAULT_SET``) would
    silently invert the protection: a feature declaring the same name would
    resolve the server setting's value. This test locks the convention so
    the regression fails here, not in a container-env leak months later.
    (#1662 adversarial review.)"""

    def test_no_settings_field_collides_with_feature_namespace(self):
        from klangk.features import _CONTAINER_ENV_KEY_PREFIX
        from klangk.settings import KlangkSettings

        # KlangkSettings uses env_prefix="KLANGK_" with no per-field aliasing
        # (verified: no field has alias or validation_alias). So every field's
        # env-var name is KLANGK_ + field_name.upper(). If any starts with the
        # feature-config prefix, the prefix rule's collision-free guarantee
        # breaks — either rename the field or revisit the prefix choice.
        colliding = sorted(
            f"KLANGK_{name.upper()}"
            for name in KlangkSettings.model_fields
            if (f"KLANGK_{name.upper()}").startswith(_CONTAINER_ENV_KEY_PREFIX)
            and f"KLANGK_{name.upper()}" != _CONTAINER_ENV_KEY_PREFIX
        )
        # Note the plural: KLANGK_FEATURES_ENABLE (features_enable field) is
        # a near-miss but does NOT start with KLANGK_FEATURE_ — "feature" vs
        # "features". If that ever flips, this assertion catches it.
        assert colliding == [], (
            f"KlangkSettings has fields whose env-var form starts with "
            f"{_CONTAINER_ENV_KEY_PREFIX!r}: {colliding}. The feature-config "
            f"prefix rule assumes no server setting lives under "
            f"{_CONTAINER_ENV_KEY_PREFIX!r} — either rename the field(s) or "
            f"revisit the prefix."
        )

    def test_features_enable_does_not_collide(self):
        # The activation knob (features_enable → KLANGK_FEATURES_ENABLE, plural)
        # is the closest near-miss. Pin it explicitly: a future rename to
        # feature_enable (singular) would collide with KLANGK_FEATURE_.
        from klangk.features import is_valid_container_env_key
        from klangk.settings import KlangkSettings

        assert "features_enable" in KlangkSettings.model_fields
        # The activation env var must NOT pass the feature-key validity check.
        assert not is_valid_container_env_key("KLANGK_FEATURES_ENABLE")


class TestFrontendConfig:
    """frontend_config() resolves frontend/both-scope values from the
    per-feature config blocks (shape from the manifest, values from the env).

    JSON keys are the lowercased **suffix** after ``KLANGK_FEATURE_`` —
    e.g. ``KLANGK_FEATURE_BOING_SPEED`` → ``boing_speed`` (not
    ``klangk_feature_boing_speed``). The prefix is stripped because it's
    the declaration-side namespace, not part of the feature-owned name the
    frontend reads. Keys without the prefix are skipped with a warning
    (#1662 — same rule as container_env, enforced on both surfaces)."""

    def test_no_manifest_returns_empty(self, tmp_path):
        p = _features(tmp_path)
        assert p.frontend_config() == {}

    def test_frontend_scope_value_resolved_stripped_lowercased(
        self, tmp_path, monkeypatch
    ):
        # The renamed soliplex key (post-#1686 vendoring): the declaration
        # carries the full KLANGK_FEATURE_SOLIPLEX_URL prefix; /api/config
        # exposes the lowercased suffix `soliplex_url`.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "soliplex",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_SOLIPLEX_URL": {
                                "description": "RAG endpoint",
                                "default": "",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv(
            "KLANGK_FEATURE_SOLIPLEX_URL", "https://rag.example.com"
        )
        p = _features(tmp_path)
        assert p.frontend_config() == {
            "soliplex_url": "https://rag.example.com"
        }

    def test_boingball_key_stripped_to_boing_speed(
        self, tmp_path, monkeypatch
    ):
        # The canonical boingball example: KLANGK_FEATURE_BOING_SPEED →
        # `boing_speed` (the lowercased suffix). The Dart feature reads
        # data['boing_speed'] (was data['klangk_boing_speed'] pre-#1662).
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "boingball",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_BOING_SPEED": {
                                "description": "speed",
                                "default": "1.0",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_BOING_SPEED", "2.5")
        p = _features(tmp_path)
        assert p.frontend_config() == {"boing_speed": "2.5"}

    def test_both_scope_appears_in_frontend_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "shared",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_SHARED_URL": {
                                "description": "",
                                "default": "http://default",
                                "scope": "both",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_SHARED_URL", "http://real")
        p = _features(tmp_path)
        assert p.frontend_config() == {"shared_url": "http://real"}

    def test_container_only_scope_excluded_from_frontend_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "git-credential",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": {
                                "description": "",
                                "default": "",
                                "scope": "container",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [
                    "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID"
                ],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID", "abc")
        p = _features(tmp_path)
        # container-only: in container_env, NOT in frontend_config.
        assert p.frontend_config() == {}
        assert p.container_env() == {
            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc"
        }

    def test_default_used_when_env_unset(self, tmp_path, monkeypatch):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "f",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_MY_KEY": {
                                "description": "",
                                "default": "fallback",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.delenv("KLANGK_FEATURE_MY_KEY", raising=False)
        p = _features(tmp_path)
        assert p.frontend_config() == {"my_key": "fallback"}

    def test_non_dict_feature_entry_skipped_in_frontend_config(self, tmp_path):
        # A feature entry that isn't a dict is skipped in frontend_config()
        # too (the guard mirrors feature_list's). Covers the type-safety path.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    "not-a-dict",
                    {
                        "name": "ok",
                        "config": {
                            "KLANGK_FEATURE_OK_KEY": {
                                "default": "v",
                                "scope": "frontend",
                            }
                        },
                    },
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.frontend_config() == {"ok_key": "v"}

    def test_non_dict_config_block_ignored(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {"name": "bad", "config": "nope"},
                    {
                        "name": "ok",
                        "config": {
                            "KLANGK_FEATURE_OK_KEY": {
                                "default": "v",
                                "scope": "frontend",
                            }
                        },
                    },
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        # bad feature's non-dict config is skipped; ok feature still resolves.
        assert p.frontend_config() == {"ok_key": "v"}

    def test_non_dict_spec_ignored(self, tmp_path):
        # A config entry whose value isn't a dict (e.g. a bare string) is
        # skipped, not crashed on.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "x",
                        "config": {"KLANGK_FEATURE_BAD_KEY": "not-a-dict"},
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.frontend_config() == {}

    def test_invalid_scope_defaults_to_container(self, tmp_path):
        # Mirrors the build's _CONTAINER_SCOPES defaulting — an unknown scope
        # is neither frontend nor both, so excluded from frontend_config.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "x",
                        "config": {
                            "KLANGK_FEATURE_X_KEY": {
                                "default": "v",
                                "scope": "bogus",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = _features(tmp_path)
        assert p.frontend_config() == {}

    def test_unprefixed_frontend_key_skipped_with_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        # The prefix rule applies to frontend scope too — a stale manifest
        # declaring e.g. the old SOLIPLEX_URL (no KLANGK_FEATURE_) is skipped
        # at runtime, not surfaced to the frontend. Belt-and-suspenders
        # against an older manifest shipping with a newer server.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "stale",
                        "config": {
                            "SOLIPLEX_URL": {
                                "default": "v",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv("SOLIPLEX_URL", "stale-value")
        p = _features(tmp_path)
        with caplog.at_level("WARNING", logger="klangk.features"):
            assert p.frontend_config() == {}
        assert any(
            "SOLIPLEX_URL" in r.message and "KLANGK_FEATURE_" in r.message
            for r in caplog.records
        )


class TestFeaturesConfigSource:
    """container_env() and frontend_config() resolve feature-declared keys
    from the features_config: block of klangkd.yaml when env is unset — the
    "tomorrow" value source #1659 adds. Precedence: env > features_config: >
    feature default; file:/cmd: prefixes on the YAML values are honored."""

    def _features_with_fc(self, frontend_dir, features_config):
        """Build Features whose settings carry a features_config: block."""
        import json

        cfg = frontend_dir / "klangkd.yaml"
        # Emit the block as a JSON-inline mapping (valid YAML) so values with
        # special chars (file:, cmd:, slashes) survive unquoted-and-safe.
        items = "\n".join(
            f"  {k}: {json.dumps(v)}" for k, v in features_config.items()
        )
        cfg.write_text(f"features_config:\n{items}\n")
        settings = make_settings(
            {"KLANGK_FRONTEND_DIR": str(frontend_dir)},
            config_file=str(cfg),
        )
        app_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=settings)
        )
        return features.Features(app_state)

    def test_container_env_resolves_from_features_config(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": [
                    "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID"
                ],
            },
        )
        p = self._features_with_fc(
            tmp_path,
            {"KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc123"},
        )
        assert p.container_env() == {
            "KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID": "abc123"
        }

    def test_container_env_env_wins_over_features_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_OAUTH_ID"],
            },
        )
        monkeypatch.setenv("KLANGK_FEATURE_OAUTH_ID", "from-env")
        p = self._features_with_fc(
            tmp_path, {"KLANGK_FEATURE_OAUTH_ID": "from-yaml"}
        )
        assert p.container_env() == {"KLANGK_FEATURE_OAUTH_ID": "from-env"}

    def test_container_env_features_config_wins_over_empty_default(
        self, tmp_path
    ):
        # container_env's per-key default is "" (empty), so a features_config
        # value must win over it — otherwise the block would be useless for
        # container-scope keys.
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_TOKEN"],
            },
        )
        p = self._features_with_fc(
            tmp_path, {"KLANGK_FEATURE_TOKEN": "token-value"}
        )
        assert p.container_env() == {"KLANGK_FEATURE_TOKEN": "token-value"}

    def test_container_env_file_prefix_in_features_config(self, tmp_path):
        secret = tmp_path / "oauth-secret"
        secret.write_text("file-secret\n")
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_OAUTH_ID"],
            },
        )
        p = self._features_with_fc(
            tmp_path, {"KLANGK_FEATURE_OAUTH_ID": f"file:{secret}"}
        )
        assert p.container_env() == {"KLANGK_FEATURE_OAUTH_ID": "file-secret"}

    def test_frontend_config_resolves_from_features_config(self, tmp_path):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "soliplex",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_SOLIPLEX_URL": {
                                "description": "RAG endpoint",
                                "default": "",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = self._features_with_fc(
            tmp_path,
            {"KLANGK_FEATURE_SOLIPLEX_URL": "https://rag.example.com"},
        )
        assert p.frontend_config() == {
            "soliplex_url": "https://rag.example.com"
        }

    def test_frontend_config_features_config_wins_over_feature_default(
        self, tmp_path
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "boingball",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_BOING_SPEED": {
                                "description": "speed",
                                "default": "1.0",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        p = self._features_with_fc(
            tmp_path, {"KLANGK_FEATURE_BOING_SPEED": "2.5"}
        )
        assert p.frontend_config() == {"boing_speed": "2.5"}

    def test_frontend_config_env_wins_over_features_config(
        self, tmp_path, monkeypatch
    ):
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "soliplex",
                        "version": "1.0.0",
                        "description": "",
                        "config": {
                            "KLANGK_FEATURE_SOLIPLEX_URL": {
                                "description": "",
                                "default": "",
                                "scope": "frontend",
                            }
                        },
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        monkeypatch.setenv(
            "KLANGK_FEATURE_SOLIPLEX_URL", "https://from-env.example.com"
        )
        p = self._features_with_fc(
            tmp_path,
            {"KLANGK_FEATURE_SOLIPLEX_URL": "https://from-yaml.example.com"},
        )
        assert p.frontend_config() == {
            "soliplex_url": "https://from-env.example.com"
        }

    def test_no_features_config_block_preserves_env_only_behavior(
        self, tmp_path, monkeypatch
    ):
        # No block → settings.features_config is None → resolve_dynamic_config
        # gets no second source → pre-#1659 behavior.
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_FEATURE_X"],
            },
        )
        monkeypatch.delenv("KLANGK_FEATURE_X", raising=False)
        p = _features(tmp_path)
        assert p.app.state.settings.features_config is None
        assert p.container_env() == {"KLANGK_FEATURE_X": ""}

    def test_reserved_key_in_features_config_not_injected_into_container(
        self, tmp_path, monkeypatch
    ):
        # Security regression: the KLANGK_FEATURE_ prefix guard runs BEFORE
        # resolution, so a reserved/non-prefixed key sitting inside a
        # features_config: block must never reach a workspace container —
        # even if the manifest (or a misbuilt one) listed it. The guard is
        # structural today (container_env iterates manifest keys, not
        # features_config.items()), but lock the property in so a future
        # refactor that injected features_config directly can't silently
        # reintroduce the leak (#1662 defense-in-depth).
        _write_manifest(
            tmp_path,
            {
                "features": [],
                "defaults": [],
                "container_env_keys": ["KLANGK_JWT_SECRET"],
            },
        )
        monkeypatch.delenv("KLANGK_JWT_SECRET", raising=False)
        p = self._features_with_fc(
            tmp_path, {"KLANGK_JWT_SECRET": "pwned-by-config"}
        )
        # The block DID load the value onto settings.features_config...
        assert p.app.state.settings.features_config == {
            "KLANGK_JWT_SECRET": "pwned-by-config"
        }
        # ...but container_env refuses to resolve it (prefix guard), so it
        # never reaches the container env dict.
        assert "KLANGK_JWT_SECRET" not in p.container_env()


class TestFeaturesEnable:
    """features_enable() forwards the KLANGK_FEATURES_ENABLE setting verbatim
    (the deploy's chosen active-feature list — canonical semantics, #1655)."""

    def test_unset_returns_none(self, tmp_path):
        p = _features(tmp_path)
        assert p.features_enable() is None

    def test_explicit_value_forwarded_verbatim(self, tmp_path):
        p = _features(
            tmp_path, env={"KLANGK_FEATURES_ENABLE": "celebrate,beep,soliplex"}
        )
        assert p.features_enable() == "celebrate,beep,soliplex"

    def test_single_value(self, tmp_path):
        p = _features(tmp_path, env={"KLANGK_FEATURES_ENABLE": "soliplex"})
        assert p.features_enable() == "soliplex"


class TestReconfigure:
    """reconfigure() re-reads the manifest on a SIGHUP settings reload
    (frontend_dir may have changed)."""

    def test_reconfigure_picks_up_new_manifest(self, tmp_path):
        # Start with no manifest → empty feature list.
        p = _features(tmp_path)
        assert p.feature_list() == []

        # Write a manifest, build a new app_state pointing at the same dir,
        # reconfigure → feature list reflects the new manifest.
        _write_manifest(
            tmp_path,
            {
                "features": [
                    {
                        "name": "new-feature",
                        "version": "1.0.0",
                        "description": "",
                    }
                ],
                "defaults": [],
                "container_env_keys": [],
            },
        )
        new_app_state = types.SimpleNamespace(
            state=types.SimpleNamespace(
                settings=make_settings({"KLANGK_FRONTEND_DIR": str(tmp_path)})
            )
        )
        p.reconfigure(new_app_state)
        assert p.feature_list() == [
            {"name": "new-feature", "version": "1.0.0", "description": ""}
        ]
