"""Typed configuration via pydantic-settings (#1394, #1395).

This module is the single source of truth for all ``KLANGK_*`` configuration.
It replaces the ad-hoc ``resolve_env_value`` / ``resolve_env_bool`` /
``os.environ.get`` reads that were scattered across the codebase.

Design (see #1392, #1394):

- **pydantic-settings** reads env vars (``env_prefix="KLANGK_"``) into a typed
  ``KlangkSettings`` model.  Fields are ``Optional[str]`` in this chunk to
  preserve the exact string-returning behavior of the legacy
  ``resolve_env_value``; typed fields (``int`` / ``bool`` / ``list``) arrive
  incrementally as call sites migrate to direct ``settings.field`` access.
- **``file:`` / ``cmd:`` resolution** is applied by :func:`resolve_indirection`,
  shared between :func:`resolve_env_value` (legacy) and direct settings access.
  Both paths produce identical results regardless of whether the value came
  from an env var or (future) a config file — capability is a property of the
  value, not the source.
- **Env-change-detection cache** (:func:`get_settings`): the settings singleton
  is re-instantiated whenever any ``KLANGK_*`` env var changes, so
  ``monkeypatch.setenv`` / ``monkeypatch.delenv`` in tests invalidates the
  cache automatically — preserving the call-time env-reading behavior that
  ~337 test env-manipulations rely on.
- **Startup validation**: :func:`validate_at_startup` instantiates the model
  eagerly so bogus config fails fast, before the server serves traffic.
"""

from __future__ import annotations

import logging
import os
import subprocess

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)

# Re-exported for backward compat — callers that ``from ..util import ...``
# still work because util.py re-exports these.
__all__ = [
    "KlangkSettings",
    "get_settings",
    "resolve_env_value",
    "resolve_env_bool",
    "resolve_indirection",
    "validate_at_startup",
]

# ---------------------------------------------------------------------------
# file: / cmd: indirection resolver (shared by all read paths)
# ---------------------------------------------------------------------------

_CMD_TIMEOUT_SECONDS = 10


def _read_file(value: str) -> tuple[str | None, OSError | None]:
    """Strip a ``file:`` prefix and read the referenced file."""
    path = value[5:]
    try:
        with open(path) as f:
            return f.read().strip(), None
    except OSError as e:
        e.filename = e.filename or path
        return None, e


def _run_cmd(value: str) -> tuple[str | None, str | None]:
    """Strip a ``cmd:`` prefix and run the referenced command."""
    command = value[4:]
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_CMD_TIMEOUT_SECONDS}s"
    except OSError as e:  # pragma: no cover — shell=True means /bin/sh spawns; OSError only if sh itself is missing
        return None, str(e)
    if proc.returncode != 0:
        return (
            None,
            f"exited with code {proc.returncode}: {proc.stderr.strip()}",
        )
    return proc.stdout.strip(), None


def resolve_indirection(value: str | None, key: str = "") -> str | None:
    """Resolve ``file:`` / ``cmd:`` prefixes on a raw config value.

    If *value* starts with ``file:`` the remainder is a file path (contents
    returned stripped).  If it starts with ``cmd:`` the remainder is a shell
    command (stdout returned stripped).  Otherwise the value is returned
    as-is.  On resolution failure, logs an error and returns ``None``.

    *key* is used only for error messages (identifying which config var
    failed to resolve); it is a caller-supplied variable *name* (never the
    secret value itself), so it is safe to log. The *value* and any
    value-derived data (e.g. the file path) are never logged — they may
    name a secret — so CodeQL ``py/clear-text-logging-sensitive-data`` does
    not fire (this mirrors the legacy ``resolve_file_value``, which is
    un-flagged for the same reason).
    """
    if value is None:
        return None
    if value.startswith("file:"):
        contents, err = _read_file(value)
        if err is not None:
            # Log only the OS-level message (err.strerror, a fixed string
            # like "No such file or directory") + the var name — never the
            # value or err.filename (both derived from value, which may name
            # a secret).
            logger.error(
                "Cannot read %s: %s",
                key or "config value",
                err.strerror or "I/O error",
            )
            return None
        return contents
    if value.startswith("cmd:"):
        contents, err = _run_cmd(value)
        if err is not None:
            logger.error(
                "Cannot resolve %s via cmd: %s",
                key or "config value",
                err,
            )
            return None
        return contents
    return value


