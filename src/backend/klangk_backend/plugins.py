"""Plugin configuration: load declared config keys and resolve values.

Scans ``$KLANGK_PLUGINS_DIR/*/package.json`` for ``klangk.config`` entries
and resolves each declared key from the server environment.  Provides
helpers to retrieve values by scope (container, frontend, or both).
"""

import json
import logging
import os

from .settings import resolve_env_value

logger = logging.getLogger(__name__)

VALID_SCOPES = {"container", "frontend", "both"}


class Plugins:
    """Plugin config scanner: loads declared keys and resolved values.

    Constructed once in :func:`build_app` and stored on
    ``app.state.plugins`` (#1451). The plugins dir is computed at
    construction from ``self.settings.plugins_dir`` — no import-time env
    read (#1450's frozen-at-import hazard). Declarations and values are
    instance attrs (no mutable module globals).

    Plugin-declared config keys (discovered at ``load()`` time from
    ``package.json``) are dynamic — they're not settings fields — so
    their values are still resolved via :func:`resolve_env_value` at
    load time (honoring ``file:``/``cmd:`` prefixes for plugin secrets).
    """

    def __init__(self, app_state=None):
        self.app_state = app_state
        self.settings = app_state.settings
        self.plugins_dir = self.settings.plugins_dir or os.path.join(
            os.path.expanduser("~"), ".klangk", "plugins"
        )
        # Loaded at startup: {env_key: {plugin, description, default, scope}}
        self.declarations: dict[str, dict] = {}
        # Resolved values: {env_key: str}
        self.values: dict[str, str] = {}

    def load(self) -> None:
        """Scan plugin package.json files and resolve config values."""
        self.declarations = {}
        self.values = {}

        if not os.path.isdir(self.plugins_dir):
            return

        for name in sorted(os.listdir(self.plugins_dir)):
            pkg_json = os.path.join(self.plugins_dir, name, "package.json")
            if not os.path.isfile(pkg_json):
                continue

            try:
                with open(pkg_json) as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, ValueError, OSError):
                continue

            config = manifest.get("klangk", {}).get("config", {})
            if not isinstance(config, dict):
                continue

            for key, spec in config.items():
                if not isinstance(spec, dict):
                    continue
                scope = spec.get("scope", "container")
                if scope not in VALID_SCOPES:
                    scope = "container"
                self.declarations[key] = {
                    "plugin": name,
                    "description": spec.get("description", ""),
                    "default": spec.get("default", ""),
                    "scope": scope,
                }

        for key, spec in self.declarations.items():
            default = spec.get("default", "")
            # resolve_env_value (not raw os.environ) so plugin-declared keys
            # also honor the file:/cmd: prefixes — plugin config may itself be
            # a secret (e.g. an API token declared by a plugin). These keys
            # are dynamic (discovered from package.json), not settings fields,
            # so they can't be migrated to typed settings.
            self.values[key] = resolve_env_value(key, default) or ""

        if self.declarations:
            logger.info(
                "Loaded %d plugin config key(s): %s",
                len(self.declarations),
                ", ".join(sorted(self.declarations)),
            )

    def plugin_list(self) -> list[dict[str, str]]:
        """Return metadata for each loaded plugin (name, version, description)."""
        if not os.path.isdir(self.plugins_dir):
            return []
        plugins = []
        for name in sorted(os.listdir(self.plugins_dir)):
            pkg_json = os.path.join(self.plugins_dir, name, "package.json")
            if not os.path.isfile(pkg_json):
                continue
            try:
                with open(pkg_json) as f:
                    manifest = json.load(f)
            except (json.JSONDecodeError, ValueError, OSError):
                continue
            plugins.append(
                {
                    "name": name,
                    "version": manifest.get("version", ""),
                    "description": manifest.get("description", ""),
                }
            )
        return plugins

    def container_env(self) -> dict[str, str]:
        """Return env vars to inject into workspace containers."""
        result = {}
        for key, spec in self.declarations.items():
            scope = spec.get("scope", "container")
            if scope in ("container", "both"):
                result[key] = self.values.get(key, "")
        return result

    def frontend_config(self) -> dict[str, str]:
        """Return config entries for the GET /api/config response.

        Keys are lowercased for JSON convention (e.g. SOLIPLEX_URL → soliplex_url).
        """
        result = {}
        for key, spec in self.declarations.items():
            scope = spec.get("scope", "container")
            if scope in ("frontend", "both"):
                result[key.lower()] = self.values.get(key, "")
        return result
