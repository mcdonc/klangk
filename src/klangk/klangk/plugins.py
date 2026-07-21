"""Feature manifest: read the build-emitted ``features.json`` and bridge the
declared config keys.

The runtime no longer scans ``KLANGK_PLUGINS_DIR`` for per-plugin
``package.json`` files — that presumed materialized source trees on the
``klangkd`` host, which pip/uv installs never have (#1655). Instead the build
(``import_dart_plugins.py``) emits a single ``features.json`` into the frontend
bundle directory (next to ``index.html``), and the frontend reads its sibling
file for per-feature metadata + the default-on set. ``klangkd`` reads **one
field** of that same file — ``container_env_keys`` — to bridge the declared
container-scope env vars into workspace containers; it does not read the
per-feature metadata (the frontend owns that).

``features.json`` shape (emitted by the build)::

    {
      "features": [
        {"name": "celebrate", "version": "1.0.0", "description": "...",
         "config": { "KEY": {"description": "...", "default": "", "scope": "container"|"frontend"|"both"} }},
        ...
      ],
      "defaults": ["celebrate", "beep", ...],
      "container_env_keys": ["KLANGK_FEATURE_GITHUB_OAUTH_CLIENT_ID", ...]
    }

Values for the declared keys are resolved via :func:`resolve_dynamic_config`
(honoring ``file:``/``cmd:`` prefixes — feature config may itself be a
secret). Today the value source is the server's env; a future issue (#1659)
adds a ``features_config:`` block in ``klangkd.yaml`` as an additional source.
"""

import json
import logging
import os

from .settings import resolve_dynamic_config

logger = logging.getLogger(__name__)

# Scopes that make a klangk.config key eligible for the container env bridge
# (injected into workspace containers at create-time). "frontend" only is
# excluded — those go to the UI via /api/config, not into the container env.
# Mirrors _CONTAINER_SCOPES in scripts/import_dart_plugins.py.
_CONTAINER_SCOPES = {"container", "both"}
_FRONTEND_SCOPES = {"frontend", "both"}

# Every klangk.config key a plugin declares for the container env bridge
# (scope container/both) must start with this prefix. Server-side settings
# are all ``KLANGK_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone
# guarantees a plugin can never declare a key that collides with a server
# secret, path, or infra field (``KLANGK_JWT_SECRET``, ``KLANGK_DATA_DIR``, …)
# — no denylist / reserved-set needed, and nothing to keep in sync between
# this file and the build emitter (#1662). Non-KLANGK_ environment poison
# (``PATH``, ``HOME``, ``LD_PRELOAD``, …) is rejected by the same rule.
# Mirrors _CONTAINER_ENV_KEY_PREFIX in scripts/import_dart_plugins.py.
_CONTAINER_ENV_KEY_PREFIX = "KLANGK_FEATURE_"

# Features.json is a build artifact shipped in the wheel — not attacker-
# controlled at runtime — but cap its read size as defense-in-depth against
# a buggy build emitting a runaway structure (#1662). The real manifest is
# ~1KB for 7 features; 1MB is a generous ceiling that still rejects any
# pathological growth.
_MAX_MANIFEST_BYTES = 1024 * 1024


def is_valid_container_env_key(key: str) -> bool:
    """True if *key* is a safe container-env declaration.

    Must start with :data:`_CONTAINER_ENV_KEY_PREFIX` (``KLANGK_FEATURE_``).
    That prefix is the feature-config namespace; every server setting is
    ``KLANGK_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone keeps
    plugin-declared container env vars from ever colliding with a server
    secret / path / infra field — no reserved-set / denylist required (#1662).
    Used by both the runtime resolver (here) and re-implemented by the build
    emitter (``import_dart_plugins.py``).
    """
    return key.startswith(_CONTAINER_ENV_KEY_PREFIX)