# ---------------------------------------------------------------------------
# KlangkSettings model
# ---------------------------------------------------------------------------

# The insecure default JWT secret — matches the constant in auth.py.
_INSECURE_DEFAULT_SECRET = "change-this-to-a-random-secret"


class KlangkSettings(BaseSettings):
    """Typed configuration for all ``KLANGK_*`` environment variables.

    Fields are ``Optional[str]`` (default ``None``) in this chunk to preserve
    the exact behavior of the legacy ``resolve_env_value`` function: a call
    with no default returns ``None`` when unset; a call with a default returns
    the default.  Typed fields (``int``, ``bool``, ``list[str]``, ``Literal``)
    arrive incrementally as call sites migrate to ``settings.field`` access.

    ``extra="ignore"`` preserves the lenient behavior for unknown keys (typo'd
    *keys* are tolerated; only typo'd *values* of known keys newly reject once
    fields gain strict types).
    """

    model_config = SettingsConfigDict(
        env_prefix="KLANGK_",
        extra="ignore",
        # Do NOT set env_nested_delimiter — KLANGK_ACCESS_TOKEN_HOURS is a
        # flat field (access_token_hours), not a nested table.
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add a YAML config file source when one is configured.

        Precedence (highest first): **env vars** > **config file** > built-in
        defaults.  pydantic-settings applies sources in tuple order with later
        entries overriding earlier ones, so env (later) overrides the file
        (earlier), which overrides the built-in defaults (from the model).
        When no config file is set (:data:`_config_file_path` is None or
        "none"), only the default sources are used (env-only behavior from
        #1394).
        """
        sources: list[PydanticBaseSettingsSource] = [env_settings]
        path = _config_file_path
        if path is not None and path != "none":
            sources.append(
                YamlConfigSettingsSource(settings_cls, yaml_file=path)
            )
        # init_settings (kwargs passed to the constructor) wins over everything.
        sources.append(init_settings)
        return tuple(sources)

    # --- Deployment profiles (#1397) ---
    # ``preset`` is the SINGLE deployment-shape selector — the new name for
    # the "deployment profiles" from #1392. It is one of the four corners
    # of the (transport × auth-gate) space:
    #
    #     uds-noauth / uds-auth   (UDS transport, gate off / on)
    #     ip-noauth  / ip-auth    (TCP transport, gate off / on)
    #
    # Everything the earlier #1397 draft exposed as separate axis keys is
    # *derived* from the preset and is NOT individually configurable:
    #   - the auth GATE is the ``-auth``/``-noauth`` suffix (no KLANGK_AUTH);
    #   - browser ingress is implied by the ``ip-*`` presets — a browser can't
    #     ingress over a UDS — so there is no KLANGK_BROWSER_INGRESS;
    #   - container egress paths have one fixed default per preset, with no
    #     override, so there is no KLANGK_CONTAINER_EGRESS_PATHS.
    #
    # The one thing the operator still chooses separately is the auth
    # BACKEND (password vs OIDC vs both) via the existing KLANGK_AUTH_MODES —
    # that decision can't be made for them. The two keys are cross-validated
    # at config-load: a ``*-noauth`` preset requires ``auth_modes() == none``;
    # a ``*-auth`` preset requires a gated backend. See
    # :func:`_validate_preset` (wired into :func:`validate_at_startup`).
    #
    # Naming: ``preset`` (a named bundle of defaults) was chosen over
    # ``profile`` (vague) and ``role`` (collides with klangk's RBAC workspace
    # roles — the wrong mental model for a deployment *shape*).
    # Defaults to None so a deployment that sets none of these behaves
    # identically to pre-#1392 (acceptance criterion: defaults preserved).
    preset: str | None = None

    # --- Auth / identity ---
    auth_modes: str | None = None
    jwt_secret: str | None = _INSECURE_DEFAULT_SECRET
    prevent_insecure_jwt_secret: str | None = None
    default_user: str | None = "admin@example.com"
    default_password: str | None = None
    access_token_hours: str | None = "24"
    workspace_token_hours: str | None = "24"
    min_password_length: str | None = "8"
    login_lockout_failures: str | None = "5"
    login_lockout_duration: str | None = "900"
    login_lockout_window: str | None = "300"
    disable_registration: str | None = None
    disable_invites: str | None = None
    invite_expire_hours: str | None = "72"
    allow_insecure_no_auth: str | None = None
    reject_proxy_headers: str | None = None
    trusted_proxy_cidrs: str | None = "127.0.0.1,::1"

    # --- Server / network ---
    listen: str | None = "127.0.0.1"
    nginx_port: str | None = "8995"
    port_range_start: str | None = "9000"
    # uds_mode: the honest "are we in UDS mode?" switch (#1396). Set only by
    # klangkd (the launcher that binds a UDS and owns nginx); bare uvicorn /
    # unit tests never set it, so the lifespan's nginx watchdog no-ops and
    # _UDS_MODE stays False. A mode switch, not a path — the socket path is
    # derived from state_dir (<state_dir>/klangk.sock) so there's one path
    # knob, not two.
    uds_mode: str | None = None
    # state_dir: runtime state (the UDS, rendered nginx.conf, pid). Defaults to
    # a klangk subdir under the system temp; devenv pins it to DEVENV_STATE and
    # the host container to /tmp/klangk-state. The UDS lives at
    # <state_dir>/klangk.sock.
    state_dir: str | None = None
    # nginx_bin: the nginx executable the renderer spawns. Falls back to
    # shutil.which("nginx") then /usr/sbin/nginx at render time.
    nginx_bin: str | None = None
    # trust_outer_proxy: opt-in to surviving an outer trusted proxy's
    # X-Forwarded-* in nginx's catch-all (see #1396 renderer). Mirrors the
    # KLANGK_TRUST_OUTER_PROXY env var the old nginx.sh read.
    trust_outer_proxy: str | None = None
    # ws_msg_size_max: max WebSocket message size (bytes), passed to uvicorn.
    # Default 16 MiB; klangkd reads it through the typed config (config file +
    # file:/cmd: resolution), not raw env.
    ws_msg_size_max: str | None = "16777216"
    cors_origins: str | None = None
    dns_servers: str | None = None
    hosting_hostname: str | None = None
    hosting_proto: str | None = None
    hosting_base_path: str | None = None
    bridge_timeout_seconds: str | None = None
    idle_timeout_seconds: str | None = None

    # --- Container / workspace ---
    data_dir: str | None = None
    customize_dir: str | None = None
    plugins_dir: str | None = None
    image_name: str | None = "klangk-workspace"
    image_pull_policy: str | None = "never"
    allowed_images: str | None = None
    allowed_mount_roots: str | None = None
    allow_autostart: str | None = None
    allow_sudo: str | None = None
    container_subnets: str | None = None
    userns: str | None = None
    podman_bin: str | None = "podman"
    disable_tmux: str | None = None
    health_check_interval: str | None = None
    health_check_startup_grace: str | None = None
    health_check_timeout: str | None = None
    hosted_ports_per_workspace: str | None = None
    test_mode: str | None = None
    version_file: str | None = None

    # --- LLM ---
    # llm_base_url is consumed by the nginx renderer (the /llm-proxy/
    # location proxies to it so containers never see the API key); it's
    # not read by the backend itself. Kept here so the renderer reads it
    # through the same typed config path as everything else (#1396).
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None

    # --- OIDC ---
    oidc_config: str | None = None
    oidc_login_hook: str | None = None
    oidc_providers: list[dict] | None = None

    # --- SMTP / email ---
    smtp_host: str | None = None
    smtp_port: str | None = "587"
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_reply_to: str | None = None
    smtp_use_tls: str | None = "true"
    sendmail_path: str | None = "sendmail"
    email_templates_dir: str | None = None

    # --- Legal / support links ---
    terms_url: str | None = None
    privacy_url: str | None = None
    aup_url: str | None = None
    support_url: str | None = None
    support_email: str | None = None

    # --- Branding / UI ---
    product_name: str | None = "Klangk"
    logo_url: str | None = None
    brand_color: str | None = "#E65100"
    login_banner: str | None = None
    login_banner_title: str | None = None
    terminal_banner: str | None = None

    # --- Agent ---
    agent_email: str | None = "clanker@example.com"
    agent_handle: str | None = "clanker"
    agent_disabled: str | None = None

    # --- SSL / certs ---
    ssl_cert_dir: str | None = None

    # --- File upload ---
    file_upload_size_max: str | None = None


# ---------------------------------------------------------------------------
# Singleton with env-change-detection cache + config-file path
# ---------------------------------------------------------------------------

_settings_instance: KlangkSettings | None = None
_settings_env_signature: tuple | None = None

# The config-file path, set by ``klangkd --config <path>`` before the settings
# are first instantiated.  None means "no config file" (the #1394 env-only
# behavior); "none" is the explicit opt-out string (same effect).  Any other
# value is a filesystem path to a YAML config file.
_config_file_path: str | None = None


def set_config_file(path: str | None) -> None:
    """Set the config-file path (called by the ``klangkd`` launcher).

    ``path`` is one of:
    - A filesystem path to a YAML config file (must exist when settings are
      instantiated; checked by the launcher, not here).
    - ``"none"`` — the explicit opt-out: run from env vars + built-in defaults
      only, no config file (#1394 behavior, made explicit).
    - ``None`` — reset to the default (no config file; the launcher handles the
      default-location logic).

    Invalidates the settings cache so the next :func:`get_settings` call
    re-instantiates with the new source chain.
    """
    global _config_file_path
    _config_file_path = path
    _invalidate_cache()


def get_config_file() -> str | None:
    """Return the currently-set config-file path (or None)."""
    return _config_file_path


def _env_signature() -> tuple:
    """A cheap hash of all KLANGK_* env vars + the config-file path, for
    change detection."""
    return (
        _config_file_path,
        tuple(
            sorted(
                (k, v)
                for k, v in os.environ.items()
                if k.startswith("KLANGK_") or k.startswith("LOGFIRE_")
            )
        ),
    )


def get_settings() -> KlangkSettings:
    """Return the settings singleton, re-instantiating if env has changed.

    The env-change detection makes this safe for tests: ``monkeypatch.setenv``
    / ``monkeypatch.delenv`` changes ``os.environ``, which invalidates the
    cache, so the next read sees the updated value.  In production (where env
    is stable), the cache holds for the process lifetime after first access.
    """
    global _settings_instance, _settings_env_signature
    sig = _env_signature()
    if _settings_instance is None or sig != _settings_env_signature:
        _settings_instance = KlangkSettings()
        _settings_env_signature = sig
    return _settings_instance


def _invalidate_cache() -> None:
    """Force the next :func:`get_settings` call to re-instantiate."""
    global _settings_instance, _settings_env_signature
    _settings_instance = None
    _settings_env_signature = None


# The four ``KLANGK_PRESET`` values (#1397) — the (transport × auth-gate)
# corners. ``-noauth`` means "no auth gate"; ``-auth`` means "gate required".
# Kept module-level so the nginx renderer (chunk 3 of #1392) can import it as
# the canonical preset set. See KlangkSettings.preset for the full rationale.
PRESETS: frozenset[str] = frozenset(
    {"uds-noauth", "uds-auth", "ip-noauth", "ip-auth"}
)


def _validate_preset() -> None:
    """Validate ``KLANGK_PRESET`` and its consistency with the auth backend.

    ``KLANGK_PRESET`` is the single deployment-shape selector (#1397); the
    auth *backend* (password vs OIDC vs both) stays the operator's choice via
    ``KLANGK_AUTH_MODES``. The two must agree when ``KLANGK_AUTH_MODES`` is
    set EXPLICITLY:

    - a ``*-noauth`` preset requires the resolved auth mode to be ``none``;
    - a ``*-auth``   preset requires it to be ``password`` / ``oidc`` / ``both``.

    An UNSET ``KLANGK_AUTH_MODES`` never conflicts: :func:`oidc.auth_modes`
    is preset-aware in the unset path (#1397), self-defaulting to ``password``
    for ``*-auth`` presets and ``none`` for ``*-noauth``.

    Raises :class:`~klangk_backend.exceptions.ConfigurationError` on an
    unknown preset value or an explicit preset/auth-mode conflict.

    ``klangk_backend.oidc`` is imported lazily to avoid a settings <-> oidc
    import cycle (``oidc`` imports :func:`get_settings` from this module).
    """
    preset = get_settings().preset
    if preset is None:
        return  # no preset set → today's pre-#1392 behavior
    if preset not in PRESETS:
        raise ConfigurationError(
            f"KLANGK_PRESET={preset!r} is not one of {sorted(PRESETS)}"
        )
    from . import oidc  # noqa: allow-deferred-import  (settings <-> oidc cycle)

    mode = oidc.auth_modes()
    noauth = preset.endswith("-noauth")
    if noauth and mode != "none":
        raise ConfigurationError(
            f"KLANGK_PRESET={preset!r} requires KLANGK_AUTH_MODES=none, "
            f"but the resolved auth mode is {mode!r}"
        )
    if not noauth and mode == "none":
        raise ConfigurationError(
            f"KLANGK_PRESET={preset!r} requires an auth-gated backend "
            f"(KLANGK_AUTH_MODES=password|oidc|both), but the resolved "
            f"auth mode is 'none'"
        )


def validate_at_startup() -> KlangkSettings:
    """Instantiate settings eagerly for fail-fast validation at boot.

    Call once from the lifespan startup (and from the ``klangkd`` launcher).
    Bogus config (once fields gain strict types) fails here with a
    :class:`ValidationError` before the server serves traffic. Also runs the
    :func:`_validate_preset` cross-check so a ``KLANGK_PRESET`` that
    disagrees with the resolved ``KLANGK_AUTH_MODES`` backend fails fast.
    Returns the validated settings instance (which also primes the cache).
    """
    _invalidate_cache()
    settings = get_settings()
    _validate_preset()
    return settings


# ---------------------------------------------------------------------------
# Legacy read functions (delegate to settings, apply file:/cmd: resolution)
# ---------------------------------------------------------------------------


def _key_to_field(key: str) -> str:
    """Map an env-var name (``KLANGK_JWT_SECRET``) to a field name (``jwt_secret``)."""
    if key.startswith("KLANGK_"):
        return key[len("KLANGK_") :].lower()
    return key.lower()


def resolve_env_value(key: str, default: str | None = None) -> str | None:
    """Read a config value, dereferencing ``file:`` / ``cmd:`` prefixes.

    Delegates to the settings singleton (which reads ``os.environ`` via
    pydantic-settings) and applies :func:`resolve_indirection` to the result.
    If the value is unset (``None``), returns *default*.

    Non-``KLANGK_`` keys (``LOGFIRE_TOKEN``, ``KLANGKC_DEBUG_SSH_AGENT``,
    etc.) fall back to ``os.environ.get`` directly, since they're outside the
    settings model's ``env_prefix``.
    """
    if key.startswith("KLANGK_"):
        field = _key_to_field(key)
        settings = get_settings()
        raw = getattr(settings, field, None)
    else:
        # Non-KLANGK_ env vars (LOGFIRE_*, KLANGKC_*, etc.) are outside the
        # settings model's env_prefix. Read directly from os.environ.
        raw = os.environ.get(key)
    if raw is None:
        return default
    resolved = resolve_indirection(raw, key)
    return resolved if resolved is not None else default


def resolve_env_bool(key: str, default: bool = False) -> bool:
    """Read a config value as a boolean.

    Truthy values: ``"1"``, ``"true"``, ``"yes"`` (case-insensitive).
    Everything else is falsy.  Unset returns *default*.
    """
    val = resolve_env_value(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes")
