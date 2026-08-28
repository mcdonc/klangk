"""Deployment-local lifecycle hooks loaded from the customize directory.

The customize directory (``KLANGKD_CUSTOMIZE_DIR``) already hosts the
OIDC login hook; this module adds the second runtime extension point,
the **workspace-created hook** (#2762): a deployment-local Python
callback that runs after a workspace is created (on every creation
path — ``POST /workspaces``, import, and duplicate) and may mutate the
workspace and rewrite its ACL.

Wiring mirrors the login hook (``KLANGKD_OIDC_LOGIN_HOOK`` /
:meth:`klangk.oidc.OIDC.load_login_hook`):

- ``KLANGKD_WORKSPACE_CREATED_HOOK`` points at a Python file, optionally
  followed by ``:func_name`` (default ``on_workspace_created``). The
  file is loaded directly via ``importlib.util`` — it does not need to
  be on ``PYTHONPATH``.
- Loaded at startup and re-loaded on SIGHUP reconfigure
  (:meth:`Hooks.reconfigure`). A reload that fails (missing file,
  unparseable, no callable) keeps the previously loaded hook — the
  assign-on-success shape of :meth:`klangk.oidc.OIDC.load_login_hook`.
- A missing file or a missing/uncallable function is a configuration
  error (boot refusal) — identical to a broken login hook.

Failure semantics differ from the login hook on purpose: the login hook
is a *gate* (its exception rejects the login), while the
workspace-created hook is a *mutation extension point*. If it raises,
the workspace still exists and the create response is returned
normally — the error is logged loudly (a WARNING with the hook source,
workspace id, and the exception) so partial effects are visible.
"""

import asyncio
import copy
import importlib.util
import logging
import os
from typing import Any, Callable

from .container import ContainerRegistry
from .exceptions import ConfigurationError
from .model import EGRESS_MODES, SETUP_STATES
from .model.workspaces import normalize_classification_banner
from .netfilter import parse_allowed_domains
from .workspace_settings import validate_settings

logger = logging.getLogger(__name__)

# Workspace-row fields a hook may mutate in place. The diff between the
# pre-hook row and the hook's copy is persisted through
# ``model.workspaces.update_workspace`` — after the same validation the
# create API applies (see _validate_hook_changes): the settings-bag
# schema, the image allowlist, mount-spec policy, and the domain-list
# grammar, plus the enum/bool fields mirrored from
# ``create_workspace_with_acl``. Keys outside this set (id, user_id,
# container_id, num_ports, created_at) are not persisted — they are
# provisioned, not declarative. Deleting a mutable key from the handle
# clears the column (``del workspace["env"]`` persists NULL).
_HOOK_MUTABLE_FIELDS = frozenset(
    {
        "name",
        "image",
        "service_command",
        "auto_start",
        "setup_state",
        "health_check",
        "mounts",
        "env",
        "allowed_domains",
        "rejected_domains",
        "settings",
        "egress_mode",
        "per_handle_home",
        "classification_banner",
    }
)


def _parse_hook_value(raw: str) -> tuple[str, str]:
    """Parse ``KLANGKD_WORKSPACE_CREATED_HOOK`` into (path, func_name).

    Accepted formats:

    - ``/path/to/hook.py:func_name``
    - ``/path/to/hook.py`` (defaults to ``on_workspace_created``)
    """
    if ":" in raw:
        path, func_name = raw.rsplit(":", 1)
    else:
        path = raw
        func_name = "on_workspace_created"
    return path, func_name


def _coerce_acl_int(value: Any, field: str) -> int:
    """Coerce an ACL entry field to int (pydantic parity for hooks).

    The HTTP ACL endpoints get this for free — pydantic coerces a JSON
    ``"2"`` to ``2`` before the model sees it. A hand-written hook dict
    skips pydantic, and the model-layer guards compare with strict int
    equality, so a string-typed ``principal_type`` would silently skip
    them (SQLite INTEGER affinity then stores it as an int). Coerce
    here so the guards always run.
    """
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ACL entry field {field!r} must be an integer, got {value!r}"
        ) from exc


