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
- **``file:`` / ``cmd:`` resolution** is applied once, at construction, by
  the ``_resolve_indirections`` model validator on :class:`KlangkSettings`
  (#1461). Every ``settings.field`` read thereafter returns the already-
  resolved value — no caller wraps in ``resolve_indirection``. The private
  ``_resolve_indirection`` survives for two callers: that validator, and the
  non-``KLANGK_`` path of :func:`resolve_env_value` (plugin-declared dynamic
  keys discovered from ``package.json``, which are not settings fields and so
  cannot be resolved at construction).
- **Env-change-detection cache** (:func:`get_settings`): cache-free —
  re-constructs on every call, so ``monkeypatch.setenv`` /
  ``monkeypatch.delenv`` in tests is picked up automatically.
- **Startup validation**: field validators (e.g. ``auth_modes``) run at
  construction, so bogus config fails fast when ``KlangkSettings(...)`` is
  first built in ``build_app(settings)``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Mapping

from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from pydantic import field_validator, model_validator
from pydantic_settings.sources.providers.env import parse_env_vars

logger = logging.getLogger(__name__)

# Valid values for ``KLANGK_AUTH_MODES``. ``None`` (unset) defaults to ``none``
# at *read* time (in ``oidc.auth_modes``), but a non-None value must be one of
# these — rejecting typos at construction so a misspelled mode fails loudly at
# boot instead of silently downgrading to the no-auth ``none`` mode (which
# freely issues an admin token). See the ``auth_modes`` field validator below.
_VALID_AUTH_MODES = frozenset({"password", "oidc", "both", "none"})

# Re-exported for backward compat — callers that ``from ..util import ...``
# still work because util.py re-exports these.  ``resolve_indirection`` is
# NOT exported: ``file:``/``cmd:`` resolution now happens once, inside
# ``KlangkSettings`` at construction (#1461).  The private ``_resolve_indirection``
# is shared by the model validator and the non-KLANGK path of
# ``resolve_env_value`` (plugin-declared dynamic keys).
__all__ = [
    "KlangkSettings",
    "resolve_dynamic_config",
]

# ---------------------------------------------------------------------------
# file: / cmd: indirection resolver (shared by all read paths)
# ---------------------------------------------------------------------------

_CMD_TIMEOUT_SECONDS = 10

# Default frontend dir: repo-relative build output (dev/devenv). Computed
# from this module's location so it resolves identically whether running
# from a checkout (exists -> UI mounted) or an installed package (absent ->
# build_app's exists()-skip handles it). KLANGK_FRONTEND_DIR overrides (#1456).
_DEFAULT_FRONTEND_DIR = str(
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "build"
    / "web"
)


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


def _resolve_indirection(value: str | None, key: str = "") -> str | None:
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

    Private: ``file:``/``cmd:`` resolution for ``KlangkSettings`` fields
    happens once at construction via the ``_resolve_indirections`` model
    validator (#1461).  This helper survives for two callers: that
    validator, and the non-KLANGK path of ``resolve_env_value`` (plugin-
    declared dynamic keys discovered from ``package.json``, which are not
    settings fields and so cannot be resolved at construction).
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

# The insecure default JWT secret. Single source of truth — auth.py's
# Auth.jwt_secret_is_secure() compares against this (#1501).
INSECURE_DEFAULT_SECRET = "change-this-to-a-random-secret"
# Back-compat alias (was the private name).
_INSECURE_DEFAULT_SECRET = INSECURE_DEFAULT_SECRET


# --- Env-source override for injectable env dicts (#1426 Slice 1) ---
#
# pydantic-settings reads os.environ in exactly one spot:
# EnvSettingsSource._load_env_vars(), which calls parse_env_vars(os.environ,
# ...).  Subclassing to run a *different* mapping through the *same*
# parse_env_vars normalizer preserves all base behavior (case handling,
# env_parse_none_str, prefix logic).  This lets tests pass a plain dict via
# ``KlangkSettings(env={...})`` instead of monkeypatching os.environ.


class _EnvDictSource(EnvSettingsSource):
    """EnvSettingsSource pointed at an arbitrary env mapping.

    Used instead of the default env source when an explicit ``env`` dict is
    passed to :class:`KlangkSettings`.
    """

    def __init__(
        self, settings_cls: type[BaseSettings], env: Mapping[str, str]
    ):
        self._env = env
        super().__init__(settings_cls)

    def _load_env_vars(self):
        return parse_env_vars(
            self._env,
            self.case_sensitive,
            self.env_ignore_empty,
            self.env_parse_none_str,
        )


class _KebabYamlConfigSettingsSource(YamlConfigSettingsSource):
    """YAML config source that accepts kebab-case *and* snake_case keys.

    The config file is documented in snake_case (matching the field names),
    but klangk's wider config-file style is kebab-case (e.g. the CLI's
    ``cli.yaml`` and the OIDC provider dicts).  pydantic-settings matches
    config keys against snake_case field names only, so a bare
    ``YamlConfigSettingsSource`` silently ignores hyphenated keys.  This
    subclass normalizes top-level hyphenated keys (``nginx-port`` →
    ``nginx_port``) so an operator may write **either** form for any key
    (#1538); snake_case keys pass through unchanged.

    Only **top-level** keys are normalized.  Nested mappings (the dicts inside
    ``oidc_providers``) are left as-is — their dual-form lookup is already
    handled by :func:`klangk_backend.oidc.get`, which checks kebab then snake.
    """

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        data = super()._read_file(file_path)
        # Normalize only top-level keys: ``-`` → ``_`` so either form maps to
        # the same snake_case field.  Nested values (e.g. oidc_providers
        # dicts) are preserved verbatim.
        return {
            (key.replace("-", "_") if isinstance(key, str) else key): value
            for key, value in data.items()
        }


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

    Constructor (``#1426``): ``KlangkSettings(env, config_file=None)``.
    *env* is required — it is the env-var mapping the model reads from.  In
    production pass ``os.environ``; in tests pass a dict.  ``os.environ`` is
    never read unless it is explicitly passed as *env*.
    """

    # Bridges for the classmethod boundary: ``settings_customise_sources``
    # runs inside ``BaseSettings.__init__`` before ``self`` exists, so it
    # can't read ``self.env``.  ``__init__`` stashes the env mapping and
    # config-file path here before calling ``super().__init__()``.  These are
    # ``ClassVar``s (NOT pydantic private attrs) so they stay pure class
    # state — not per-instance slots, not model fields.  Construction is
    # single-threaded at startup and one-at-a-time in tests.
    _env_for_sources: ClassVar[Mapping[str, str] | None] = None
    _config_file_for_sources: ClassVar[str | None] = None

    model_config = SettingsConfigDict(
        env_prefix="KLANGK_",
        extra="ignore",
        # Do NOT set env_nested_delimiter — KLANGK_ACCESS_TOKEN_HOURS is a
        # flat field (access_token_hours), not a nested table.
    )

    def __init__(
        self, env: Mapping[str, str], config_file: str | None = None
    ) -> None:
        """Construct settings from *env* and an optional config file.

        - ``KlangkSettings(os.environ)`` — production (no config file).
        - ``KlangkSettings(os.environ, config_file="/path/to/config.yaml")``
          — production with a YAML config file.
        - ``KlangkSettings(env={...})`` — tests; reads the dict only,
          ``os.environ`` is never consulted.

        *env* is required — every construction is explicit about where
        configuration comes from.  *config_file* defaults to ``None``
        (no config file; env-only).  ``"none"`` is the explicit opt-out
        string (same effect as ``None``).
        """
        type(self)._env_for_sources = env
        type(self)._config_file_for_sources = config_file
        try:
            super().__init__()
        finally:
            # Clean up the bridges (exception-safe) so dicts don't leak onto
            # the class if ``super().__init__()`` raises.
            type(self)._env_for_sources = None
            type(self)._config_file_for_sources = None

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

        Precedence (highest first): **the env dict passed to the constructor**
        > **config file** > built-in defaults.  The env source is ALWAYS
        the dict passed to ``__init__`` via ``env=`` — either ``os.environ``
        (production, the default) or a test dict
        (``KlangkSettings(env={...})``).  ``os.environ`` is never consulted
        directly by the framework; it is merely the default value of the
        ``env`` parameter.  In tests, when a dict is passed, ``os.environ``
        is never read.
        """
        env = cls._env_for_sources
        active_env: PydanticBaseSettingsSource = (
            _EnvDictSource(settings_cls, env)
            if env is not None
            else env_settings
        )
        sources: list[PydanticBaseSettingsSource] = [active_env]
        # config_file from the constructor (class-var bridge).
        path = cls._config_file_for_sources
        if path is not None and path != "none":
            sources.append(
                _KebabYamlConfigSettingsSource(settings_cls, yaml_file=path)
            )
        # init_settings (kwargs passed to the constructor) wins over everything.
        sources.append(init_settings)
        return tuple(sources)

    # --- Auth / identity ---
    auth_modes: str | None = None
    jwt_secret: str | None = _INSECURE_DEFAULT_SECRET
    prevent_insecure_jwt_secret: str = ""
    default_user: str = "admin@example.com"
    default_password: str | None = None
    access_token_hours: str | None = "24"
    workspace_token_hours: str | None = "24"
    min_password_length: str | None = "8"
    login_lockout_failures: str | None = "5"
    login_lockout_duration: str | None = "900"
    login_lockout_window: str | None = "300"
    disable_registration: str = ""
    disable_invites: str = ""
    invite_expire_hours: str | None = "72"
    allow_insecure_no_auth: str = ""
    reject_proxy_headers: str | None = None
    trusted_proxy_cidrs: str | None = "127.0.0.1,::1"

    # --- Server / network ---
    # listen: the uvicorn bind spec — **polymorphic** (#1422). Either a TCP
    # host (e.g. ``127.0.0.1``, ``0.0.0.0``) or a UNIX socket path
    # (e.g. ``/tmp/klangk.sock``). Classification (see :func:`classify_listen`):
    # an absolute path with no ``://`` scheme ⇒ socket; otherwise TCP. The
    # deployment shape is *derived* from listen's shape + auth_modes — there
    # is no amalgamated ``KLANGK_UI_MODE``/``KLANGK_PRESET`` setting (it never
    # shipped). Socket ⇒ nginx renders the minimal (headless) template; TCP
    # ⇒ full (browser) template. ``KLANGK_PORT`` applies only when listen is
    # TCP. Default is None → klangkd derives a socket path from state_dir
    # (#1400: headless UDS posture is the production default).
    listen: str | None = None
    port: str | None = "8997"
    nginx_port: str | None = "8995"
    port_range_start: str | None = "9000"
    # state_dir: runtime state (the UDS when listen is a socket path, rendered
    # nginx.conf, pid). **Required** — no default; a missing value fails at
    # construction (#1461). Devenv pins it to ``$DEVENV_STATE/klangk`` via
    # ``env.KLANGK_STATE_DIR`` in devenv.nix; the host container sets it to
    # ``/tmp/klangk-state``.
    state_dir: str | None = None
    # nginx_bin: the nginx executable the renderer spawns. Falls back to
    # shutil.which("nginx") then /usr/sbin/nginx at render time.
    nginx_bin: str | None = None
    # trust_outer_proxy: opt-in to surviving an outer trusted proxy's
    # X-Forwarded-* in nginx's catch-all (see #1396 renderer). Mirrors the
    # KLANGK_TRUST_OUTER_PROXY env var the old nginx.sh read.
    trust_outer_proxy: str = ""
    # frontend_dir: directory the built Flutter Web UI is served from
    # (#1456). Defaults to the repo-relative build path (src/frontend/build/web,
    # computed above as _DEFAULT_FRONTEND_DIR) so dev/devenv keeps working
    # unchanged; klangkd deployments override with the installed path.
    frontend_dir: str = _DEFAULT_FRONTEND_DIR
    # ws_msg_size_max: max WebSocket message size (bytes), passed to uvicorn.
    # Default 16 MiB; klangkd reads it through the typed config (config file +
    # file:/cmd: resolution), not raw env.
    ws_msg_size_max: str | None = "16777216"
    cors_origins: str | None = None
    dns_servers: str = ""
    hosting_hostname: str | None = None
    hosting_proto: str | None = None
    hosting_base_path: str | None = None
    bridge_timeout_seconds: str | None = None
    idle_timeout_seconds: str | None = None

    # --- Container / workspace ---
    # data_dir: persistent storage (SQLite DB, workspace volumes). Defaults
    # to ``<state_dir>/data`` when unset (derived in the ``_require_dirs``
    # validator after state_dir is resolved), so an operator who sets only
    # ``state_dir`` gets a sensible data location. An explicit
    # ``KLANGK_DATA_DIR`` / config-file value wins (#1506).
    data_dir: str | None = None
    customize_dir: str | None = None
    # plugins_dir: plugin packages. Defaults to ``<state_dir>/plugins`` when
    # unset (derived in the ``_require_dirs`` validator after state_dir is
    # resolved). Shell scripts and the host container set KLANGK_PLUGINS_DIR
    # explicitly.
    plugins_dir: str | None = None
    image_name: str | None = "klangk-workspace"
    image_pull_policy: str | None = "never"
    allowed_images: str | None = None
    allowed_mount_roots: str | None = None
    allow_autostart: str = ""
    allow_sudo: str = ""
    container_subnets: str | None = None
    userns: str = "keep-id:uid=1000,gid=1000"
    podman_bin: str | None = "podman"
    disable_tmux: str = ""
    health_check_interval: str | None = None
    health_check_startup_grace: str | None = None
    health_check_timeout: str | None = None
    hosted_ports_per_workspace: str = "5"
    test_mode: str | None = None
    version_file: str | None = None

    # --- LLM ---
    # llm_base_url is consumed by the nginx renderer (the /llm-proxy/
    # location proxies to it so containers never see the API key); it's
    # not read by the backend itself. Kept here so the renderer reads it
    # through the same typed config path as everything else (#1396).
    llm_base_url: str | None = None
    llm_api_key: str = ""
    llm_model: str = ""

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
    smtp_reply_to: str = ""
    smtp_use_tls: str | None = "true"
    sendmail_path: str | None = "sendmail"
    email_templates_dir: str = ""

    # --- Legal / support links ---
    terms_url: str = ""
    privacy_url: str = ""
    aup_url: str = ""
    support_url: str = ""
    support_email: str = ""

    # --- Branding / UI ---
    product_name: str = "Klangk"
    logo_url: str = ""
    brand_color: str = "#E65100"
    login_banner: str = ""
    login_banner_title: str = ""
    terminal_banner: str = ""

    # --- Agent ---
    agent_email: str = "clanker@example.com"
    agent_handle: str = "clanker"
    agent_disabled: str = ""

    # --- SSL / certs ---
    ssl_cert_dir: str | None = None

    # --- File upload ---
    file_upload_size_max: str | None = "524288000"

    @model_validator(mode="after")
    def _resolve_indirections(self) -> "KlangkSettings":
        """Resolve ``file:``/``cmd:`` prefixes once, at construction (#1461).

        Every string field is run through :func:`_resolve_indirection` before
        the object is handed to anything. Thereafter ``settings.field`` returns
        the already-resolved value — no caller wraps in ``resolve_indirection``.
        A field set to ``file:/nonexistent`` or ``cmd:false`` fails *here*
        (fail-fast at boot), not silently at use time.

        Resolution is idempotent: a plain (non-``file:``/``cmd:``) value passes
        through unchanged, so re-resolving an already-resolved value is a
        no-op. This keeps the legacy ``resolve_env_value`` path (still used by
        plugin-declared dynamic keys and not-yet-migrated modules) correct —
        it reads the already-resolved field and the redundant
        ``_resolve_indirection`` call it makes is a harmless no-op.

        Only ``str`` fields are candidates: ``list[dict]`` (``oidc_providers``)
        and any non-string field are skipped. ``None`` (unset) is left alone.
        """
        for name in type(self).model_fields:
            val = getattr(self, name)
            if isinstance(val, str):
                resolved = _resolve_indirection(val, name)
                if resolved is None:
                    raise ValueError(
                        f"KLANGK_{name.upper()} could not be resolved: the "
                        f"file:/cmd: reference failed. See logs for detail."
                    )
                setattr(self, name, resolved)
        return self

    @model_validator(mode="after")
    def _require_dirs(self) -> "KlangkSettings":
        """Require ``state_dir``; derive ``data_dir``, ``customize_dir``, ``plugins_dir``.

        ``state_dir`` has no default — an operator must set it (env or config
        file); a missing value fails fast at construction (boot), not at the
        first use that dereferences a ``None`` path (#1461).

        ``data_dir``, ``customize_dir``, and ``plugins_dir`` all derive from
        ``state_dir`` when unset (#1506), so an operator who sets only
        ``state_dir`` gets sensible locations without extra vars. Explicit
        values win.
        """
        if not self.state_dir:
            raise ValueError(
                "KLANGK_STATE_DIR is required (env var or config file). "
                "Set it to the runtime state directory (UDS socket, rendered "
                "nginx.conf, pid file)."
            )
        if not self.data_dir:
            self.data_dir = os.path.join(self.state_dir, "data")
        if not self.plugins_dir:
            self.plugins_dir = os.path.join(self.state_dir, "plugins")
        if not self.customize_dir:
            self.customize_dir = os.path.join(self.state_dir, "custom")
        return self

    @field_validator("auth_modes")
    @classmethod
    def _validate_auth_modes(cls, v: str | None) -> str | None:
        """Reject typo'd auth modes so a misspelling fails loudly at boot.

        Without this, ``KLANGK_AUTH_MODES=passdword`` (or any value outside the
        valid set) would fall through ``oidc.auth_modes()`` to the ``none``
        default — a *silent security downgrade*: ``none`` freely issues an
        admin token via ``POST /api/v1/auth/local``. ``None`` is allowed (the
        unset case, which legitimately means "default to none"); only a
        *set-but-garbage* value is rejected.

        Runs at construction (``KlangkSettings(...)``), so the bad value aborts
        boot (via ``build_app(settings)`` → ``app.state.settings``) before
        traffic.
        """
        if v is None or v == "":
            # Unset or empty → default to ``none`` at read time (in
            # ``oidc.auth_modes``). Legitimate: the operator didn't set a mode.
            return None
        if v not in _VALID_AUTH_MODES:
            raise ValueError(
                f"KLANGK_AUTH_MODES={v!r} is invalid. "
                f"Must be one of {sorted(_VALID_AUTH_MODES)} (or unset "
                "→ defaults to 'none')."
            )
        return v


# ---------------------------------------------------------------------------
# Singleton with env-change-detection cache + config-file path
# ---------------------------------------------------------------------------


def classify_listen(value: str | None) -> str:
    """Classify a ``KLANGK_LISTEN`` value as ``"socket"`` or ``"tcp"``.

    ``KLANGK_LISTEN`` is polymorphic: a UNIX socket path (e.g.
    ``/tmp/klangk.sock``) or a TCP host (e.g. ``127.0.0.1``). The port is NOT
    part of listen — it comes from ``KLANGK_PORT`` when listen is TCP.
    Classification rule (shared with the CLI ``--server`` resolver, #1399):

    - an **absolute path** with no ``://`` scheme ⇒ ``"socket"``;
    - otherwise ⇒ ``"tcp"`` (a bare hostname/IP, e.g. ``127.0.0.1``).

    A bare/relative non-scheme value (e.g. ``klangk.sock``) is ambiguous and
    is classified as ``"tcp"`` — callers that need a socket should pass an
    absolute path. This mirrors #1399's "socket paths must be absolute" rule
    and keeps the classifier total (no exceptions). ``None`` ⇒ ``"tcp"``
    (the default bind is loopback TCP until #1400 flips it to a socket).
    """
    if not value:
        return "tcp"
    if "://" in value:
        return "tcp"  # http(s)://... — TCP (CLI-style absolute URL)
    if value.startswith("/"):
        return "socket"  # absolute path, no scheme — UDS
    return "tcp"  # bare hostname/IP — TCP


def listen_is_socket(value: str) -> bool:
    """True iff the resolved ``KLANGK_LISTEN`` is a socket path.

    Convenience wrapper around :func:`classify_listen` that reads the merged
    setting when *value* is omitted. This is what the nginx renderer and the
    lifespan watchdog key off to decide "headless/minimal template + UDS
    bind" vs "full template + TCP bind."

    ``KlangkSettings`` already resolves ``file:``/``cmd:`` at construction
    (#1461), so ``settings.listen`` is the resolved value — no wrap needed.
    """
    v = value
    return classify_listen(v) == "socket"


# ---------------------------------------------------------------------------
# Plugin dynamic-key resolver (the only remaining file:/cmd: deref path)
# ---------------------------------------------------------------------------


def resolve_dynamic_config(key: str, default: str | None = None) -> str | None:
    """Resolve a plugin-declared dynamic config key.

    Plugin config keys (discovered from each plugin's ``package.json``) are
    outside the ``KLANGK_`` settings model — they are not known at settings
    construction, so they can't be resolved by the model validator. This
    reads ``os.environ`` directly and applies :func:`_resolve_indirection`
    so plugin config honors ``file:``/``cmd:`` prefixes (a plugin-declared
    key may itself be a secret, e.g. an API token).
    """
    raw = os.environ.get(key)
    if raw is None:
        return default
    resolved = _resolve_indirection(raw, key)
    return resolved if resolved is not None else default
