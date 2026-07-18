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
      "container_env_keys": ["KLANGK_GITHUB_OAUTH_CLIENT_ID", ...]
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
        file, bad JSON) — callers degrade to empty feature/env lists."""
        path = self._features_path
        try:
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
        resolves each key from its environment via
        :func:`resolve_dynamic_config` (so ``file:``/``cmd:`` prefixes work
        for feature secrets). The value source today is the server's env;
        #1659 adds a ``features_config:`` block in ``klangkd.yaml`` as an
        additional source.
        """
        result: dict[str, str] = {}
        for key in self._manifest.get("container_env_keys", []):
            if not isinstance(key, str):
                continue
            result[key] = resolve_dynamic_config(key, "") or ""
        return result

    def frontend_config(self) -> dict[str, str]:
        """Return config entries for the ``GET /api/config`` response.

        Keys are lowercased for JSON convention (e.g. ``SOLIPLEX_URL`` →
        ``soliplex_url``). The shape (which keys exist, descriptions,
        defaults) is read from the per-feature ``config`` blocks in
        ``features.json``; the values are resolved server-side via
        :func:`resolve_dynamic_config` so the frontend doesn't need access
        to klangkd's environment (today's only value source).
        """
        result: dict[str, str] = {}
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
                default = spec.get("default", "")
                result[key.lower()] = (
                    resolve_dynamic_config(key, default) or ""
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
