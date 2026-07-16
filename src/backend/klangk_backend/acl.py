"""ACL authorization: resource tree walk and FastAPI dependency."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from . import auth, model
from .util import API_PREFIX
from .model import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)


async def get_principals(user_id: str) -> dict:
    """Build principal info for the given user."""
    group_ids = await model.get_user_group_ids(user_id)
    return {
        "user_id": user_id,
        "group_ids": group_ids,
        "authenticated": True,
    }


def ace_matches_principals(ace: dict, principals: dict) -> bool:
    """Check whether a single ACE matches the given principals."""
    pt = ace["principal_type"]
    if pt == PRINCIPAL_SYSTEM:
        sp = ace["system_principal"]
        if sp == SYSTEM_EVERYONE:
            return True
        if sp == SYSTEM_AUTHENTICATED:
            return principals["authenticated"]
    elif pt == PRINCIPAL_USER:
        return ace["user_id"] == principals["user_id"]
    elif pt == PRINCIPAL_GROUP:
        return ace["group_id"] in principals["group_ids"]
    return False


def resource_ancestors(resource_path: str) -> list[str]:
    """Return [resource_path, ..., '/'] — the paths ``check_permission`` walks.

    Mirrors the loop in [check_permission] so a caller can preload ACL
    entries for exactly these paths and evaluate permissions in memory.
    """
    paths: list[str] = []
    path = resource_path
    while True:
        paths.append(path)
        if path == "/":
            break
        parent = path.rsplit("/", 1)[0]
        path = parent if parent else "/"
    return paths


async def check_permission(
    resource_path: str,
    principals: dict,
    permission: str,
) -> bool:
    """Walk from resource_path up to root, checking ACEs.

    Returns True if an Allow ACE matches, False if a Deny ACE matches
    or no match is found (default deny).

    Fetches the ACEs for every ancestor of [resource_path] in a single
    query ([model.get_acl_entries_map]) and evaluates them in memory via
    [check_permission_inmemory] — equivalent to one [model.get_acl_entries]
    call per path segment, but without opening a fresh [NullPool] database
    connection per segment (2-3 connections per call -> 1). Nothing is
    retained across requests; the live ``acl_entries`` table is re-read on
    every call, so a permission change is reflected immediately.
    """
    entries = await model.get_acl_entries_map(
        resource_ancestors(resource_path)
    )
    return check_permission_inmemory(
        resource_path, principals, permission, entries
    )


def check_permission_inmemory(
    resource_path: str,
    principals: dict,
    permission: str,
    entries_by_resource: dict[str, list[dict]],
) -> bool:
    """In-memory equivalent of [check_permission] over a preloaded ACE map.

    ``entries_by_resource`` must contain every ancestor of
    ``resource_path`` (see [resource_ancestors]); missing paths are treated
    as having no entries, matching the async version's behavior.

    This does no I/O and stores nothing: ``entries_by_resource`` is a local
    built fresh on each call by [permissions_for_resources], so every
    request re-reads the live ``acl_entries`` table. There is no
    cross-request cache and therefore no invalidation surface.
    """
    for path in resource_ancestors(resource_path):
        for ace in entries_by_resource.get(path, ()):
            if ace_matches_principals(ace, principals):
                if ace["permission"] == "*" or ace["permission"] == permission:
                    return ace["action"] == ACTION_ALLOW
    return False


async def permissions_for_resources(
    resources: list[str],
    principals: dict,
    permissions: list[str],
) -> dict[str, list[str]]:
    """Effective permissions for each resource, preload-then-evaluate.

    Fetches ACL entries for the union of every resource's ancestor paths
    in a single query ([model.get_acl_entries_map]), then checks each
    (resource, permission) pair in memory via [check_permission_inmemory].
    This is equivalent to awaiting [check_permission] once per pair, but
    avoids opening a fresh database connection per pair — the static
    resource set previously triggered ~300 sequential connection-per-query
    reads on every ``/my-permissions`` call.

    No results are retained between requests: ``entries`` is a local that
    is reloaded from the live ``acl_entries`` table on every call, so a
    permission change is reflected on the next request immediately.

    Only resources with at least one granted permission appear in the
    result, matching the historical response shape.
    """
    ancestor_paths: list[str] = []
    for res in resources:
        ancestor_paths.extend(resource_ancestors(res))
    entries = await model.get_acl_entries_map(ancestor_paths)
    result: dict[str, list[str]] = {}
    for res in resources:
        perms = [
            p
            for p in permissions
            if check_permission_inmemory(res, principals, p, entries)
        ]
        if perms:
            result[res] = perms
    return result


def request_to_resource(request: Request) -> str:
    """Derive a resource path from the request URL.

    Maps URL paths to the ACL resource tree:
      /workspaces          -> /workspaces
      /workspaces/{id}     -> /workspaces/{id}
      /workspaces/{id}/... -> /workspaces/{id}
      /admin/users         -> /admin/users
      /admin/users/{id}    -> /admin/users/{id}
      /admin/invitations   -> /admin/invitations
      /admin/groups        -> /admin/groups
    """
    # Strip the versioned API prefix to get the logical resource path.
    path = request.url.path
    if path.startswith(API_PREFIX + "/"):
        path = path[len(API_PREFIX) :]
    parts = path.strip("/").split("/")
    if not parts or parts[0] == "":
        return "/"

    if parts[0] == "workspaces" and len(parts) >= 2:
        return f"/workspaces/{parts[1]}"
    if parts[0] == "admin":
        if len(parts) >= 3:
            return f"/admin/{parts[1]}/{parts[2]}"
        if len(parts) >= 2:
            return f"/admin/{parts[1]}"
        return "/admin"
    return "/" + parts[0]


def has_permission(permission: str, resource_fn=None):
    """FastAPI dependency that checks ACL permission.

    resource_fn: optional async callable(request, user) -> resource_path.
    If not provided, the resource is derived from the request URL path.

    The permission check runs on the request's ``ACL(app_state)`` instance
    (``request.app.state.acl``) — the same shape as ``auth.get_current_user``
    resolving ``request.app.state.auth``. The dependency is built at route
    definition time, so the instance is resolved per-request rather than
    closed over (#1577).
    """

    async def check(
        request: Request, user: dict = Depends(auth.get_current_user)
    ) -> dict:
        acl = request.app.state.acl
        if resource_fn:
            resource = await resource_fn(request, user)
        else:
            resource = request_to_resource(request)
        principals = await acl.get_principals(user["id"])
        if not await acl.check_permission(resource, principals, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return check


# ---------------------------------------------------------------------------
# ACL(app_state): the app_state-owned permission layer.
#
# This is the one genuinely new class the #1563 hoist forces (#1577): the
# FastAPI permission layer in this module reaches the model layer's DB
# delegates (``model.get_user_group_ids``, ``model.get_acl_entries_map``),
# so once those become ``Model(app_state)`` methods the layer needs
# ``app_state`` too. ``ACL(app_state)`` is wired in ``build_app`` as
# ``app.state.acl = ACL(app_state)`` alongside the other owned instances.
#
# The DB-touching entry points (``get_principals``, ``check_permission``,
# ``permissions_for_resources``) become methods reaching
# ``self.app_state.model.{users,acl}``. The FastAPI dependency factory
# ``has_permission`` stays module-level (it's built at route-definition
# time and can't close over an instance); its closure resolves
# ``request.app.state.acl`` per request, exactly like
# ``auth.get_current_user`` resolving ``request.app.state.auth``.
#
# The module-level free-function backstops above stay until #1578
# dissolves the ``_current_db`` ContextVar; they're still used by the test
# suite (``test_acl.py`` calls ``get_principals`` / ``check_permission``
# directly). The pure helpers (``ace_matches_principals``, ``resource_ancestors``,
# ``check_permission_inmemory``, ``request_to_resource``) are stateless and
# stay module-level — the methods below reuse them.
# ---------------------------------------------------------------------------


class ACL:
    """FastAPI permission layer, resolved through ``app_state.model.*``.

    Constructed once at startup (``app.state.acl = ACL(app_state)``) and
    reached per-request via ``request.app.state.acl`` (by the
    ``has_permission`` dependency factory) or directly as ``app_state.acl``
    (by handlers / the WebSocket connection layer). Reaches the DB through
    ``self.app_state.model`` — the single owned ``Model`` instance.
    """

    def __init__(self, app_state):
        self.app_state = app_state

    async def get_principals(self, user_id: str) -> dict:
        """Build principal info for the given user."""
        group_ids = await self.app_state.model.users.get_user_group_ids(
            user_id
        )
        return {
            "user_id": user_id,
            "group_ids": group_ids,
            "authenticated": True,
        }

    async def check_permission(
        self,
        resource_path: str,
        principals: dict,
        permission: str,
    ) -> bool:
        """Walk from resource_path up to root, checking ACEs.

        Returns True if an Allow ACE matches, False if a Deny ACE matches
        or no match is found (default deny). Fetches the ACEs for every
        ancestor of ``resource_path`` in a single query and evaluates them
        in memory via :func:`check_permission_inmemory`. Nothing is
        retained across requests.
        """
        entries = await self.app_state.model.acl.get_acl_entries_map(
            resource_ancestors(resource_path)
        )
        return check_permission_inmemory(
            resource_path, principals, permission, entries
        )

    async def permissions_for_resources(
        self,
        resources: list[str],
        principals: dict,
        permissions: list[str],
    ) -> dict[str, list[str]]:
        """Effective permissions for each resource, preload-then-evaluate.

        Fetches ACL entries for the union of every resource's ancestor
        paths in a single query, then checks each (resource, permission)
        pair in memory. Only resources with at least one granted
        permission appear in the result.
        """
        ancestor_paths: list[str] = []
        for res in resources:
            ancestor_paths.extend(resource_ancestors(res))
        entries = await self.app_state.model.acl.get_acl_entries_map(
            ancestor_paths
        )
        result: dict[str, list[str]] = {}
        for res in resources:
            perms = [
                p
                for p in permissions
                if check_permission_inmemory(res, principals, p, entries)
            ]
            if perms:
                result[res] = perms
        return result
