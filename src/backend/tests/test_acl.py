"""Tests for acl.py: ACL walk, principal matching, has_permission."""

from unittest.mock import MagicMock


from klangk_backend import acl, model
from klangk_backend.model import (
    ACTION_ALLOW,
    ACTION_DENY,
    PRINCIPAL_GROUP,
    PRINCIPAL_SYSTEM,
    PRINCIPAL_USER,
    SYSTEM_AUTHENTICATED,
    SYSTEM_EVERYONE,
)


class TestAceMatchesPrincipals:
    def test_system_everyone_matches_all(self):
        ace = {
            "principal_type": PRINCIPAL_SYSTEM,
            "system_principal": SYSTEM_EVERYONE,
        }
        principals = {"user_id": "u1", "group_ids": [], "authenticated": False}
        assert acl._ace_matches_principals(ace, principals) is True

    def test_system_authenticated_matches_authed(self):
        ace = {
            "principal_type": PRINCIPAL_SYSTEM,
            "system_principal": SYSTEM_AUTHENTICATED,
        }
        principals = {"user_id": "u1", "group_ids": [], "authenticated": True}
        assert acl._ace_matches_principals(ace, principals) is True

    def test_system_authenticated_no_match_unauthed(self):
        ace = {
            "principal_type": PRINCIPAL_SYSTEM,
            "system_principal": SYSTEM_AUTHENTICATED,
        }
        principals = {"user_id": "u1", "group_ids": [], "authenticated": False}
        assert acl._ace_matches_principals(ace, principals) is False

    def test_user_principal_matches(self):
        ace = {"principal_type": PRINCIPAL_USER, "user_id": "u1"}
        principals = {"user_id": "u1", "group_ids": [], "authenticated": True}
        assert acl._ace_matches_principals(ace, principals) is True

    def test_user_principal_no_match(self):
        ace = {"principal_type": PRINCIPAL_USER, "user_id": "u2"}
        principals = {"user_id": "u1", "group_ids": [], "authenticated": True}
        assert acl._ace_matches_principals(ace, principals) is False

    def test_group_principal_matches(self):
        ace = {"principal_type": PRINCIPAL_GROUP, "group_id": "g1"}
        principals = {
            "user_id": "u1",
            "group_ids": ["g1", "g2"],
            "authenticated": True,
        }
        assert acl._ace_matches_principals(ace, principals) is True

    def test_group_principal_no_match(self):
        ace = {"principal_type": PRINCIPAL_GROUP, "group_id": "g3"}
        principals = {
            "user_id": "u1",
            "group_ids": ["g1", "g2"],
            "authenticated": True,
        }
        assert acl._ace_matches_principals(ace, principals) is False

    def test_unknown_principal_type(self):
        ace = {"principal_type": 99}
        principals = {"user_id": "u1", "group_ids": [], "authenticated": True}
        assert acl._ace_matches_principals(ace, principals) is False


class TestCheckPermission:
    async def test_allow_on_exact_resource(self, user):
        group = await model.create_group("testers")
        await model.add_user_to_group(user["id"], group["id"])
        await model.add_acl_entry(
            "/test",
            0,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_GROUP,
            group_id=group["id"],
        )
        principals = await acl.get_principals(user["id"])
        assert await acl.check_permission("/test", principals, "view") is True

    async def test_deny_on_exact_resource(self, user):
        await model.add_acl_entry(
            "/test",
            0,
            ACTION_DENY,
            "view",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        principals = await acl.get_principals(user["id"])
        assert await acl.check_permission("/test", principals, "view") is False

    async def test_walk_to_parent(self, user):
        await model.add_acl_entry(
            "/",
            0,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        principals = await acl.get_principals(user["id"])
        assert (
            await acl.check_permission("/workspaces/123", principals, "view")
            is True
        )

    async def test_wildcard_permission(self, user):
        await model.add_acl_entry(
            "/admin",
            0,
            ACTION_ALLOW,
            "*",
            PRINCIPAL_USER,
            user_id=user["id"],
        )
        principals = await acl.get_principals(user["id"])
        assert (
            await acl.check_permission("/admin", principals, "anything")
            is True
        )

    async def test_default_deny(self, user):
        principals = await acl.get_principals(user["id"])
        assert (
            await acl.check_permission("/secret", principals, "view") is False
        )

    async def test_first_match_wins(self, user):
        # Deny first, then allow — deny should win
        await model.add_acl_entry(
            "/test",
            0,
            ACTION_DENY,
            "edit",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_AUTHENTICATED,
        )
        await model.add_acl_entry(
            "/test",
            1,
            ACTION_ALLOW,
            "edit",
            PRINCIPAL_USER,
            user_id=user["id"],
        )
        principals = await acl.get_principals(user["id"])
        assert await acl.check_permission("/test", principals, "edit") is False


class TestRequestToResource:
    def _make_request(self, path):
        request = MagicMock()
        request.url.path = path
        return request

    def test_root(self):
        assert acl._request_to_resource(self._make_request("/")) == "/"

    def test_workspaces_collection(self):
        assert (
            acl._request_to_resource(self._make_request("/workspaces"))
            == "/workspaces"
        )

    def test_workspace_detail(self):
        assert (
            acl._request_to_resource(self._make_request("/workspaces/abc-123"))
            == "/workspaces/abc-123"
        )

    def test_workspace_sub_path(self):
        assert (
            acl._request_to_resource(
                self._make_request("/workspaces/abc-123/export")
            )
            == "/workspaces/abc-123"
        )

    def test_admin_users(self):
        assert (
            acl._request_to_resource(self._make_request("/admin/users"))
            == "/admin/users"
        )

    def test_admin_users_detail(self):
        assert (
            acl._request_to_resource(self._make_request("/admin/users/u1"))
            == "/admin/users/u1"
        )

    def test_admin_base(self):
        assert (
            acl._request_to_resource(self._make_request("/admin")) == "/admin"
        )

    def test_other_path(self):
        assert (
            acl._request_to_resource(self._make_request("/health"))
            == "/health"
        )


class TestGetPrincipals:
    async def test_returns_user_and_groups(self, user):
        g1 = await model.create_group("g1")
        g2 = await model.create_group("g2")
        await model.add_user_to_group(user["id"], g1["id"])
        await model.add_user_to_group(user["id"], g2["id"])
        principals = await acl.get_principals(user["id"])
        assert principals["user_id"] == user["id"]
        assert set(principals["group_ids"]) == {g1["id"], g2["id"]}
        assert principals["authenticated"] is True
