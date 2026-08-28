"""Per-workspace behavioral settings: validation + resolution (#864).

This is the resolution layer for the JSON ``settings`` bag on the
workspaces table. Every per-workspace behavioral override (idle timeout,
bridge timeout, CPU / memory / PIDs limits, ...) lives in that one bag,
not in its own column — one column, one migration, one resolution path.
Structural fields (``image`` / ``mounts`` / ``env`` / ``default_command``)
and the behavioral ``allowed_domains`` column (which predates this
decision, #1365) stay as dedicated columns.

Precedence everywhere is **workspace override > deploy default > none**,
matching how ``allowed_domains`` already resolves (a workspace's non-empty
list overrides ``KLANGKD_NETFILTER_DEFAULT_DOMAINS``).

Two responsibilities:

- :func:`validate_settings` — schema gate. Rejects unknown keys and
  malformed values (typed coercion) so a typo'd setting name or a bogus
  value fails loudly at the API boundary (HTTP 400), not silently at use
  time. Returns a normalized dict (or ``None`` for an empty/``None``
  input) safe to ``json.dumps`` into the column.

- :func:`resolve` — precedence lookup. ``resolve(workspace, key,
  deploy_default)`` returns the workspace's override for *key* when set,
  else *deploy_default*, else ``None``. Typed resolvers
  (:func:`resolve_bridge_timeout`, :func:`resolve_cpu_limit`,
  :func:`resolve_memory_limit`, :func:`resolve_pids_limit`) bind a
  settings key to its deploy default for the common call sites. There is
  deliberately no ``resolve_idle_timeout`` sibling: per-workspace
  ``idle_timeout`` is runtime state, not spec — the registry captures the
  bag's override at container start and leaves ``state.idle_timeout`` at
  None when unset so ``get_idle_timeout()`` follows the live deploy
  default across SIGHUP reloads (#2514).

This module is deliberately pure (no ``app`` / ``app_state``): the deploy
defaults are passed *in* by the caller, read live off
``app.state.settings.<field>`` at call time (the #1608 ownership rule —
never cache a subobject of ``app`` on an instance). That also makes the
resolution helpers trivial to test.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# Container memory-limit syntax (mirrors the ``KLANGKD_CONTAINER_MEMORY_LIMIT``
# regex in settings.py / #34): matches docker/go-units ParseSize grammar —
# a positive number (decimals ok) with an optional unit suffix (single base
# unit k/m/g/t/p, case-insensitive, optional trailing b). Rejects 0 (podman
# treats --memory=0 as "no limit", same ambiguity the PIDs validator
# rejects). Syntax guard only — podman is the authority on what the runtime
# can actually apply.
_MEMORY_LIMIT_RE = re.compile(r"^(?P<num>\d+(\.\d+)?)[kKmMgGtTpP]?[bB]?$")

# Ulimit value grammar (#2085, mirrors ``KLANGKD_CONTAINER_NPROC_LIMIT`` /
# ``KLANGKD_CONTAINER_NOFILE_LIMIT`` in settings.py): ``<soft>[:<hard>]``
# with non-negative integer parts — the value half of podman's
# ``--ulimit name=<soft>[:<hard>]``. ``\d+`` rejects negatives and stray
# units outright; the soft<=hard check happens in :func:`_coerce_ulimit`
# (setrlimit rejects soft > hard with EINVAL, so an API-time 400 beats a
# workspace-start failure). Podman sets hard=soft when the hard part is
# omitted.
_ULIMIT_VALUE_RE = re.compile(r"^(?P<soft>\d+)(?::(?P<hard>\d+))?$")


def _coerce_int(key: str, value: Any) -> int:
    """Coerce a settings value to an ``int`` (no sign gating).

    Accepts an actual int or a numeric string (``"512"``); rejects floats,
    booleans, and non-numeric strings. Does **not** reject 0 or negatives —
    callers compose this with the sign check they need:
    :func:`_coerce_positive_int` (``> 0``) for pids / bridge timeout, or
    :func:`_coerce_nonnegative_int` (``>= 0``) for idle_timeout, where ``0``
    means "never idle out" (the idle reaper guards with ``timeout > 0``).
    """
    if isinstance(
        value, bool
    ):  # bool is a subclass of int — reject explicitly
        raise ValueError(f"settings.{key} must be an integer, not a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(
            f"settings.{key} must be an integer, got non-integer float"
            f" {value!r}"
        )
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"settings.{key} must not be empty")
        try:
            # int("1.5") raises — that's what we want (reject non-integer
            # strings); int("0x10", base=10) is the documented base-10 parse.
            return int(s, base=10)
        except ValueError as exc:
            raise ValueError(
                f"settings.{key} must be an integer, got {value!r}"
            ) from exc
    raise ValueError(
        f"settings.{key} must be an integer, got {type(value).__name__}"
    )


def _coerce_positive_int(key: str, value: Any) -> int:
    n = _coerce_int(key, value)
    if n <= 0:
        raise ValueError(
            f"settings.{key} must be a positive integer, got {n!r}"
        )
    return n


def _coerce_nonnegative_int(key: str, value: Any) -> int:
    """Coerce a settings value to a non-negative ``int`` (``>= 0``).

    For ``idle_timeout``: ``0`` is meaningful (never idle out — the idle
    reaper's ``timeout > 0`` guard skips reaping when the timeout is 0),
    but a negative timeout is nonsense.
    """
    n = _coerce_int(key, value)
    if n < 0:
        raise ValueError(
            f"settings.{key} must be a non-negative integer, got {n!r}"
        )
    return n


def _coerce_float(key: str, value: Any) -> float:
    """Coerce a settings value to a positive ``float`` (CPU limit)."""
    if isinstance(value, bool):
        raise ValueError(f"settings.{key} must be a number, not a boolean")
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"settings.{key} must not be empty")
        try:
            f = float(s)
        except ValueError as exc:
            raise ValueError(
                f"settings.{key} must be a number, got {value!r}"
            ) from exc
    else:
        raise ValueError(
            f"settings.{key} must be a number, got {type(value).__name__}"
        )
    if f <= 0:
        raise ValueError(
            f"settings.{key} must be a positive number, got {f!r}"
        )
    return f


def _coerce_memory(key: str, value: Any) -> str:
    """Coerce a settings value to a podman memory-limit string (``2g``)."""
    if not isinstance(value, str):
        # Allow an integer/float of bare bytes for ergonomics, then
        # re-stringify so the stored bag always carries the podman form.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"settings.{key} must be a size string (e.g. '2g', '512m'),"
                f" got {type(value).__name__}"
            )
        value = str(int(value))
    value = value.strip()
    m = _MEMORY_LIMIT_RE.match(value)
    if not m:
        raise ValueError(
            f"settings.{key}={value!r} is not a valid memory size:"
            " expected a positive number with an optional unit"
            " (k/m/g/t/p, optional trailing b), e.g. '2g' or '512mb'"
        )
    # Reject 0 — podman treats --memory=0 as "no limit", the same
    # ambiguity the PIDs validator rejects (an explicit 0 must not silently
    # mean "unlimited"). Matches settings.py's memory-limit validator.
    if float(m.group("num")) == 0:
        raise ValueError(
            f"settings.{key}={value!r} is not a valid memory size:"
            " must be a positive number"
        )
    return value


def _coerce_bool(key: str, value: Any) -> bool:
    """Coerce a settings value to a bool.

    Accepts a real bool, 0/1, or the strings true/false/yes/no/on/off
    (case-insensitive). Used by the per-workspace ``nix`` flag (#2202).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off", ""):
            return False
    raise ValueError(f"settings.{key}={value!r} is not a boolean")


def _coerce_ulimit(key: str, value: Any) -> str:
    """Coerce a settings value to a podman ulimit string (``1024:2048``).

    #2085: accepts a ``<soft>[:<hard>]`` string (non-negative integers;
    omitting the hard part sets both — same as podman) or a bare
    non-negative int, re-stringified so the stored bag always carries
    the podman form. Rejects bools, floats, negatives, and ``soft >
    hard`` (setrlimit rejects that with EINVAL — fail at the API
    boundary, not at workspace start). ``0`` is accepted (an rlimit of
    0 unambiguously means zero, unlike a cgroup pids limit of 0).
    """
    if isinstance(value, bool):
        raise ValueError(
            f"settings.{key} must be a ulimit value (<soft>[:<hard>]),"
            " not a boolean"
        )
    if isinstance(value, int):
        s = str(value)
    elif isinstance(value, str):
        s = value.strip()
    else:
        raise ValueError(
            f"settings.{key} must be a ulimit value (<soft>[:<hard>]),"
            f" got {type(value).__name__}"
        )
    m = _ULIMIT_VALUE_RE.match(s)
    if m is None:
        raise ValueError(
            f"settings.{key}={value!r} is not a valid ulimit value:"
            " expected <soft>[:<hard>] with non-negative integers,"
            " e.g. 1024 or 1024:2048 (omitting the hard part sets both)"
        )
    if m.group("hard") is not None and int(m.group("hard")) < int(
        m.group("soft")
    ):
        raise ValueError(
            f"settings.{key}={value!r} is not a valid ulimit value:"
            " the hard limit must be >= the soft limit"
        )
    return s


# Schema: each known settings key maps to a normalizer that validates +
# coerces the value (raising ``ValueError`` on a bad value) and returns the
# normalized form to store. Keys not in this dict are rejected by
# :func:`validate_settings`. Add a setting here to make it settable.
SCHEMA: dict[str, Callable[[str, Any], Any]] = {
    "idle_timeout": _coerce_nonnegative_int,
    "bridge_timeout": _coerce_positive_int,
    "cpu_limit": _coerce_float,
    "memory_limit": _coerce_memory,
    "pids_limit": _coerce_positive_int,
    # #2085: per-process rlimit (podman --ulimit nofile=) — the open-fd
    # ceiling per process. Deploy-only for ``nproc``: the kernel counts
    # RLIMIT_NPROC against the host uid across all namespaces, so on the
    # default rootless keep-id deployment a per-workspace nproc threshold
    # would gate the same shared counter every workspace (and the daemon)
    # draws from — not an isolation knob. nofile is per-process, so a
    # per-workspace override is meaningful.
    "nofile_limit": _coerce_ulimit,
    # #2378: per-workspace /tmp tmpfs size (podman size string, same grammar
    # as memory_limit — a positive number + optional k/m/g/t/p unit).
    "tmp_size": _coerce_memory,
    # #2202: per-workspace nix flag — triggers the per-workspace /nix mount
    # (Nix.ensure_workspace_nix) when a backend is configured (``nix_seed``,
    # #2219/#2220).
    "nix": _coerce_bool,
    # #2017: per-workspace sudo flag. The deploy-wide ``allow_sudo`` is a
    # *ceiling*: the workspace value may only further restrict (see
    # :func:`resolve_allow_sudo`). Defaults to True (follow the deploy
    # posture); an explicit False locks the workspace down with ``!ALL``
    # even on a deploy where sudo is on.
    "allow_sudo": _coerce_bool,
}

# The known setting keys, exported for callers that want to enumerate the
# schema (e.g. an API that lists what a workspace may override).
KNOWN_SETTINGS = frozenset(SCHEMA)


def validate_settings(
    settings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate + normalize a full per-workspace ``settings`` bag.

    Full-replace semantics (the :class:`CreateWorkspaceRequest` /
    :class:`UpdateWorkspaceRequest` path): the input *is* the complete bag.
    Rejects unknown keys and malformed values (a non-positive timeout, a
    non-numeric CPU limit, a malformed memory size, ...) by raising
    ``ValueError``; the API boundary translates that into HTTP 400. Returns
    a new dict of normalized values (so the stored bag always carries
    coerced forms — ``"512"`` → ``512``), or ``None`` when the input is
    ``None`` or empty (no overrides → NULL column).

    An explicit ``null`` for a key is dropped (in a full-replace bag it's
    meaningless noise — just omit the key). Use
    :func:`validate_settings_patch` for partial-merge semantics where
    ``null`` means "delete this key".

    A non-dict input (a list, a string) is rejected: the bag is always a
    JSON object.
    """
    normalized = _validate_settings_dict(settings)
    if not normalized:
        return None
    return normalized


def validate_settings_patch(
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate + normalize a partial-merge ``settings`` patch.

    PATCH semantics (the ``PATCH /workspaces/{id}/settings`` path): each
    key in *patch* is either a value (set/replace that override) or
    ``None`` (delete that override, reverting it to the deploy default).
    Returns the validated patch with ``None`` deletion markers preserved,
    so the model merge step can distinguish "set to X" from "clear."

    Raises ``ValueError`` on unknown keys / malformed non-null values
    (translated to HTTP 400 at the API boundary). An empty patch (``None``
    or ``{}``) raises ``ValueError`` — a PATCH that changes nothing is a
    client error, not a silent no-op.
    """
    if patch is None:
        raise ValueError("settings patch must not be empty")
    if not isinstance(patch, dict):
        raise ValueError(
            "settings patch must be a JSON object (dict), got"
            f" {type(patch).__name__}"
        )
    if not patch:
        raise ValueError("settings patch must not be empty")
    result: dict[str, Any] = {}
    for key, value in patch.items():
        if not isinstance(key, str):
            raise ValueError(
                f"settings keys must be strings, got {type(key).__name__}"
            )
        normalizer = SCHEMA.get(key)
        if normalizer is None:
            raise ValueError(
                f"Unknown setting {key!r}; known settings: "
                f"{sorted(KNOWN_SETTINGS)}"
            )
        # None is the deletion marker — preserved through validation so
        # the model merge can act on it. Everything else is coerced.
        if value is None:
            result[key] = None
        else:
            result[key] = normalizer(key, value)
    return result


def _validate_settings_dict(
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    """Shared validation core for :func:`validate_settings`.

    Returns the normalized dict (null values dropped) — the caller decides
    whether an empty result maps to ``None`` (full-replace) or is an error
    (patch). Kept private because the two public entry points differ on
    that empty-handling and on null semantics.
    """
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ValueError(
            "settings must be a JSON object (dict), got"
            f" {type(settings).__name__}"
        )
    normalized: dict[str, Any] = {}
    for key, value in settings.items():
        if not isinstance(key, str):
            raise ValueError(
                f"settings keys must be strings, got {type(key).__name__}"
            )
        normalizer = SCHEMA.get(key)
        if normalizer is None:
            raise ValueError(
                f"Unknown setting {key!r}; known settings: "
                f"{sorted(KNOWN_SETTINGS)}"
            )
        if value is None:
            continue
        normalized[key] = normalizer(key, value)
    return normalized


def resolve(
    workspace: dict | None,
    key: str,
    deploy_default: Any,
) -> Any:
    """Resolve a setting with **workspace override > deploy default > none**.

    ``workspace`` is a workspace dict (the shape returned by
    :class:`~klangk.model.workspaces.WorkspacesModel`); its ``settings`` key
    holds the parsed bag (or ``None``). If the workspace sets ``key``, that
    value wins; otherwise *deploy_default* is returned; if that too is
    ``None`` the result is ``None`` (no limit / unset).

    Mirrors how ``allowed_domains`` resolves: a non-empty workspace value
    overrides the deploy-wide default, never merges with it.
    """
    bag = (workspace or {}).get("settings") or {}
    if key in bag:
        return bag[key]
    return deploy_default


def resolve_bridge_timeout(
    workspace: dict | None, deploy_default: float | None
) -> float | None:
    """Resolve the per-workspace browser-delegate bridge timeout (seconds)."""
    return resolve(workspace, "bridge_timeout", deploy_default)


def resolve_cpu_limit(
    workspace: dict | None, deploy_default: float | None
) -> float | None:
    """Resolve the per-workspace CPU limit (``--cpus``, float)."""
    return resolve(workspace, "cpu_limit", deploy_default)


def resolve_memory_limit(
    workspace: dict | None, deploy_default: str | None
) -> str | None:
    """Resolve the per-workspace memory limit (``--memory``, size string)."""
    return resolve(workspace, "memory_limit", deploy_default)


def resolve_pids_limit(
    workspace: dict | None, deploy_default: int | None
) -> int | None:
    """Resolve the per-workspace PIDs limit (``--pids-limit``, int)."""
    return resolve(workspace, "pids_limit", deploy_default)


def resolve_nofile_limit(
    workspace: dict | None, deploy_default: str | None
) -> str | None:
    """Resolve the per-workspace ``nofile`` rlimit (``--ulimit``).

    #2085: same precedence as the other resolvers (workspace override >
    deploy default > none); the value is a ``<soft>[:<hard>]`` string.
    """
    return resolve(workspace, "nofile_limit", deploy_default)


def resolve_tmp_size(
    workspace: dict | None, deploy_default: str | None
) -> str | None:
    """Resolve the per-workspace ``/tmp`` tmpfs size (``size=<n>``, #2378).

    Same precedence as the other resolvers (workspace override > deploy
    default > none); ``None`` means "mount /tmp with no explicit size option"
    (podman then sizes the tmpfs at half of RAM). Same size-string grammar as
    :func:`resolve_memory_limit`.
    """
    return resolve(workspace, "tmp_size", deploy_default)


def parse_allow_sudo(value: str | None) -> bool:
    """Parse the deploy-wide ``allow_sudo`` setting string (#2017).

    The settings field is a free-form string; the truthy forms match the
    deploy-wide check the container registry has always done
    (``1`` / ``true`` / ``yes``, case-insensitive, whitespace-tolerant).
    """
    return (value or "").strip().lower() in ("1", "true", "yes")


def resolve_allow_sudo(workspace: dict | None, deploy_default: bool) -> bool:
    """Resolve the effective sudo posture for a workspace (#2017).

    Unlike the other resolvers, the deploy-wide ``allow_sudo`` is a
    **ceiling**, not a fallback the workspace may raise: the per-workspace
    value defaults to ``True`` (follow the deploy posture) and may only
    further restrict, so ``effective = workspace AND deploy``. A workspace
    can lock itself down (``allow_sudo: false`` → ``!ALL`` sudoers rule) on
    a sudo-enabled deploy, but can never grant itself sudo on a deploy that
    forbids it.
    """
    return bool(resolve(workspace, "allow_sudo", True)) and bool(
        deploy_default
    )