def _validate_hook_changes(app, changed: dict) -> str | None:
    """Validate a hook's row mutations — the create API's checks.

    Returns ``None`` when every changed field is valid, else an error
    message describing the first invalid field. Mirrors what
    ``POST /workspaces`` validates before persisting: the enum/bool
    constraints of ``create_workspace_with_acl`` (enforced *before* the
    write, so a bad value is dropped rather than coerced by
    ``update_workspace``), the settings-bag schema
    (:func:`klangk.workspace_settings.validate_settings`), the image
    allowlist, the mount-spec policy
    (:meth:`ContainerRegistry.validate_mounts`), and the domain-list
    grammar (:func:`klangk.netfilter.parse_allowed_domains`, plus the
    rejected-domains no-CIDR rule). Validated lists/bags are normalized
    in place in ``changed`` (de-duplicated / coerced), exactly as the
    API persists them.
    """
    if "setup_state" in changed and changed["setup_state"] not in SETUP_STATES:
        return f"invalid setup_state: {changed['setup_state']!r}"
    if "egress_mode" in changed and changed["egress_mode"] not in EGRESS_MODES:
        return f"invalid egress_mode: {changed['egress_mode']!r}"
    if "per_handle_home" in changed and not isinstance(
        changed["per_handle_home"], bool
    ):
        return f"invalid per_handle_home: {changed['per_handle_home']!r}"
    if (
        "classification_banner" in changed
        and changed["classification_banner"] is not None
    ):
        try:
            changed["classification_banner"] = normalize_classification_banner(
                changed["classification_banner"]
            )
        except ValueError as exc:
            return f"invalid classification_banner: {exc}"
    if "image" in changed and changed["image"] is not None:
        registry: ContainerRegistry = app.state.container_registry
        if changed["image"] not in registry.allowed_images:
            return (
                f"image {changed['image']!r} is not in the allowed image"
                f" list: {sorted(registry.allowed_images)}"
            )
    if "mounts" in changed and changed["mounts"] is not None:
        error = app.state.container_registry.validate_mounts(changed["mounts"])
        if error:
            return f"invalid mounts: {error}"
    if "settings" in changed and changed["settings"] is not None:
        try:
            changed["settings"] = validate_settings(changed["settings"])
        except ValueError as exc:
            return f"invalid settings: {exc}"
    if "allowed_domains" in changed and changed["allowed_domains"] is not None:
        try:
            changed["allowed_domains"] = parse_allowed_domains(
                changed["allowed_domains"]
            )
        except ValueError as exc:
            return f"invalid allowed_domains: {exc}"
    if (
        "rejected_domains" in changed
        and changed["rejected_domains"] is not None
    ):
        for spec in changed["rejected_domains"]:
            if spec.strip() and "/" in spec:
                return (
                    "invalid rejected_domains: no CIDR specs (a rejected"
                    f" name is NXDOMAIN'd before resolution): {spec!r}"
                )
        try:
            changed["rejected_domains"] = parse_allowed_domains(
                changed["rejected_domains"], label="rejected_domains"
            )
        except ValueError as exc:
            return f"invalid rejected_domains: {exc}"
    return None


