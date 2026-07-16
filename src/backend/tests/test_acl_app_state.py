"""Direct coverage for ``ACL(app_state)`` — the FastAPI permission layer (#1577).

Exercises the three DB-touching methods on ``app_state.acl``
(``get_principals``, ``check_permission``, ``permissions_for_resources``),
which reach the model layer through ``self.app_state.model.{users,acl}`` —
and the ``has_permission`` dependency factory, whose closure resolves
``request.app.state.acl`` per request. Mirrors the #1572–#1575
``test_model_*`` pattern: ``app_state`` (db + model + acl wired via the
ContextVar DB) with the schema initialized.
"""

import pytest

from klangk_backend import acl
from klangk_backend.model import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
)


@pytest.fixture
async def ac(app_state, db):
    """``app_state.acl`` with the schema initialized."""
    return app_state.acl


async def test_get_principals(ac, user, app_state):

    group = await app_state.model.users.create_group("g")
    await app_state.model.users.add_user_to_group(user["id"], group["id"])
    principals = await ac.get_principals(user["id"])
    assert principals == {
        "user_id": user["id"],
        "group_ids": [group["id"]],
        "authenticated": True,
    }


async def test_check_permission_allow_and_deny(ac, user, app_state):

    # Allow on exact resource via user principal.
    await app_state.model.acl.add_acl_entry(
        "/ws-allow",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    principals = await ac.get_principals(user["id"])
    assert await ac.check_permission("/ws-allow", principals, "view") is True
    # Wrong permission -> not granted (default deny).
    assert await ac.check_permission("/ws-allow", principals, "edit") is False

    # Deny wins over a deeper allow (first-match-wins on the walked path).
    await app_state.model.acl.add_acl_entry(
        "/ws-deny",
        0,
        ACTION_DENY,
        "view",
        PRINCIPAL_SYSTEM,
        system_principal=SYSTEM_AUTHENTICATED,
    )
    assert await ac.check_permission("/ws-deny", principals, "view") is False


async def test_check_permission_walks_to_parent(ac, user, app_state):

    # Allow at the parent; a child resource inherits it via the walk.
    await app_state.model.acl.add_acl_entry(
        "/parent",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    principals = await ac.get_principals(user["id"])
    assert (
        await ac.check_permission("/parent/child", principals, "view") is True
    )
    # No matching ancestor -> default deny.
    assert await ac.check_permission("/unrelated", principals, "view") is False


async def test_permissions_for_resources(ac, user, app_state):

    await app_state.model.acl.add_acl_entry(
        "/r1",
        0,
        ACTION_ALLOW,
        "view",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    await app_state.model.acl.add_acl_entry(
        "/r2",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    principals = await ac.get_principals(user["id"])
    result = await ac.permissions_for_resources(
        ["/r1", "/r2", "/r3"], principals, ["view", "edit"]
    )
    # /r1 grants only view; /r2 grants both (wildcard); /r3 grants nothing.
    assert result == {"/r1": ["view"], "/r2": ["view", "edit"]}
    # Empty resource list -> empty result (no query needed).
    assert await ac.permissions_for_resources([], principals, ["view"]) == {}


async def test_permissions_for_resources_via_group(ac, user, app_state):

    group = await app_state.model.users.create_group("editors")
    await app_state.model.users.add_user_to_group(user["id"], group["id"])
    await app_state.model.acl.add_acl_entry(
        "/grp-res",
        0,
        ACTION_ALLOW,
        "terminal",
        PRINCIPAL_GROUP,
        group_id=group["id"],
    )
    principals = await ac.get_principals(user["id"])
    result = await ac.permissions_for_resources(
        ["/grp-res"], principals, ["terminal", "files"]
    )
    assert result == {"/grp-res": ["terminal"]}


async def test_has_permission_allows(ac, user):
    """The dependency factory resolves request.app.state.acl and returns
    the user when the permission check passes."""
    import types

    from unittest.mock import AsyncMock

    # Fake a request whose app.state.acl.check_permission returns True.
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(acl=ac))
    ac.check_permission = AsyncMock(return_value=True)
    ac.get_principals = AsyncMock(
        return_value={
            "user_id": user["id"],
            "group_ids": [],
            "authenticated": True,
        }
    )
    request = types.SimpleNamespace(
        app=fake_app, url=types.SimpleNamespace(path="/api/v1/workspaces/ws-1")
    )
    dep = acl.has_permission("terminal")
    result = await dep(request, user)
    assert result is user
    ac.check_permission.assert_awaited_once()
    # The resource was derived from the request URL path.
    _res, _principals, _perm = ac.check_permission.call_args.args
    assert _res == "/workspaces/ws-1"
    assert _perm == "terminal"


async def test_has_permission_denies_via_resource_fn(ac, user):
    """When denied, the dependency raises 403; a resource_fn overrides the
    path-derived resource."""
    import types

    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(acl=ac))
    ac.check_permission = AsyncMock(return_value=False)
    ac.get_principals = AsyncMock(
        return_value={
            "user_id": user["id"],
            "group_ids": [],
            "authenticated": True,
        }
    )
    request = types.SimpleNamespace(app=fake_app)

    async def _resource_fn(req, u):
        return "/custom-resource"

    dep = acl.has_permission("share", _resource_fn)
    with pytest.raises(HTTPException) as exc:
        await dep(request, user)
    assert exc.value.status_code == 403
    _res, _principals, _perm = ac.check_permission.call_args.args
    assert _res == "/custom-resource"
    assert _perm == "share"
