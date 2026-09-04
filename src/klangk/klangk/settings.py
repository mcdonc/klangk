"""Typed configuration via pydantic-settings (#1394, #1395).

This module is the single source of truth for all ``KLANGKD_*`` configuration.
It replaces the ad-hoc ``resolve_env_value`` / ``resolve_env_bool`` /
``os.environ.get`` reads that were scattered across the codebase.

Design (see #1392, #1394):

- **pydantic-settings** reads env vars (``env_prefix="KLANGKD_"``) into a typed
  ``KlangkSettings`` model.  Fields are ``Optional[str]`` in this chunk to
  preserve the exact string-returning behavior of the legacy
  ``resolve_env_value``; typed fields (``int`` / ``bool`` / ``list``) arrive
  incrementally as call sites migrate to direct ``settings.field`` access.
- **``file:`` / ``cmd:`` resolution** is applied once, at construction, by
  the ``_resolve_indirections`` model validator on :class:`KlangkSettings`
  (#1461). Every ``settings.field`` read thereafter returns the already-
  resolved value — no caller wraps in ``resolve_indirection``. The private
  ``resolve_indirection`` survives for two callers: that validator, and the
  non-``KLANGKD_`` path of :func:`resolve_env_value` (feature-declared dynamic
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
import math
import os
import re
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping


import getpass

from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# netfilter.py is pure-stdlib (no settings import), so this top-level import
# is cycle-safe. Used by the netfilter_default_domains field validator below.
from klangk.netfilter import parse_allowed_domains

# Same for model.workspaces' normalize_classification_banner (model imports
# neither settings nor anything that does) — used by the
# classification_banner field validator below.
from klangk.model.workspaces import normalize_classification_banner

# And util's cmd: runner (util is stdlib-only, so no cycle) — the single
# implementation of the ``cmd:`` secret prefix, shared with
# util.resolve_file_value.
from klangk.util import run_cmd_value
from pydantic_settings.sources.providers.env import parse_env_vars

logger = logging.getLogger(__name__)

# Valid values for ``KLANGKD_AUTH_MODES``. ``None`` (unset) defaults to ``none``
# at *read* time (in ``oidc.auth_modes``), but a non-None value must be one of
# these — rejecting typos at construction so a misspelled mode fails loudly at
# boot instead of silently downgrading to the no-auth ``none`` mode (which
# freely issues an admin token). See the ``auth_modes`` field validator below.
_VALID_AUTH_MODES = frozenset({"password", "oidc", "both", "none"})

# Container memory-limit syntax (KLANGKD_CONTAINER_MEMORY_LIMIT, #34):
# matches the grammar docker/go-units ParseSize (podman's --memory) accepts —
# a positive number (decimals ok, parsed via ParseFloat) with an optional
# unit suffix: a single base unit k/m/g/t/p (case-insensitive) and an
# optional trailing b (case-insensitive), so 2g, 2gb, 2G, 512mb, 2t, 1024
# (bare bytes), 1.5g all pass. go-units does NOT accept the IEC i-forms
# (kib/gib/...), so neither do we. Captures the numeric portion in group
# "num" so the validator can reject 0 (podman treats --memory=0 as "no
# limit", same ambiguity the PIDs validator rejects). Syntax guard only —
# podman is the authority on what the runtime can actually apply; a value it
# ultimately can't honour surfaces loudly at podman create.
_CONTAINER_MEM_LIMIT_RE = re.compile(
    r"^(?P<num>\d+(\.\d+)?)[kKmMgGtTpP]?[bB]?$"
)

# The XDG "klangkd" subdir used by the default-roots (state + config). The
# server's tree is ``klangkd`` (the binary name) — distinct from the CLI's
# ``klangk`` tree. Different audiences, different shapes: server state is
# GB-scale operator-owned DBs + UDS; CLI state is a few hundred bytes of
# user tokens. Splitting at the filesystem level mirrors the code-level
# isolation rule (``klangk.cli`` must not import from the server). See
# #1607 / #1644 / #1646.
XDG_SUBDIR = "klangkd"


def _is_unset(v) -> bool:
    """True when a raw setting value is ``None`` or empty.

    Shared first check of the strict coercers: unset env / empty YAML
    means "use the default", never an error.
    """
    return v is None or v == ""


def _reject_bool_or_float(v, *, bool_msg: str, float_msg: str) -> None:
    """Raise ``ValueError`` when *v* is a bool or a native float.

    Shared type guard for the strict integer coercers (#2303 / #2603):
    a bool or a native YAML float must abort startup rather than
    silently truncate. The messages are caller-supplied so each coercer
    keeps its exact operator-facing wording.
    """
    if isinstance(v, bool):
        raise ValueError(bool_msg)
    if isinstance(v, float):
        raise ValueError(float_msg)


def _coerce_prune_int(v, name: str, *, default: int) -> int:
    """Shared coercion for the #2303 prune knobs (retention days / row cap).

    Accepts an integer string (env) or a real int (YAML); ``None`` / empty
    -> the field default. A native YAML float (e.g. ``7.5``) or bool
    (``true``) is rejected rather than silently truncated / coerced --
    same strict-on-malformed posture as ``container_pids_limit`` (a
    truncated ``0.5`` days would silently disable the feature). ``0`` is
    the meaningful floor for both knobs (it disables).
    """
    if _is_unset(v):
        return default
    _reject_bool_or_float(
        v,
        bool_msg=(
            f"{name}={v!r} must be a non-negative integer (0 disables), "
            "not a boolean."
        ),
        float_msg=(
            f"{name}={v!r} must be a non-negative integer (0 disables) -- "
            "use an integer like 30, not 0.5."
        ),
    )
    try:
        value = int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name}={v!r} must be a non-negative integer (0 disables)."
        ) from exc
    if value < 0:
        raise ValueError(
            f"{name}={v!r} must be >= 0 (0 disables; negative is not a "
            "meaningful window)."
        )
    return value


def _parse_positive_float(v, name: str) -> float:
    """``float(v)`` with strict finite/positive checking (#2562 / #2603).

    Shared body of the two positive-float coercers: non-numeric input
    raises with the *unset* hint; nan/inf or ``<= 0`` raises the
    finite/positive error, both naming *name*.
    """
    try:
        value = float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name}={v!r} must be a positive number, or unset."
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{name}={v!r} must be a finite, positive number "
            "(nan/inf and <= 0 are rejected)."
        )
    return value


def _coerce_positive_float(v, name: str) -> float | None:
    """Coerce *v* to a finite positive float or None; raise with *name*.

    Shared body of the numeric limit validators (#2562): ``None``/empty
    → ``None`` (no cap); non-numeric, non-finite (``nan``/``inf``), or
    ``<= 0`` raises ``ValueError`` naming *name* (the ENV_VAR string) so
    ``KlangkSettings(...)`` construction fails and the server refuses to
    boot (#34: a safety control must not silently disable itself on a
    typo).
    """
    if _is_unset(v):
        return None
    return _parse_positive_float(v, name)


def _coerce_positive_int(v, name: str) -> int | None:
    """Coerce *v* to a positive int or None; raise with *name*.

    Shared body of the integer limit validators (#2562): ``None``/empty
    → ``None`` (no cap); a native float (e.g. YAML ``1.5``) is rejected
    rather than silently truncated; non-integer or ``<= 0`` raises
    (``0`` is "unset", not a cap).
    """
    if _is_unset(v):
        return None
    if isinstance(v, float):
        raise ValueError(
            f"{name}={v!r} must be a positive integer "
            "(got a float — use an integer, not 1.5)."
        )
    try:
        value = int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name}={v!r} must be a positive integer, or unset."
        ) from exc
    if value <= 0:
        raise ValueError(f"{name}={v!r} must be > 0.")
    return value


# Sanity ceiling for the KLANGKD_PASSWORD_REQUIRE_* counts: passwords are
# capped at 72 bytes (auth.MAX_PASSWORD_BYTES), so requiring more than 72
# of one class would make every password unsettable. Duplicated here to
# avoid an import cycle with klangk.auth.
_PASSWORD_REQUIRE_MAX = 72

# The KLANGKD_PORT / KLANGKD_EGRESS_PORT / KLANGKD_PROXY_PORT validation
# message (#3124): a set port must be numeric 1-65535; empty means unset.
_BAD_PORT_MSG = (
    "{}={!r} is invalid. Must be a TCP port number (1-65535), or "
    "unset/empty to use the default."
)

# Ceiling for KLANGKD_PASSWORD_HISTORY_COUNT (#2582): every remembered
# hash costs one PBKDF2 verify per password set (in a worker thread, but
# still real CPU), so an unbounded count is a self-inflicted DoS knob.
# 24 matches the ceiling of Windows' "Enforce password history"
# policy, a common industry benchmark for this knob.
_PASSWORD_HISTORY_MAX = 24


def _resolve_numeric_indirection(v, name: str):
    """Resolve ``file:``/``cmd:`` on a raw numeric-setting value (#2603).

    Retyping the numeric fields to ``int``/``float`` takes them out of
    ``_resolve_indirections``' str-only pass, so a value that arrives as
    an indirection reference (``smtp_port: file:/run/secrets/port`` —
    legal while the field was ``str``) must be resolved **here**, before
    coercion. Plain strings pass through unchanged; a failed resolution
    raises so construction fails fast (matching the
    ``_resolve_indirections`` posture).
    """
    if isinstance(v, str) and v.startswith(("file:", "cmd:")):
        resolved = resolve_indirection(v, name)
        if resolved is None:
            raise ValueError(
                f"KLANGKD_{name.upper()} could not be resolved: the "
                "file:/cmd: reference failed. See logs for detail."
            )
        return resolved
    return v


def _coerce_setting_int(
    v, name: str, *, minimum: int = 1, default: int | None = None
) -> int | None:
    """Coerce a numeric setting to int; raise with *name* (#2603).

    Accepts every input form the sources produce: a native ``int`` (bare
    YAML ``login-lockout-failures: 5``), an integer string (env var,
    quoted YAML), or a ``file:``/``cmd:`` reference that resolves to one.
    ``None``/empty → *default* — the field's declared default, so an
    explicitly-emptied value can never surface as ``None`` on a field
    whose consumers assume a number (the request-time ``int(None)``
    crashes of #2603's review). Fields whose default **is** None (the
    ``health_check_*`` trio) pass ``default=None`` and stay optional.
    Bools, native floats (``1.5`` — silently truncating would hide a
    typo), non-integers, and values below *minimum* raise so the server
    refuses to boot on a malformed config instead of failing at request
    time.
    """
    v = _resolve_numeric_indirection(v, name)
    if _is_unset(v):
        return default
    _reject_bool_or_float(
        v,
        bool_msg=(
            f"{name}={v!r} must be an integer >= {minimum}, not a boolean."
        ),
        float_msg=(
            f"{name}={v!r} must be an integer, not a float (use 5, not 5.0)."
        ),
    )
    try:
        value = int(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name}={v!r} must be an integer >= {minimum}, or unset."
        ) from exc
    if value < minimum:
        raise ValueError(f"{name}={v!r} must be >= {minimum}.")
    return value


def _coerce_setting_float(
    v, name: str, *, default: float | None = None
) -> float | None:
    """Coerce a numeric setting to a positive float (#2603).

    Same contract as :func:`_coerce_setting_int` for float fields:
    native float (bare YAML), numeric string (env / quoted YAML),
    ``file:``/``cmd:`` reference, ``None``/empty → *default* (the
    declared field default; None only for genuinely-optional fields).
    Bools, non-finite or non-numeric values, and values <= 0 raise (a
    token lifetime or health-check interval of 0 or less is always a
    typo).
    """
    v = _resolve_numeric_indirection(v, name)
    if _is_unset(v):
        return default
    if isinstance(v, bool):
        raise ValueError(
            f"{name}={v!r} must be a positive number, not a boolean."
        )
    return _parse_positive_float(v, name)


def parse_bool_setting(value: str | None) -> bool:
    """Truthiness shared by the str-typed boolean settings (#2796).

    Several settings (``allow_sudo``, ``allow_autostart``, ...) are
    str-typed for env-var fidelity but consumed as booleans; every
    consumer matches the same truthy forms (``1`` / ``true`` / ``yes``,
    case-insensitive, whitespace-tolerant). Centralizing the parse keeps
    the reads identical — the #2796 unification of
    ``api.common.autostart_allowed``, the boot auto-start gate in
    ``workspaces.auto_start_workspaces`` (which previously used plain
    string truthiness, so ``allow_autostart: "false"`` read as
    *enabled*), the ``smtp_use_tls`` consumer in ``emailsvc``, and the
    ``test_mode`` registration gate in ``api.auth``.

    Note the deliberate difference from the native-``bool`` fields'
    coercion (``_coerce_fips_mode`` et al.): those also accept ``on``/
    ``off`` spellings; this family never has, so it still doesn't.
    """
    return (value or "").strip().lower() in ("1", "true", "yes")


def _coerce_podman_size(v, name: str) -> str | None:
    """Validate a podman size-string (``2g``/``512mb``/``1024``) or None.

    Shared body of the size-string validators (#2562): the
    go-units/ParseSize grammar via :data:`_CONTAINER_MEM_LIMIT_RE`
    (b/k/m/g/t/p + optional trailing b, case-insensitive; no IEC
    i-forms), ``None``/empty → ``None``, malformed or ``<= 0`` raises
    naming *name*. Returns the stripped string unchanged (podman remains
    the authority on what the runtime can apply).
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    m = _CONTAINER_MEM_LIMIT_RE.match(s)
    if m is None:
        raise ValueError(
            f"{name}={v!r} is invalid. Expected a positive size with an "
            "optional unit suffix (b/k/m/g/t/p, e.g. 2g, 2gb, 512m, 1024)."
        )
    if float(m.group("num")) <= 0:
        raise ValueError(f"{name}={v!r} must be > 0.")
    return s


def _xdg_dir(var: str, fallback: str) -> str:
    """``$VAR`` or the XDG base-dir spec *fallback* expanded (#2562).

    The shared resolution of ``XDG_CONFIG_HOME``→``~/.config`` and
    ``XDG_STATE_HOME``→``~/.local/state``: unset/empty resolves to the
    spec's default. Applies on Linux *and* macOS (we deliberately do not
    switch to ``~/Library/Application Support`` — see #1607's
    cross-platform note).
    """
    return os.environ.get(var) or os.path.expanduser(fallback)


def xdg_config_home() -> str:
    """``$XDG_CONFIG_HOME`` (→ ``~/.config``).

    Used for the config-tree default of ``config_dir`` (→ ``customize_dir``,
    #1644/#1649).
    """
    return _xdg_dir("XDG_CONFIG_HOME", "~/.config")


def _xdg_state_home() -> str:
    """``$XDG_STATE_HOME`` (→ ``~/.local/state``).

    Used for the default ``state_dir`` (#1644): the UDS, rendered proxy
    conf/pid, ssh-agent log, and the DB are disposable runtime state, so
    ``XDG_STATE_HOME`` is the principled home.
    """
    return _xdg_dir("XDG_STATE_HOME", "~/.local/state")


def _safe_getuser() -> str:
    """Return the invoking Unix user, with a fallback for uid-less envs.

    Used for the dynamic ``default_user`` default (#1645): a bare ``klangkd``
    seeds ``<unixuser>@example.com`` so the solo user's identity is derived
    from who's actually running it. In containers / CI where the uid has no
    passwd entry, ``getpass.getuser()`` raises — fall back to ``"user"`` so
    construction doesn't crash (the identity is cosmetic in ``none`` mode).
    """
    try:
        return getpass.getuser()
    except OSError:
        # In containers/CI where the uid has no passwd entry, getpass.getuser()
        # raises OSError. Fall back to "user" so construction doesn't crash
        # (the identity is cosmetic in none mode).
        return "user"


def _default_state_dir() -> str:
    """Derive the default ``state_dir``; raise when no home is derivable.

    If neither ``$XDG_STATE_HOME`` nor ``$HOME`` is set (the pathological
    case — no way to compute a home path), the default cannot be derived
    and we raise, preserving the fail-fast intent (#1461) for the
    genuinely-unconfigured case. We check ``$HOME`` directly rather than
    probing ``expanduser("~")``, which falls back to the pwd database
    (the real home from /etc/passwd) when HOME is unset and so would
    never actually be "~".
    """
    if not os.environ.get("HOME") and not os.environ.get("XDG_STATE_HOME"):
        raise ValueError(
            "KLANGKD_STATE_DIR is required (env var or config file), "
            "and no default could be derived: $XDG_STATE_HOME and $HOME "
            "are both unset. Set KLANGKD_STATE_DIR to the runtime state "
            "directory (UDS socket, rendered proxy config, pid file)."
        )
    return os.path.join(_xdg_state_home(), XDG_SUBDIR)


# Re-exported for backward compat — callers that ``from ..util import ...``
# still work because util.py re-exports these.  ``resolve_indirection`` is
# NOT exported: ``file:``/``cmd:`` resolution now happens once, inside
# ``KlangkSettings`` at construction (#1461).  The private ``resolve_indirection``
# is shared by the model validator and the non-KLANGK path of
# ``resolve_env_value`` (feature-declared dynamic keys).
__all__ = [
    "KlangkSettings",
    "resolve_dynamic_config",
]

# ---------------------------------------------------------------------------
# file: / cmd: indirection resolver (shared by all read paths)
# ---------------------------------------------------------------------------
# (The cmd: runner itself is util.run_cmd_value; only the file: reader
# lives here — its OSError is returned to the caller for strerror-only
# logging, a contract util's string-returning read_file_value does not
# have.)

# Default frontend dir: the built Flutter Web UI ships inside the wheel at
# klangk/frontend (force-include, #1600), so an installed (non-editable)
# package serves the UI out of the box. Resolved from this module's location
# so it lands at <site-packages>/klangk/frontend for a wheel install.
# Source-tree deployments (devenv, the host container) don't have the
# in-package dir -- they set KLANGKD_FRONTEND_DIR to the repo's
# src/frontend/build/web (see devenv.nix, src/containers/host/Dockerfile).
# KLANGKD_FRONTEND_DIR always overrides (#1456).
_DEFAULT_FRONTEND_DIR = str(Path(__file__).resolve().parent / "frontend")


def _read_file(value: str) -> tuple[str | None, OSError | None]:
    """Strip a ``file:`` prefix and read the referenced file."""
    path = value[5:]
    try:
        with open(path) as f:
            return f.read().strip(), None
    except OSError as e:
        e.filename = e.filename or path
        return None, e


def _resolve_file_ref(value: str, key: str) -> str | None:
    """Resolve one ``file:`` reference; log strerror-only on failure."""
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


def _resolve_cmd_ref(value: str, key: str) -> str | None:
    """Resolve one ``cmd:`` reference; log the failure reason."""
    contents, err = run_cmd_value(value)
    if err is not None:
        logger.error(
            "Cannot resolve %s via cmd: %s",
            key or "config value",
            err,
        )
        return None
    return contents


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

    Private: ``file:``/``cmd:`` resolution for ``KlangkSettings`` fields
    happens once at construction via the ``_resolve_indirections`` model
    validator (#1461).  This helper survives for two callers: that
    validator, and the non-KLANGK path of ``resolve_env_value`` (feature-
    declared dynamic keys discovered from ``package.json``, which are not
    settings fields and so cannot be resolved at construction).
    """
    if value is None:
        return None
    if value.startswith("file:"):
        return _resolve_file_ref(value, key)
    if value.startswith("cmd:"):
        return _resolve_cmd_ref(value, key)
    return value


# ---------------------------------------------------------------------------
# KlangkSettings model
# ---------------------------------------------------------------------------

# The insecure default JWT secret. Single source of truth — auth.py's
# Auth.jwt_secret_is_secure() compares against this (#1501).
INSECURE_DEFAULT_SECRET = "change-this-to-a-random-secret"


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
    ``klangk.yaml`` and the OIDC provider dicts).  pydantic-settings matches
    config keys against snake_case field names only, so a bare
    ``YamlConfigSettingsSource`` silently ignores hyphenated keys.  This
    subclass normalizes top-level hyphenated keys (``proxy-port`` →
    ``proxy_port``) so an operator may write **either** form for any key
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


class NixSeedConfig(BaseModel):
    """Per-workspace ``/nix`` seed config (#2198, #2201, #2220).

    One seed path consumed by one of two backends (selected by ``type``).
    Omit ``nix_seed`` entirely — or leave ``path`` unset — to disable the
    feature; nix is then image-only (pick the nix image ``klangk-workspace-nix``
    for its baked ``/nix``). Image selection is always the user's; this never
    forces an image.
    """

    # How to consume the seed. "btrfs-snapshot": a CoW btrfs-subvolume snapshot
    # per workspace (needs a btrfs filesystem mounted with
    # user_subvol_rm_allowed). "fuse-overlayfs": a fuse-overlayfs overlay per
    # workspace (any filesystem; the default).
    type: Literal["btrfs-snapshot", "fuse-overlayfs"] = "fuse-overlayfs"
    # Path to the seed tree (klangk-build-nix-seed output: holds nix/ +
    # nix.conf). For "btrfs-snapshot" it must be a btrfs subvolume (loaded by
    # klangk-load-nix-seed-btrfs); for "fuse-overlayfs", a plain directory.
    path: str | None = None


# The str-typed settings consumed as booleans (their consumers match
# "1"/"true"/"yes" via parse_bool_setting). One tuple feeds the
# field validator below and the tests, so the family can't drift
# (#2796).
BOOL_STRING_FIELDS = (
    "allow_sudo",
    "allow_autostart",
    "disable_registration",
    "disable_invites",
    "disable_tmux",
    "prevent_insecure_jwt_secret",
    "allow_insecure_no_auth",
    "reject_proxy_headers",
    "smtp_use_tls",
    "test_mode",
)

# The str-typed settings consumed as port numbers / second counts (their
# consumers int()/float() the string). They stay str-typed — env vars are
# always strings, ``file:``/``cmd:`` indirection keeps working — but a
# native YAML int is translated to its string form at construction so a
# bare ``port: 8997`` parses the same as ``port: "8997"`` (#2967). The
# deprecated ``proxy_port`` alias is included: it is still folded into
# ``egress_port`` by ``_resolve_socket_and_ports``, so its natural form
# must parse too. One tuple feeds the field validator below and the
# tests, so the family can't drift (same shape as BOOL_STRING_FIELDS,
# #2796).
INT_STRING_FIELDS = (
    "port",
    "egress_port",
    "proxy_port",
    "bridge_timeout_seconds",
    "idle_timeout_seconds",
)


class KlangkSettings(BaseSettings):
    """Typed configuration for all ``KLANGKD_*`` environment variables.

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
        env_prefix="KLANGKD_",
        extra="ignore",
        # env_nested_delimiter="__" lets the nix_seed sub-model read
        # KLANGKD_NIX_SEED__TYPE / KLANGKD_NIX_SEED__PATH. Safe for the flat
        # fields: every field name is single-underscore snake_case, so no flat
        # env var contains "__" and none is misparsed as a nested table.
        env_nested_delimiter="__",
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
        ``os.environ``, so a reload after an operator edits ``KLANGKD_*``
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
    jwt_secret: str | None = INSECURE_DEFAULT_SECRET
    prevent_insecure_jwt_secret: str = ""
    default_user: str | None = None
    default_password: str | None = None
    # Numeric settings are typed int/float and accept every source form —
    # bare YAML numbers (``access-token-hours: 48``), quoted strings
    # (``"48"``), env strings, and file:/cmd: references — via the
    # _coerce_numeric_* before-validators (#2603). ``None`` (unset) means
    # the caller-side default applies.
    access_token_hours: float | None = 24.0
    workspace_token_hours: float | None = 24.0
    min_password_length: int | None = 8
    # Character-class complexity counts (#2581). Each is the number of
    # characters of that class a password must contain; 0 (the default)
    # disables that class. Ints (YAML) or integer strings (env) both work
    # (``_coerce_setting_int``, minimum=0), e.g.
    # KLANGKD_PASSWORD_REQUIRE_UPPER=2 demands two uppercase letters.
    password_require_upper: int = 0
    password_require_lower: int = 0
    password_require_digit: int = 0
    password_require_special: int = 0
    # Password reuse window (#2582): how many **previous** password
    # hashes to retire into history per user — the current hash always
    # counts separately. A new password is rejected (400) when it
    # matches the current or any retired hash. 0 (the default)
    # disables reuse checking and history recording entirely. Capped
    # at 24 (each retired hash costs a PBKDF2 verify per set).
    password_history_count: int = 0
    login_lockout_failures: int | None = 5
    login_lockout_duration: int | None = 900
    login_lockout_window: int | None = 300
    # Concurrent-session cap per user (#2585). 0 (the default) = no limit.
    # Each login/verification/reset/invite/OIDC/local issuance counts as
    # one session; a refresh replaces the old session's row (same slot).
    # When a new login pushes a user past the cap, the OLDEST session is
    # revoked via the token blocklist (its next HTTP request 401s and
    # its next WS connect is rejected with 4001 -> client logout).
    max_sessions_per_user: int | None = 0
    # Dormant-account auto-disable (#2588). Accounts (except the system
    # agent and members of the admin group) whose newest activity
    # signal — last API access, last login, or creation — is older than
    # this many days are disabled by an hourly sweep; login and API
    # access then fail with 403 until an admin re-enables them via
    # PATCH /admin/users/{id}. 0 disables the sweep. Reloadable on
    # SIGHUP (read live by the sweeper each pass).
    inactivity_disable_days: int = 35
    disable_registration: str = ""
    disable_invites: str = ""
    invite_expire_hours: int | None = 72
    allow_insecure_no_auth: str = ""
    reject_proxy_headers: str | None = None
    trusted_proxy_cidrs: str | None = "127.0.0.1,::1"

    # --- Logging ---
    # log_level: root logger level for the klangkd backend. A level name
    # (DEBUG/INFO/WARNING/ERROR/CRITICAL, any case) or a numeric string.
    # Defaults to INFO. Applied by ``klangk.logger.configure(settings)`` in
    # build_app, and re-applied on every SIGHUP reload (after the settings
    # swap), so ``KLANGKD_LOG_LEVEL`` can be changed without a process restart
    # (#1467). The field validator below rejects garbage at construction
    # (fail-fast) so a typo'd level aborts boot rather than silently leaving
    # logging at the wrong verbosity.
    log_level: str = "INFO"
    # log_format: root logger output format — ``text`` (the colored console
    # format; default) or ``json`` (one JSON object per line: ISO-8601 UTC
    # timestamp, level, logger, message, plus exc_info when present) for
    # SIEM ingestion (#3156, ASD-STIG V-222481/482). Like log_level, applied
    # by ``klangk.logger.configure(settings)`` in build_app and re-applied on
    # every SIGHUP reload; the validator below rejects anything but
    # text/json (fail-fast) at construction.
    log_format: str = "text"

    # --- Server / network ---
    # listen: the proxy's **browser** interface/address (e.g. ``127.0.0.1``,
    # ``0.0.0.0``). Rendered as ``listen {listen}:{port};`` only when
    # ``KLANGKD_PORT`` is set (full/browser mode). Default ``127.0.0.1``
    # (loopback) keeps the browser listener reachable only from the operator's
    # machine unless an operator deliberately widens it (#1542). The
    # polymorphic socket-path meaning (#1422) never shipped in a release and
    # is retired — the UDS path is now ``KLANGKD_SOCKET``.
    listen: str = "127.0.0.1"
    # port: the proxy's **browser** port (e.g. ``8997``). **No default** — unset
    # ⇒ headless mode (no browser listener is rendered; only the container-
    # egress listener on ``KLANGKD_EGRESS_PORT`` is served). Set ⇒ full/browser
    # mode (browser UI + API + hosted apps on ``listen {listen}:{port};``).
    port: str | None = None
    # egress_port: the container-egress port the proxy listens on for
    # container→backend traffic (``/llm-proxy``, ``/api/v1/browser-delegate``).
    # Serves both headless and full
    # modes. Default ``8995``. Must differ from ``port`` so ingress vs egress
    # can be firewalled separately (#1542). ``None`` here is a sentinel —
    # ``_resolve_socket_and_ports`` resolves it to ``"8995"`` (or folds the
    # deprecated ``KLANGKD_PROXY_PORT`` into it).
    egress_port: str | None = None
    # egress_listen: the interface/address the proxy binds for the container-
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
    # proxy_port: **deprecated** alias for ``egress_port`` (#1542, #1430).
    # Folded into ``egress_port`` by ``_resolve_socket_and_ports``: if both
    # are set, ``egress_port`` wins and ``proxy_port`` is ignored (with a
    # warning); if only ``proxy_port`` is set it is used as the egress port
    # (with a deprecation warning). Renamed from ``nginx_port``/``KLANGKD_NGINX_PORT``
    # to drop nginx-specific terminology (#1430); the old ``KLANGKD_NGINX_PORT``
    # name is no longer recognized. To be removed in a future release.
    # **Callers read ``settings.egress_port`` — nothing reads ``proxy_port``
    # except that one validator.**
    proxy_port: str | None = None
    port_range_start: int | None = 9000
    # socket: the backend UDS path klangkd binds. Default
    # ``<state_dir>/klangk.sock`` (derived in ``_resolve_socket_and_ports``
    # after ``state_dir`` is resolved). A fail-fast validator rejects resolved
    # paths exceeding the portable AF_UNIX ``sun_path`` bound (104 chars) with
    # a diagnostic telling the deployer to shorten ``KLANGKD_SOCKET`` or move
    # ``KLANGKD_STATE_DIR`` shallower (#1531, #1542).
    socket: str | None = None
    # caddy_admin_socket: the admin-API UDS path for the Caddy engine
    # (KLANGKD_PROXY_ENGINE=caddy, #1559). Default
    # ``<state_dir>/caddy-admin.sock`` (derived in ``_resolve_socket_and_ports``
    # after ``state_dir`` is resolved — mirrors ``socket``). A fail-fast
    # validator rejects resolved paths exceeding the portable AF_UNIX
    # ``sun_path`` bound (104 chars), pointing the deployer at
    # ``KLANGKD_CADDY_ADMIN_SOCKET`` / ``KLANGKD_STATE_DIR`` (#1636 — the
    # backend-UDS ``socket`` field has the same guard from #1531/#1542).
    # The nginx engine never reads this field.
    caddy_admin_socket: str | None = None
    # state_dir: runtime state (the UDS when listen is a socket path, rendered
    # proxy config, pid). Defaults to ``$XDG_STATE_HOME/klangkd`` (→
    # ``~/.local/state/klangkd`` when the var is unset, incl. macOS) when no
    # explicit value is supplied (#1644); explicit ``KLANGKD_STATE_DIR`` /
    # config-file values still win (devenv pins it to ``$DEVENV_STATE/klangk``
    # via devenv.nix; the host container sets ``/tmp/klangk-state``). If
    # neither ``$XDG_STATE_HOME`` nor ``$HOME`` is set, construction fails
    # fast (the #1461 intent preserved for the genuinely-unconfigured case).
    state_dir: str | None = None
    # proxy_bin: the proxy executable the renderer spawns (Caddy in 2.X,
    # #1642). Falls back to shutil.which("caddy") then /usr/bin/caddy at
    # render time. Renamed from ``nginx_bin``/``KLANGKD_NGINX_BIN`` (#1430);
    # the old ``KLANGKD_NGINX_BIN`` name is no longer recognized.
    proxy_bin: str | None = None
    # frontend_dir: directory the built Flutter Web UI is served from
    # (#1456, #1600). Defaults to the in-package location (klangk/frontend,
    # computed above as _DEFAULT_FRONTEND_DIR) so a packaged/installed
    # klangkd serves the UI out of the box; source-tree deployments (devenv,
    # the host container) override via KLANGKD_FRONTEND_DIR to point at the
    # repo's src/frontend/build/web. The UI is mounted only when the dir
    # exists; build_app logs a warning otherwise (#1600).
    frontend_dir: str = _DEFAULT_FRONTEND_DIR
    # websocket_msg_size_max: max WebSocket message size (bytes), passed to uvicorn.
    # Default 16 MiB; klangkd reads it through the typed config (config file +
    # file:/cmd: resolution), not raw env.
    websocket_msg_size_max: int | None = 16777216
    cors_origins: str | None = None
    # dns_servers: comma-separated DNS nameserver IPs passed to workspace
    # containers via podman --dns (container_dns_config() → create_container).
    # Pairs with dns_search (#2055): dns_servers is the ``nameserver`` line and
    # dns_search is the ``search`` line of the container's /etc/resolv.conf.
    # Both read live off settings (reloadable on SIGHUP); apply to newly-created
    # containers (a running container keeps its resolv.conf until recreated).
    dns_servers: str = ""
    # dns_search: comma-separated DNS search domains passed to workspace
    # containers via podman --dns-search (#2055), so short hostnames that rely
    # on a search suffix (e.g. ``db`` → ``db.corp.example``) resolve inside
    # containers. Unset → podman's default search behavior (no change).
    dns_search: str = ""
    hosting_hostname: str | None = None
    hosting_proto: str | None = None
    hosting_base_path: str | None = None
    bridge_timeout_seconds: str | None = None
    idle_timeout_seconds: str | None = None

    # --- Container / workspace ---
    # data_dir: persistent storage (SQLite DB, workspace volumes). Defaults
    # to ``<state_dir>/data`` when unset (derived in the ``require_dirs``
    # validator after state_dir is resolved), so an operator who sets only
    # ``state_dir`` gets a sensible data location. An explicit
    # ``KLANGKD_DATA_DIR`` / config-file value wins (#1506).
    data_dir: str | None = None
    # config_dir: the config-tree root for user-edited, durable intent
    # (branding, email templates) — the config-tree analogue of
    # ``state_dir`` (#1649). Defaults to ``$XDG_CONFIG_HOME/klangkd`` (→
    # ``~/.config/klangkd``, read-with-fallback) when unset; ``customize_dir``
    # derives from the resolved ``config_dir`` (like ``data_dir`` derives
    # from ``state_dir``). An explicit ``KLANGKD_CONFIG_DIR`` wins; per-sub-dir
    # env vars still win over the derivation. Read at boot and on SIGHUP
    # (reloadable, like the sub-dirs).
    config_dir: str | None = None
    # customize_dir: branding + email templates — user-edited, durable
    # intent, so it's **config**, not state. Defaults to
    # ``<config_dir>/custom`` (→ ``~/.config/klangkd/custom``) when unset,
    # deriving from the resolved ``config_dir`` (#1644, #1649); no longer
    # under ``state_dir``. Explicit ``KLANGKD_CUSTOMIZE_DIR`` still wins.
    customize_dir: str | None = None
    # features_enable: which compiled-in features (features) are turned on for
    # this deploy. Canonical semantics (#1655): unset → the manifest's
    # ``defaults`` list (the stock set, backwards-compatible); any explicit
    # value → exactly that comma-separated list, nothing implied (no `*`
    # form). The frontend reads its sibling ``features.json`` for the
    # per-feature metadata + defaults, and this value (forwarded via
    # ``/api/config``) for the deploy's chosen set; filtering happens in
    # ``main.dart`` before ``registry.register()``. Distinct from build-time
    # declaration (#1651): "what's compiled in" is build-time; "what's
    # turned on" is deploy-time. Read at boot and on SIGHUP (reloadable).
    features_enable: str | None = None
    image_name: str | None = "klangk-workspace"
    image_pull_policy: str | None = "never"
    allowed_images: str | None = None
    allowed_mount_roots: str | None = None
    allow_autostart: str = ""
    # allow_sudo: passwordless sudo for the klangk user inside workspace
    # containers, written as a sudoers rule at container-create time.
    # #3047: this flag is ONLY a ceiling — "is the per-workspace Allow
    # sudo box allowed to be checked". It grants nothing by itself:
    # sudo is on for a workspace only when its settings bag stores
    # allow_sudo: true AND this flag is on (see workspace_settings.
    # resolve_allow_sudo). Default "true"; reloadable on SIGHUP;
    # applies to containers started after the change.
    allow_sudo: str = "true"
    # per_handle_home: deploy-wide CEILING for per-handle homes (#2169
    # chunk 1, #2719, #3135). #3047's ceiling shape: the flag gates
    # whether a workspace may opt in to per-handle homes at all — it is
    # no longer the default a workspace override flips in either
    # direction. True = workspaces choose either layout (per_handle_home
    # on POST/PUT; omitted on create still stores this flag's value, so
    # an untouched create gets per-handle homes). False = every
    # workspace shares /home/klangk: a stored true is inert
    # (resolve_per_handle_home clamps at start/connect — m0009's
    # backfilled `true` population is never rewritten), which is what
    # hardened deploys (e.g. FIPS hosts needing one auditable home)
    # require. Default false (the #2723 Breaking flip). Read live off
    # settings at resolution time: reloadable on SIGHUP (applies to
    # containers started after the reload). The /config fields are
    # per_handle_home_available (the ceiling, authenticated-only) and
    # default_per_handle_home (the create default, pre-auth).
    per_handle_home: bool = False
    # classification_banner: deploy-wide default classification marking
    # for workspaces (#2768), free text (e.g. UNCLASSIFIED, CUI, SECRET).
    # Rendered as a persistent banner at the top and bottom of the web
    # workspace page and as a status line in the TUI (the STIG "mark
    # sensitive/classified output when required" control). A workspace
    # overrides it per workspace (``classification_banner`` on
    # POST/PUT /workspaces); NULL/absent workspaces inherit THIS value
    # at display time. Empty (the default) = no deploy-wide marking: no
    # banner is rendered anywhere and no screen space is reserved.
    # Reloadable on SIGHUP. Resolution is at display time, not create
    # time — clients re-resolve the deploy default on page (re)entry and
    # on every workspaces-changed push, so a reload re-marks inheriting
    # workspaces on the next re-resolve (not on already-idle open
    # screens). Validated by _coerce_classification_banner (same rules as
    # the per-workspace value); a malformed value aborts boot / denies
    # the reload.
    classification_banner: str = ""
    container_subnets: str | None = None
    # Nix workspace feature (#2198, #2201, #2220): per-workspace /nix from a
    # shared seed. ``nix_seed`` groups the seed path + the backend that
    # consumes it (see NixSeedConfig). Omit ``nix_seed`` entirely — or leave
    # its ``path`` unset — to disable the feature; nix is then image-only (pick
    # the nix image ``klangk-workspace-nix`` for its baked ``/nix``). Image
    # selection is always the user's; this never overrides it.
    nix_seed: NixSeedConfig = Field(default_factory=NixSeedConfig)
    # nix_enabled: master on/off switch for the per-workspace /nix feature
    # (#2560). Defaults to False — the feature is maturing (#2237, #2221),
    # so its surfaces stay hidden and new opt-ins are rejected until an
    # operator arms it. While off, Nix.ensure_workspace_nix is a no-op (a
    # workspace start with a stored nix flag logs once and proceeds without
    # the mount; re-enabling resumes it — the per-workspace layers persist),
    # the /api/v1/images nix_available field is false (all three create/edit
    # surfaces hide the toggle), and the API rejects a new/changed nix=true
    # opt-in (an echo of an already-stored true is tolerated). Workspace
    # delete still tears down per-workspace layers. The resolved armed status
    # is nix_enabled AND nix_seed.path (Nix.available). Reloadable on SIGHUP
    # (#1587) — every read is live off settings.
    nix_enabled: bool = False
    userns: str = "keep-id:uid=1000,gid=1000"
    podman_bin: str | None = "podman"
    disable_tmux: str = ""
    # browser_delegate_enabled: master switch for the browser-delegate
    # bridge (#2710) — the workspace-token-gated endpoints
    # (``/api/v1/browser-delegate{,/stream}``) that let a container drive
    # the user's browser tab (fetch with the user's cookies, clipboard,
    # feature actions) and read back everything it renders. That is a
    # workspace-data read channel that bypasses file permissions
    # entirely, so a hardened deploy may want it off. Defaults to True
    # (the feature ships on). Set False to: return 403 from both
    # endpoints, stop registering browser tabs for bridge routing, stop
    # attaching a browser ID into the container's tmux env (so
    # ``klangk-browser-id`` comes up empty and container-side helpers
    # fail fast), and advertise ``browser_delegate_enabled: false`` via
    # ``/api/v1/config`` so the frontend doesn't start its
    # BrowserDelegate. Read at boot and on SIGHUP (reloadable) — a reload
    # applies to requests and registrations after it; already-attached
    # container envs keep the stale ID but the endpoint 403s regardless.
    browser_delegate_enabled: bool = True
    health_check_interval: float | None = None
    health_check_startup_grace: float | None = None
    health_check_timeout: float | None = None
    # --- Host memory-pressure eviction (#2526) ---
    # memory_eviction_*: the k8s node-pressure-eviction analogue. When
    # memory availability (platform-aware: MemAvailable/MemTotal from
    # /proc/meminfo on Linux — plus the cgroup limit when running in a
    # memory-limited container such as Docker -m, since meminfo inside a
    # container shows the host; vm_stat/sysctl on macOS) stays below
    # memory_eviction_threshold_percent for
    # memory_eviction_sustain_polls consecutive polls (each
    # memory_eviction_poll_interval seconds), klangkd gracefully stops
    # the least-recently-active workspace with no connected clients —
    # one per poll — until availability recovers to
    # memory_eviction_recovery_percent (hysteresis, so availability
    # hovering at the threshold cannot flap-evict). Workspaces with
    # live terminal/browser clients are never chosen while an idle
    # one exists; evictions use the normal idle-stop path (state
    # preserved, next connect restarts) and emit a distinct
    # ``workspace_evicted`` WS event. Protects the host — and klangkd
    # itself — from the kernel OOM killer picking a random victim. All
    # fields read live off settings every poll: reloadable on SIGHUP
    # (#1587).
    memory_eviction_enabled: bool = True
    memory_eviction_threshold_percent: float = 10.0
    memory_eviction_recovery_percent: float = 15.0
    memory_eviction_sustain_polls: int = 3
    memory_eviction_poll_interval: float = 10.0
    hosted_ports_per_workspace: int | None = 5
    # netfilter_enabled: master on/off switch for per-workspace egress
    # filtering (#1774). Defaults to True — together with the defaulted
    # network_sidecar_image, FQDN egress filtering is available out of the
    # box (#2255). Set False to disable the feature entirely: enabled()
    # reports false and a workspace that declares allowed_domains fails to
    # start (fail-closed) until filtering is re-enabled. The
    # /api/v1/config field of the same name is the resolved armed status
    # (this switch AND network_sidecar_image set).
    netfilter_enabled: bool = True
    # netfilter_default_domains: a deploy-wide allow-list applied to every
    # workspace that doesn't declare its own (#1365). A workspace with a
    # non-empty allowed_domains *overrides* (replaces) this default; a
    # workspace with none inherits it. Unset (default) preserves the
    # original per-workspace-only behavior (empty = unrestricted).
    #
    # Accepts either a comma-separated string (env var) or a real list
    # (YAML config file), normalized + validated at construction by
    # _coerce_netfilter_default_domains. A malformed value aborts startup
    # (raises) rather than silently falling back to None — a SIGHUP reload
    # with a bad value is denied and keeps the old config (#1939, reversing
    # #1772).
    netfilter_default_domains: list[str] | None = None
    # network_sidecar_image: the container image for the FQDN network sidecar
    # (#2250). Filtered workspaces run two containers sharing a netns: this
    # sidecar (NET_ADMIN, runs the DNS proxy) + the workspace
    # (--network container:<sidecar>). Defaults to the published sidecar
    # image name (publishing alongside a release is tracked separately);
    # set to "" to disable egress filtering entirely. Read live (SIGHUP
    # reload-safe).
    network_sidecar_image: str = "klangk-network-sidecar"
    # egress_consent_rate_limit / egress_consent_timeout: interactive-mode
    # consent monitor (#2242) tuning. rate_limit caps pending requests per
    # workspace (anti attention-flood from adversarial containers); 0
    # disables the cap (unlimited pending holds, #3083). timeout is how long
    # a request stays pending before the monitor auto-expires it
    # (DECISION_EXPIRED). Now that consent gates the connection SYN (#2324)
    # the human window is the kernel's connect timeout (~127s), so the default
    # matches it (was 30s when the gate was the DNS query, bounded by
    # getaddrinfo). Read live (SIGHUP reload-safe).
    egress_consent_rate_limit: int = 50
    egress_consent_timeout: float = 120.0
    # consent_decider_timeout (#2308): a consent decider (a live client that
    # can approve/deny held egress) is registered while its WebSocket is
    # connected and pinging. This is the liveness window -- a decider whose
    # last ping is older than this is reaped (its registration dropped), so
    # an unclean disconnect (crash, network drop) can't leave a workspace
    # falsely "interactive" with a dead decider. Read live (SIGHUP
    # reload-safe).
    consent_decider_timeout: float = 45.0
    # egress_consent_retention_days / egress_consent_row_cap (#2303): bound
    # the ``egress_consent`` table on long-lived deploys. retention_days
    # deletes terminal rows older than the window (static policy records,
    # expired, revoked, and elapsed timed verdicts; rows still in effect --
    # ``forever``/``tilrestart`` or a timed window not yet elapsed -- are
    # enforcement state and are never pruned; they leave via workspace
    # deletion / the tilrestart reap). row_cap is a per-workspace
    # belt-and-suspenders cap on total rows (a flood of decided requests can
    # outpace age-based pruning; in-effect rows are exempt there too). 0
    # disables either knob. Swept hourly by the consent monitor; read live
    # (SIGHUP reload-safe -- a reload applies on the next sweep).
    egress_consent_retention_days: int = 30
    egress_consent_row_cap: int = 2000
    # container_events_retention_days / container_events_row_cap (#2924):
    # bound the ``container_events`` audit table (#2915) on long-lived
    # deploys. retention_days deletes rows older than the window; row_cap is
    # a deploy-wide cap on total rows (per-workspace fairness matters less
    # than a total bound for an audit log) keeping the newest when exceeded.
    # Unlike egress consent there is no in-effect exemption -- every row is
    # history at write time. 0 disables either knob. Swept hourly by the
    # consent sweeper's retention pass (once at startup, so an upgrade over
    # a bloated table trims immediately); read live (SIGHUP reload-safe --
    # a reload applies on the next sweep).
    container_events_retention_days: int = 90
    container_events_row_cap: int = 10000
    # Container resource limits (#34): deploy-wide CPU / memory / PIDs caps
    # passed to every workspace container as podman --cpus / --memory /
    # --pids-limit. Ships with protective defaults (2 CPUs / 8g / 16384 PIDs,
    # #2030) so a fresh install is bounded out of the box — a workspace
    # exceeding a limit is throttled / OOM-killed / fork-bomb-contained
    # rather than taking down the host or its neighbours. Set a field to an
    # empty value (env `""`) to explicitly disable that one cap and restore
    # unbounded behavior for it. Read at boot and on SIGHUP (reloadable) and
    # passed through container.create_kwargs at every workspace start, so a
    # reload applies to containers started after the reload (existing
    # containers keep their original cgroup limits for the rest of their
    # life — cgroup limits can't be retroactively re-applied). A malformed
    # value aborts startup (and is denied on SIGHUP) rather than silently
    # disabling the safety control — see each field's validator. Per-
    # workspace overrides (creator may go larger *or* smaller than the
    # deploy default, no clamping) are a follow-up (Phase 2).
    container_cpu_limit: float | None = 2.0
    container_memory_limit: str | None = "8g"
    container_pids_limit: int | None = 16384
    # --- Admission control (#2525) ---
    # Start-time host-capacity fit + per-user running quota, the
    # k8s-scheduler/ResourceQuota analogue. Both gates run at the
    # container-start choke point (every start path: API start/restart,
    # WS connect, create eager start, boot auto-start, crash-recovery
    # restart) and raise WorkspaceCapacityError (API 503 / WS error
    # frame) with an actionable message when they refuse.
    #
    # admission_memory_enabled: compare available host memory
    # (MemAvailable, platform-aware — same measurement family as the
    # #2526 eviction loop) against the workspace's resolved
    # container_memory_limit + admission_memory_margin before creating
    # the container, refusing the start when it does not fit. Default
    # OFF: the check is advisory against the *limit*, and the default
    # 8g limit exceeds what small dev/CI hosts have available —
    # defaulting it on would refuse every start there. Multi-user
    # deployments (the motivation, #34) should set it with limits sized
    # to the host. Skipped when no memory limit is configured; fails
    # open (start allowed, one-time warning) when memory cannot be
    # measured. Read live (SIGHUP reload-safe).
    admission_memory_enabled: bool = False
    # admission_memory_margin: the reserve kept for the server itself
    # (klangkd, the proxy, page cache) when fitting a workspace's
    # memory limit against available host memory. Podman size-string
    # grammar (same as container_memory_limit); unset/empty = no
    # reserve (fit against the bare limit). Malformed aborts startup
    # (and is denied on SIGHUP). Read live (SIGHUP reload-safe).
    admission_memory_margin: str | None = "1g"
    # max_running_workspaces_per_user: deploy-wide cap on concurrently
    # RUNNING workspaces per owner, checked at start time. 0 (the
    # default) = unlimited. Workspaces mid-start/stop count too (the
    # per-workspace operation lock), which closes the concurrent-start
    # race. Read live (SIGHUP reload-safe).
    max_running_workspaces_per_user: int = 0
    # volume_quota_per_user: deploy-wide cap on instance-managed named
    # volumes per user (#2972), enforced at both doors that mint
    # user-owned volumes: the POST /volumes route and the
    # workspace-start auto-create of mounted named volumes
    # (container/spec.py ensure_volumes). Each volume is a directory
    # tree on the host's storage, so without a cap any user granted
    # manage-volumes (seeded to the admins group only since #2997;
    # operators may grant it further) can consume unbounded disk —
    # including by adding mounts to a
    # workspace. A create past the cap fails with 429 (start path: a
    # clear start error) naming this setting; the count is the user's
    # volumes carrying this instance's klangk.instance label and their
    # klangk.user-id label, and count+create hold the per-user lock
    # (podman.volume_create_lock) so concurrent creates cannot jointly
    # exceed the cap. 0 (the default) = unlimited (the pre-#2972
    # behavior, and no extra podman call on the create path). Read
    # live (SIGHUP reload-safe).
    volume_quota_per_user: int = 0
    # #2378: per-workspace /tmp tmpfs size (``--tmpfs /tmp:...,size=<n>``).
    # Default ``2g`` preserves the pre-#2378 hardcoded mount size; a
    # workspace may override it via its settings bag (``settings.tmp_size``).
    container_tmp_size: str | None = "2g"
    # Crash recovery (#2524): detect unexpectedly-dead workspace
    # containers (OOM kill, non-zero exit, external removal), classify
    # the cause into the death events/logs, and — opt-in — auto-restart
    # the workspace after an exponential backoff
    # (``base * 2^(n-1)``, capped at 60s) with a bounded retry count.
    # Exhausting the retries leaves a visible ``crash-loop`` terminal
    # state (surfaced on /workspaces/<id>/status) instead of spinning
    # forever. Default off: recovery stays manual (the pre-#2524
    # behavior). Expected deaths (user stop, idle stop, delete, logout)
    # never enter the restart path. Read live (SIGHUP reload-safe).
    container_restart_enabled: bool = False
    container_restart_max_retries: int = 5
    container_restart_backoff_seconds: float = 5.0
    # Graceful SIGHUP restart and TERM/INT shutdown (#2527, #2664):
    # after new container starts are refused, the restart/shutdown waits
    # this many seconds for in-flight HTTP requests to finish before
    # draining the containers. Requests still running at expiry are
    # logged and left to finish against the recycling/exiting runtime
    # (streaming responses may be interrupted). The restart path reads
    # it from the freshly-reloaded settings, so a change takes effect
    # on the very restart that re-reads it; the shutdown path reads it
    # from the live settings.
    quiesce_timeout: float = 15.0
    # FIPS mode (#2570, #2591, #2628): when enabled, every workspace
    # container must prove an actively-enforcing OpenSSL FIPS provider
    # at start (fail closed — the container is removed and the start
    # raises), and the klangkd process's own OpenSSL is probed once at
    # startup: warn-only on a control host, boot-refusal when klangkd
    # itself runs in a container (see klangk/fips.py). The probes are
    # distro-agnostic. Default off. Reloadable on SIGHUP.
    fips_mode: bool = False
    test_mode: str | None = None
    version_file: str | None = None

    # --- LLM (#2070) ---
    # Default API key for models that don't specify their own.
    llm_api_key: str = ""
    # Model list for the in-process litellm.Router. Accepts
    # colon-delimited strings (env var) or LiteLLM-native dicts (YAML).
    # When set, the in-process router handles /llm-proxy/ requests.
    llm_models: list[str | dict] | None = None

    # --- OIDC ---
    oidc_config: str | None = None
    oidc_login_hook: str | None = None
    oidc_providers: list[dict] | None = None

    # --- Lifecycle hooks (customize dir) ---
    # File path to a Python workspace-created hook, optionally followed
    # by ``:func_name`` (default ``on_workspace_created``). Fired after a
    # workspace is created on every creation path (create / import /
    # duplicate); the hook may mutate the workspace and rewrite its ACL.
    # Failures are logged, never fatal (#2762). Reloaded on SIGHUP.
    workspace_created_hook: str | None = None

    # --- SMTP / email ---
    smtp_host: str | None = None
    smtp_port: int | None = 587
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

    # --- File upload ---
    file_upload_size_max: int | None = 524288000

    # --- Feature / feature config (#1659) ---
    # A config-file source for feature-declared dynamic keys (the keys the
    # build emits into features.json's container_env_keys + the per-feature
    # config blocks). Values here are the "tomorrow" answer to "where does
    # the operator set a feature value?" — today that's env only; this block
    # lets long-lived deploy config (OAuth client IDs, RAG endpoints) live
    # in the committed klangkd.yaml instead. Precedence when a feature key is
    # resolved via resolve_dynamic_config: env > features_config: > feature
    # default. Values keep their raw file:/cmd: prefixes here (the
    # _resolve_indirections validator only processes top-level str fields,
    # so a dict is left untouched) — resolve_dynamic_config derefs them at
    # call time, consistent with how it treats env values.
    features_config: dict[str, str] | None = None

    @model_validator(mode="after")
    def _resolve_indirections(self) -> "KlangkSettings":
        """Resolve ``file:``/``cmd:`` prefixes once, at construction (#1461).

        Every string field is run through :func:`resolve_indirection` before
        the object is handed to anything. Thereafter ``settings.field`` returns
        the already-resolved value — no caller wraps in ``resolve_indirection``.
        A field set to ``file:/nonexistent`` or ``cmd:false`` fails *here*
        (fail-fast at boot), not silently at use time.

        Resolution is idempotent: a plain (non-``file:``/``cmd:``) value passes
        through unchanged, so re-resolving an already-resolved value is a
        no-op. This keeps the legacy ``resolve_env_value`` path (still used by
        feature-declared dynamic keys and not-yet-migrated modules) correct —
        it reads the already-resolved field and the redundant
        ``resolve_indirection`` call it makes is a harmless no-op.

        Only ``str`` fields are candidates: ``list[dict]`` (``oidc_providers``)
        and any non-string field are skipped. ``None`` (unset) is left alone.
        """
        for name in type(self).model_fields:
            val = getattr(self, name)
            if isinstance(val, str):
                resolved = resolve_indirection(val, name)
                if resolved is None:
                    raise ValueError(
                        f"KLANGKD_{name.upper()} could not be resolved: the "
                        f"file:/cmd: reference failed. See logs for detail."
                    )
                setattr(self, name, resolved)
        return self

    @model_validator(mode="after")
    def require_dirs(self) -> "KlangkSettings":
        """Default ``state_dir``; derive ``data_dir``, ``customize_dir``, ``config_dir``.

        ``state_dir`` defaults to ``$XDG_STATE_HOME/klangkd`` (→
        ``~/.local/state/klangkd`` when the var is unset, incl. macOS) when no
        explicit value is supplied (#1644). This does **not** undo #1461's
        intent — that decision was about rejecting a ``None`` path so a
        dereference fails fast at boot rather than at first use; a concrete
        default still satisfies "non-None at construction." Explicit values
        (env / config file / container pin) still win, so operators who want
        it pinned keep fail-fast behavior; the default only kicks in when
        nothing is set. If neither ``XDG_STATE_HOME`` nor ``$HOME`` is set
        (the pathological case — no way to compute a home path), the default
        cannot be derived and we raise, preserving the fail-fast intent for
        the genuinely-unconfigured case.

        ``data_dir`` still derives from ``state_dir`` (the SQLite DB +
        workspace volumes are runtime state too), so one default populates
        the state tree. ``config_dir`` defaults to
        ``$XDG_CONFIG_HOME/klangkd`` (the config-tree root, #1649) and
        ``customize_dir`` derives from it (user-edited, durable config).
        ``plugins_dir`` is gone from settings entirely (#1655): the runtime
        reads the build-emitted ``features.json`` from ``frontend_dir``. The
        build reads the checked-in ``features.yaml`` at the repo root and
        materializes feature trees into a throwaway tempdir (#1660) — no
        ``KLANGKD_PLUGINS_DIR`` env var exists at any layer.
        """
        if not self.state_dir:
            self.state_dir = _default_state_dir()
        self._derive_missing_defaults()
        return self

    def _derive_missing_defaults(self) -> None:
        """Derive ``data_dir`` / ``config_dir`` / ``customize_dir`` /
        ``default_user`` when unset (see ``require_dirs``)."""
        if not self.data_dir:
            self.data_dir = os.path.join(self.state_dir, "data")
        # config_dir is the config-tree root (the state_tree analogue of
        # state_dir, #1649): customize_dir derives from it.
        if not self.config_dir:
            self.config_dir = os.path.join(xdg_config_home(), XDG_SUBDIR)
        # customize_dir is config (user-edited, durable) — derive from
        # config_dir, not state_dir (#1644/#1649).
        if not self.customize_dir:
            self.customize_dir = os.path.join(self.config_dir, "custom")
        # default_user: the admin identity for first-boot seeding. Derived
        # from the invoking Unix user (<user>@example.com) so a bare
        # ``klangkd`` seeds the operator's own identity (#1645). Explicit
        # KLANGKD_DEFAULT_USER (env/config) always wins — unaffected for
        # intentional deployments that stage a specific admin email.
        if not self.default_user:
            self.default_user = f"{_safe_getuser()}@example.com"

    @model_validator(mode="after")
    def _resolve_socket_and_ports(self) -> "KlangkSettings":
        """Resolve the listen-shape settings: fold ``proxy_port`` into
        ``egress_port``, default ``socket``, enforce egress≠browser and the
        socket-length invariant.

        Runs after ``_resolve_indirections`` (so ``proxy_port`` /
        ``egress_port`` / ``socket`` string values are already
        ``file:``/``cmd:``-resolved) and after ``require_dirs`` (so
        ``state_dir`` is non-None for the ``socket`` default). After this,
        **every consumer reads ``self.egress_port`` and ``self.socket`` —
        nothing reads ``proxy_port``.**

        ``KLANGKD_PROXY_PORT`` deprecation ladder (no hard error, #1542):

        - ``egress_port`` set, ``proxy_port`` unset → use egress (clean).
        - ``egress_port`` unset, ``proxy_port`` set → use ``proxy_port`` as
          the egress port + a loud deprecation warning.
        - both set → ``egress_port`` wins, ``proxy_port`` ignored + a warning.
        """
        self._normalize_port_fields()
        self._fold_proxy_port()
        if self.egress_port is None:
            self.egress_port = "8995"

        # --- egress ≠ browser port (ingress/egress firewall separation) ---
        if self.port is not None and self.egress_port == self.port:
            raise ValueError(
                f"KLANGKD_EGRESS_PORT ({self.egress_port!r}) must differ from "
                f"KLANGKD_PORT ({self.port!r}). The two proxy listeners carry "
                "browser ingress vs container egress so operators can firewall "
                "them separately; sharing a port defeats that and the proxy cannot "
                "bind two server blocks to the same port."
            )

        self._default_sockets()
        # Portable bound: macOS sun_path is 104 usable bytes; Linux is 107.
        # Use the smaller so one check is correct on both platforms.
        # Applied to BOTH UDS paths the engines bind: the backend socket
        # (always) and the Caddy admin socket (only bound under the Caddy
        # engine, but checked unconditionally so a deep state_dir fails at
        # construction regardless of engine — the diagnostic names which
        # var to fix). See #1531/#1542 (backend) and #1636 (admin).
        max_socket_len = 104
        self._enforce_socket_length(
            self.socket, "KLANGKD_SOCKET", max_socket_len
        )
        self._enforce_socket_length(
            self.caddy_admin_socket,
            "KLANGKD_CADDY_ADMIN_SOCKET",
            max_socket_len,
        )
        return self

    def _normalize_port_fields(self) -> None:
        """Empty-string port settings mean unset; a set value must be
        numeric 1-65535 (#3124).

        ``KLANGKD_PORT=`` (an explicitly emptied env var) must mean
        "unset" — headless for the browser port, the built-in default for
        the egress port — never an empty string that crashes the
        launcher's ``int()`` and renders a broken ``listen`` directive.
        A non-numeric value must fail construction with the setting
        named (the fail-fast posture every other numeric knob has), not
        crash later in ``main._check_port_collisions``.
        """
        self.port = self._validated_port("KLANGKD_PORT", self.port)
        self.egress_port = self._validated_port(
            "KLANGKD_EGRESS_PORT", self.egress_port
        )
        self.proxy_port = self._validated_port(
            "KLANGKD_PROXY_PORT", self.proxy_port
        )

    @classmethod
    def _validated_port(cls, env_var: str, value: str | None) -> str | None:
        """One port setting: ``None``/``""`` → ``None``; else validated
        numeric 1-65535 and returned **normalized** (``str(int(v))``) —
        the egress≠browser equality check and the Caddyfile render both
        consume the raw string, so a whitespace/zero-padded form must
        not survive as a distinct value (#3124)."""
        if value is None or value == "":
            return None
        try:
            port = int(value)
        except ValueError as exc:
            raise ValueError(_BAD_PORT_MSG.format(env_var, value)) from exc
        if not 1 <= port <= 65535:
            raise ValueError(_BAD_PORT_MSG.format(env_var, value))
        return str(port)

    def _fold_proxy_port(self) -> None:
        """Apply the ``KLANGKD_PROXY_PORT`` → ``egress_port`` deprecation
        ladder (warnings + fold; see ``_resolve_socket_and_ports``)."""
        if self.proxy_port is None:
            return
        if self.egress_port is not None:
            logger.warning(
                "KLANGKD_PROXY_PORT is ignored because KLANGKD_EGRESS_PORT "
                "is also set; KLANGKD_EGRESS_PORT takes precedence. "
                "KLANGKD_PROXY_PORT is deprecated — remove it and use "
                "KLANGKD_EGRESS_PORT."
            )
            return
        logger.warning(
            "KLANGKD_PROXY_PORT is deprecated; rename it to "
            "KLANGKD_EGRESS_PORT. Its value is used as the egress "
            "port for this run, but a future release will stop "
            "recognizing KLANGKD_PROXY_PORT."
        )
        self.egress_port = self.proxy_port

    def _default_sockets(self) -> None:
        """Default the two UDS paths under ``state_dir`` (#1531, #1636)."""
        if self.socket is None:
            self.socket = os.path.join(self.state_dir, "klangk.sock")
        if self.caddy_admin_socket is None:
            self.caddy_admin_socket = os.path.join(
                self.state_dir, "caddy-admin.sock"
            )

    @staticmethod
    def _enforce_socket_length(value: str, env_var: str, max_len: int) -> None:
        """Raise ValueError if a UDS path exceeds the portable sun_path bound.

        Naming the env var in the message lets the operator fix *this* socket
        (vs the generic "move KLANGKD_STATE_DIR shallower") when only one of
        the two is too long.
        """
        if len(value) > max_len:
            raise ValueError(
                f"{env_var} resolves to {value!r} "
                f"({len(value)} chars), which exceeds the "
                f"{max_len}-character AF_UNIX sun_path limit. "
                f"Either set {env_var} to a shorter absolute path "
                "(e.g. /tmp/klangk.sock) or move KLANGKD_STATE_DIR shallower "
                "in the filesystem. (The kernel caps UDS paths at "
                "sockaddr_un.sun_path: 108 bytes incl. NUL on Linux → 107 "
                "usable; 104 on macOS, so a deep state_dir overflows the "
                "default <state_dir>/...sock and the bind fails.) "
                "See #1531 / #1636."
            )

    @field_validator(
        "min_password_length",
        "login_lockout_failures",
        "login_lockout_duration",
        "login_lockout_window",
        "max_sessions_per_user",
        "invite_expire_hours",
        "password_history_count",
        "inactivity_disable_days",
        "port_range_start",
        "websocket_msg_size_max",
        "file_upload_size_max",
        "hosted_ports_per_workspace",
        "memory_eviction_sustain_polls",
        "max_running_workspaces_per_user",
        "volume_quota_per_user",
        mode="before",
    )
    @classmethod
    def _coerce_numeric_int_fields(cls, v, info):
        """Int settings accept int, integer string, or file:/cmd: (#2603).

        Bare YAML numbers (``min-password-length: 12``) used to fail
        validation because the fields were str-typed; now a native int,
        an integer string (env var / quoted YAML), and an indirection
        reference all work. Bools, floats, garbage, and negatives abort
        startup with the field named — ``min_password_length``
        previously had **no** validator and only exploded at request
        time. Zero stays legal exactly where the consuming code has
        explicit zero-handling (see ``_ZERO_MEANINGFUL`` below).
        """
        # 0 keeps its pre-existing per-field meaning where the code has
        # explicit zero handling: disables the length floor
        # (min_password_length), disables lockout (the login_lockout_*
        # trio, guarded by ``> 0`` in auth.py), disables the session cap
        # (max_sessions_per_user), disables hosted ports
        # (hosted_ports_per_workspace), disables password-reuse checking
        # (password_history_count), disables the dormant-account sweep
        # (inactivity_disable_days, #2588). Elsewhere 0 is nonsense (port
        # 0, zero-byte uploads, empty port range) and is rejected.
        _ZERO_MEANINGFUL = {
            "min_password_length",
            "login_lockout_failures",
            "login_lockout_duration",
            "login_lockout_window",
            "max_sessions_per_user",
            "hosted_ports_per_workspace",
            "password_history_count",
            "inactivity_disable_days",
            # Disables the per-user running-workspace cap (#2525).
            "max_running_workspaces_per_user",
            # Disables the per-user volume quota (#2972).
            "volume_quota_per_user",
        }
        minimum = 0 if info.field_name in _ZERO_MEANINGFUL else 1
        return _coerce_setting_int(
            v,
            info.field_name,
            minimum=minimum,
            default=cls.model_fields[info.field_name].default,
        )

    @model_validator(mode="after")
    def _validate_memory_eviction_hysteresis(self) -> "KlangkSettings":
        """Recovery threshold must be >= the pressure threshold (#2526).

        The gap between the two is the hysteresis that prevents
        flap-eviction when availability hovers at the pressure threshold;
        an inverted (or equal — zero-gap, i.e. no hysteresis) pair would
        re-trigger an eviction episode the moment availability dips back
        to the boundary. Equal values are tolerated (operator explicitly
        chooses no hysteresis gap); strictly-inverted pairs abort startup
        with both fields named.
        """
        if (
            self.memory_eviction_recovery_percent
            < self.memory_eviction_threshold_percent
        ):
            raise ValueError(
                "memory_eviction_recovery_percent ("
                f"{self.memory_eviction_recovery_percent!r}) must be >= "
                "memory_eviction_threshold_percent ("
                f"{self.memory_eviction_threshold_percent!r}) — recovery "
                "below pressure would re-arm eviction the moment it ends."
            )
        return self

    @field_validator("password_history_count", mode="after")
    @classmethod
    def _cap_password_history(cls, v):
        """Reject counts above ``_PASSWORD_HISTORY_MAX`` (#2582): each
        retired hash costs one PBKDF2 verify per password set, so an
        unbounded count is a self-inflicted CPU knob."""
        if v > _PASSWORD_HISTORY_MAX:
            raise ValueError(
                f"password_history_count={v} must be <= "
                f"{_PASSWORD_HISTORY_MAX} — every remembered hash costs"
                " a PBKDF2 verify on each password set."
            )
        return v

    @field_validator("port_range_start", mode="after")
    @classmethod
    def _check_port_range_start(cls, v):
        """Reject a start above the last legal host port (#2603).

        The allocator hands out ``start..start+MAX_HOST_PORT``, so a start
        beyond 65535 (or near enough to run past it) can never allocate a
        valid port. Checked after coercion, on the int.
        """
        if v is not None and v > 65535:
            raise ValueError(
                f"port_range_start={v!r} must be <= 65535 (the last legal "
                "host port)."
            )
        return v

    @field_validator("smtp_port", mode="before")
    @classmethod
    def _coerce_smtp_port(cls, v):
        """SMTP port accepts int/string/indirection and must be 1-65535."""
        coerced = _coerce_setting_int(v, "smtp_port", minimum=1, default=587)
        if coerced is not None and coerced > 65535:
            raise ValueError(
                f"smtp_port={coerced!r} must be between 1 and 65535."
            )
        return coerced

    @field_validator(
        "access_token_hours",
        "workspace_token_hours",
        "health_check_interval",
        "health_check_startup_grace",
        "health_check_timeout",
        "memory_eviction_threshold_percent",
        "memory_eviction_recovery_percent",
        "memory_eviction_poll_interval",
        "quiesce_timeout",
        mode="before",
    )
    @classmethod
    def _coerce_numeric_float_fields(cls, v, info):
        """Float settings accept float, numeric string, or file:/cmd:
        (#2603) — same contract as the int fields above."""
        return _coerce_setting_float(
            v,
            info.field_name,
            default=cls.model_fields[info.field_name].default,
        )

    @field_validator(*BOOL_STRING_FIELDS, mode="before")
    @classmethod
    def _coerce_bool_string_fields(cls, v, info):
        """Accept a native YAML bool for the str-typed boolean settings
        (#2796, generalizing the #2603 ``smtp_use_tls`` one-off).

        The family is :data:`BOOL_STRING_FIELDS`. These fields stay
        str-typed — env vars are always strings, and ``file:``/``cmd:``
        indirection plus the consumers' own matching
        ("1"/"true"/"yes" case-insensitively, see
        :func:`parse_bool_setting`) keep working — but a bare
        ``allow_sudo: true`` in YAML parses as a bool and used to fail
        validation. Translate the two bools to their canonical strings;
        an explicitly emptied value (``""``) means *unset* → the field's
        declared default, so ``KLANGKD_SMTP_USE_TLS=`` keeps TLS on
        (unset ⇒ ``"true"``) instead of silently disabling it (#3124);
        everything else (strings, ``None``) passes through unchanged.
        """
        if v is True:
            return "true"
        if v is False:
            return "false"
        if v == "":
            default = cls.model_fields[info.field_name].default
            return None if default is None else str(default)
        return v

    @field_validator(*INT_STRING_FIELDS, mode="before")
    @classmethod
    def _coerce_int_string_fields(cls, v, info):
        """Accept a native YAML int for the str-typed port/timeout
        settings, including the deprecated ``proxy_port`` alias (#2967
        — the int sibling of ``_coerce_bool_string_fields``, #2796).

        The family is :data:`INT_STRING_FIELDS`. A bare
        ``port: 8997`` in YAML parses as an int and used to fail
        validation with ``Input should be a valid string``. Translate a
        native int to its string form. A bool (``port: true``) or a
        float (``port: 8997.5`` — silently truncating would hide a
        typo) raises naming the field, matching the strict-on-malformed
        posture of the #2603 numeric coercers instead of pydantic's
        generic str error (which steers the operator to *quote* the
        value). Everything else (strings, ``None``) passes through
        unchanged — env vars, quoted YAML, and ``file:``/``cmd:``
        references are unaffected.
        """
        if isinstance(v, bool):
            raise ValueError(
                f"{info.field_name}={v!r} must be an integer, not a boolean."
            )
        if isinstance(v, float):
            raise ValueError(
                f"{info.field_name}={v!r} must be an integer, not a float "
                "(use 8997, not 8997.5)."
            )
        if isinstance(v, int):
            return str(v)
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        """Reject typo'd/invalid log levels at construction (fail-fast, #1467).

        Accepts a level name (case-insensitive: ``debug``, ``INFO``, ...) or
        a numeric string (``"20"``). ``None``/empty defaults to ``INFO``. A
        bogus value aborts boot rather than silently leaving logging at the
        wrong verbosity — the same fail-fast posture as ``auth_modes``.
        """
        if _is_unset(v):
            return "INFO"
        upper = v.strip().upper()
        if upper.isdigit():
            return upper
        if isinstance(getattr(logging, upper, None), int):
            return upper
        raise ValueError(
            f"KLANGKD_LOG_LEVEL={v!r} is invalid. "
            "Must be a level name (DEBUG/INFO/WARNING/ERROR/CRITICAL) "
            "or a numeric value."
        )

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        """Normalize/reject the log output format at construction (#3156).

        Accepts ``text`` or ``json`` (case-insensitive), normalized to
        lowercase. ``None``/empty defaults to ``text``. Anything else aborts
        boot — the same fail-fast posture as ``log_level`` above.
        """
        if _is_unset(v):
            return "text"
        lower = v.strip().lower()
        if lower not in ("text", "json"):
            raise ValueError(
                f"KLANGKD_LOG_FORMAT={v!r} is invalid. Must be text or json."
            )
        return lower

    @field_validator("auth_modes")
    @classmethod
    def _validate_auth_modes(cls, v: str | None) -> str | None:
        """Reject typo'd auth modes so a misspelling fails loudly at boot.

        Without this, ``KLANGKD_AUTH_MODES=passdword`` (or any value outside the
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
                f"KLANGKD_AUTH_MODES={v!r} is invalid. "
                f"Must be one of {sorted(_VALID_AUTH_MODES)} (or unset "
                "→ defaults to 'none')."
            )
        return v

    @staticmethod
    def _domain_list_items(v, setting: str) -> list[str]:
        """A domain-list setting's items: a comma-separated string (env var)
        or a real list (YAML), stripped + de-emptied; a wrong type raises
        ValueError (startup aborts — see the coerce methods' docstrings)."""
        return _setting_items(v, setting)

    @field_validator("netfilter_default_domains", mode="before")
    @classmethod
    def _coerce_netfilter_default_domains(cls, v):
        """Accept either a comma-separated string (env var) or a real list
        (YAML config file), then validate + de-dupe via
        :func:`klangk.netfilter.parse_allowed_domains` (#1365).

        Env vars deliver a single string (``a.com,b.com``); the YAML source
        delivers a native list. Both are normalized to a validated, de-duped
        ``list[str]`` of ``host[:port]`` specs. ``None`` / empty → ``None``
        (no deploy default; workspaces unrestricted unless they declare their
        own).

        A malformed value — a wrong type (non-list / non-string) or a bad
        ``host[:port]`` spec — **aborts startup** by raising ``ValueError``
        (pydantic surfaces it as a ``ValidationError`` out of
        ``KlangkSettings(...)``). This is a safety control: a deploy-wide
        egress allow-list that silently disables itself on a typo leaves
        workspaces running unrestricted while the operator believes egress
        is filtered, so a misconfigured value must fail loudly. A SIGHUP
        reload with a bad value is denied by ``reload_settings``
        (main.py), which catches the construction error and keeps the
        runtime on the prior config. Reverses the warn-and-fallback posture
        of #1772; matches the malformed→abort rule decided for the
        container resource limits (#34).
        """
        if v is None:
            return None
        items = cls._domain_list_items(v, "KLANGKD_NETFILTER_DEFAULT_DOMAINS")
        if not items:
            return None
        try:
            return parse_allowed_domains(items)
        except ValueError as exc:
            raise ValueError(
                f"KLANGKD_NETFILTER_DEFAULT_DOMAINS={v!r} has an invalid "
                f"spec: {exc}"
            ) from exc

    @field_validator("classification_banner", mode="before")
    @classmethod
    def _coerce_classification_banner(cls, v):
        """Validate ``KLANGKD_CLASSIFICATION_BANNER`` (#2768).

        Applies the same rules as the per-workspace marking
        (:func:`klangk.model.workspaces.normalize_classification_banner`):
        strip, one line, no control/invisible format characters, at most
        :data:`CLASSIFICATION_BANNER_MAX_LEN` chars. ``None`` / empty /
        whitespace-only → ``""`` (no deploy-wide marking). The value rides
        ``/api/v1/config`` to every client and renders as a banner, so a
        malformed value (a stray newline, a bidirectional override) must
        fail loudly: a malformed value **raises**, aborting boot; a SIGHUP
        reload with a bad value is denied by ``reload_settings``
        (``main.py``), which keeps the runtime on the prior config — same
        posture as ``netfilter_default_domains`` (#1939).
        """
        if v is None:
            return ""
        try:
            return normalize_classification_banner(v) or ""
        except ValueError as exc:
            raise ValueError(
                f"KLANGKD_CLASSIFICATION_BANNER is invalid: {exc}"
            ) from exc

    @field_validator("container_cpu_limit", mode="before")
    @classmethod
    def _coerce_container_cpu_limit(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_CPU_LIMIT`` (#34).

        Accepts a numeric string (env var) or a real ``float``/``int`` (YAML
        config file); ``None`` / empty → ``None`` (no cap). A non-numeric,
        non-finite (``nan``/``inf``), or ``<= 0`` value **raises** — so
        ``KlangkSettings(...)`` construction fails and the server refuses to
        boot. (``float()`` happily parses ``"nan"`` / ``"inf"`` / ``"-inf"``;
        the ``<= 0`` guard catches ``-inf`` but not ``nan`` (``nan <= 0`` is
        ``False``) or ``inf`` (``inf > 0``), so an explicit ``isfinite``
        check closes both — otherwise a syntactically-valid setting reaches
        ``podman create`` and fails at workspace-start instead of boot.) A
        safety control that silently disables itself on a typo (warn-and-
        fall-back) is worse than none: you think the host is protected and it
        isn't (#34). SIGHUP reload is still safe — ``reload_settings``
        (``main.py``) catches the construction error, logs "SIGHUP: denying
        restart — invalid configuration", and keeps the runtime on the old
        (valid) config.
        """
        return _coerce_positive_float(v, "KLANGKD_CONTAINER_CPU_LIMIT")

    @field_validator("container_memory_limit", mode="before")
    @classmethod
    def _coerce_container_memory_limit(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_MEMORY_LIMIT`` (#34).

        Accepts a podman ``--memory`` size string — a positive number with
        an optional unit suffix matching docker/go-units ParseSize exactly:
        a single base unit ``b``/``k``/``m``/``g``/``t``/``p`` (case-
        insensitive) plus an optional trailing ``b`` (case-insensitive), so
        ``2g``, ``2gb``, ``2G``, ``512m``, ``512mb``, ``2t``, ``1024`` (bare
        bytes), ``1.5g`` all pass; the IEC i-forms (``kib``/``gib``/…) do
        not, because go-units doesn't accept them either. ``None`` / empty →
        ``None`` (no cap). A malformed value **raises** and aborts startup —
        same posture as ``container_cpu_limit`` (#34). A value of ``0``
        (``0``, ``0b``, ``0g``, …) also raises: podman treats
        ``--memory=0`` as "no limit", the same ambiguity the PIDs validator
        rejects, so the zero-handling is consistent across all three fields.
        The format check is a syntax guard only; podman remains the authority
        on what the runtime can actually apply (cgroups v2 availability,
        delegation, etc.) and will fail loudly at ``podman create`` if it
        can't honour the value.
        """
        return _coerce_podman_size(v, "KLANGKD_CONTAINER_MEMORY_LIMIT")

    @field_validator("container_pids_limit", mode="before")
    @classmethod
    def _coerce_container_pids_limit(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_PIDS_LIMIT`` (#34).

        Accepts an integer string (env var) or a real ``int`` (YAML config
        file); ``None`` / empty → ``None`` (no cap). A non-integer, native
        float, or ``<= 0`` value **raises** and aborts startup — same posture
        as the other two limits (#34). A native YAML float (e.g.
        ``pids_limit: 1.5``) is rejected explicitly rather than silently
        truncated to ``1`` (``int(1.5)`` truncates without error), for
        consistency with the strict-on-malformed posture; from the env path
        ``int("1.5")`` already raises. (Podman treats
        ``--pids-limit=0`` as unlimited, but a safety cap of "unlimited" is
        just an unset var, so 0 is rejected to keep the semantics
        unambiguous.)
        """
        return _coerce_positive_int(v, "KLANGKD_CONTAINER_PIDS_LIMIT")

    @field_validator("container_tmp_size", mode="before")
    @classmethod
    def _coerce_container_tmp_size(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_TMP_SIZE`` (#2378).

        Accepts the same podman size-string grammar as
        ``container_memory_limit`` (``2g`` / ``2gb`` / ``512m`` / ``1024``
        bare bytes / ``1.5g`` / …). ``None`` / empty -> ``None`` (mount
        ``/tmp`` with no explicit ``size=`` option, letting podman size it at
        half of RAM). Default ``2g`` preserves the pre-#2378 hardcoded
        ``/tmp`` tmpfs size. A malformed or ``<= 0`` value raises and aborts
        startup, same posture as the other container limits (#34).
        """
        return _coerce_podman_size(v, "KLANGKD_CONTAINER_TMP_SIZE")

    @field_validator("admission_memory_margin", mode="before")
    @classmethod
    def _coerce_admission_memory_margin(cls, v):
        """Coerce + validate ``KLANGKD_ADMISSION_MEMORY_MARGIN`` (#2525).

        Accepts the same podman size-string grammar as
        ``container_memory_limit`` (``1g`` / ``512m`` / ``1024`` …);
        ``None`` / empty -> ``None`` (no reserve — fit against the bare
        memory limit). A malformed or ``<= 0`` value raises and aborts
        startup, same strict-on-malformed posture as the other safety
        knobs (#34: a control that silently disables itself on a typo is
        worse than none).
        """
        return _coerce_podman_size(v, "KLANGKD_ADMISSION_MEMORY_MARGIN")

    @field_validator("container_restart_enabled", mode="before")
    @classmethod
    def _coerce_container_restart_enabled(cls, v):
        """Treat unset/empty as an explicit False (#2524).

        The sibling ``KLANGKD_CONTAINER_*`` knobs treat env ``""`` as
        "unset/default"; a plain bool field would instead raise a
        ``bool_parsing`` ValidationError and abort boot. Parse the string
        here (accepting the usual true/1/yes/on spellings) so both the
        empty and non-empty paths stay plain-Python and validate the
        same way regardless of source.
        """
        if v is None or v == "":
            v = False
        if not isinstance(v, str):
            v = "true" if v else "false"
        return v.strip().lower() in {"1", "true", "yes", "on"}

    @field_validator("fips_mode", mode="before")
    @classmethod
    def _coerce_fips_mode(cls, v):
        """Treat unset/empty as False (#2570); parse the usual bool
        spellings in plain Python (same convention as
        ``container_restart_enabled``)."""
        if v is None or v == "":
            v = False
        if not isinstance(v, str):
            v = "true" if v else "false"
        return v.strip().lower() in {"1", "true", "yes", "on"}

    @field_validator("container_restart_max_retries", mode="before")
    @classmethod
    def _coerce_container_restart_max_retries(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_RESTART_MAX_RETRIES`` (#2524).

        Integer string (env) or int (YAML); ``None`` / empty -> the default
        (5). A non-integer, native float, or ``<= 0`` value raises and
        aborts startup — a retry budget that silently parses as 0 would
        make every unexpected death an immediate crash-loop, and one that
        parses as unlimited would reintroduce the infinite restart loop
        the bound exists to prevent.
        """
        if v is None or v == "":
            return 5
        return _coerce_positive_int(v, "KLANGKD_CONTAINER_RESTART_MAX_RETRIES")

    @field_validator("container_restart_backoff_seconds", mode="before")
    @classmethod
    def _coerce_container_restart_backoff_seconds(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS`` (#2524).

        Positive float (seconds); ``None`` / empty -> the default (5.0).
        Non-numeric, non-finite, or ``<= 0`` raises and aborts startup —
        same strict-on-malformed posture as the other container knobs
        (#34).
        """
        if v is None or v == "":
            return 5.0
        return _coerce_positive_float(
            v, "KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS"
        )

    @field_validator("egress_consent_retention_days", mode="before")
    @classmethod
    def _coerce_egress_consent_retention_days(cls, v):
        """Coerce + validate ``KLANGKD_EGRESS_CONSENT_RETENTION_DAYS`` (#2303).

        Integer string (env) or int (YAML); ``None`` / empty -> the default.
        Negative raises and aborts startup (a negative retention window is a
        misconfiguration, not a "keep everything" request -- that is ``0``).
        """
        return _coerce_prune_int(
            v,
            "KLANGKD_EGRESS_CONSENT_RETENTION_DAYS",
            default=30,
        )

    @field_validator("egress_consent_row_cap", mode="before")
    @classmethod
    def _coerce_egress_consent_row_cap(cls, v):
        """Coerce + validate ``KLANGKD_EGRESS_CONSENT_ROW_CAP`` (#2303).

        Integer string (env) or int (YAML); ``None`` / empty -> the default.
        Negative raises and aborts startup (``0`` disables the cap).
        """
        return _coerce_prune_int(
            v,
            "KLANGKD_EGRESS_CONSENT_ROW_CAP",
            default=2000,
        )

    @field_validator("container_events_retention_days", mode="before")
    @classmethod
    def _coerce_container_events_retention_days(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_EVENTS_RETENTION_DAYS``
        (#2924).

        Integer string (env) or int (YAML); ``None`` / empty -> the default.
        Negative raises and aborts startup (a negative retention window is a
        misconfiguration, not a "keep everything" request -- that is ``0``).
        """
        return _coerce_prune_int(
            v,
            "KLANGKD_CONTAINER_EVENTS_RETENTION_DAYS",
            default=90,
        )

    @field_validator("container_events_row_cap", mode="before")
    @classmethod
    def _coerce_container_events_row_cap(cls, v):
        """Coerce + validate ``KLANGKD_CONTAINER_EVENTS_ROW_CAP`` (#2924).

        Integer string (env) or int (YAML); ``None`` / empty -> the default.
        Negative raises and aborts startup (``0`` disables the cap).
        """
        return _coerce_prune_int(
            v,
            "KLANGKD_CONTAINER_EVENTS_ROW_CAP",
            default=10000,
        )

    @field_validator(
        "password_require_upper",
        "password_require_lower",
        "password_require_digit",
        "password_require_special",
        mode="before",
    )
    @classmethod
    def _coerce_password_require_counts(cls, v, info):
        """Coerce + validate the ``KLANGKD_PASSWORD_REQUIRE_*`` counts
        (#2581).

        Integer string (env var), native int (YAML config file —
        ``password_require_upper: 2`` parses as an int, no quotes
        needed), or a ``file:``/``cmd:`` reference; ``None`` / empty ->
        ``0`` (class not required). Negative, non-integer, or a count
        above 72 (the password byte cap) raises and aborts startup, same
        posture as the other numeric settings (``_coerce_setting_int``
        with ``minimum=0``, #2603). Errors name the setting field
        (``password_require_*``), which is unambiguous whichever source
        (env or YAML) supplied it.
        """
        value = _coerce_setting_int(v, info.field_name, minimum=0, default=0)
        if value is not None and value > _PASSWORD_REQUIRE_MAX:
            raise ValueError(
                f"{info.field_name}={v!r} must be <= "
                f"{_PASSWORD_REQUIRE_MAX} — passwords are capped at 72 "
                "bytes, so a higher count can never be satisfied."
            )
        return value

    @field_validator("llm_models", mode="before")
    @classmethod
    def _coerce_llm_models(cls, v):
        """Accept comma-separated string (env) or list (YAML) (#2070).

        Each entry is either:

        - A colon-delimited string: ``provider/model:api_base:api_key``
        - A LiteLLM-native dict with ``model_name``/``model-name`` and
          ``litellm_params``/``litellm-params``.

        String entries must have at least two colons.  Dict entries are
        passed through as-is (normalization and ``file:``/``cmd:``
        indirection are handled by :func:`llm_router._normalize_dict_entry`
        at router construction time).

        An empty value or ``None`` → ``None`` (router disabled).
        """
        if v is None:
            return None
        raw = _llm_model_entries(v)
        if not raw:
            return None
        return [_llm_model_entry(entry) for entry in raw]


def _llm_model_entry(entry) -> str | dict:
    """Normalize one ``KLANGKD_LLM_MODELS`` entry (#2070).

    Dict entries pass through; string entries are stripped and must carry
    at least two colons (``provider/model:api_base:api_key``).
    """
    if isinstance(entry, dict):
        return entry
    item = str(entry).strip()
    if item.count(":") < 2:
        raise ValueError(
            f"KLANGKD_LLM_MODELS entry {item!r} "
            f"must be 'provider/model:api_base:api_key' "
            f"(need at least two colons separating the three fields)."
        )
    return item


def _llm_model_entries(v) -> list:
    """KLANGKD_LLM_MODELS entries: a comma-separated string (env) or a real
    list (YAML), stripped + de-emptied; a wrong type raises ValueError."""
    return _setting_items(v, "KLANGKD_LLM_MODELS", stringify=False)


def _setting_items(v, label: str, *, stringify: bool = True) -> list:
    """Split a str-or-list setting value into stripped, non-empty items.

    Env vars deliver a comma-separated string; the YAML source delivers
    a native list. Any other type raises ``ValueError`` naming *label*
    (startup aborts). ``stringify=False`` keeps native list entries
    as-is (``llm_models`` list entries may be LiteLLM-native dicts).
    """
    if isinstance(v, str):
        return _str_setting_items(v)
    if isinstance(v, list):
        return _list_setting_items(v, stringify=stringify)
    raise ValueError(
        f"{label}={v!r} must be a list or "
        f"a comma-separated string (got {type(v).__name__})."
    )


def _str_setting_items(value: str) -> list[str]:
    """Stripped, non-empty items of a comma-separated string."""
    parts = [part.strip() for part in value.split(",")]
    return [part for part in parts if part]


def _list_setting_items(items: list, *, stringify: bool) -> list:
    """Non-empty items of a native list, optionally str()'d + stripped."""
    if stringify:
        items = [str(item).strip() for item in items]
    return [item for item in items if item]


# ---------------------------------------------------------------------------
# Singleton with env-change-detection cache + config-file path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Feature dynamic-key resolver (the only remaining file:/cmd: deref path)
# ---------------------------------------------------------------------------


# Feature-config namespace prefix (mirrors _CONTAINER_ENV_KEY_PREFIX in
# features.py / import_dart_features.py). Every feature-declared config key
# starts with this; the features_config: block accepts either this full form
# or the stripped, lowercased short form (see resolve_dynamic_config, #1737).
_FEATURE_CONFIG_PREFIX = "KLANGKWS_FEATURE_"


def resolve_dynamic_config(
    key: str,
    default: str | None = None,
    features_config: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a feature-declared dynamic config key.

    Feature config keys (discovered from each feature's ``package.json``) are
    outside the ``KLANGKD_`` settings model — they are not known at settings
    construction, so they can't be resolved by the model validator. This
    reads ``os.environ`` directly and applies :func:`resolve_indirection`
    so feature config honors ``file:``/``cmd:`` prefixes (a feature-declared
    key may itself be a secret, e.g. an API token).

    Precedence (highest first, #1659):

    1. **env** — ``os.environ[key]`` (the global precedence rule: env wins
       over file over defaults).
    2. **``features_config:``** — the YAML block from ``klangkd.yaml``, passed
       in by the caller (``Features.container_env`` / ``frontend_config`` read
       it off ``settings.features_config``). Lets long-lived deploy config
       (OAuth client IDs, RAG endpoints) live in the committed config file
       instead of env. ``file:``/``cmd:`` prefixes on these values are
       honored too — consistent with the env path and the rest of this
       resolver. A bad ``file:``/``cmd:`` ref here does NOT abort boot (the
       values can't be resolved at construction); it logs and falls through
       to *default*, mirroring how a bad env ref behaves.

    The block accepts either the full declared name
    (``KLANGKWS_FEATURE_SOLIPLEX_URL``) or the stripped, lowercased short form
    (``soliplex_url`` — the same key surfaced to the frontend via
    ``/api/v1/config``); env stays full-prefixed (#1737).
    3. **feature default** — the *default* argument (the feature-declared
       default from ``features.json``).

    *features_config* defaults to ``None`` (env-only, the pre-#1659
    behavior), so direct callers (e.g. tests) don't need to supply it.

    Note: env is consulted *first* and wins even on a broken ``file:``/``cmd:``
    ref — a bad env value returns *default* (the pre-#1659 behavior), not the
    ``features_config`` value. The block is a fallback for *unset* keys, not a
    recovery path for *broken* env values. This matches the global precedence
    rule (env is authoritative when set, regardless of whether it resolves).
    """
    raw = os.environ.get(key)
    if raw is not None:
        resolved = resolve_indirection(raw, key)
        return resolved if resolved is not None else default
    if features_config is not None:
        resolved = _resolve_features_config(features_config, key)
        if resolved is not None:
            return resolved
    return default


def _resolve_features_config(
    features_config: Mapping[str, str], key: str
) -> str | None:
    """Resolve *key* against the ``features_config:`` block (#1659, #1737).

    The block accepts the full declared name
    (``KLANGKWS_FEATURE_SOLIPLEX_URL``) or the stripped, lowercased short
    form (``soliplex_url``); env stays full-prefixed. Returns the
    ``file:``/``cmd:``-resolved value, or ``None`` when the key is absent
    or its ref failed to resolve — the caller falls through to the
    feature default (a bad ref does not abort boot here; a bad env ref
    behaves the same way).
    """
    fc_raw = features_config.get(key)
    if fc_raw is None:
        fc_raw = features_config.get(
            key.removeprefix(_FEATURE_CONFIG_PREFIX).lower()
        )
    if fc_raw is None:
        return None
    return resolve_indirection(fc_raw, key)