class WorkspaceHookHandle(dict):
    """The workspace row as seen by a workspace-created hook (#2762).

    A ``dict`` subclass holding a **deep copy** of the freshly created
    workspace row — hooks read and assign keys like the plain row
    (``workspace["egress_mode"] = "static"``), and nested edits
    (``workspace["env"]["K"] = "v"``) are detected and persisted too.
    Deleting a mutable key clears the column. Attribute edits made in
    place are persisted (with the create API's validation) by
    :meth:`Hooks.fire_workspace_created` after the hook returns; ACL
    edits are the hook's own explicit calls.
    """

    def __init__(self, workspace: dict, app, allow_await: bool = True):
        super().__init__(copy.deepcopy(workspace))
        self.app = app
        self.allow_await = allow_await

    @property
    def resource(self) -> str:
        """The workspace's ACL resource (``/workspaces/{id}``)."""
        return f"/workspaces/{self['id']}"

    def _require_async_hook(self, helper: str) -> None:
        """Refuse ACL helpers from a sync hook — loudly.

        A ``def`` (non-``async``) hook that called an ``async def``
        helper would get back an unawaited coroutine: a silent no-op
        plus a GC warning. The helpers are therefore plain functions
        that check first and return the coroutine only for async hooks,
        so a sync call fails fast with a clear message (the surrounding
        log-and-continue turns it into a WARNING).
        """
        if not self.allow_await:
            raise RuntimeError(
                f"workspace.{helper}() is awaitable and requires an"
                " 'async def' workspace-created hook"
            )

    async def _acl_entries(self) -> list[dict]:
        return await self.app.state.model.acl.get_acl_entries_resolved(
            self.resource
        )

    def acl_entries(self):
        """This workspace's ACL entries, ordered by position.

        Resolved like ``GET /api/v1/workspaces/{id}/acl``: each entry
        carries ``position``, ``action``, ``principal_type``,
        ``permission``, the raw principal fields (``user_id`` /
        ``group_id`` / ``system_principal``) and a display
        ``principal`` — the group name, the user's email, or
        ``Everyone`` / ``Authenticated`` for system principals.
        Awaitable: ``entries = await workspace.acl_entries()``.
        """
        self._require_async_hook("acl_entries")
        return self._acl_entries()

    async def _rewrite_acl_impl(self, entries: list[dict]) -> None:
        normalized = [
            {
                **entry,
                "position": i,
                "action": _coerce_acl_int(entry["action"], "action"),
                "principal_type": _coerce_acl_int(
                    entry["principal_type"], "principal_type"
                ),
                **(
                    {
                        "system_principal": _coerce_acl_int(
                            entry["system_principal"], "system_principal"
                        )
                    }
                    if entry.get("system_principal") is not None
                    else {}
                ),
            }
            for i, entry in enumerate(entries)
        ]
        await self.app.state.model.acl.replace_acl_entries(
            self.resource, normalized
        )

    def rewrite_acl(self, entries: list[dict]):
        """Replace the workspace's whole ACL (add/remove/reorder).

        ``entries`` is a list in the shape :meth:`acl_entries` returns
        (entries returned by a read can be filtered/edited and handed
        straight back); positions are renumbered by list index, so the
        list order is the new ACL order. New entries may be appended as
        plain dicts with ``action``, ``principal_type``, ``permission``,
        and one principal field (``action`` / ``principal_type`` /
        ``system_principal`` accept ints or int-strings; they are
        coerced so the same guards as the API endpoints always apply —
        the system agent can never become a principal, and
        workspace-role groups are grantable only on their own
        workspace). Keep an entry granting the owner access, or every
        new workspace starts fully locked out. Awaitable:
        ``await workspace.rewrite_acl(entries)``.
        """
        self._require_async_hook("rewrite_acl")
        return self._rewrite_acl_impl(entries)


