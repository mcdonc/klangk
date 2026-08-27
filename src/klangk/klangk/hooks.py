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
  (:meth:`Hooks.reconfigure`, same lifecycle as the login hook).
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
import importlib.util
import logging
import os
from typing import Callable

from .exceptions import ConfigurationError
from .model import EGRESS_MODES, SETUP_STATES

logger = logging.getLogger(__name__)

# Workspace-row fields a hook may mutate in place. The diff between the
# pre-hook row and the hook's edits is persisted through
# ``model.workspaces.update_workspace``, whose validation applies
# (mirrored here for the enum/bool fields so a bad value is rejected
# before the write, exactly like ``create_workspace_with_acl``). Keys
# outside this set (id, user_id, container_id, num_ports, created_at)
# are not persisted — they are provisioned, not declarative.
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


def _validate_hook_changes(changed: dict) -> str | None:
    """Validate a hook's row mutations.

    Returns ``None`` when every changed field is valid, else an error
    message describing the first invalid field. Enforces the same
    enum/bool constraints as ``create_workspace_with_acl`` /
    ``update_workspace`` — but *before* the write, so an invalid change
    is dropped wholesale rather than half-persisted or silently coerced
    (``update_workspace`` would turn a truthy ``per_handle_home`` into 1).
    """
    if "setup_state" in changed and changed["setup_state"] not in SETUP_STATES:
        return f"invalid setup_state: {changed['setup_state']!r}"
    if "egress_mode" in changed and changed["egress_mode"] not in EGRESS_MODES:
        return f"invalid egress_mode: {changed['egress_mode']!r}"
    if "per_handle_home" in changed and not isinstance(
        changed["per_handle_home"], bool
    ):
        return f"invalid per_handle_home: {changed['per_handle_home']!r}"
    return None


class WorkspaceHookHandle(dict):
    """The workspace row as seen by a workspace-created hook (#2762).

    A ``dict`` subclass of the freshly created workspace row — hooks
    read and assign keys like the plain row (``workspace["egress_mode"]
    = "static"``) — plus two async ACL helpers that go through the ACL
    model API. Attribute edits made in place are persisted (with
    validation) by :meth:`Hooks.fire_workspace_created` after the hook
    returns; ACL edits are the hook's own explicit calls.
    """

    def __init__(self, workspace: dict, app):
        super().__init__(workspace)
        self.app = app

    @property
    def resource(self) -> str:
        """The workspace's ACL resource (``/workspaces/{id}``)."""
        return f"/workspaces/{self['id']}"

    async def acl_entries(self) -> list[dict]:
        """This workspace's ACL entries, ordered by position.

        Resolved like ``GET /api/v1/workspaces/{id}/acl``: each entry
        carries ``position``, ``action``, ``principal_type``,
        ``permission``, the raw principal fields (``user_id`` /
        ``group_id`` / ``system_principal``) and a display
        ``principal`` — the group name, the user's email, or
        ``Everyone`` / ``Authenticated`` for system principals.
        """

        return await self.app.state.model.acl.get_acl_entries_resolved(
            self.resource
        )

    async def rewrite_acl(self, entries: list[dict]) -> None:
        """Replace the workspace's whole ACL (add/remove/reorder).

        ``entries`` is a list in the shape :meth:`acl_entries` returns
        (entries returned by a read can be filtered/edited and handed
        straight back); positions are renumbered by list index, so the
        list order is the new ACL order. New entries may be appended as
        plain dicts with ``action``, ``principal_type``, ``permission``,
        and one principal field. The same guards as the API endpoints
        apply (the system agent can never become a principal;
        workspace-role groups are grantable only on their own
        workspace).
        """
        normalized = [
            {**entry, "position": i} for i, entry in enumerate(entries)
        ]
        await self.app.state.model.acl.replace_acl_entries(
            self.resource, normalized
        )


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
        :meth:`klangk.oidc.OIDC.load_login_hook`.
        """
        self.workspace_created_hook = None
        self.workspace_created_hook_is_async = False
        self.workspace_created_hook_source = None
        raw = self.app.state.settings.workspace_created_hook
        if not raw:
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

        The hook receives a :class:`WorkspaceHookHandle` (the row dict
        plus ACL helpers) and the creating user's row as ``actor``.
        Sync and async hook functions are both dispatched. Failures are
        **log-and-continue**: a raising hook (or a rejected/failed
        persist of its attribute edits) leaves the workspace exactly as
        created and returns the input row — the create is never failed
        or rolled back (#2762 failure semantics).

        Returns the workspace dict to hand back to the caller: the
        row re-read from the DB when the hook's attribute edits were
        persisted, else the input dict.
        """
        hook = self.workspace_created_hook
        if hook is None:
            return workspace
        source = self.workspace_created_hook_source
        ws_id = workspace.get("id")
        # A copy, not the caller's dict: a raising hook must not leave
        # half-applied edits on the row the caller returns to the user.
        handle = WorkspaceHookHandle(workspace, self.app)
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
        changed = {
            field: handle[field]
            for field in _HOOK_MUTABLE_FIELDS
            if field in handle and handle[field] != workspace.get(field)
        }
        if not changed:
            return workspace
        invalid = _validate_hook_changes(changed)
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
            return refreshed
        return workspace  # pragma: no cover — row vanished mid-fire
