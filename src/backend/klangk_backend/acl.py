"""ACL authorization: resource tree walk and FastAPI dependency."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from . import auth, model
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


def _ace_matches_principals(ace: dict, principals: dict) -> bool:
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


async def check_permission(
    resource_path: str,
    principals: dict,
    permission: str,
) -> bool:
    """Walk from resource_path up to root, checking ACEs.

    Returns True if an Allow ACE matches, False if a Deny ACE matches
    or no match is found (default deny).
    """
    path = resource_path
    while True:
        aces = await model.get_acl_entries(path)
        for ace in aces:
            if _ace_matches_principals(ace, principals):
                if ace["permission"] == "*" or ace["permission"] == permission:
                    return ace["action"] == ACTION_ALLOW
        if path == "/":
            break
        parent = path.rsplit("/", 1)[0]
        path = parent if parent else "/"
    return False


def _request_to_resource(request: Request) -> str:
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
    parts = request.url.path.strip("/").split("/")
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
    """

    async def check(
        request: Request, user: dict = Depends(auth.get_current_user)
    ) -> dict:
        if resource_fn:
            resource = await resource_fn(request, user)
        else:
            resource = _request_to_resource(request)
        principals = await get_principals(user["id"])
        if not await check_permission(resource, principals, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return check
