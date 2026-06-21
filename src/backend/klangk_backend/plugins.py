"""Plugin configuration: load declared config keys and resolve values.

Scans ``$KLANGK_PLUGINS_DIR/*/package.json`` for ``klangk.config`` entries
and resolves each declared key from the server environment.  Provides
helpers to retrieve values by scope (container, frontend, or both).
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_PLUGINS_DIR = os.environ.get("KLANGK_PLUGINS_DIR") or os.path.join(
    os.path.expanduser("~"), ".klangk", "plugins"
)

VALID_SCOPES = {"container", "frontend", "both"}

# Loaded at startup: {env_key: {plugin, description, default, scope}}
_declarations: dict[str, dict] = {}

# Resolved values: {env_key: str}
_values: dict[str, str] = {}


def load() -> None:
    """Scan plugin package.json files and resolve config values from env."""
    global _declarations, _values  # noqa: PLW0603
    _declarations = {}
    _values = {}

    if not os.path.isdir(_PLUGINS_DIR):
        return

    for name in sorted(os.listdir(_PLUGINS_DIR)):
        pkg_json = os.path.join(_PLUGINS_DIR, name, "package.json")
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
            _declarations[key] = {
                "plugin": name,
                "description": spec.get("description", ""),
                "default": spec.get("default", ""),
                "scope": scope,
            }

    for key, spec in _declarations.items():
        default = spec.get("default", "")
        _values[key] = os.environ.get(key, default)

    if _declarations:
        logger.info(
            "Loaded %d plugin config key(s): %s",
            len(_declarations),
            ", ".join(sorted(_declarations)),
        )


def plugin_list() -> list[dict[str, str]]:
    """Return metadata for each loaded plugin (name, version, description)."""
    if not os.path.isdir(_PLUGINS_DIR):
        return []
    plugins = []
    for name in sorted(os.listdir(_PLUGINS_DIR)):
        pkg_json = os.path.join(_PLUGINS_DIR, name, "package.json")
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


def container_env() -> dict[str, str]:
    """Return env vars to inject into workspace containers."""
    result = {}
    for key, spec in _declarations.items():
        scope = spec.get("scope", "container")
        if scope in ("container", "both"):
            result[key] = _values.get(key, "")
    return result


def frontend_config() -> dict[str, str]:
    """Return config entries for the GET /api/config response.

    Keys are lowercased for JSON convention (e.g. SOLIPLEX_URL → soliplex_url).
    """
    result = {}
    for key, spec in _declarations.items():
        scope = spec.get("scope", "container")
        if scope in ("frontend", "both"):
            result[key.lower()] = _values.get(key, "")
    return result