class Plugins:
    """Feature manifest reader + config-key bridge.

    Constructed once in :func:`build_app` and stored on ``app.state.plugins``.
    Reads ``features.json`` (sibling of the frontend's ``index.html``) at
    construction; the manifest is a build artifact, so a SIGHUP settings
    reload (which may change ``frontend_dir``) re-reads it via
    :meth:`reconfigure`.

    The class keeps the ``Plugins`` name (and ``app.state.plugins`` slot) for
    continuity with the broader codebase even though the user-facing concept
    is now "feature" (#1655) — the Flutter ``ToolPlugin`` API contract is
    unchanged; only the deploy/runtime activation surface was renamed.
    """

    def __init__(self, app):
        self.app = app
        # Parsed features.json: {features: [...], defaults: [...],
        # container_env_keys: [...]}. Empty when no manifest is present
        # (pre-build source deploy, missing frontend_dir) — every method
        # degrades cleanly to "no features, no env bridge."
        self._manifest = self._read_manifest()

    def reconfigure(self, app) -> None:
        # Re-read on a SIGHUP settings reload (frontend_dir may have changed).
        self.app = app
        self._manifest = self._read_manifest()

    @property
    def _features_path(self) -> str:
        return os.path.join(
            self.app.state.settings.frontend_dir, "features.json"
        )

    def _read_manifest(self) -> dict:
        """Read + parse features.json. Empty dict on any failure (missing
        file, bad JSON, oversize). Callers degrade to empty feature/env lists.

        Size-capped at :data:`_MAX_MANIFEST_BYTES` as defense-in-depth against
        a buggy build emitting a runaway structure (#1662)."""
        path = self._features_path
        try:
            if (
                os.path.isfile(path)
                and os.path.getsize(path) > _MAX_MANIFEST_BYTES
            ):
                logger.warning(
                    "features.json at %s is %d bytes (cap %d) — ignoring "
                    "manifest, degrading to empty feature/env lists",
                    path,
                    os.path.getsize(path),
                    _MAX_MANIFEST_BYTES,
                )
                return {}
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def feature_list(self) -> list[dict[str, str]]:
        """Return metadata for every compiled-in feature (name, version,
        description).

        Backs the ``plugins`` field of ``GET /api/version`` — the full set
        of features possible to use on this install, regardless of whether
        they're active for this deploy (#1655: activation is a frontend
        concern, gated by KLANGK_FEATURES_ENABLE against this list).
        """
        features = self._manifest.get("features", [])
        return [
            {
                "name": f.get("name", ""),
                "version": f.get("version", ""),
                "description": f.get("description", ""),
            }
            for f in features
            if isinstance(f, dict)
        ]

    def container_env(self) -> dict[str, str]:
        """Return env vars to inject into workspace containers.

        The build emits ``container_env_keys`` (every klangk.config key
        declared with scope ``container`` or ``both`` across all compiled-in
        features) into ``features.json``; the server reads that list and
        resolves each key via :func:`resolve_dynamic_config` (so
        ``file:``/``cmd:`` prefixes work for feature secrets). Value
        sources, in descending precedence (#1659): the server's env, then
        the ``features_config:`` block of ``klangkd.yaml`` (long-lived
        deploy config like OAuth client IDs), then the plugin-declared
        default. Env remains the escape hatch for per-invocation overrides.

        Defense-in-depth (#1662): even though the build layer refuses to
        emit reserved/non-KLANGK_ keys, this runtime guard skips them too —
        a stale or older manifest shipping with a newer server must not
        leak ``KLANGK_JWT_SECRET`` etc. into a container. A skipped key is
        logged at warning level so a misbuilt manifest is visible.
        """
        result: dict[str, str] = {}
        features_config = self.app.state.settings.features_config
        for key in self._manifest.get("container_env_keys", []):
            if not isinstance(key, str):
                continue
            if not is_valid_container_env_key(key):
                logger.warning(
                    "features.json container_env_keys lists %r — refusing "
                    "to resolve (missing KLANGK_FEATURE_ prefix); "
                    "skipping. Rebuild with a corrected plugin.",
                    key,
                )
                continue
            result[key] = (
                resolve_dynamic_config(
                    key, "", features_config=features_config
                )
                or ""
            )
        return result

    def frontend_config(self) -> dict[str, str]:
        """Return config entries for the ``GET /api/config`` response.

        Keys are the lowercased **suffix** after ``KLANGK_FEATURE_``
        (e.g. ``KLANGK_FEATURE_BOING_SPEED`` → ``boing_speed``). Declared
        keys that don't carry the ``KLANGK_FEATURE_`` prefix are skipped —
        the prefix is the plugin-config namespace (#1662): it keeps
        plugin-declared config from colliding with server settings
        (``KLANGK_<SETTING>``) and gives the frontend a stable, un-prefixed
        JSON key shape. The shape (which keys exist, descriptions,
        defaults) is read from the per-feature ``config`` blocks in
        ``features.json``; the values are resolved server-side via
        :func:`resolve_dynamic_config` so the frontend doesn't need access
        to klangkd's environment. Value sources, in descending precedence
        (#1659): the server's env, then the ``features_config:`` block of
        ``klangkd.yaml``, then the plugin-declared default.
        """
        result: dict[str, str] = {}
        features_config = self.app.state.settings.features_config
        for feature in self._manifest.get("features", []):
            if not isinstance(feature, dict):
                continue
            config = feature.get("config", {})
            if not isinstance(config, dict):
                continue
            for key, spec in config.items():
                if not isinstance(spec, dict):
                    continue
                scope = spec.get("scope", "container")
                if scope not in _FRONTEND_SCOPES:
                    continue
                if not isinstance(key, str) or not key.startswith(
                    _CONTAINER_ENV_KEY_PREFIX
                ):
                    logger.warning(
                        "features.json frontend-scope config key %r — "
                        "missing KLANGK_FEATURE_ prefix; skipping. Rebuild "
                        "with a corrected plugin.",
                        key,
                    )
                    continue
                default = spec.get("default", "")
                # Strip the KLANGK_FEATURE_ prefix and lowercase the suffix
                # for the JSON key (e.g. KLANGK_FEATURE_BOING_SPEED →
                # boing_speed). The prefix is enforced above; the suffix is
                # the plugin-owned name, surfaced un-prefixed to the frontend.
                json_key = key[len(_CONTAINER_ENV_KEY_PREFIX) :].lower()
                result[json_key] = (
                    resolve_dynamic_config(
                        key, default, features_config=features_config
                    )
                    or ""
                )
        return result

    def features_enable(self) -> str | None:
        """The deploy's chosen active-feature list (``KLANGK_FEATURES_ENABLE``).

        Forwarded verbatim via ``/api/config`` so the frontend can resolve
        the active set against its sibling ``features.json`` (canonical
        semantics: unset → manifest ``defaults``; any explicit value →
        exactly that list). The server does no resolution itself — the
        frontend owns the activation logic (#1655).
        """
        return self.app.state.settings.features_enable
