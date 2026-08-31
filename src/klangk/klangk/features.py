"""Feature manifest: read the build-emitted ``features.json`` and bridge the
declared config keys.

The runtime no longer scans ``KLANGKD_PLUGINS_DIR`` for per-feature
``package.json`` files — that presumed materialized source trees on the
``klangkd`` host, which pip/uv installs never have (#1655). Instead the build
(``import_dart_features.py``) emits a single ``features.json`` into the frontend
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
      "container_env_keys": ["KLANGKWS_FEATURE_GITHUB_OAUTH_CLIENT_ID", ...]
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
# Mirrors _CONTAINER_SCOPES in scripts/import_dart_features.py.
_CONTAINER_SCOPES = {"container", "both"}
_FRONTEND_SCOPES = {"frontend", "both"}

# Every klangk.config key a feature declares for the container env bridge
# (scope container/both) must start with this prefix. Server-side settings
# are all ``KLANGKD_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone
# guarantees a feature can never declare a key that collides with a server
# secret, path, or infra field (``KLANGKD_JWT_SECRET``, ``KLANGKD_DATA_DIR``, …)
# — no denylist / reserved-set needed, and nothing to keep in sync between
# this file and the build emitter (#1662). Non-KLANGKD_ environment poison
# (``PATH``, ``HOME``, ``LD_PRELOAD``, …) is rejected by the same rule.
# Mirrors CONTAINER_ENV_KEY_PREFIX in scripts/import_dart_features.py.
CONTAINER_ENV_KEY_PREFIX = "KLANGKWS_FEATURE_"

# Features.json is a build artifact shipped in the wheel — not attacker-
# controlled at runtime — but cap its read size as defense-in-depth against
# a buggy build emitting a runaway structure (#1662). The real manifest is
# ~1KB for 7 features; 1MB is a generous ceiling that still rejects any
# pathological growth.
MAX_MANIFEST_BYTES = 1024 * 1024

# Feature names that were removed from the product. A deploy that still
# lists one in ``KLANGKD_FEATURES_ENABLE`` boots fine — the name simply
# never matches anything in the manifest, so ``is_enabled`` is False —
# but the resolver logs a tailored line so the operator knows the knob
# entry is dead rather than silently swallowing the typo-looking setting.
_REMOVED_FEATURES = {"chat"}


def warn_removed_features(raw: str | None) -> None:
    """Log a tailored warning for removed feature names in *raw*.

    Called once at Features construction / reconfigure, not per
    ``is_enabled`` call, so the warning appears exactly once per
    settings load.
    """
    if not raw:
        return
    names = {part for part in (e.strip() for e in raw.split(",")) if part}
    for name in names & _REMOVED_FEATURES:
        logger.warning(
            "feature %r was removed from Klangk; ignoring it in "
            "KLANGKD_FEATURES_ENABLE (remove the entry to silence this "
            "warning)",
            name,
        )


def is_valid_container_env_key(key: str) -> bool:
    """True if *key* is a safe container-env declaration.

    Must start with :data:`CONTAINER_ENV_KEY_PREFIX` (``KLANGKWS_FEATURE_``).
    That prefix is the feature-config namespace; every server setting is
    ``KLANGKD_<SETTING>`` (no ``FEATURE_`` infix), so the prefix alone keeps
    feature-declared container env vars from ever colliding with a server
    secret / path / infra field — no reserved-set / denylist required (#1662).
    Used by both the runtime resolver (here) and re-implemented by the build
    emitter (``import_dart_features.py``).
    """
    return key.startswith(CONTAINER_ENV_KEY_PREFIX)


def _deploy_feature_names(raw: str) -> set[str]:
    """Parse the deploy-chosen comma-separated list: trimmed, empties
    dropped (e.g. "a,,b", a trailing comma)."""
    return {
        part for part in (entry.strip() for entry in raw.split(",")) if part
    }


def _manifest_default_names(manifest: dict) -> set[str]:
    """The manifest's ``defaults`` list as a name set; empty when absent or
    malformed."""
    defaults = manifest.get("defaults", [])
    if isinstance(defaults, list):
        return {d for d in defaults if isinstance(d, str)}
    return set()


def _all_manifest_feature_names(manifest: dict) -> set[str]:
    """Every compiled-in feature name. Skip non-dict entries, missing names,
    and empty names."""
    return {
        name
        for f in manifest.get("features", [])
        if isinstance(f, dict)
        and isinstance((name := f.get("name")), str)
        and name
    }


class Features:
    """Feature manifest reader + config-key bridge.

    Constructed once in :func:`build_app` and stored on ``app.state.features``.
    Reads ``features.json`` (sibling of the frontend's ``index.html``) at
    construction; the manifest is a build artifact, so a SIGHUP settings
    reload (which may change ``frontend_dir``) re-reads it via
    :meth:`reconfigure`.

    The Flutter ``ToolPlugin`` API contract (defined in the external
    ``klangk_plugin_api`` package) is unchanged; this class owns only the
    build-emitted manifest read and the config-value bridge (#1655).
    """

    def __init__(self, app):
        self.app = app
        # Parsed features.json: {features: [...], defaults: [...],
        # container_env_keys: [...]}. Empty when no manifest is present
        # (pre-build source deploy, missing frontend_dir) — every method
        # degrades cleanly to "no features, no env bridge."
        self._manifest = self._read_manifest()
        warn_removed_features(self.app.state.settings.features_enable)

    def reconfigure(self, app) -> None:
        # Re-read on a SIGHUP settings reload (frontend_dir may have changed).
        self.app = app
        self._manifest = self._read_manifest()
        warn_removed_features(self.app.state.settings.features_enable)

    @property
    def _features_path(self) -> str:
        return os.path.join(
            self.app.state.settings.frontend_dir, "features.json"
        )

    def _read_manifest(self) -> dict:
        """Read + parse features.json. Empty dict on any failure (missing
        file, bad JSON, oversize). Callers degrade to empty feature/env lists.

        Size-capped at :data:`MAX_MANIFEST_BYTES` as defense-in-depth against
        a buggy build emitting a runaway structure (#1662)."""
        path = self._features_path
        try:
            if (
                os.path.isfile(path)
                and os.path.getsize(path) > MAX_MANIFEST_BYTES
            ):
                logger.warning(
                    "features.json at %s is %d bytes (cap %d) — ignoring "
                    "manifest, degrading to empty feature/env lists",
                    path,
                    os.path.getsize(path),
                    MAX_MANIFEST_BYTES,
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

        Backs the ``features`` field of ``GET /api/version`` — the full set
        of features possible to use on this install, regardless of whether
        they're active for this deploy (#1655: activation is a frontend
        concern, gated by KLANGKD_FEATURES_ENABLE against this list).
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

    def is_enabled(self, name: str) -> bool:
        """True if feature *name* is active for this deploy.

        Server-side activation resolver (#1974): the backend can now ask
        "is feature X on?" the same way the frontend does — feature-gating
        server-side subsystems instead of bespoke settings.

        Canonical semantics — mirrors ``_resolveActiveFeatures`` in
        ``src/frontend/lib/main.dart`` so the server gates on the **same**
        active set the UI resolves (drift between the two would gate the
        agent on a different set than the UI shows):

        - ``KLANGKD_FEATURES_ENABLE`` unset **or** blank/whitespace → the
          manifest's ``defaults`` list (the stock set). A blank value is
          treated as unset, exactly as the frontend treats a blank string
          read from ``/api/config`` (its ``v.trim().isNotEmpty`` guard).
        - any non-blank value → exactly that comma-separated list, split on
          ``,``, each entry trimmed, empties dropped. No ``*`` form, and
          **not** additive over ``defaults`` (an explicit list replaces the
          stock set entirely).
        - no usable deploy list and no usable ``defaults`` → every
          compiled-in feature (the manifest's ``features`` array) is active
          — the back-compat fallback the frontend uses for a source deploy
          with no built manifest. The server's compiled-in inventory *is*
          the manifest, so with no manifest at all nothing is active (a safe
          default for the rare server-without-built-frontend case).

        Reads ``self.app.state.settings.features_enable`` live at call time
        (app ownership rule — no cached snapshot), so a SIGHUP reload of the
        knob is picked up without a :meth:`reconfigure` call; only a manifest
        change (``frontend_dir``) needs ``reconfigure``.
        """
        return name in self._active_feature_names()

    def _active_feature_names(self) -> set[str]:
        """The deploy's active-feature set, per :meth:`is_enabled`.

        Computed fresh on every call (no caching) so a settings reload of
        ``KLANGKD_FEATURES_ENABLE`` propagates immediately. The active set is
        a handful of names, so recomputing per call is cheap.
        """
        raw = self.app.state.settings.features_enable
        if raw is not None and raw.strip():
            # Explicit deploy-chosen list: exact comma-separated membership.
            # Trim each entry and drop empties (e.g. "a,,b", a trailing comma).
            return _deploy_feature_names(raw)
        names = _manifest_default_names(self._manifest)
        if names:
            return names
        # No deploy list and no usable defaults → every compiled-in feature
        # active (back-compat, mirroring the frontend's step-3 fallback for
        # a source deploy with no built manifest).
        return _all_manifest_feature_names(self._manifest)

    def container_env(self) -> dict[str, str]:
        """Return env vars to inject into workspace containers.

        The build emits ``container_env_keys`` (every klangk.config key
        declared with scope ``container`` or ``both`` across all compiled-in
        features) into ``features.json``; the server reads that list and
        resolves each key via :func:`resolve_dynamic_config` (so
        ``file:``/``cmd:`` prefixes work for feature secrets). Value
        sources, in descending precedence (#1659): the server's env, then
        the ``features_config:`` block of ``klangkd.yaml`` (long-lived
        deploy config like OAuth client IDs), then the feature-declared
        default. Env remains the escape hatch for per-invocation overrides.

        Defense-in-depth (#1662): even though the build layer refuses to
        emit reserved/non-KLANGKD_ keys, this runtime guard skips them too —
        a stale or older manifest shipping with a newer server must not
        leak ``KLANGKD_JWT_SECRET`` etc. into a container. A skipped key is
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
                    "to resolve (missing KLANGKWS_FEATURE_ prefix); "
                    "skipping. Rebuild with a corrected feature.",
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

        Keys are the lowercased **suffix** after ``KLANGKWS_FEATURE_``
        (e.g. ``KLANGKWS_FEATURE_BOING_SPEED`` → ``boing_speed``). Declared
        keys that don't carry the ``KLANGKWS_FEATURE_`` prefix are skipped —
        the prefix is the feature-config namespace (#1662): it keeps
        feature-declared config from colliding with server settings
        (``KLANGKD_<SETTING>``) and gives the frontend a stable, un-prefixed
        JSON key shape. The shape (which keys exist, descriptions,
        defaults) is read from the per-feature ``config`` blocks in
        ``features.json``; the values are resolved server-side via
        :func:`resolve_dynamic_config` so the frontend doesn't need access
        to klangkd's environment. Value sources, in descending precedence
        (#1659): the server's env, then the ``features_config:`` block of
        ``klangkd.yaml``, then the feature-declared default.
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
                    CONTAINER_ENV_KEY_PREFIX
                ):
                    logger.warning(
                        "features.json frontend-scope config key %r — "
                        "missing KLANGKWS_FEATURE_ prefix; skipping. Rebuild "
                        "with a corrected feature.",
                        key,
                    )
                    continue
                default = spec.get("default", "")
                # Strip the KLANGKWS_FEATURE_ prefix and lowercase the suffix
                # for the JSON key (e.g. KLANGKWS_FEATURE_BOING_SPEED →
                # boing_speed). The prefix is enforced above; the suffix is
                # the feature-owned name, surfaced un-prefixed to the frontend.
                json_key = key[len(CONTAINER_ENV_KEY_PREFIX) :].lower()
                result[json_key] = (
                    resolve_dynamic_config(
                        key, default, features_config=features_config
                    )
                    or ""
                )
        return result

    def features_enable(self) -> str | None:
        """The deploy's chosen active-feature list (``KLANGKD_FEATURES_ENABLE``).

        Forwarded verbatim via ``/api/config`` so the frontend can resolve
        the active set against its sibling ``features.json`` (canonical
        semantics: unset → manifest ``defaults``; any explicit value →
        exactly that list). The server does no resolution itself — the
        frontend owns the activation logic (#1655).
        """
        return self.app.state.settings.features_enable