class Hooks:
    """Customize-dir lifecycle hooks (``app.state.hooks``, #2762).

    Constructed once in :func:`build_app` and stored on
    ``app.state.hooks``. Reaches config through the single ``app``
    reference — settings are read live off
    ``self.app.state.settings`` — so the SIGHUP settings swap
    propagates. Hook state is instance attrs so it never leaks across
    test runs.
    """

    def __init__(self, app):
        self.app = app
        self.workspace_created_hook: Callable | None = None
        self.workspace_created_hook_is_async: bool = False
        self.workspace_created_hook_source: str | None = None

    def reconfigure(self, app) -> None:
        """SIGHUP reconfigure: swap the app reference, reload the hook."""
        self.app = app
        self.load_workspace_created_hook()

    def load_workspace_created_hook(self) -> None:
        """Load the workspace-created hook from
        ``KLANGKD_WORKSPACE_CREATED_HOOK``.

        The value is a file path to a Python script, optionally followed
        by ``:func_name`` (default ``on_workspace_created``); the file is
        loaded directly via ``importlib.util``. Raises
        :class:`~klangk.exceptions.ConfigurationError` when the path is
        set but the file is missing, unparseable, or lacks a callable of
        the requested name — same failure semantics as
        :meth:`klangk.oidc.OIDC.load_login_hook`. Like the login hook,
        the instance attrs are assigned only on success, so a failed
        SIGHUP reload keeps the previously loaded hook active.
        """
        raw = self.app.state.settings.workspace_created_hook
        if not raw:
            self.workspace_created_hook = None
            self.workspace_created_hook_is_async = False
            self.workspace_created_hook_source = None
            return
        path, func_name = _parse_hook_value(raw)
        if not os.path.isfile(path):
            raise ConfigurationError(
                f"KLANGKD_WORKSPACE_CREATED_HOOK: file not found: {path!r}"
            )
        spec = importlib.util.spec_from_file_location(
            "_klangk_workspace_created_hook", path
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ConfigurationError(
                f"KLANGKD_WORKSPACE_CREATED_HOOK: could not load: {path!r}"
            )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hook = getattr(mod, func_name, None)
        if hook is None or not callable(hook):
            raise ConfigurationError(
                f"KLANGKD_WORKSPACE_CREATED_HOOK: {func_name!r} not found"
                f" or not callable in {path!r}"
            )
        self.workspace_created_hook = hook
        self.workspace_created_hook_is_async = asyncio.iscoroutinefunction(
            hook
        )
        self.workspace_created_hook_source = raw
        logger.info("Workspace-created hook loaded: %s", raw)

    async def fire_workspace_created(
        self, workspace: dict, actor: dict
    ) -> dict:
        """Fire the workspace-created hook for a fresh workspace.

        The hook receives a :class:`WorkspaceHookHandle` (a deep copy of
        the row plus ACL helpers) and the creating user's row as
        ``actor``. Sync and async hook functions are both dispatched.
        Failures are **log-and-continue**: a raising hook (or a
        rejected/failed persist of its attribute edits) leaves the
        workspace exactly as created and returns the input row — the
        create is never failed or rolled back (#2762 failure
        semantics).

        Returns the workspace dict to hand back to the caller: the
        row re-read from the DB when the hook's attribute edits were
        persisted (``created_at`` carried over from the input — the
        re-read omits it), else the input dict.
        """
        hook = self.workspace_created_hook
        if hook is None:
            return workspace
        source = self.workspace_created_hook_source
        ws_id = workspace.get("id")
        # A deep copy, not the caller's dict: a raising hook must not
        # leave half-applied edits on the row the caller returns to the
        # user — including nested structures (env, settings, mounts).
        handle = WorkspaceHookHandle(
            workspace,
            self.app,
            allow_await=self.workspace_created_hook_is_async,
        )
        try:
            if self.workspace_created_hook_is_async:
                await hook(handle, actor)
            else:
                hook(handle, actor)
        except Exception:
            logger.warning(
                "workspace-created hook %s failed for workspace %s; "
                "the workspace was created unchanged",
                source,
                ws_id,
                exc_info=True,
            )
            return workspace
        changed = {}
        for field in _HOOK_MUTABLE_FIELDS:
            if field not in handle:
                # Deleted from the handle → clear the column.
                if field in workspace:
                    changed[field] = None
            elif handle[field] != workspace.get(field):
                changed[field] = handle[field]
        if not changed:
            return workspace
        invalid = _validate_hook_changes(self.app, changed)
        if invalid is not None:
            logger.warning(
                "workspace-created hook %s made an invalid change to "
                "workspace %s (%s); no attribute changes were applied",
                source,
                ws_id,
                invalid,
            )
            return workspace
        try:
            await self.app.state.model.workspaces.update_workspace(
                workspace["id"], workspace["user_id"], **changed
            )
            refreshed = await self.app.state.model.workspaces.get_workspace(
                workspace["id"]
            )
        except Exception:
            logger.warning(
                "workspace-created hook %s: persisting attribute changes "
                "for workspace %s failed; the row is unchanged",
                source,
                ws_id,
                exc_info=True,
            )
            return workspace
        if refreshed is not None:
            # get_workspace omits created_at; carry it from the input so
            # the create response's shape is hook-invariant.
            if "created_at" not in refreshed and "created_at" in workspace:
                refreshed["created_at"] = workspace["created_at"]
            return refreshed
        return workspace  # pragma: no cover — row vanished mid-fire
