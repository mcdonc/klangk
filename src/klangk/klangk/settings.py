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
from pydantic import PrivateAttr, field_validator, model_validator
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
    handled by :func:`klangk.oidc.get`, which checks kebab then snake.
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

    # The sources this instance was built from, retained so :meth:`reload`
    # can re-resolve identically (env-only or env + the same YAML config
    # file).  Private attrs (NOT model fields) — they carry no config data
    # and must not be validated.  ``_reload_env`` is a reference to the
    # mapping passed to ``__init__``: ``os.environ`` in production (a live
    # mapping, so reload picks up operator edits), a dict in tests (so
    # reload re-reads that dict, never ``os.environ`` — #1457 isolation).
    _reload_env: Mapping[str, str] | None = PrivateAttr(default=None)
    _reload_config_file: str | None = PrivateAttr(default=None)

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
        # Retain the real sources for reload() (see the PrivateAttr decl).
        self._reload_env = env
        self._reload_config_file = config_file

    def reload(self) -> "KlangkSettings":
        """Re-resolve settings from the same sources used to build this instance.

        Returns a fresh ``KlangkSettings`` built from the env mapping + config
        file captured at construction (see ``_reload_env`` /
        ``_reload_config_file``).  In production the env mapping is the live
        ``os.environ``, so a reload after an operator edits ``KLANGK_*``
        picks up the new values; in tests it is the dict passed to the
        constructor, so reload re-reads that dict and never touches
        ``os.environ``.

        Raises whatever construction raises — pydantic ``ValidationError``
        for a bogus/invalid value (a dangling ``file:``/``cmd:`` ref, a
        failed field/model validator, a duplicate port, ...) or ``OSError``
        if the config file can no longer be read.  Callers that want a
        deny-on-invalid gate (e.g. the SIGHUP restart path, #1587) wrap this
        in a try/except and refuse to act on failure.
        """
        return type(self)(
            self._reload_env, config_file=self._reload_config_file
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
    # listen: the nginx **browser** interface/address (e.g. ``127.0.0.1``,
    # ``0.0.0.0``). Rendered as ``listen {listen}:{port};`` only when
    # ``KLANGK_PORT`` is set (full/browser mode). Default ``127.0.0.1``
    # (loopback) keeps the browser listener reachable only from the operator's
    # machine unless an operator deliberately widens it (#1542). The
    # polymorphic socket-path meaning (#1422) never shipped in a release and
    # is retired — the UDS path is now ``KLANGK_SOCKET``.
    listen: str = "127.0.0.1"
    # port: the nginx **browser** port (e.g. ``8997``). **No default** — unset
    # ⇒ headless mode (no browser listener is rendered; only the container-
    # egress listener on ``KLANGK_EGRESS_PORT`` is served). Set ⇒ full/browser
    # mode (browser UI + API + hosted apps on ``listen {listen}:{port};``).
    port: str | None = None
    # egress_port: the container-egress port nginx listens on for
    # container→backend traffic (``/llm-proxy``, ``/api/v1/browser-delegate``,
    # ``/api/v1/workspaces/post-chat-message``). Serves both headless and full
    # modes. Default ``8995``. Must differ from ``port`` so ingress vs egress
    # can be firewalled separately (#1542). ``None`` here is a sentinel —
    # ``_resolve_socket_and_ports`` resolves it to ``"8995"`` (or folds the
    # deprecated ``KLANGK_NGINX_PORT`` into it).
    egress_port: str | None = None
    # egress_listen: the interface/address nginx binds for the container-
    # egress listener, rendered as ``listen {egress_listen}:{egress_port};``.
    # Default ``0.0.0.0`` (all interfaces) — the only value portable across
    # podman network modes, because the host interface container traffic lands
    # on is environment-specific (host LAN IP under pasta/netavark-NAT, bridge
    # gateway under rootful bridge) and cannot be detected reliably at render
    # time. The actual security boundary on the egress locations is the
    # ``CONTAINER_ACL`` allowlist + ``auth_request`` workspace-token gate, not
    # the bind address. An operator who knows their specific container-facing
    # host IP may set this to that IP to drop every other interface from the
    # egress surface (#1542).
    egress_listen: str = "0.0.0.0"
    # nginx_port: **deprecated** alias for ``egress_port`` (#1542). Folded into
    # ``egress_port`` by ``_resolve_socket_and_ports``: if both are set,
    # ``egress_port`` wins and ``nginx_port`` is ignored (with a warning); if
    # only ``nginx_port`` is set it is used as the egress port (with a
    # deprecation warning). To be removed in a future release. **Callers read
    # ``settings.egress_port`` — nothing reads ``nginx_port`` except that one
    # validator.**
    nginx_port: str | None = None
    port_range_start: str | None = "9000"
    # socket: the backend UDS path klangkd binds. Default
    # ``<state_dir>/klangk.sock`` (derived in ``_resolve_socket_and_ports``
    # after ``state_dir`` is resolved). A fail-fast validator rejects resolved
    # paths exceeding the portable AF_UNIX ``sun_path`` bound (104 chars) with
    # a diagnostic telling the deployer to shorten ``KLANGK_SOCKET`` or move
    # ``KLANGK_STATE_DIR`` shallower (#1531, #1542).
    socket: str | None = None
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
    # When true, the consent banner must be re-accepted on every fresh app
    # load / login (acceptance is tracked in-memory for the session only).
    # When false (default) acceptance is cached permanently against the
    # banner text hash (#1544).
    login_banner_every_visit: bool = False
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

    @model_validator(mode="after")
    def _resolve_socket_and_ports(self) -> "KlangkSettings":
        """Resolve the listen-shape settings: fold ``nginx_port`` into
        ``egress_port``, default ``socket``, enforce egress≠browser and the
        socket-length invariant.

        Runs after ``_resolve_indirections`` (so ``nginx_port`` /
        ``egress_port`` / ``socket`` string values are already
        ``file:``/``cmd:``-resolved) and after ``_require_dirs`` (so
        ``state_dir`` is non-None for the ``socket`` default). After this,
        **every consumer reads ``self.egress_port`` and ``self.socket`` —
        nothing reads ``nginx_port``.**

        ``KLANGK_NGINX_PORT`` deprecation ladder (no hard error, #1542):

        - ``egress_port`` set, ``nginx_port`` unset → use egress (clean).
        - ``egress_port`` unset, ``nginx_port`` set → use ``nginx_port`` as
          the egress port + a loud deprecation warning.
        - both set → ``egress_port`` wins, ``nginx_port`` ignored + a warning.
        """
        # --- KLANGK_NGINX_PORT → egress_port fold ---
        if self.nginx_port is not None:
            if self.egress_port is not None:
                logger.warning(
                    "KLANGK_NGINX_PORT is ignored because KLANGK_EGRESS_PORT "
                    "is also set; KLANGK_EGRESS_PORT takes precedence. "
                    "KLANGK_NGINX_PORT is deprecated — remove it and use "
                    "KLANGK_EGRESS_PORT."
                )
            else:
                logger.warning(
                    "KLANGK_NGINX_PORT is deprecated; rename it to "
                    "KLANGK_EGRESS_PORT. Its value is used as the egress "
                    "port for this run, but a future release will stop "
                    "recognizing KLANGK_NGINX_PORT."
                )
                self.egress_port = self.nginx_port
        if self.egress_port is None:
            self.egress_port = "8995"

        # --- egress ≠ browser port (ingress/egress firewall separation) ---
        if self.port is not None and self.egress_port == self.port:
            raise ValueError(
                f"KLANGK_EGRESS_PORT ({self.egress_port!r}) must differ from "
                f"KLANGK_PORT ({self.port!r}). The two nginx listeners carry "
                "browser ingress vs container egress so operators can firewall "
                "them separately; sharing a port defeats that and nginx cannot "
                "bind two server blocks to the same port."
            )

        # --- socket default + length guard (#1531, #1542) ---
        if self.socket is None:
            self.socket = os.path.join(self.state_dir, "klangk.sock")
        # Portable bound: macOS sun_path is 104 usable bytes; Linux is 107.
        # Use the smaller so one check is correct on both platforms.
        max_socket_len = 104
        if len(self.socket) > max_socket_len:
            raise ValueError(
                f"KLANGK_SOCKET resolves to {self.socket!r} "
                f"({len(self.socket)} chars), which exceeds the "
                f"{max_socket_len}-character AF_UNIX sun_path limit. "
                "Either set KLANGK_SOCKET to a shorter absolute path "
                "(e.g. /tmp/klangk.sock) or move KLANGK_STATE_DIR shallower "
                "in the filesystem. (The kernel caps UDS paths at "
                "sockaddr_un.sun_path: 108 bytes incl. NUL on Linux → 107 "
                "usable; 104 on macOS, so a deep state_dir overflows the "
                "default <state_dir>/klangk.sock and the bind fails.) "
                "See #1531."
            )
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
